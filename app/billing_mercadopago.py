"""Cobrança recorrente via Mercado Pago (Preapproval).

Modelo escolhido: **Preapproval + Checkout Pro**. O assinante autoriza uma
única vez numa página do próprio Mercado Pago e as cobranças seguintes são
automáticas. O JARBAS nunca vê número de cartão, o que mantém o escritório
fora do escopo de PCI-DSS e reduz a superfície de LGPD.

NENHUMA credencial mora neste arquivo. Tudo vem do .env.local:

    MERCADOPAGO_ACCESS_TOKEN    chave privada — só no backend, nunca no front
    MERCADOPAGO_PUBLIC_KEY      chave pública (opcional aqui)
    MERCADOPAGO_WEBHOOK_SECRET  segredo do webhook — é OUTRO valor, gerado
                                em "Suas integrações", NÃO é o access token
    JARBAS_PUBLIC_URL           URL pública https do JARBAS (back_url/webhook)

Token com prefixo TEST- opera em sandbox e não movimenta dinheiro real.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

API = "https://api.mercadopago.com"
TIMEOUT = 30.0

# Tolerância entre o ts assinado e o relógio local. Protege contra replay de
# notificações antigas capturadas por um atacante.
TOLERANCIA_S = 600


class BillingError(RuntimeError):
    """Falha na comunicação com o Mercado Pago. Sempre propagada."""


class AssinaturaInvalida(BillingError):
    """Webhook com assinatura ausente, malformada, vencida ou incorreta."""


# --------------------------------------------------------------------------
# Configuração
# --------------------------------------------------------------------------

def access_token() -> str:
    t = os.getenv("MERCADOPAGO_ACCESS_TOKEN", "").strip()
    if not t:
        raise BillingError(
            "MERCADOPAGO_ACCESS_TOKEN ausente. Configure no .env.local com a "
            "credencial de Suas integrações no painel do Mercado Pago."
        )
    return t


def webhook_secret() -> str:
    s = os.getenv("MERCADOPAGO_WEBHOOK_SECRET", "").strip()
    if not s:
        raise BillingError(
            "MERCADOPAGO_WEBHOOK_SECRET ausente. É um valor distinto do "
            "access token, gerado na configuração de webhooks da aplicação."
        )
    return s


def url_publica() -> str:
    u = os.getenv("JARBAS_PUBLIC_URL", "").strip().rstrip("/")
    if not u:
        raise BillingError(
            "JARBAS_PUBLIC_URL ausente. O Mercado Pago precisa de uma URL "
            "https alcançável pela internet para entregar as notificações."
        )
    if not u.startswith("https://") and "localhost" not in u:
        raise BillingError("JARBAS_PUBLIC_URL precisa ser https em produção.")
    return u


def em_sandbox() -> bool:
    return os.getenv("MERCADOPAGO_ACCESS_TOKEN", "").strip().startswith("TEST-")


def configurado() -> bool:
    return bool(os.getenv("MERCADOPAGO_ACCESS_TOKEN", "").strip())


def status_gateway() -> dict[str, Any]:
    """Diagnóstico para a tela de configurações — sem expor segredo."""
    tok = os.getenv("MERCADOPAGO_ACCESS_TOKEN", "").strip()
    return {
        "configurado": bool(tok),
        "ambiente": "sandbox (TEST-)" if tok.startswith("TEST-") else ("produção" if tok else "—"),
        "token_dica": f"••••{tok[-4:]}" if len(tok) >= 4 else "",
        "webhook_secret": bool(os.getenv("MERCADOPAGO_WEBHOOK_SECRET", "").strip()),
        "url_publica": os.getenv("JARBAS_PUBLIC_URL", "").strip(),
    }


# --------------------------------------------------------------------------
# Validação de assinatura do webhook
#
# Manifesto: "id:{data.id};request-id:{x-request-id};ts:{ts};"
# HMAC-SHA256 hex, chave = webhook secret. data.id vai em minúsculas.
# Segmentos ausentes são OMITIDOS por inteiro, não deixados em branco.
# --------------------------------------------------------------------------

def montar_manifesto(data_id: Optional[str], request_id: Optional[str], ts: str) -> str:
    partes = []
    if data_id:
        partes.append(f"id:{str(data_id).lower()};")
    if request_id:
        partes.append(f"request-id:{request_id};")
    partes.append(f"ts:{ts};")
    return "".join(partes)


def _partes_do_header(x_signature: str) -> tuple[str, str]:
    ts = v1 = ""
    for pedaco in x_signature.split(","):
        chave, _, valor = pedaco.partition("=")
        chave, valor = chave.strip(), valor.strip()
        if chave == "ts":
            ts = valor
        elif chave == "v1":
            v1 = valor
    return ts, v1


def validar_assinatura(x_signature: Optional[str], x_request_id: Optional[str],
                       data_id: Optional[str], secret: Optional[str] = None,
                       agora: Optional[float] = None,
                       tolerancia_s: int = TOLERANCIA_S) -> str:
    """Levanta AssinaturaInvalida se o webhook não for autêntico.

    Endpoint de webhook é público: sem esta checagem, qualquer um na internet
    ativa assinaturas no seu sistema mandando um POST.
    """
    if not x_signature:
        raise AssinaturaInvalida("header x-signature ausente")

    secret = secret if secret is not None else webhook_secret()
    ts, v1 = _partes_do_header(x_signature)
    if not ts or not v1:
        raise AssinaturaInvalida("x-signature malformado: esperado 'ts=...,v1=...'")

    try:
        ts_num = int(ts)
    except ValueError:
        raise AssinaturaInvalida("ts não numérico") from None
    if ts_num > 1_000_000_000_000:      # alguns eventos vêm em milissegundos
        ts_num //= 1000

    agora = time.time() if agora is None else agora
    if tolerancia_s > 0 and abs(agora - ts_num) > tolerancia_s:
        raise AssinaturaInvalida(
            f"assinatura fora da janela de {tolerancia_s}s (possível replay)")

    esperado = hmac.new(secret.encode("utf-8"),
                        montar_manifesto(data_id, x_request_id, ts).encode("utf-8"),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(esperado, v1):
        raise AssinaturaInvalida("assinatura não confere")
    return ts


# --------------------------------------------------------------------------
# Chamadas à API
# --------------------------------------------------------------------------

def _requisitar(metodo: str, caminho: str, corpo: dict | None = None) -> dict:
    try:
        import httpx
    except Exception as exc:
        raise BillingError("pacote 'httpx' indisponível no runtime.") from exc

    cabecalhos = {"Authorization": f"Bearer {access_token()}",
                  "Content-Type": "application/json"}
    try:
        r = httpx.request(metodo, f"{API}{caminho}", headers=cabecalhos,
                          json=corpo, timeout=TIMEOUT)
    except Exception as exc:
        raise BillingError(f"falha de rede ao falar com o Mercado Pago: {exc}") from exc

    if r.status_code >= 400:
        try:
            det = r.json().get("message") or r.text[:300]
        except Exception:
            det = r.text[:300]
        if r.status_code in (401, 403):
            det = "credencial recusada — confira o MERCADOPAGO_ACCESS_TOKEN."
        raise BillingError(f"Mercado Pago HTTP {r.status_code}: {det}")
    try:
        return r.json()
    except Exception as exc:
        raise BillingError("resposta do Mercado Pago não é JSON.") from exc


@dataclass
class Preapproval:
    id: str
    status: str
    init_point: str
    external_reference: str


def criar_preapproval(*, org_id: int, plan_code: str, plan_name: str,
                      valor_brl: float, email_pagador: str,
                      dia_cobranca: int | None = None) -> Preapproval:
    """Cria a assinatura recorrente e devolve a URL de autorização."""
    if valor_brl <= 0:
        raise BillingError("valor da assinatura precisa ser maior que zero.")
    base = url_publica()
    referencia = f"jarbas:org:{org_id}:plano:{plan_code}"

    corpo: dict[str, Any] = {
        "reason": f"JARBAS Jurídico — plano {plan_name}",
        "external_reference": referencia,
        "payer_email": email_pagador,
        "back_url": f"{base}/billing/retorno",
        "status": "pending",
        "auto_recurring": {
            "frequency": 1,
            "frequency_type": "months",
            "transaction_amount": round(float(valor_brl), 2),
            "currency_id": "BRL",
        },
    }
    if dia_cobranca:
        corpo["auto_recurring"]["billing_day"] = int(dia_cobranca)
        corpo["auto_recurring"]["billing_day_proportional"] = True

    dados = _requisitar("POST", "/preapproval", corpo)
    ponto = dados.get("init_point") or dados.get("sandbox_init_point") or ""
    if not dados.get("id") or not ponto:
        raise BillingError("Mercado Pago não devolveu id/init_point da assinatura.")
    return Preapproval(str(dados["id"]), str(dados.get("status", "pending")),
                       ponto, referencia)


def consultar_preapproval(preapproval_id: str) -> dict:
    return _requisitar("GET", f"/preapproval/{preapproval_id}")


def cancelar_preapproval(preapproval_id: str) -> dict:
    return _requisitar("PUT", f"/preapproval/{preapproval_id}", {"status": "cancelled"})


# --------------------------------------------------------------------------
# Máquina de estados
#
# NUNCA confiamos no corpo do webhook para saber o status: o webhook diz
# apenas QUAL recurso mudou. O status vem de uma consulta autenticada à API.
# --------------------------------------------------------------------------

MAPA_STATUS = {
    "pending":   "pending",    # criada, aguardando autorização do assinante
    "authorized": "active",    # autorizada e cobrando
    "paused":    "past_due",   # cobrança falhou; acesso deve ser restringido
    "cancelled": "canceled",
    "finished":  "canceled",
}

# Estados em que o escritório pode usar o sistema.
ESTADOS_COM_ACESSO = {"active"}


def status_interno(status_mp: str) -> str:
    return MAPA_STATUS.get((status_mp or "").lower(), "pending")


def tem_acesso(status_interno_: str) -> bool:
    return status_interno_ in ESTADOS_COM_ACESSO


def org_da_referencia(external_reference: str) -> Optional[int]:
    """Extrai o org_id de 'jarbas:org:{id}:plano:{code}'."""
    partes = (external_reference or "").split(":")
    if len(partes) >= 3 and partes[0] == "jarbas" and partes[1] == "org":
        try:
            return int(partes[2])
        except ValueError:
            return None
    return None


def plano_da_referencia(external_reference: str) -> Optional[str]:
    partes = (external_reference or "").split(":")
    if len(partes) >= 5 and partes[3] == "plano":
        return partes[4] or None
    return None


def resumo_evento(dados: dict) -> dict:
    """Normaliza a resposta do /preapproval para gravação."""
    ref = dados.get("external_reference") or ""
    bruto = str(dados.get("status", ""))
    return {
        "preapproval_id": str(dados.get("id", "")),
        "status_mp": bruto,
        "status": status_interno(bruto),
        "org_id": org_da_referencia(ref),
        "plan_code": plano_da_referencia(ref),
        "external_reference": ref,
        "proxima_cobranca": (dados.get("next_payment_date") or ""),
        "valor": (dados.get("auto_recurring") or {}).get("transaction_amount"),
        "recebido_em": datetime.now().isoformat(timespec="seconds"),
        "payload": json.dumps(dados, ensure_ascii=False)[:8000],
    }
