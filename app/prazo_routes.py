"""Painel de prazos e linha do tempo processual.

O que existia: duas rotas soltas (criar e concluir) e nenhuma tela. O prazo
era um título e uma data digitada à mão, sem fundamento, sem termo inicial e
sem contagem.

O que existe agora: /prazos com semáforo por criticidade, cálculo automático
em dias úteis conforme o art. 219 do CPC, fundamento legal registrado e
recálculo auditável.
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import prazos as P
from .database import db

router = APIRouter()


def _main():
    from . import main as m
    return m


def _hoje() -> str:
    return date.today().isoformat()


def _data(valor: str) -> date | None:
    try:
        return date.fromisoformat((valor or "").strip()[:10])
    except ValueError:
        return None


# --------------------------------------------------------------------------

def listar(org_id: int, status: str = "abertos") -> list[dict]:
    onde = "d.organization_id=?"
    args: list = [org_id]
    if status == "abertos":
        onde += " AND (d.status IS NULL OR d.status NOT IN ('Concluído','Cancelado'))"
    elif status == "concluidos":
        onde += " AND d.status='Concluído'"

    with db() as conn:
        linhas = conn.execute(
            f"""SELECT d.*, c.title case_title, c.number case_number, u.name responsavel
                FROM deadlines d
                LEFT JOIN cases c ON c.id=d.case_id AND c.organization_id=d.organization_id
                LEFT JOIN users u ON u.id=d.responsible_user_id
                WHERE {onde}
                ORDER BY d.due_date""", tuple(args)).fetchall()

    saida = []
    for r in linhas:
        venc = _data(r["due_date"])
        nivel, texto = P.criticidade(venc) if venc else ("normal", "sem data")
        saida.append({**dict(r), "nivel": nivel, "texto_prazo": texto,
                      "vencimento": venc})
    ordem = {"vencido": 0, "hoje": 1, "critico": 2, "atencao": 3, "normal": 4}
    saida.sort(key=lambda x: (ordem.get(x["nivel"], 9), x["due_date"] or "9999"))
    return saida


def contagem_por_nivel(itens: list[dict]) -> dict[str, int]:
    c = {"vencido": 0, "hoje": 0, "critico": 0, "atencao": 0, "normal": 0}
    for i in itens:
        c[i["nivel"]] = c.get(i["nivel"], 0) + 1
    return c


@router.get("/prazos", response_class=HTMLResponse)
def painel(request: Request, status: str = "abertos"):
    m = _main()
    user, org = m.require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org

    itens = listar(org["id"], status)
    with db() as conn:
        casos = conn.execute(
            "SELECT id,number,title FROM cases WHERE organization_id=? ORDER BY id DESC LIMIT 300",
            (org["id"],)).fetchall()
        equipe = conn.execute(
            """SELECT u.id,u.name FROM memberships mb JOIN users u ON u.id=mb.user_id
               WHERE mb.organization_id=? AND mb.is_active=1""", (org["id"],)).fetchall()

    return m.safe_template_response("prazos.html", m.common_context(
        request, user, org,
        prazos=itens, contagem=contagem_por_nivel(itens),
        catalogo=P.CATALOGO, casos=casos, equipe=equipe,
        filtro=status, hoje=_hoje(), uf=P.uf_padrao(),
    ))


# --------------------------------------------------------------------------

@router.post("/prazos/criar")
def criar(request: Request,
          case_id: str = Form(""), title: str = Form(""),
          term_type: str = Form(""), start_date: str = Form(...),
          days: str = Form(""), business_days: str = Form("1"),
          doubled: str = Form("0"), alert_days: str = Form("3"),
          responsible_user_id: str = Form(""), notes: str = Form(""),
          csrf: str = Form("", alias="_csrf")):
    m = _main()
    if not m.valid_csrf(request, csrf):
        return m.csrf_error()
    user, org = m.require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org

    termo = _data(start_date)
    if not termo:
        return RedirectResponse("/prazos?erro=data_invalida", status_code=303)

    dobro = doubled == "1"
    try:
        if term_type and term_type in P.POR_CODIGO:
            calc = P.calcular_do_catalogo(term_type, termo, dobro=dobro)
            tipo = P.POR_CODIGO[term_type]
            titulo = (title.strip() or tipo.nome)
        else:
            n = int(days or 0)
            calc = P.calcular(termo, n, uteis=(business_days == "1"), dobro=dobro)
            titulo = title.strip() or f"Prazo de {n} dias"
    except ValueError as exc:
        return RedirectResponse(f"/prazos?erro={exc}", status_code=303)

    caso = None
    if case_id.strip().isdigit():
        with db() as conn:
            caso = conn.execute("SELECT id FROM cases WHERE id=? AND organization_id=?",
                                (int(case_id), org["id"])).fetchone()

    with db() as conn:
        conn.execute(
            """INSERT INTO deadlines
               (organization_id,case_id,title,due_date,priority,status,created_at,
                term_type,start_date,count_start,days,business_days,legal_basis,
                doubled,alert_days,responsible_user_id,notes,source)
               VALUES (?,?,?,?,?,'Pendente',?,?,?,?,?,?,?,?,?,?,?,'manual')""",
            (org["id"], caso["id"] if caso else None, titulo,
             calc.vencimento.isoformat(),
             "Alta" if calc.dias <= 5 else "Normal",
             datetime.now().isoformat(timespec="seconds"),
             term_type or None, calc.termo_inicial.isoformat(),
             calc.inicio_contagem.isoformat(), calc.dias,
             1 if calc.uteis else 0, calc.fundamento, 1 if dobro else 0,
             int(alert_days or 3),
             int(responsible_user_id) if responsible_user_id.strip().isdigit() else user["id"],
             "\n".join(calc.suspensoes + ([notes.strip()] if notes.strip() else []))))

    m.log_action(request, f"Prazo criado: {titulo} — {calc.resumo()}")
    return RedirectResponse("/prazos?criado=1", status_code=303)


@router.post("/prazos/{deadline_id}/protocolar")
def protocolar(request: Request, deadline_id: int,
               protocol_date: str = Form(""), csrf: str = Form("", alias="_csrf")):
    m = _main()
    if not m.valid_csrf(request, csrf):
        return m.csrf_error()
    user, org = m.require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    quando = _data(protocol_date) or date.today()
    with db() as conn:
        conn.execute(
            """UPDATE deadlines SET status='Concluído',protocol_date=?
               WHERE id=? AND organization_id=?""",
            (quando.isoformat(), deadline_id, org["id"]))
    m.log_action(request, f"Prazo #{deadline_id} protocolado em {quando}")
    return RedirectResponse("/prazos?protocolado=1", status_code=303)


@router.post("/prazos/{deadline_id}/cancelar")
def cancelar(request: Request, deadline_id: int, csrf: str = Form("", alias="_csrf")):
    m = _main()
    if not m.valid_csrf(request, csrf):
        return m.csrf_error()
    user, org = m.require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    with db() as conn:
        conn.execute("UPDATE deadlines SET status='Cancelado' WHERE id=? AND organization_id=?",
                     (deadline_id, org["id"]))
    return RedirectResponse("/prazos?cancelado=1", status_code=303)


# --------------------------------------------------------------------------

@router.get("/prazos/simular", response_class=HTMLResponse)
def simular(request: Request, tipo: str = "", inicio: str = "", dobro: str = "0"):
    """Calculadora avulsa: confere um prazo sem cadastrar nada."""
    m = _main()
    user, org = m.require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    resultado = erro = None
    termo = _data(inicio)
    if tipo and termo:
        try:
            resultado = P.calcular_do_catalogo(tipo, termo, dobro=(dobro == "1"))
        except ValueError as exc:
            erro = str(exc)
    return m.safe_template_response("prazo_simulador.html", m.common_context(
        request, user, org, catalogo=P.CATALOGO, resultado=resultado, erro=erro,
        tipo=tipo, inicio=inicio, dobro=dobro, uf=P.uf_padrao(), hoje=_hoje(),
    ))
