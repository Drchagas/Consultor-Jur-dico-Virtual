"""A chave da IA e o que o operador lê quando ela é recusada.

Origem: a tela do Intake exibiu, na mesma frase,

    "Chave recusada (401). Não reconheci o formato desta chave.
     Uma chave da Anthropic começa com 'sk-ant-'."

Duas afirmações verdadeiras e contraditórias na leitura: o servidor recusou
(logo a chave foi enviada) e o formato não foi reconhecido (logo nem deveria
ter sido). O operador não tinha o que fazer com isso.

A causa das duas juntas é sempre a mesma: o que está guardado NÃO é a chave
pura — tem aspas, caractere invisível ou a linha inteira do .env.local colada
no campo. O servidor recusa porque recebe o lixo junto; a checagem local não
reconhece o prefixo pelo mesmo motivo.
"""

import os
import re
import sys
from pathlib import Path

import pytest

_RAIZ = Path(__file__).resolve().parent.parent
RAIZ = _RAIZ / "payload" if (_RAIZ / "payload" / "app").is_dir() else _RAIZ
sys.path.insert(0, str(RAIZ))

from app import ai_gateway as G  # noqa: E402

CHAVE = "sk-ant-api03-EXEMPLOdeCHAVEparaTESTE"


class _Recusa(Exception):
    status_code = 401


@pytest.fixture(autouse=True)
def ambiente_limpo(monkeypatch):
    monkeypatch.delenv(G.ENV_CHAVE, raising=False)
    yield


# ============================================== limpeza do valor guardado

def test_valores_sujos_viram_a_chave_pura():
    """Cada caso aqui é uma forma real de a chave chegar suja."""
    casos = {
        'aspas duplas do .env.local': f'"{CHAVE}"',
        "aspas simples": f"'{CHAVE}'",
        "linha inteira colada no campo": f"{G.ENV_CHAVE}={CHAVE}",
        "linha inteira com aspas": f'{G.ENV_CHAVE}="{CHAVE}"',
        "espaço em volta": f"  {CHAVE}  ",
        "BOM do Bloco de Notas": f"﻿{CHAVE}",
        "espaço de largura zero do navegador": f"{CHAVE}​",
        "espaço não separável": f" {CHAVE}",
    }
    for rotulo, sujo in casos.items():
        assert G._limpar_chave(sujo) == CHAVE, f"não limpou: {rotulo}"


def test_chave_limpa_nao_e_alterada(monkeypatch):
    monkeypatch.setenv(G.ENV_CHAVE, CHAVE)
    assert G._key() == CHAVE
    assert G.chave_saneada() == []


def test_nao_confunde_igual_que_pertence_a_chave():
    """Só o prefixo com o NOME da variável é cortado.

    Cortar em qualquer '=' mutilaria uma chave que legitimamente contenha um,
    e o resultado seria um 401 causado pelo próprio saneamento.
    """
    com_igual = "sk-ant-api03-AAA=BBB=CCC"
    assert G._limpar_chave(com_igual) == com_igual


def test_o_saneamento_e_registrado_para_o_operador(monkeypatch):
    """Limpar em silêncio faria o sistema funcionar por acidente.

    O arquivo continuaria errado e a próxima instalação repetiria o problema.
    """
    monkeypatch.setenv(G.ENV_CHAVE, f'"{CHAVE}"')
    assert G._key() == CHAVE
    assert any("aspas" in motivo for motivo in G.chave_saneada())

    monkeypatch.setenv(G.ENV_CHAVE, CHAVE)
    assert G.chave_saneada() == [], "chave limpa não pode acusar saneamento"


# ====================================================== formato da chave

def test_chave_entre_aspas_passa_a_ser_reconhecida(monkeypatch):
    """Era exatamente este o caso da tela: formato 'desconhecido'."""
    monkeypatch.setenv(G.ENV_CHAVE, f'"{CHAVE}"')
    tipo, _ = G.formato_chave()
    assert tipo == "anthropic"


def test_chave_da_openai_continua_sendo_identificada(monkeypatch):
    monkeypatch.setenv(G.ENV_CHAVE, "sk-proj-umaCHAVEdaOPENAI")
    tipo, msg = G.formato_chave()
    assert tipo == "openai" and "OpenAI" in msg


# ================================================== mensagem do erro 401

def test_a_mensagem_de_401_nao_mistura_dois_diagnosticos(monkeypatch):
    """Nunca mais "recusada pelo servidor" e "formato irreconhecível" juntos."""
    monkeypatch.setenv(G.ENV_CHAVE, "isto-nao-e-uma-chave")
    msg = G.friendly_error(_Recusa("invalid x-api-key"))
    assert not ("401" in msg and "Não reconheci o formato" in msg), (
        "a mensagem voltou a emendar os dois diagnósticos:\n" + msg
    )
    assert "DIAGNOSTICAR_IA" in msg, "toda recusa precisa apontar o próximo passo"


def test_401_com_valor_sujo_manda_corrigir_a_origem(monkeypatch):
    monkeypatch.setenv(G.ENV_CHAVE, f'"{CHAVE}"')
    msg = G.friendly_error(_Recusa("invalid x-api-key"))
    assert "aspas" in msg
    assert "CONFIGURAR_IA" in msg


def test_401_com_chave_bem_formada_fala_de_revogacao_e_credito(monkeypatch):
    monkeypatch.setenv(G.ENV_CHAVE, CHAVE)
    msg = G.friendly_error(_Recusa("invalid x-api-key"))
    assert "revogada" in msg and "credito" in msg.lower()


def test_toda_mensagem_de_401_diz_o_que_fazer(monkeypatch):
    """Mensagem de erro que não indica o próximo passo é só um beco."""
    for valor in (CHAVE, f'"{CHAVE}"', "sk-proj-openai", "lixo"):
        monkeypatch.setenv(G.ENV_CHAVE, valor)
        msg = G.friendly_error(_Recusa("invalid x-api-key"))
        assert re.search(r"console\.anthropic\.com|CONFIGURAR_IA|DIAGNOSTICAR_IA", msg), (
            f"sem próximo passo para o valor {valor[:12]!r}: {msg}"
        )


def test_a_chave_nunca_aparece_na_mensagem_de_erro(monkeypatch):
    """A mensagem vai para a tela e para o log. O segredo não pode ir junto."""
    monkeypatch.setenv(G.ENV_CHAVE, CHAVE)
    msg = G.friendly_error(_Recusa("invalid x-api-key"))
    assert CHAVE not in msg and CHAVE[8:] not in msg


# =========================================== scripts do Windows (fonte)

SCRIPTS = _RAIZ / "scripts"


def _ps1(nome):
    arq = SCRIPTS / nome
    if not arq.is_file():
        pytest.skip(f"{nome} ausente nesta árvore")
    return arq.read_text(encoding="utf-8-sig")


def test_o_leitor_de_env_do_windows_tira_aspas():
    """Sem isto, o Python não tem como consertar.

    O app roda load_dotenv(override=False): o que o PowerShell exporta vence
    o arquivo. Exportar `"sk-ant-..."` com aspas garante o 401.
    """
    fonte = _ps1("INICIAR_JARBAS.ps1")
    assert re.search(r"Trim\(\s*'\"'\s*\)", fonte), (
        "Load-Env precisa remover aspas do valor lido do .env.local"
    )


def test_configurar_ia_nao_grava_modelo_de_provedor_extinto():
    """A lista $Allowed forçava os modelos de volta para nomes da OpenAI.

    Inclusive por cima de uma configuração correta que o operador já tivesse.
    """
    fonte = _ps1("CONFIGURAR_IA.ps1")
    codigo = "\n".join(l for l in fonte.splitlines() if not l.strip().startswith("#"))
    assert "gpt-" not in codigo, "CONFIGURAR_IA ainda oferece modelo da OpenAI"
    for padrao in ("claude-opus-5", "claude-sonnet-5"):
        assert padrao in codigo, f"{padrao} deveria ser o padrão oferecido"


def test_configurar_ia_limpa_a_chave_antes_de_testar():
    """Colar a linha inteira, com aspas ou com invisível não pode virar 401."""
    fonte = _ps1("CONFIGURAR_IA.ps1")
    assert ".Trim('\"')" in fonte, "a chave colada precisa perder as aspas"
    assert "0xFEFF" in fonte, "precisa remover os caracteres invisíveis do copiar-e-colar"
    assert re.search(r"ANTHROPIC_API_KEY\\s\*=", fonte), (
        "precisa cortar o nome da variável quando a linha inteira é colada"
    )


def test_configurar_ia_nao_se_anuncia_como_openai():
    fonte = _ps1("CONFIGURAR_IA.ps1")
    linhas = [l for l in fonte.splitlines()
              if "Write-Host" in l and "OPENAI" in l.upper()]
    assert not linhas, "o assistente ainda se apresenta como OpenAI:\n" + "\n".join(linhas)


# ================================== o diagnóstico não pode vazar a chave

def test_o_diagnostico_geral_mascara_qualquer_chave():
    """O arquivo do diagnóstico existe para ser ENVIADO ao suporte.

    A versão anterior mascarava OPENAI_API_KEY — que a 9.0 nem usa — e deixava
    a ANTHROPIC_API_KEY passar inteira. Quem seguisse a instrução de mandar o
    diagnóstico entregaria a própria chave junto.
    """
    fonte = _ps1("DIAGNOSTICO_JARBAS.ps1")
    assert "KEY|TOKEN|SECRET|PASSWORD" in fonte, (
        "o diagnóstico precisa mascarar QUALQUER variável de segredo, não uma lista"
    )
    assert "OPENAI_API_KEY=[CONFIGURADA/OCULTA]" not in fonte, (
        "a lista fixa antiga deixava a chave da Anthropic passar em claro"
    )


def test_o_diagnostico_mostra_a_chave_do_ambiente_do_windows():
    """Variável do Windows vence o .env.local e some do diagnóstico.

    O app usa load_dotenv(override=False). Com a chave definida no ambiente do
    Windows e o .env.local vazio, o operador corrige o arquivo, nada muda, e
    não há no relatório nada que explique por quê.
    """
    fonte = _ps1("DIAGNOSTICO_JARBAS.ps1")
    assert "GetEnvironmentVariable('ANTHROPIC_API_KEY'" in fonte
    for escopo in ("Process", "User", "Machine"):
        assert escopo in fonte, f"falta conferir o escopo {escopo}"


def test_o_diagnostico_reporta_o_sdk_certo():
    fonte = _ps1("DIAGNOSTICO_JARBAS.ps1")
    assert "import anthropic" in fonte
    assert "import openai" not in fonte, "a 9.0 não usa o SDK da OpenAI"
