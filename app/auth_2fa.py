"""Segunda etapa de verificação (TOTP) e recuperação de senha.

Sem dependência externa: TOTP é RFC 6238, HMAC-SHA1 sobre um contador de
30 segundos. `hmac`, `hashlib`, `base64` e `struct` da biblioteca padrão
bastam, e cada dependência a menos é uma superfície de ataque a menos numa
aplicação que guarda autos sob sigilo profissional.

Compatível com Google Authenticator, Authy, 1Password e Microsoft
Authenticator — todos implementam a mesma RFC.

Decisões de segurança que não são óbvias:

- O segredo TOTP e os códigos de recuperação nunca ficam em texto puro no
  banco. Códigos de recuperação são guardados como hash, igual senha.
- A verificação aceita uma janela de ±1 período (30s) para tolerar relógio
  fora de sincronia, e não mais que isso.
- Cada código já usado é registrado: sem isso, quem interceptar um código
  o reutiliza dentro dos mesmos 30 segundos.
- Redefinição de senha nunca revela se o e-mail existe. Caso contrário, o
  formulário vira ferramenta de enumeração de usuários.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
import urllib.parse
from datetime import datetime, timedelta

PERIODO = 30
DIGITOS = 6
JANELA = 1                  # ±1 período: tolera 30s de deriva de relógio
VALIDADE_RESET_H = 1        # link de redefinição vive 1 hora
CODIGOS_RECUPERACAO = 10


# --------------------------------------------------------------------- TOTP

def gerar_segredo(bytes_: int = 20) -> str:
    """Segredo base32 de 160 bits, como recomenda a RFC 4226."""
    return base64.b32encode(secrets.token_bytes(bytes_)).decode("ascii").rstrip("=")


def _codigo(segredo_b32: str, contador: int) -> str:
    preenchido = segredo_b32.strip().replace(" ", "").upper()
    preenchido += "=" * (-len(preenchido) % 8)
    chave = base64.b32decode(preenchido, casefold=True)
    digest = hmac.new(chave, struct.pack(">Q", contador), hashlib.sha1).digest()
    deslocamento = digest[-1] & 0x0F
    trecho = struct.unpack(">I", digest[deslocamento:deslocamento + 4])[0] & 0x7FFFFFFF
    return str(trecho % (10 ** DIGITOS)).zfill(DIGITOS)


def codigo_atual(segredo_b32: str, agora: float | None = None) -> str:
    agora = time.time() if agora is None else agora
    return _codigo(segredo_b32, int(agora) // PERIODO)


def verificar_codigo(segredo_b32: str, informado: str,
                     agora: float | None = None, janela: int = JANELA) -> bool:
    """Compara em tempo constante, dentro da janela tolerada."""
    informado = (informado or "").strip().replace(" ", "").replace("-", "")
    if not informado.isdigit() or len(informado) != DIGITOS:
        return False
    agora = time.time() if agora is None else agora
    base = int(agora) // PERIODO
    for desvio in range(-janela, janela + 1):
        if hmac.compare_digest(_codigo(segredo_b32, base + desvio), informado):
            return True
    return False


def uri_otpauth(segredo_b32: str, email: str, emissor: str = "JARBAS Jurídico") -> str:
    """URI para o QR Code do aplicativo autenticador."""
    rotulo = urllib.parse.quote(f"{emissor}:{email}")
    params = urllib.parse.urlencode({
        "secret": segredo_b32, "issuer": emissor,
        "algorithm": "SHA1", "digits": DIGITOS, "period": PERIODO,
    })
    return f"otpauth://totp/{rotulo}?{params}"


# ---------------------------------------------------- códigos de recuperação

def gerar_codigos_recuperacao(quantos: int = CODIGOS_RECUPERACAO) -> list[str]:
    """Códigos de uso único, para quando o celular se perde."""
    return ["-".join(secrets.token_hex(2).upper() for _ in range(2))
            for _ in range(quantos)]


def hash_codigo(codigo: str) -> str:
    """Códigos de recuperação são credenciais: guardados só como hash."""
    limpo = (codigo or "").strip().upper().replace(" ", "")
    return hashlib.sha256(limpo.encode("utf-8")).hexdigest()


def conferir_codigo(codigo: str, hashes: list[str]) -> str | None:
    """Devolve o hash que casou, para o chamador marcar como usado."""
    alvo = hash_codigo(codigo)
    for h in hashes:
        if hmac.compare_digest(h, alvo):
            return h
    return None


# ------------------------------------------------------ redefinição de senha

def gerar_token_reset() -> tuple[str, str, str]:
    """Devolve (token_em_claro, hash, expira_em).

    O token em claro vai no link e não é guardado. O banco só vê o hash: um
    vazamento da tabela não permite redefinir a senha de ninguém.
    """
    token = secrets.token_urlsafe(32)
    expira = (datetime.now() + timedelta(hours=VALIDADE_RESET_H)).isoformat(timespec="seconds")
    return token, hashlib.sha256(token.encode()).hexdigest(), expira


def hash_token_reset(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def token_expirado(expira_em: str, agora: datetime | None = None) -> bool:
    if not expira_em:
        return True
    try:
        limite = datetime.fromisoformat(expira_em)
    except ValueError:
        return True
    return (agora or datetime.now()) > limite


def link_reset(token: str) -> str:
    base = os.getenv("JARBAS_PUBLIC_URL", "").strip().rstrip("/") or "http://127.0.0.1:8765"
    return f"{base}/reset-senha?token={urllib.parse.quote(token)}"


# --------------------------------------------------------------- obrigatório

def exige_2fa(user: dict | None, org: dict | None = None) -> bool:
    """Se o escritório ativar a exigência, todo membro precisa configurar."""
    if os.getenv("JARBAS_2FA_OBRIGATORIO", "0") == "1":
        return True
    if org is not None:
        try:
            return bool(org["require_2fa"])
        except (KeyError, IndexError, TypeError):
            return False
    return False
