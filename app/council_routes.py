"""Rotas do Conselho tri-IA (JARBAS 8.5).

Segue o mesmo padrão do v7.py: APIRouter incluído por main.py, reaproveitando
require_workspace, valid_csrf e safe_template_response do módulo principal.
"""

from __future__ import annotations

import json

from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import ai_council as council
from . import plan_limits
from .database import db

router = APIRouter()
BASE_DIR = Path(__file__).resolve().parent.parent


def _main():
    """Import tardio: main.py importa este módulo no fim, evitando ciclo."""
    from . import main as m
    return m


# --------------------------------------------------------------------------
# Consumo real de IA no mês, em dólar — base do teto.
# --------------------------------------------------------------------------

def gasto_mes_usd(org_id: int) -> float:
    primeiro = date.today().replace(day=1).isoformat()
    with db() as conn:
        linha = conn.execute(
            """SELECT COALESCE(SUM(cost_usd),0) total FROM ai_council_runs
               WHERE organization_id=? AND created_at>=?""",
            (org_id, primeiro),
        ).fetchone()
    return float(linha["total"] or 0.0)


def registrar_etapa(org_id: int, user_id: int, case_id: int, r: council.CouncilResult) -> None:
    with db() as conn:
        conn.execute(
            """INSERT INTO ai_council_runs
               (organization_id,user_id,case_id,role,provider,model,
                input_tokens,output_tokens,cost_usd,elapsed_s,fallback_from,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (org_id, user_id, case_id, r.role, r.provider, r.model,
             r.input_tokens, r.output_tokens, r.cost_usd, r.elapsed_s,
             r.fallback_from, datetime.now().isoformat(timespec="seconds")),
        )


# --------------------------------------------------------------------------
# Painel
# --------------------------------------------------------------------------

@router.get("/conselho", response_class=HTMLResponse)
def painel(request: Request):
    m = _main()
    user, org = m.require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org

    status = council.status_conselho()
    gasto = gasto_mes_usd(org["id"])
    with db() as conn:
        historico = conn.execute(
            """SELECT * FROM ai_council_runs WHERE organization_id=?
               ORDER BY id DESC LIMIT 40""", (org["id"],)).fetchall()
        casos = conn.execute(
            "SELECT id,number,title FROM cases WHERE organization_id=? ORDER BY id DESC LIMIT 200",
            (org["id"],)).fetchall()

    return m.safe_template_response("conselho.html", m.common_context(
        request, user, org,
        status=status, gasto_usd=gasto,
        teto_usd=status["teto_usd_mes"],
        restante_usd=max(0.0, status["teto_usd_mes"] - gasto) if status["teto_usd_mes"] else None,
        historico=historico, casos=casos,
    ))


# --------------------------------------------------------------------------
# Deliberação colegiada sobre um caso
# --------------------------------------------------------------------------

@router.post("/cases/{case_id}/conselho/deliberar", response_class=HTMLResponse)
def deliberar(request: Request, case_id: int,
              pedido: str = Form(...),
              usar_pdfs: str = Form("1"),
              pular_critica: str = Form("0"),
              csrf: str = Form("", alias="_csrf")):
    m = _main()
    if not m.valid_csrf(request, csrf):
        return m.csrf_error()
    user, org = m.require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org

    with db() as conn:
        caso = conn.execute(
            "SELECT * FROM cases WHERE id=? AND organization_id=?",
            (case_id, org["id"])).fetchone()
        if not caso:
            return RedirectResponse("/cases", status_code=303)
        docs = conn.execute(
            """SELECT stored_path FROM case_documents
               WHERE organization_id=? AND case_id=? ORDER BY id DESC LIMIT 10""",
            (org["id"], case_id)).fetchall()

    pdfs: list[Path] = []
    if usar_pdfs == "1":
        for d in docs:
            p = m.safe_data_file(d["stored_path"], m.UPLOAD_ROOT)
            if p and p.is_file():
                pdfs.append(p)

    material = "\n\n".join(filter(None, [
        f"CASO: {caso['title']}",
        f"NÚMERO: {caso['number']}" if caso["number"] else "",
        f"ÁREA: {caso['area']}" if caso["area"] else "",
        f"FATOS: {caso['facts']}" if caso["facts"] else "",
        f"PROVAS: {caso['evidence']}" if caso["evidence"] else "",
        f"ESTRATÉGIA: {caso['strategy']}" if caso["strategy"] else "",
    ]))

    gasto = gasto_mes_usd(org["id"])
    etapas_ok: list[council.CouncilResult] = []

    def anotar(papel, msg):  # ponto de extensão para progresso via SSE
        return None

    try:
        lim = plan_limits.limites(org["id"])
        resultado = council.deliberar(
            material=material, pdfs=pdfs, pedido=pedido.strip(),
            gasto_atual_usd=gasto,
            faixa=lim.papeis(),
            teto_usd=lim.ai_usd_mes or None,
            pular_critica=(pular_critica == "1"),
            progresso=anotar,
        )
        etapas_ok = resultado.etapas
    except council.BudgetExceeded as exc:
        return m.safe_template_response("conselho_resultado.html", m.common_context(
            request, user, org, case=caso, erro=str(exc), bloqueio_orcamento=True,
        ), status_code=402)
    except council.CouncilError as exc:
        # Erro é erro: nunca vira conteúdo da peça.
        return m.safe_template_response("conselho_resultado.html", m.common_context(
            request, user, org, case=caso, erro=str(exc),
        ), status_code=502)
    finally:
        for etapa in etapas_ok:
            registrar_etapa(org["id"], user["id"], case_id, etapa)

    with db() as conn:
        draft_id = conn.insert_id(
            """INSERT INTO drafts
               (organization_id,case_id,user_id,title,draft_type,content,sources_json,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (org["id"], case_id, user["id"],
             f"[Conselho tri-IA] {pedido.strip()[:120]}",
             "conselho",
             resultado.texto_final,
             json.dumps([{"papel": e.role, "provedor": e.provider, "modelo": e.model,
                          "tokens": e.total_tokens, "custo_usd": round(e.cost_usd, 6)}
                         for e in resultado.etapas], ensure_ascii=False),
             datetime.now().isoformat(timespec="seconds")))

    m.log_action(request,
                 f"Conselho tri-IA no caso #{case_id}: {len(resultado.etapas)} etapas, "
                 f"US$ {resultado.custo_total_usd:.4f}")

    return m.safe_template_response("conselho_resultado.html", m.common_context(
        request, user, org, case=caso, r=resultado, draft_id=draft_id,
    ))
