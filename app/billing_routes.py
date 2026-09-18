"""Rotas de cobrança — Mercado Pago Preapproval.

Três caminhos:

  POST /billing/assinar/{plan_code}   autenticado, com CSRF
  GET  /billing/retorno               volta do checkout
  POST /webhooks/mercadopago          PÚBLICO, sem login e sem CSRF

O webhook é a exceção deliberada às regras do resto do sistema, e por bons
motivos: quem chama é o servidor do Mercado Pago, que não tem sessão nem
token CSRF. A autenticação dele é a assinatura HMAC do header x-signature —
por isso ela é obrigatória, e um webhook sem assinatura válida é recusado
com 401 antes de qualquer efeito no banco.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import billing_mercadopago as mp
from .database import db

router = APIRouter()


def _main():
    from . import main as m
    return m


# --------------------------------------------------------------------------
# Assinar um plano
# --------------------------------------------------------------------------

@router.post("/billing/assinar/{plan_code}")
def assinar(request: Request, plan_code: str, csrf: str = Form("", alias="_csrf")):
    m = _main()
    if not m.valid_csrf(request, csrf):
        return m.csrf_error()
    user, org = m.require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    if not m.can_manage_workspace(org):
        return RedirectResponse("/billing?erro=sem_permissao", status_code=303)

    with db() as conn:
        plano = conn.execute(
            "SELECT * FROM plans WHERE code=? AND active=1", (plan_code,)).fetchone()
    if not plano:
        return RedirectResponse("/billing?erro=plano_invalido", status_code=303)

    try:
        pre = mp.criar_preapproval(
            org_id=org["id"], plan_code=plano["code"], plan_name=plano["name"],
            valor_brl=float(plano["monthly_price"] or 0),
            email_pagador=(org["email"] or user["email"]),
        )
    except mp.BillingError as exc:
        request.session["billing_error"] = str(exc)
        return RedirectResponse("/billing?erro=gateway", status_code=303)

    # Assinatura fica PENDENTE. Só o webhook, com assinatura válida, ativa.
    agora = datetime.now().isoformat(timespec="seconds")
    with db() as conn:
        conn.execute(
            """INSERT INTO subscription_orders
               (organization_id,plan_code,regular_price,promo_price,discount_percent,
                billing_cycle,status,provider,external_id,created_at)
               VALUES (?,?,?,?,0,'monthly','pending','mercadopago',?,?)""",
            (org["id"], plano["code"], plano["monthly_price"], plano["monthly_price"],
             pre.id, agora))
        existente = conn.execute(
            "SELECT id FROM subscriptions WHERE organization_id=? ORDER BY id DESC LIMIT 1",
            (org["id"],)).fetchone()
        if existente:
            conn.execute(
                """UPDATE subscriptions SET plan_id=?,status='pending',
                   provider='mercadopago',external_id=? WHERE id=?""",
                (plano["id"], pre.id, existente["id"]))
        else:
            conn.execute(
                """INSERT INTO subscriptions
                   (organization_id,plan_id,status,started_at,provider,external_id)
                   VALUES (?,?,'pending',?,'mercadopago',?)""",
                (org["id"], plano["id"], agora, pre.id))

    m.log_action(request, f"Assinatura iniciada: plano {plano['code']} "
                          f"(preapproval {pre.id}, {'sandbox' if mp.em_sandbox() else 'produção'})")
    return RedirectResponse(pre.init_point, status_code=303)


@router.get("/billing/retorno", response_class=HTMLResponse)
def retorno(request: Request):
    """Volta do checkout. NÃO ativa nada: quem ativa é o webhook assinado.

    O usuário pode chegar aqui digitando a URL, então tratar este retorno
    como confirmação de pagamento seria liberar acesso de graça.
    """
    m = _main()
    user, org = m.require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    return RedirectResponse("/billing?retorno=1", status_code=303)


# --------------------------------------------------------------------------
# Webhook — público, autenticado por HMAC
# --------------------------------------------------------------------------

@router.post("/webhooks/mercadopago")
async def webhook(request: Request):
    corpo: dict = {}
    try:
        corpo = await request.json()
    except Exception:
        corpo = {}

    q = request.query_params
    data_id = (q.get("data.id") or q.get("id")
               or str((corpo.get("data") or {}).get("id") or "") or None)
    tipo = (q.get("type") or q.get("topic") or corpo.get("type")
            or corpo.get("topic") or "")

    # 1. Autenticidade primeiro. Nada toca o banco antes disto.
    try:
        mp.validar_assinatura(
            request.headers.get("x-signature"),
            request.headers.get("x-request-id"),
            data_id,
        )
    except mp.AssinaturaInvalida as exc:
        return JSONResponse({"erro": f"assinatura inválida: {exc}"}, status_code=401)
    except mp.BillingError as exc:
        # Segredo não configurado é erro NOSSO, não do Mercado Pago. Devolver
        # 500 faz o gateway reenviar depois que arrumarmos.
        return JSONResponse({"erro": str(exc)}, status_code=500)

    if not data_id:
        return JSONResponse({"ignorado": "sem data.id"}, status_code=200)

    # Só assinaturas interessam aqui.
    if tipo and "preapproval" not in str(tipo).lower():
        return JSONResponse({"ignorado": f"tipo {tipo}"}, status_code=200)

    # 2. Idempotência: o Mercado Pago reenvia. Índice único decide.
    chave = f"{tipo or 'preapproval'}:{data_id}:{request.headers.get('x-request-id', '')}"
    agora = datetime.now().isoformat(timespec="seconds")
    with db() as conn:
        ja = conn.execute(
            "SELECT id,processed FROM billing_events WHERE provider='mercadopago' AND event_key=?",
            (chave,)).fetchone()
        if ja and ja["processed"]:
            return JSONResponse({"ok": True, "duplicado": True}, status_code=200)
        if not ja:
            try:
                conn.execute(
                    """INSERT INTO billing_events
                       (provider,event_key,preapproval_id,created_at)
                       VALUES ('mercadopago',?,?,?)""",
                    (chave, str(data_id), agora))
            except Exception:
                return JSONResponse({"ok": True, "duplicado": True}, status_code=200)

    # 3. O status vem da API, nunca do corpo do webhook. O corpo diz apenas
    #    QUAL recurso mudou; confiar nele deixaria forjar "authorized".
    try:
        dados = mp.consultar_preapproval(str(data_id))
    except mp.BillingError as exc:
        with db() as conn:
            conn.execute(
                "UPDATE billing_events SET error=? WHERE provider='mercadopago' AND event_key=?",
                (str(exc)[:500], chave))
        # 500 faz o Mercado Pago tentar de novo.
        return JSONResponse({"erro": str(exc)}, status_code=500)

    ev = mp.resumo_evento(dados)
    aplicar_evento(ev, chave)
    return JSONResponse({"ok": True, "status": ev["status"]}, status_code=200)


def aplicar_evento(ev: dict, chave: str) -> None:
    """Grava o evento e sincroniza a assinatura do escritório."""
    with db() as conn:
        conn.execute(
            """UPDATE billing_events
               SET organization_id=?,plan_code=?,status_provider=?,status_internal=?,
                   amount=?,payload=?,processed=1,error=NULL
               WHERE provider='mercadopago' AND event_key=?""",
            (ev["org_id"], ev["plan_code"], ev["status_mp"], ev["status"],
             ev["valor"], ev["payload"], chave))

        if not ev["org_id"]:
            return

        plano = None
        if ev["plan_code"]:
            plano = conn.execute(
                "SELECT id FROM plans WHERE code=?", (ev["plan_code"],)).fetchone()

        atual = conn.execute(
            "SELECT id FROM subscriptions WHERE organization_id=? ORDER BY id DESC LIMIT 1",
            (ev["org_id"],)).fetchone()

        if atual:
            if plano:
                conn.execute(
                    """UPDATE subscriptions SET status=?,plan_id=?,provider='mercadopago',
                       external_id=?,current_period_end=? WHERE id=?""",
                    (ev["status"], plano["id"], ev["preapproval_id"],
                     ev["proxima_cobranca"], atual["id"]))
            else:
                conn.execute(
                    """UPDATE subscriptions SET status=?,provider='mercadopago',
                       external_id=?,current_period_end=? WHERE id=?""",
                    (ev["status"], ev["preapproval_id"], ev["proxima_cobranca"],
                     atual["id"]))
        elif plano:
            conn.execute(
                """INSERT INTO subscriptions
                   (organization_id,plan_id,status,started_at,current_period_end,
                    provider,external_id)
                   VALUES (?,?,?,?,?,'mercadopago',?)""",
                (ev["org_id"], plano["id"], ev["status"], ev["recebido_em"],
                 ev["proxima_cobranca"], ev["preapproval_id"]))

        conn.execute(
            "UPDATE subscription_orders SET status=? WHERE external_id=? AND provider='mercadopago'",
            (ev["status"], ev["preapproval_id"]))
