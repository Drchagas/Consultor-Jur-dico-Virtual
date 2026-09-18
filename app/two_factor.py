"""Segundo fator ligado ao banco e ao fluxo de login.

`auth_2fa.py` é o motor criptográfico puro: RFC 6238, sem estado, sem banco.
Este módulo é a metade que faltava — a que guarda o segredo, controla o
reuso de código e decide se um usuário pode ou não passar do login.

Até a 9.0.2 o motor existia, tinha testes e NÃO era chamado por rota alguma.
`JARBAS_2FA_OBRIGATORIO=1` no .env.example não fazia nada: as tabelas eram
criadas vazias e o login só olhava a senha. Um escritório que lesse a
documentação e ligasse a variável acreditaria estar protegido sem estar —
pior do que não ter a variável.

Decisões que não são óbvias:

- O segredo só vale depois de CONFIRMADO. Entre gerar e confirmar existe
  uma linha com confirmed_at nulo: se o advogado fechar a página no meio da
  inscrição, ele não fica trancado para fora da própria conta.
- Cada código aceito é gravado por PERÍODO, não por valor. A janela tolera
  ±30s, então o mesmo código serve para três períodos; gravar o valor
  deixaria a porta aberta nos outros dois.
- Código de recuperação usado é marcado, nunca apagado: a auditoria precisa
  saber que aquele código foi gasto, e quando.
- Desativar o segundo fator apaga os códigos de recuperação junto. Um código
  órfão de uma ativação antiga voltaria a valer na ativação seguinte.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from . import auth_2fa
from .database import db

# Janela para concluir a segunda etapa depois de a senha ser aceita. Passou
# disso, o login recomeça. Sem prazo, uma sessão meio-autenticada esquecida
# num computador público fica esperando indefinidamente por um código.
PRAZO_SEGUNDA_ETAPA_MIN = 10

# Retenção do registro de códigos gastos. O suficiente para cobrir qualquer
# deriva de relógio plausível; manter mais só faz a tabela crescer.
RETENCAO_CODIGOS_USADOS_H = 24


def _agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------------ consulta

def registro(user_id: int) -> Optional[dict]:
    with db() as conn:
        return conn.execute("SELECT * FROM user_totp WHERE user_id=?", (user_id,)).fetchone()


def ativo(user_id: int) -> bool:
    """True somente com segredo confirmado. Inscrição pela metade não conta."""
    linha = registro(user_id)
    if not linha:
        return False
    try:
        return bool(linha["confirmed_at"])
    except (KeyError, IndexError, TypeError):
        return False


def codigos_restantes(user_id: int) -> int:
    with db() as conn:
        linha = conn.execute(
            "SELECT COUNT(*) AS n FROM user_recovery_codes WHERE user_id=? AND used_at IS NULL",
            (user_id,),
        ).fetchone()
    return int(linha["n"]) if linha else 0


# --------------------------------------------------------------- inscrição

def iniciar_inscricao(user_id: int, email: str) -> tuple[str, str]:
    """Gera (ou reaproveita) um segredo ainda não confirmado.

    Reaproveitar importa: se o usuário recarregar a página de inscrição, um
    segredo novo invalidaria o que ele acabou de cadastrar no aplicativo, e
    o código que aparece no celular passaria a ser recusado sem explicação.
    """
    linha = registro(user_id)
    if linha and not linha["confirmed_at"]:
        segredo = linha["secret"]
    else:
        if linha:
            # Já confirmado: reinscrição só acontece após desativar.
            raise ValueError("Segundo fator já está ativo para este usuário.")
        segredo = auth_2fa.gerar_segredo()
        with db() as conn:
            conn.execute(
                "INSERT INTO user_totp (user_id,secret,confirmed_at,created_at) VALUES (?,?,NULL,?)",
                (user_id, segredo, _agora()),
            )
    return segredo, auth_2fa.uri_otpauth(segredo, email)


def confirmar_inscricao(user_id: int, codigo: str) -> Optional[list[str]]:
    """Confirma o segredo e devolve os códigos de recuperação em claro.

    Os códigos são mostrados UMA vez. O banco guarda só o hash, então nem o
    administrador da plataforma consegue recuperá-los depois — o que é o
    comportamento correto e precisa estar claro na tela.
    """
    linha = registro(user_id)
    if not linha or linha["confirmed_at"]:
        return None
    if not auth_2fa.verificar_codigo(linha["secret"], codigo):
        return None

    codigos = auth_2fa.gerar_codigos_recuperacao()
    agora = _agora()
    with db() as conn:
        conn.execute("UPDATE user_totp SET confirmed_at=? WHERE user_id=?", (agora, user_id))
        conn.execute("DELETE FROM user_recovery_codes WHERE user_id=?", (user_id,))
        for codigo_claro in codigos:
            conn.execute(
                "INSERT INTO user_recovery_codes (user_id,code_hash,used_at,created_at) VALUES (?,?,NULL,?)",
                (user_id, auth_2fa.hash_codigo(codigo_claro), agora),
            )
    return codigos


def desativar(user_id: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM user_totp WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM user_recovery_codes WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM totp_used WHERE user_id=?", (user_id,))


def regerar_codigos_recuperacao(user_id: int) -> Optional[list[str]]:
    if not ativo(user_id):
        return None
    codigos = auth_2fa.gerar_codigos_recuperacao()
    agora = _agora()
    with db() as conn:
        conn.execute("DELETE FROM user_recovery_codes WHERE user_id=?", (user_id,))
        for codigo_claro in codigos:
            conn.execute(
                "INSERT INTO user_recovery_codes (user_id,code_hash,used_at,created_at) VALUES (?,?,NULL,?)",
                (user_id, auth_2fa.hash_codigo(codigo_claro), agora),
            )
    return codigos


# ----------------------------------------------------------- verificação

def _limpar_codigos_usados(conn, user_id: int) -> None:
    limite = int((datetime.now() - timedelta(hours=RETENCAO_CODIGOS_USADOS_H)).timestamp()) // auth_2fa.PERIODO
    conn.execute("DELETE FROM totp_used WHERE user_id=? AND period < ?", (user_id, limite))


def verificar_totp(user_id: int, codigo: str) -> bool:
    """Aceita o código uma única vez.

    A unicidade vem do índice UNIQUE(user_id,period) da tabela totp_used: se
    a inserção falhar, aquele período já foi gasto. Fazer SELECT antes do
    INSERT deixaria uma corrida entre duas requisições simultâneas — o banco
    resolve isso sem corrida.
    """
    linha = registro(user_id)
    if not linha or not linha["confirmed_at"]:
        return False
    periodo = auth_2fa.periodo_do_codigo(linha["secret"], codigo)
    if periodo is None:
        return False
    with db() as conn:
        _limpar_codigos_usados(conn, user_id)
        gasto = conn.execute(
            "SELECT 1 AS x FROM totp_used WHERE user_id=? AND period=?", (user_id, periodo)
        ).fetchone()
        if gasto:
            return False
        conn.execute(
            "INSERT INTO totp_used (user_id,code,period,created_at) VALUES (?,?,?,?)",
            (user_id, "usado", periodo, _agora()),
        )
    return True


def consumir_codigo_recuperacao(user_id: int, codigo: str) -> bool:
    """Gasta um código de recuperação. Uso único, marcado e auditável."""
    with db() as conn:
        linhas = conn.execute(
            "SELECT id,code_hash FROM user_recovery_codes WHERE user_id=? AND used_at IS NULL",
            (user_id,),
        ).fetchall()
        hashes = [linha["code_hash"] for linha in linhas]
        casou = auth_2fa.conferir_codigo(codigo, hashes)
        if not casou:
            return False
        alvo = next(linha["id"] for linha in linhas if linha["code_hash"] == casou)
        conn.execute("UPDATE user_recovery_codes SET used_at=? WHERE id=?", (_agora(), alvo))
    return True


def verificar(user_id: int, codigo: str) -> bool:
    """Aceita TOTP de 6 dígitos ou código de recuperação, nesta ordem."""
    limpo = (codigo or "").strip()
    if not limpo:
        return False
    if verificar_totp(user_id, limpo):
        return True
    return consumir_codigo_recuperacao(user_id, limpo)
