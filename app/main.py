from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import unicodedata
import calendar
from datetime import date, datetime, timedelta
import traceback
import uuid
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.gzip import GZipMiddleware

from .database import DATA_DIR, db, ensure_column, initialize_schema
from . import plan_limits
from . import nome_documento
from . import movimentacoes
from . import auth_2fa
from . import two_factor
from .smart_intake import detect_case_metadata
from .document_generator import create_power_of_attorney, create_ajg_declaration
from .petition_generator import build_identity_context, create_draft_pdf, complete_local_draft
from .pdf_pipeline import ocr_capability
from .ai_gateway import (
    configured as office_ai_configured, model_name as office_ai_model, ask as office_ai_ask, OFFICE_RULES,
    connection_status as office_ai_status, legal_model_name as office_legal_model,
    intake_model_name as office_intake_model, routine_model_name as office_routine_model,
)
from .copilot import (
    ai_available, ai_model, answer_question, case_stats, deep_analysis, draft_petition,
    hard_truth, hard_truth_enhanced, index_pdf, infer_measure, recent_context, remove_document_files,
    safe_filename, search_case, store_uploaded_pdf, timeline, resolve_uploaded_pdf_path,
)

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env.local", override=False)
WORKSPACE_ASSET_DIR = BASE_DIR / "app" / "static" / "workspaces"
WORKSPACE_ASSET_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_ROOT = DATA_DIR / "uploads"
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
IMPORT_ROOT = DATA_DIR / "imports"
IMPORT_ROOT.mkdir(parents=True, exist_ok=True)
GENERATED_ROOT = DATA_DIR / "generated"
GENERATED_ROOT.mkdir(parents=True, exist_ok=True)

APP_ENV = os.getenv("JARBAS_ENV", "development").strip().lower()
SECRET_KEY = os.getenv("JARBAS_SECRET_KEY", "")
if APP_ENV == "production" and len(SECRET_KEY) < 32:
    raise RuntimeError("JARBAS_SECRET_KEY deve possuir ao menos 32 caracteres em produção.")
if not SECRET_KEY:
    SECRET_KEY = "DESENVOLVIMENTO-TROQUE-ESTA-CHAVE"

# Versão vem do VERSION.txt: fonte única, evita /health mentir sobre o build.
try:
    APP_VERSION = (BASE_DIR / "VERSION.txt").read_text(encoding="utf-8").strip() or "0.0.0"
except OSError:
    APP_VERSION = "0.0.0"

# Identificador do build. Duas instalações podem declarar a MESMA versão e
# ter conteúdo diferente; o build não repete. Sem ele não há como responder
# "qual pacote está instalado?" — e essa pergunta já custou uma rodada
# inteira de correção, com a instalação falhando por um defeito que já estava
# corrigido no pacote que não havia sido aplicado.
try:
    _carimbo = (BASE_DIR / "BUILD.txt").read_text(encoding="utf-8")
    APP_BUILD = next(
        (l.split(":", 1)[1].strip() for l in _carimbo.splitlines()
         if l.lower().startswith("build")), "desconhecido")
except OSError:
    APP_BUILD = "desenvolvimento"

app = FastAPI(title="JARBAS Jurídico — Plataforma + Copiloto", version=APP_VERSION, docs_url=None if APP_ENV == "production" else "/docs")
allowed_hosts = [h.strip() for h in os.getenv("JARBAS_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts or ["localhost"])
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="lax",
    https_only=os.getenv("JARBAS_HTTPS_ONLY", "0") == "1",
    max_age=int(os.getenv("JARBAS_SESSION_MAX_AGE", "28800")),
    session_cookie="jarbas_session_830",
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")


def safe_template_response(name: str, context: Optional[dict] = None, status_code: int = 200):
    """Renderização compatível com Starlette atual e futuras versões.

    Mantém o formato legado usado pelo restante do código, mas chama a API
    moderna request-first do TemplateResponse e injeta variáveis públicas
    padrão para evitar UndefinedError nos templates base.
    """
    context = dict(context or {})
    request = context.get("request")
    if request is None:
        raise RuntimeError("Template sem objeto Request no contexto.")
    context.setdefault("user", None)
    context.setdefault("organization", None)
    context.setdefault("workspaces", [])
    context.setdefault("subscription", None)
    context.setdefault("usage", 0)
    context.setdefault("credits_left", 0)
    return templates.TemplateResponse(request=request, name=name, context=context, status_code=status_code)


def csrf_token(request: Request) -> str:
    token = request.session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["_csrf"] = token
    return token


def valid_csrf(request: Request, token: str) -> bool:
    expected = request.session.get("_csrf", "")
    return bool(expected and token and secrets.compare_digest(expected, token))


def csrf_error():
    return HTMLResponse("Solicitação recusada: sessão ou token de segurança inválido. Atualize a página e tente novamente.", status_code=403)


templates.env.globals["csrf_token"] = csrf_token
# A versão exibida no rodapé, na barra lateral e em outras quatro telas
# estava escrita à mão nos templates, e ficou parada numa release antiga
# enquanto o sistema avançava. Quem abria o sistema — ou mandava um print
# ao suporte — lia a versão errada. Agora sai da mesma fonte que /health:
# o VERSION.txt.
templates.env.globals["app_version"] = APP_VERSION
templates.env.globals["app_build"] = APP_BUILD


# --------------------------------------------------------------------------
# Formatação brasileira.
#
# O sistema exibia "R$ 25000.00" e "2026-09-18" — ponto decimal e data ISO,
# que é como o Python serializa, não como o Brasil escreve. Num sistema que
# mostra valor de causa, honorário e prazo lado a lado, isso não é questão de
# estética: "R$ 25000.00" e "R$ 25.000,00" são lidos com esforço diferente, e
# quem confere uma planilha de honorários às pressas erra a casa decimal.
# Data em ISO tem o mesmo problema — 2026-09-18 exige tradução mental a cada
# leitura, e um prazo lido errado é perda de prazo.
# --------------------------------------------------------------------------

def formatar_moeda(valor, simbolo: bool = True) -> str:
    """1234.5 -> 'R$ 1.234,50'. Vazio e texto inválido viram travessão."""
    if valor is None or valor == "":
        return "—"
    try:
        n = float(valor)
    except (TypeError, ValueError):
        return "—"
    # Troca ponto e vírgula em duas etapas para não embaralhar os separadores.
    inteiro, _, decimal = f"{abs(n):,.2f}".partition(".")
    corpo = inteiro.replace(",", ".") + "," + decimal
    sinal = "-" if n < 0 else ""
    return f"{sinal}R$ {corpo}" if simbolo else f"{sinal}{corpo}"


def formatar_numero(valor, casas: int = 2) -> str:
    """1234.5 -> '1.234,50'. Para o que NÃO é dinheiro: tamanho, horas, taxa."""
    if valor is None or valor == "":
        return "—"
    try:
        n = float(valor)
    except (TypeError, ValueError):
        return "—"
    inteiro, _, decimal = f"{abs(n):,.{casas}f}".partition(".")
    corpo = inteiro.replace(",", ".")
    if casas:
        corpo += "," + decimal
    return ("-" if n < 0 else "") + corpo


def formatar_data(valor) -> str:
    """'2026-09-18' ou datetime -> '18/09/2026'. Guarda o que não reconhece."""
    if not valor:
        return "—"
    if isinstance(valor, (datetime, date)):
        return valor.strftime("%d/%m/%Y")
    texto = str(valor).strip()
    try:
        return date.fromisoformat(texto[:10]).strftime("%d/%m/%Y")
    except ValueError:
        # Não inventa formato: devolve como está, para o dado nunca sumir.
        return texto


def formatar_datahora(valor) -> str:
    """'2026-09-18T14:30:05' -> '18/09/2026 14:30'."""
    if not valor:
        return "—"
    if isinstance(valor, datetime):
        return valor.strftime("%d/%m/%Y %H:%M")
    texto = str(valor).strip().replace("T", " ")
    try:
        return datetime.fromisoformat(texto[:19]).strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return formatar_data(texto)


templates.env.filters["moeda"] = formatar_moeda
templates.env.filters["numero"] = formatar_numero
templates.env.filters["data"] = formatar_data
templates.env.filters["datahora"] = formatar_datahora

# Proteção simples de força bruta no MVP. Em escala, substituir por Redis/WAF.
LOGIN_FAILURES: dict[str, list[float]] = {}

# Quantos proxies reversos existem na frente do JARBAS. 0 = nenhum.
#
# Atrás de nginx/Caddy/Traefik, request.client.host é o IP do PROXY, igual
# para todo mundo. Com isso o bloqueio de força bruta vira global: oito
# senhas erradas de um estranho na internet trancam o escritório inteiro
# para fora, e a auditoria registra o IP do proxy em vez do IP de quem agiu.
#
# X-Forwarded-For só pode ser lido quando SABEMOS que um proxy o reescreve,
# porque o cliente pode forjar o cabeçalho e escapar do bloqueio escolhendo
# um IP novo a cada tentativa. Por isso a leitura é opt-in e conta saltos a
# partir da DIREITA, que é a parte que o nosso proxy acrescentou.
TRUSTED_PROXY_HOPS = max(0, int(os.getenv("JARBAS_TRUSTED_PROXY_HOPS", "0") or 0))


def login_client_ip(request: Request) -> str:
    direto = request.client.host if request.client else "unknown"
    if not TRUSTED_PROXY_HOPS:
        return direto
    encaminhados = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    if not encaminhados:
        return direto
    # O salto mais à direita é o proxy imediato; recuamos o número de saltos
    # confiáveis para chegar ao primeiro endereço que o cliente não controla.
    indice = len(encaminhados) - TRUSTED_PROXY_HOPS
    return encaminhados[max(0, min(indice, len(encaminhados) - 1))]

def login_is_blocked(request: Request) -> bool:
    import time
    ip = login_client_ip(request)
    now = time.time()
    window = 15 * 60
    attempts = [t for t in LOGIN_FAILURES.get(ip, []) if now - t < window]
    LOGIN_FAILURES[ip] = attempts
    return len(attempts) >= 8

def login_failure(request: Request) -> None:
    import time
    ip = login_client_ip(request)
    LOGIN_FAILURES.setdefault(ip, []).append(time.time())

def login_success(request: Request) -> None:
    LOGIN_FAILURES.pop(login_client_ip(request), None)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    error_id = uuid.uuid4().hex[:10].upper()
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "runtime-errors.log"
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(f"\n[{datetime.now().isoformat(timespec='seconds')}] ERRO {error_id} {request.method} {request.url.path}\n")
        fh.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    return HTMLResponse(
        f"""<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'><title>JARBAS - erro</title>
        <style>body{{font-family:Arial,sans-serif;background:#17090d;color:#fff;padding:40px}}
        .box{{max-width:760px;margin:auto;background:#291018;border:1px solid #7f2943;border-radius:18px;padding:28px}}
        code{{background:#11070a;padding:4px 8px;border-radius:6px}}a{{color:#ff8faa}}</style></head><body>
        <div class='box'><h1>JARBAS encontrou um erro interno</h1><p>O erro foi registrado automaticamente.</p>
        <p>Código: <code>{error_id}</code></p><p>Arquivo de diagnóstico: <code>logs/runtime-errors.log</code></p>
        <p><a href='/health'>Testar integridade do servidor</a></p></div></body></html>""",
        status_code=500,
    )


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; font-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'self'",
    )
    if os.getenv("JARBAS_HTTPS_ONLY", "0") == "1":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if not request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-store")
    if os.getenv("JARBAS_ALLOW_INDEXING", "0") != "1":
        response.headers.setdefault("X-Robots-Tag", "noindex, nofollow, noarchive")
    return response


PASSWORD_ITERATIONS = 600_000


def hash_password(password: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        if stored.startswith("pbkdf2_sha256$"):
            _, iterations_s, salt, expected = stored.split("$", 3)
            iterations = int(iterations_s)
        else:
            # Compatibilidade com hashes criados pelas versões 1.x/2.x.
            salt, expected = stored.split("$", 1)
            iterations = 210_000
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations).hex()
        return secrets.compare_digest(digest, expected)
    except (ValueError, TypeError):
        return False


def password_hash_needs_upgrade(stored: str) -> bool:
    try:
        if not stored.startswith("pbkdf2_sha256$"):
            return True
        _, iterations_s, _, _ = stored.split("$", 3)
        return int(iterations_s) < PASSWORD_ITERATIONS
    except Exception:
        return True


def safe_data_file(path_value: str, allowed_root: Path) -> Optional[Path]:
    """Resolve um caminho persistido e só o aceita se estiver dentro da raiz esperada."""
    try:
        target = Path(path_value).resolve(strict=False)
        root = allowed_root.resolve(strict=False)
        target.relative_to(root)
        return target
    except Exception:
        return None


def resolve_case_document_row_path(doc, *, org_id: int, case_id: int) -> Optional[Path]:
    if not doc:
        return None
    try:
        return resolve_uploaded_pdf_path(
            doc["stored_path"] or "",
            org_id=org_id,
            case_id=case_id,
            stored_name=doc["stored_name"] or "",
        )
    except Exception:
        return None


def resolve_case_pdf_rows(rows, *, org_id: int, case_id: int, limit: int = 6) -> list[Path]:
    paths: list[Path] = []
    for row in list(rows)[:limit]:
        path = resolve_case_document_row_path(row, org_id=org_id, case_id=case_id)
        if path and path.is_file():
            paths.append(path)
    return paths


def case_documents_require_pdf_review(rows) -> bool:
    """True when local extraction is incomplete and the AI should inspect the PDF itself."""
    for row in rows or []:
        try:
            status = str(row["status"] or "").lower()
            pages = int(row["page_count"] or 0) if "page_count" in row.keys() else 0
            chars = int(row["text_chars"] or 0) if "text_chars" in row.keys() else 0
        except Exception:
            continue
        if status in {"needs_ocr", "partial_ocr", "error", "encrypted"}:
            return True
        if pages > 0 and chars / pages < 80:
            return True
    return False


def heal_case_document_path(conn, doc, *, org_id: int, case_id: int) -> Optional[Path]:
    path = resolve_case_document_row_path(doc, org_id=org_id, case_id=case_id)
    if path and str(path) != str(doc["stored_path"] or ""):
        try:
            conn.execute(
                "UPDATE case_documents SET stored_path=? WHERE id=? AND organization_id=? AND case_id=?",
                (str(path), doc["id"], org_id, case_id),
            )
        except Exception:
            pass
    return path


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized).strip("-").lower()
    return slug or "escritorio"



def unique_slug(conn, name: str) -> str:
    base = slugify(name)
    slug = base
    n = 2
    while conn.execute("SELECT 1 FROM organizations WHERE slug=?", (slug,)).fetchone():
        slug = f"{base}-{n}"
        n += 1
    return slug


def add_months_iso(value: str, months: int) -> str:
    """Soma meses preservando o dia quando possível, sem dependência externa."""
    base = date.fromisoformat(value)
    absolute = base.year * 12 + (base.month - 1) + months
    year, month0 = divmod(absolute, 12)
    month = month0 + 1
    day = min(base.day, calendar.monthrange(year, month)[1])
    return date(year, month, day).isoformat()


def ensure_financial_setup(conn, org_id: int) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    if not conn.execute("SELECT 1 FROM financial_accounts WHERE organization_id=? LIMIT 1", (org_id,)).fetchone():
        conn.execute(
            "INSERT INTO financial_accounts (organization_id,name,account_type,bank_name,opening_balance,active,created_at) VALUES (?,?,?,?,0,1,?)",
            (org_id, "Caixa do Escritório", "Caixa", "", now),
        )
    defaults = [
        ("Honorários contratuais", "income"), ("Honorários de êxito", "income"), ("Consultas", "income"),
        ("Sucumbência", "income"), ("Reembolsos", "income"), ("Custas processuais", "expense"),
        ("Correspondentes", "expense"), ("Folha e pró-labore", "expense"), ("Tributos", "expense"),
        ("Marketing", "expense"), ("Infraestrutura", "expense"), ("Tecnologia", "expense"),
        ("Outras receitas", "income"), ("Outras despesas", "expense"),
    ]
    for name, direction in defaults:
        conn.execute(
            "INSERT OR IGNORE INTO financial_categories (organization_id,name,direction,active,created_at) VALUES (?,?,?,1,?)",
            (org_id, name, direction, now),
        )


def migrate_legacy_finance(conn, org_id: int) -> None:
    """Copia os lançamentos antigos para o novo razão financeiro sem duplicá-los."""
    ensure_financial_setup(conn, org_id)
    rows = conn.execute("SELECT * FROM finance WHERE organization_id=? ORDER BY id", (org_id,)).fetchall()
    for row in rows:
        exists = conn.execute(
            "SELECT 1 FROM financial_transactions WHERE organization_id=? AND legacy_finance_id=?",
            (org_id, row["id"]),
        ).fetchone()
        if exists:
            continue
        direction = "payable" if (row["kind"] or "").lower() in ("despesa", "custa", "custas") else "receivable"
        category_name = "Custas processuais" if direction == "payable" else (
            "Honorários de êxito" if "êxito" in (row["kind"] or "").lower() else
            "Consultas" if "consulta" in (row["kind"] or "").lower() else "Honorários contratuais"
        )
        category = conn.execute(
            "SELECT id FROM financial_categories WHERE organization_id=? AND name=? LIMIT 1", (org_id, category_name)
        ).fetchone()
        conn.execute(
            """INSERT INTO financial_transactions
               (organization_id,client_id,case_id,contract_id,category_id,direction,description,original_amount,due_date,competence_date,status,payment_method,notes,legacy_finance_id,created_at)
               VALUES (?,?,?,NULL,?,?,?,?,?,?,?,'','Migrado do módulo financeiro legado',?,?)""",
            (org_id, row["client_id"], row["case_id"], category["id"] if category else None, direction,
             row["description"], row["amount"], row["due_date"], (row["created_at"] or "")[:10] or None,
             row["status"] or "Pendente", row["id"], row["created_at"]),
        )


def refresh_financial_statuses(conn, org_id: int) -> None:
    today = date.today().isoformat()
    rows = conn.execute(
        """SELECT t.id,t.original_amount,t.due_date,t.status,COALESCE(SUM(p.amount),0) paid
           FROM financial_transactions t LEFT JOIN financial_payments p ON p.transaction_id=t.id AND p.organization_id=t.organization_id
           WHERE t.organization_id=? GROUP BY t.id,t.original_amount,t.due_date,t.status""",
        (org_id,),
    ).fetchall()
    for row in rows:
        if row["status"] == "Cancelado":
            continue
        amount = float(row["original_amount"] or 0)
        paid = float(row["paid"] or 0)
        if paid >= amount - 0.005 and amount > 0:
            status = "Pago"
        elif paid > 0:
            status = "Parcial"
        elif row["due_date"] and row["due_date"] < today:
            status = "Vencido"
        else:
            status = "Pendente"
        if status != row["status"]:
            conn.execute("UPDATE financial_transactions SET status=? WHERE id=? AND organization_id=?", (status, row["id"], org_id))


def money(value) -> float:
    try:
        return round(float(value or 0), 2)
    except Exception:
        return 0.0


def financial_transaction_rows(conn, org_id: int, limit: int = 500):
    refresh_financial_statuses(conn, org_id)
    return conn.execute(
        """SELECT t.*,cl.name client_name,c.title case_title,c.number case_number,fc.title contract_title,
                  cat.name category_name,cat.code category_code,cc.code cost_center_code,cc.name cost_center_name,
                  COALESCE((SELECT SUM(p.amount) FROM financial_payments p WHERE p.transaction_id=t.id AND p.organization_id=t.organization_id),0) paid_amount
           FROM financial_transactions t
           LEFT JOIN clients cl ON cl.id=t.client_id AND cl.organization_id=t.organization_id
           LEFT JOIN cases c ON c.id=t.case_id AND c.organization_id=t.organization_id
           LEFT JOIN fee_contracts fc ON fc.id=t.contract_id AND fc.organization_id=t.organization_id
           LEFT JOIN financial_categories cat ON cat.id=t.category_id AND cat.organization_id=t.organization_id
           LEFT JOIN cost_centers cc ON cc.id=t.cost_center_id AND cc.organization_id=t.organization_id
           WHERE t.organization_id=?
           ORDER BY CASE t.status WHEN 'Vencido' THEN 1 WHEN 'Parcial' THEN 2 WHEN 'Pendente' THEN 3 ELSE 4 END,
                    COALESCE(t.due_date,'9999-12-31'),t.id DESC LIMIT ?""",
        (org_id, limit),
    ).fetchall()


def financial_snapshot(conn, org_id: int) -> dict:
    rows = financial_transaction_rows(conn, org_id, 5000)
    today = date.today()
    month_start = today.replace(day=1).isoformat()
    received_month = conn.execute(
        """SELECT COALESCE(SUM(p.amount),0) s FROM financial_payments p
           JOIN financial_transactions t ON t.id=p.transaction_id AND t.organization_id=p.organization_id
           WHERE p.organization_id=? AND t.direction='receivable' AND p.paid_at>=?""",
        (org_id, month_start),
    ).fetchone()["s"]
    paid_month = conn.execute(
        """SELECT COALESCE(SUM(p.amount),0) s FROM financial_payments p
           JOIN financial_transactions t ON t.id=p.transaction_id AND t.organization_id=p.organization_id
           WHERE p.organization_id=? AND t.direction='payable' AND p.paid_at>=?""",
        (org_id, month_start),
    ).fetchone()["s"]
    receive_open = pay_open = overdue_receive = overdue_pay = 0.0
    for r in rows:
        remaining = max(money(r["original_amount"]) - money(r["paid_amount"]), 0)
        if r["direction"] == "receivable" and r["status"] != "Pago":
            receive_open += remaining
            if r["status"] == "Vencido": overdue_receive += remaining
        if r["direction"] == "payable" and r["status"] != "Pago":
            pay_open += remaining
            if r["status"] == "Vencido": overdue_pay += remaining
    accounts = conn.execute(
        """SELECT a.*,
          a.opening_balance + COALESCE((SELECT SUM(CASE WHEN t.direction='receivable' THEN p.amount ELSE -p.amount END)
          FROM financial_payments p JOIN financial_transactions t ON t.id=p.transaction_id AND t.organization_id=p.organization_id
          WHERE p.account_id=a.id AND p.organization_id=a.organization_id),0) current_balance
          FROM financial_accounts a WHERE a.organization_id=? AND a.active=1 ORDER BY a.name""",
        (org_id,),
    ).fetchall()
    cash_balance = sum(money(a["current_balance"]) for a in accounts)
    return {
        "receivable_open": round(receive_open, 2), "payable_open": round(pay_open, 2),
        "overdue_receivable": round(overdue_receive, 2), "overdue_payable": round(overdue_pay, 2),
        "received_month": money(received_month), "paid_month": money(paid_month),
        "result_month": round(money(received_month)-money(paid_month), 2), "cash_balance": round(cash_balance, 2),
        "rows": rows, "accounts": accounts,
    }



def ensure_v7_setup(conn) -> None:
    """Migração incremental 7.0. Mantém compatibilidade com bases 4.x/5.x/6.x."""
    # Cadastro completo do cliente.
    for definition in (
        "person_type TEXT DEFAULT 'Pessoa Física'", "nationality TEXT", "marital_status TEXT", "profession TEXT",
        "rg TEXT", "birth_date TEXT", "address TEXT", "address_number TEXT", "complement TEXT", "neighborhood TEXT",
        "city TEXT", "state TEXT", "zip_code TEXT", "responsible_name TEXT", "updated_at TEXT",
    ):
        ensure_column(conn, "clients", definition)
    # Dossiê processual 8.1: preserva metadados capturados pelo Intake e edição posterior.
    for definition in (
        "case_class TEXT", "subject TEXT", "claim_value REAL NOT NULL DEFAULT 0", "updated_at TEXT"
    ):
        ensure_column(conn, "cases", definition)

    # Identidade profissional do workspace para documentos automáticos.
    for definition in ("lawyer_name TEXT", "oab_number TEXT", "city TEXT"):
        ensure_column(conn, "organizations", definition)
    # Minutas 8.2: PDF institucional gerado e rastreável.
    for definition in ("pdf_path TEXT", "pdf_file_name TEXT"):
        ensure_column(conn, "drafts", definition)
    # Plano de contas / centros de custo / despesas detalhadas.
    for definition in ("code TEXT", "parent_id INTEGER"):
        ensure_column(conn, "financial_categories", definition)
    for definition in ("cost_center_id INTEGER", "counterparty TEXT", "document_number TEXT"):
        ensure_column(conn, "financial_transactions", definition)
    # Preço regular + campanha promocional SaaS.
    for definition in ("regular_price REAL", "promo_discount REAL NOT NULL DEFAULT 0", "promo_active INTEGER NOT NULL DEFAULT 0"):
        ensure_column(conn, "plans", definition)

    now = datetime.now().isoformat(timespec="seconds")
    # Preços do 6.0 reduzidos em 50% como campanha promocional de lançamento.
    promo = {
        "solo": (149.00, 74.50),
        "pro": (299.00, 149.50),
        "office": (599.00, 299.50),
        "enterprise": (1499.00, 749.50),
    }
    for code, (regular, promotional) in promo.items():
        conn.execute(
            "UPDATE plans SET regular_price=?,monthly_price=?,promo_discount=50,promo_active=1 WHERE code=?",
            (regular, promotional, code),
        )

    # Plano de contas inicial padronizado e numerado.
    chart = [
        ("1", "RECEITAS", "income"),
        ("1.01", "Honorários contratuais", "income"),
        ("1.02", "Honorários de êxito", "income"),
        ("1.03", "Consultas", "income"),
        ("1.04", "Sucumbência", "income"),
        ("1.05", "Reembolsos", "income"),
        ("1.99", "Outras receitas", "income"),
        ("2", "DESPESAS", "expense"),
        ("2.01", "Custas processuais", "expense"),
        ("2.02", "Correspondentes", "expense"),
        ("2.03", "Folha e pró-labore", "expense"),
        ("2.04", "Tributos", "expense"),
        ("2.05", "Marketing", "expense"),
        ("2.06", "Infraestrutura", "expense"),
        ("2.07", "Tecnologia", "expense"),
        ("2.08", "Deslocamentos", "expense"),
        ("2.99", "Outras despesas", "expense"),
    ]
    orgs = conn.execute("SELECT id FROM organizations").fetchall()
    for orgrow in orgs:
        org_id = orgrow["id"]
        for code, name, direction in chart:
            row = conn.execute(
                "SELECT id FROM financial_categories WHERE organization_id=? AND name=? AND direction=? LIMIT 1",
                (org_id, name, direction),
            ).fetchone()
            if not row:
                conn.execute(
                    "INSERT INTO financial_categories (organization_id,name,direction,active,created_at,code,parent_id) VALUES (?,?,?,1,?,?,NULL)",
                    (org_id, name, direction, now, code),
                )
            else:
                conn.execute("UPDATE financial_categories SET code=COALESCE(code,?) WHERE id=?", (code, row["id"]))
        for cc_code, cc_name in (("ADM", "Administrativo"), ("CONT", "Contencioso"), ("CONS", "Consultivo"), ("MKT", "Marketing")):
            conn.execute(
                "INSERT OR IGNORE INTO cost_centers (organization_id,code,name,active,created_at) VALUES (?,?,?,1,?)",
                (org_id, cc_code, cc_name, now),
            )
        # Textos-base editáveis em evolução futura; os DOCX são gerados a partir dos dados estruturados.
        conn.execute(
            "INSERT OR IGNORE INTO document_templates (organization_id,code,name,body,active,updated_at) VALUES (?,?,?, ?,1,?)",
            (org_id, "procuracao", "Procuração padrão", "Modelo estruturado JARBAS — revisar antes da assinatura.", now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO document_templates (organization_id,code,name,body,active,updated_at) VALUES (?,?,?, ?,1,?)",
            (org_id, "ajg", "Declaração de hipossuficiência", "Modelo estruturado JARBAS — revisar antes da assinatura.", now),
        )

def _mark_schema_version(conn, version: str | None = None) -> None:
    version = version or APP_VERSION
    now = datetime.now().isoformat(timespec="seconds")
    try:
        row = conn.execute("SELECT key FROM app_meta WHERE key='schema_version'").fetchone()
        if row:
            conn.execute("UPDATE app_meta SET value=?,updated_at=? WHERE key='schema_version'", (version, now))
        else:
            conn.execute("INSERT INTO app_meta (key,value,updated_at) VALUES ('schema_version',?,?)", (version, now))
    except Exception:
        pass


def repair_case_document_paths(conn) -> int:
    """Corrige caminhos legados de PDFs para a raiz canônica da instalação atual."""
    fixed=0
    try:
        rows=conn.execute("SELECT id,organization_id,case_id,stored_path,stored_name FROM case_documents").fetchall()
    except Exception:
        return 0
    for row in rows:
        path=resolve_uploaded_pdf_path(row["stored_path"] or "",org_id=row["organization_id"],case_id=row["case_id"],stored_name=row["stored_name"] or "")
        if path and str(path)!=str(row["stored_path"] or ""):
            conn.execute("UPDATE case_documents SET stored_path=? WHERE id=?",(str(path),row["id"]))
            fixed+=1
    return fixed


def init_db() -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with db() as conn:
        initialize_schema(conn)
        ensure_v7_setup(conn)
        _mark_schema_version(conn)
        # Índice de busca: idempotente. Se o SQLite não tiver FTS5, segue sem.
        try:
            from . import search_index
            search_index.preparar_indice(conn)
        except Exception:
            pass
        repair_case_document_paths(conn)

        # Migração segura de bancos da versão 1.0.
        ensure_column(conn, "users", "is_superadmin INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "organizations", "address TEXT")
        ensure_column(conn, "organizations", "website TEXT")
        ensure_column(conn, "organizations", "primary_color TEXT DEFAULT '#9f2948'")
        ensure_column(conn, "organizations", "timezone TEXT DEFAULT 'America/Sao_Paulo'")
        for table in ("clients", "cases", "deadlines", "finance", "action_logs"):
            ensure_column(conn, table, "organization_id INTEGER")

        default_plans = [
            ("solo", "JARBAS Solo", 74.50, 1, 300, 10, 500),
            ("pro", "JARBAS Pro", 149.50, 3, 1000, 30, 2000),
            ("office", "JARBAS Office", 299.50, 10, 5000, 100, 6000),
            ("enterprise", "JARBAS Enterprise", 749.50, 50, 50000, 1000, 30000),
        ]
        for plan in default_plans:
            conn.execute(
                """INSERT OR IGNORE INTO plans
                   (code,name,monthly_price,user_limit,case_limit,storage_gb,monthly_credits)
                   VALUES (?,?,?,?,?,?,?)""",
                plan,
            )

        admin_email = os.getenv("JARBAS_ADMIN_EMAIL", "admin@chagasadvogados.local").strip().lower()
        # IMPORTANTE: em produção a senha administrativa só é necessária para
        # criar o primeiro usuário. Em inicializações posteriores o banco já
        # contém o hash e o servidor não deve exigir uma senha em variável de
        # ambiente. O bootstrap do instalador pode fornecer a senha pelo canal
        # efêmero JARBAS_BOOTSTRAP_ADMIN_PASSWORD.
        user = conn.execute("SELECT * FROM users WHERE lower(email)=lower(?)", (admin_email,)).fetchone()
        if not user:
            admin_password = (
                os.getenv("JARBAS_ADMIN_PASSWORD", "")
                or os.getenv("JARBAS_BOOTSTRAP_ADMIN_PASSWORD", "")
            )
            if not admin_password:
                if APP_ENV == "production":
                    raise RuntimeError(
                        "Usuário administrador ainda não existe. Execute o bootstrap do instalador "
                        "ou defina JARBAS_ADMIN_PASSWORD somente para a criação inicial."
                    )
                admin_password = "TroqueEstaSenha123!"
            conn.execute(
                "INSERT INTO users (name,email,password_hash,role,created_at,is_superadmin) VALUES (?,?,?,?,?,1)",
                ("Dr. Sandro Chagas", admin_email, hash_password(admin_password), "admin", now),
            )
            user = conn.execute("SELECT * FROM users WHERE lower(email)=lower(?)", (admin_email,)).fetchone()
        else:
            conn.execute("UPDATE users SET is_superadmin=1 WHERE id=?", (user["id"],))

        org = conn.execute("SELECT * FROM organizations WHERE slug='chagas-advogados'").fetchone()
        if not org:
            conn.execute(
                """INSERT INTO organizations
                   (name,slug,document,email,phone,brand_name,logo_path,status,created_at)
                   VALUES (?,?,?,?,?,?,?,'active',?)""",
                (
                    "CHAGAS – ADVOGADOS",
                    "chagas-advogados",
                    "",
                    "schagasadvocacia@gmail.com",
                    "(54) 99110-1959",
                    "CHAGAS – ADVOGADOS",
                    "/static/chagas_logo.jpeg",
                    now,
                ),
            )
            org = conn.execute("SELECT * FROM organizations WHERE slug='chagas-advogados'").fetchone()

        conn.execute(
            """INSERT OR IGNORE INTO memberships
               (user_id,organization_id,role,is_active,created_at) VALUES (?,?,'owner',1,?)""",
            (user["id"], org["id"], now),
        )

        plan = conn.execute("SELECT * FROM plans WHERE code='office'").fetchone()
        conn.execute(
            """INSERT OR IGNORE INTO subscriptions
               (organization_id,plan_id,status,started_at,current_period_end)
               VALUES (?,?,'active',?,?)""",
            (org["id"], plan["id"], now, (date.today() + timedelta(days=30)).isoformat()),
        )

        # Tudo o que existia na versão interna passa a pertencer ao primeiro workspace.
        for table in ("clients", "cases", "deadlines", "finance", "action_logs"):
            conn.execute(f"UPDATE {table} SET organization_id=? WHERE organization_id IS NULL", (org["id"],))

        # JARBAS 6.0: prepara ERP financeiro e migra lançamentos legados para cada workspace.
        organizations = conn.execute("SELECT id FROM organizations").fetchall()
        for org_row in organizations:
            ensure_financial_setup(conn, org_row["id"])
            migrate_legacy_finance(conn, org_row["id"])
            refresh_financial_statuses(conn, org_row["id"])
        ensure_v7_setup(conn)


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/health")
def health():
    """Endpoint mínimo para health-check de containers e balanceadores."""
    with db() as conn:
        conn.execute("SELECT 1").fetchone()
    return {"status": "ok", "product": "JARBAS Jurídico Principal",
            "version": APP_VERSION, "build": APP_BUILD,
            "ai": "connected" if ai_available() else "local"}


def current_user(request: Request):
    email = request.session.get("user")
    if not email:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT id,name,email,role,is_superadmin FROM users WHERE email=?", (email,)
        ).fetchone()
    return row


def available_workspaces(user_id: int):
    with db() as conn:
        return conn.execute(
            """SELECT o.id,o.name,o.slug,o.logo_path,m.role
               FROM memberships m JOIN organizations o ON o.id=m.organization_id
               WHERE m.user_id=? AND m.is_active=1 AND o.status='active'
               ORDER BY o.name""",
            (user_id,),
        ).fetchall()


def current_workspace(request: Request, user=None):
    user = user or current_user(request)
    if not user:
        return None
    org_id = request.session.get("org_id")
    workspaces = available_workspaces(user["id"])
    if not workspaces:
        return None
    valid_ids = {w["id"] for w in workspaces}
    if org_id not in valid_ids:
        org_id = workspaces[0]["id"]
        request.session["org_id"] = org_id
    with db() as conn:
        return conn.execute(
            """SELECT o.*,m.role membership_role
               FROM organizations o JOIN memberships m ON m.organization_id=o.id
               WHERE o.id=? AND m.user_id=? AND m.is_active=1""",
            (org_id, user["id"]),
        ).fetchone()


def current_subscription(org_id: int):
    with db() as conn:
        return conn.execute(
            """SELECT s.*,p.code plan_code,p.name plan_name,p.monthly_price,p.regular_price,p.promo_discount,p.promo_active,p.user_limit,
                      p.case_limit,p.storage_gb,p.monthly_credits
               FROM subscriptions s JOIN plans p ON p.id=s.plan_id
               WHERE s.organization_id=?""",
            (org_id,),
        ).fetchone()


def month_usage(org_id: int) -> int:
    first_day = date.today().replace(day=1).isoformat()
    with db() as conn:
        return conn.execute(
            "SELECT COALESCE(SUM(credits),0) used FROM usage_ledger WHERE organization_id=? AND created_at>=?",
            (org_id, first_day),
        ).fetchone()["used"]


def require_user(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return user


def subscription_access_state(org_id: int) -> tuple[bool, str]:
    """Retorna se o workspace pode usar o sistema conforme assinatura/trial.

    O billing e o Super Admin permanecem acessíveis para permitir regularização.
    """
    sub = current_subscription(org_id)
    if not sub:
        return False, "missing"
    status = (sub["status"] or "").lower()
    if status not in ("active", "trial"):
        return False, status or "inactive"
    end = sub["current_period_end"] or ""
    if end:
        try:
            end_day = date.fromisoformat(str(end)[:10])
            if end_day < date.today():
                return False, "expired"
        except Exception:
            pass
    return True, status


def require_workspace(request: Request):
    """Devolve (user, org). Em qualquer falha, os DOIS vêm como RedirectResponse.

    Antes devolvia (redirect, None) quando a sessão expirava. As rotas checam
    `isinstance(org, RedirectResponse)` — e None não é RedirectResponse, então
    passava direto e a linha seguinte estourava com 500:
        'NoneType' object is not subscriptable        (ao usar org["id"])
        'RedirectResponse' object is not subscriptable (ao usar user["..."])
    Devolver o mesmo redirect nos dois lugares conserta toda rota de uma vez,
    sem depender de cada uma lembrar de uma checagem extra.
    """
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user, user
    org = current_workspace(request, user)
    if not org:
        return user, RedirectResponse("/no-workspace", status_code=303)
    # Super Admin e páginas de regularização não ficam presos pelo bloqueio comercial.
    path = request.url.path
    exempt = path.startswith(("/billing", "/logout", "/switch-workspace", "/platform", "/health", "/static"))
    allowed, state = subscription_access_state(org["id"])
    if not allowed and not exempt and not user["is_superadmin"]:
        return user, RedirectResponse(f"/billing?subscription={state}", status_code=303)

    # Exigência de segundo fator. Quem ainda não configurou fica preso na
    # própria tela de configuração — sem isso a exigência seria apenas um
    # aviso, e um aviso não protege auto de cliente nenhum. A tela de
    # configuração, a troca de senha e a saída ficam de fora, senão o
    # usuário fica trancado sem ter como cumprir a exigência.
    if auth_2fa.exige_2fa(user, org) and not two_factor.ativo(user["id"]):
        if not path.startswith(("/settings/2fa", "/logout", "/health", "/static", "/account/password")):
            return user, RedirectResponse("/settings/2fa?obrigatorio=1", status_code=303)
    return user, org


def common_context(request: Request, user, org, **extra):
    workspaces = available_workspaces(user["id"])
    subscription = current_subscription(org["id"]) if org else None
    usage = month_usage(org["id"]) if org else 0
    credits_left = max((subscription["monthly_credits"] if subscription else 0) - usage, 0)
    ctx = {
        "request": request,
        "user": user,
        "organization": org,
        "workspaces": workspaces,
        "subscription": subscription,
        "usage": usage,
        "credits_left": credits_left,
        "office_ai_connected": office_ai_configured(),
        "office_ai_model": office_ai_model(),
        "office_ai_status": office_ai_status(),
    }
    ctx.update(extra)
    return ctx


def log_action(request: Request, action: str, org_id: Optional[int] = None) -> None:
    email = request.session.get("user")
    org_id = org_id if org_id is not None else request.session.get("org_id")
    with db() as conn:
        conn.execute(
            "INSERT INTO action_logs (organization_id,user_email,action,created_at) VALUES (?,?,?,?)",
            (org_id, email, action, datetime.now().isoformat(timespec="seconds")),
        )


def can_manage_workspace(org) -> bool:
    return bool(org and org["membership_role"] in ("owner", "admin"))


def _importar_movimentos(conn, org_id: int, case_id: int, doc_id: int) -> tuple[int, int]:
    """Extrai a listagem de eventos do PDF recém-indexado."""
    if not case_id:
        return 0, 0
    linhas = conn.execute(
        """SELECT text FROM document_pages
           WHERE organization_id=? AND document_id=? ORDER BY page_number LIMIT 8""",
        (org_id, doc_id)).fetchall()
    texto = "\n".join((r["text"] or "") for r in linhas)
    movimentos = movimentacoes.extrair_eventos_eproc(texto)
    if not movimentos:
        return 0, 0
    novos = movimentacoes.salvar(conn, org_id, case_id, movimentos)
    return novos, sum(1 for m in movimentos if m.intimacao)


def _renomear_documento(conn, org_id: int, case_id: int, doc_id: int,
                        original: str) -> str:
    """Troca o nome de exibição por algo útil. Não move o arquivo em disco:
    stored_path continua o mesmo, então nada quebra."""
    linhas = conn.execute(
        """SELECT text FROM document_pages
           WHERE organization_id=? AND document_id=? ORDER BY page_number LIMIT 3""",
        (org_id, doc_id)).fetchall()
    texto = "\n".join((r["text"] or "") for r in linhas)
    caso = conn.execute("SELECT number FROM cases WHERE id=? AND organization_id=?",
                        (case_id, org_id)).fetchone()
    amigavel = nome_documento.nome_amigavel(
        original, texto, (caso["number"] if caso else "") or "")
    if amigavel and amigavel != original:
        conn.execute(
            "UPDATE case_documents SET original_name=? WHERE id=? AND organization_id=?",
            (amigavel, doc_id, org_id))
        return amigavel
    return ""


def ingest_case_files(request: Request, *, org_id: int, case_id: int, files: Optional[list[UploadFile]]) -> tuple[int, list[str]]:
    """Armazena e indexa PDFs. É usado tanto no cadastro do processo quanto no Copiloto."""
    if not files:
        return 0, []
    max_mb = max(5, int(os.getenv("JARBAS_MAX_UPLOAD_MB", "200")))
    max_bytes = max_mb * 1024 * 1024
    uploaded = 0
    errors: list[str] = []
    notices: list[str] = []
    case_dir = UPLOAD_ROOT / str(org_id) / str(case_id)
    case_dir.mkdir(parents=True, exist_ok=True)
    for incoming in files:
        if not incoming or not incoming.filename:
            continue
        original = incoming.filename.strip() or "processo.pdf"
        # Identificação por CONTEÚDO, não por extensão no fim do nome.
        # Download do eproc entrega nomes com a extensão no meio:
        #   ..._downloa__1_.PDF_numIdSessao_0117...&hash=08c6d57a...
        # São PDFs legítimos. Quem dá a palavra final é o cabeçalho %PDF-
        # conferido por store_uploaded_pdf; aqui só barramos o que é
        # claramente outro tipo, para não gravar lixo grande em disco.
        if not nome_documento.parece_pdf(original, incoming.content_type or ""):
            errors.append(f"{original}: apenas PDF é aceito.")
            continue
        stored_name = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:10]}_{safe_filename(original)}"
        path = case_dir / stored_name
        try:
            size, sha = store_uploaded_pdf(incoming, path, max_bytes)
            # Cota do plano: conferida DEPOIS de saber o tamanho real e ANTES
            # de registrar o documento. Arquivo já gravado é removido se
            # estourar, para não deixar órfão ocupando disco.
            try:
                plan_limits.checar_cota_armazenamento(org_id, size)
            except plan_limits.CotaDeArmazenamentoExcedida as exc:
                path.unlink(missing_ok=True)
                errors.append(f"{original}: {exc}")
                continue
            with db() as conn:
                duplicate = conn.execute(
                    "SELECT id FROM case_documents WHERE organization_id=? AND case_id=? AND sha256=?",
                    (org_id, case_id, sha),
                ).fetchone()
                if duplicate:
                    path.unlink(missing_ok=True)
                    uploaded += 1
                    notices.append(f"{original}: este PDF já estava cadastrado e permanece disponível ao Copiloto (documento #{duplicate['id']}).")
                    continue
                doc_id = conn.insert_id(
                    """INSERT INTO case_documents
                       (organization_id,case_id,original_name,stored_name,stored_path,sha256,mime_type,size_bytes,page_count,text_chars,status,extraction_note,created_at)
                       VALUES (?,?,?,?,?,?,?,?,0,0,'uploaded','',?)""",
                    (org_id, case_id, original, stored_name, str(path), sha, incoming.content_type or "application/pdf", size,
                     datetime.now().isoformat(timespec="seconds")),
                )
                try:
                    index_pdf(conn, org_id=org_id, case_id=case_id, document_id=doc_id, path=path)
                    # Nome de download de tribunal não serve para nada: aparece
                    # na interface e nas citações do Copiloto como 200+ chars de
                    # URL. Com o texto já indexado, extraímos o número CNJ.
                    if nome_documento.precisa_renomear(original):
                        amigavel = _renomear_documento(conn, org_id, case_id, doc_id, original)
                        if amigavel:
                            notices.append(f"Documento renomeado para “{amigavel}”.")
                    # Linha do tempo: o PDF do eproc traz a "Listagem dos Eventos
                    # do Processo" nas primeiras páginas. Extraí-la é de graça —
                    # o texto já está indexado — e é dela que saem as intimações.
                    novos, intimacoes = _importar_movimentos(conn, org_id, case_id, doc_id)
                    if novos:
                        aviso = f"{novos} movimentação(ões) importada(s) dos autos"
                        if intimacoes:
                            aviso += (f" — {intimacoes} possível(eis) intimação(ões). "
                                      "Confira em /prazos se há prazo correndo.")
                        notices.append(aviso + ".")
                except Exception as exc:
                    conn.execute(
                        "UPDATE case_documents SET status='error',extraction_note=? WHERE id=? AND organization_id=?",
                        (f"Falha de extração: {type(exc).__name__}: {str(exc)[:300]}", doc_id, org_id),
                    )
                    errors.append(f"{original}: armazenado, mas a extração de texto falhou.")
            uploaded += 1
            log_action(request, f"Documento indexado no caso #{case_id}: {original}")
        except Exception as exc:
            path.unlink(missing_ok=True)
            errors.append(f"{original}: {str(exc)}")
        finally:
            try:
                incoming.file.close()
            except Exception:
                pass
    if notices:
        request.session["copilot_upload_notices"] = notices[:8]
    return uploaded, errors


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return safe_template_response("login.html", {"request": request, "error": None})


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, email: str = Form(...), password: str = Form(...), csrf: str = Form("", alias="_csrf")):
    if not valid_csrf(request, csrf):
        return csrf_error()
    if login_is_blocked(request):
        return HTMLResponse("Muitas tentativas de acesso. Aguarde alguns minutos e tente novamente.", status_code=429)
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
        valid_password = bool(user and verify_password(password, user["password_hash"]))
        if valid_password and password_hash_needs_upgrade(user["password_hash"]):
            conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(password), user["id"]))
    if not user or not valid_password:
        login_failure(request)
        return safe_template_response(
            "login.html", {"request": request, "error": "Credenciais inválidas."}, status_code=401
        )
    login_success(request)
    # Evita fixação de sessão: descarta qualquer estado anônimo e cria contexto autenticado novo.
    request.session.clear()
    request.session["_csrf"] = secrets.token_urlsafe(32)

    # Senha conferida não é sessão autenticada quando há segundo fator. A
    # sessão fica em estado intermediário, sem a chave "user", que é a única
    # coisa que current_user() aceita — nenhuma rota do sistema abre daqui.
    if two_factor.ativo(user["id"]):
        request.session["_2fa_user_id"] = user["id"]
        request.session["_2fa_email"] = user["email"]
        request.session["_2fa_expira"] = (
            datetime.now() + timedelta(minutes=two_factor.PRAZO_SEGUNDA_ETAPA_MIN)
        ).isoformat(timespec="seconds")
        return RedirectResponse("/login/2fa", status_code=303)

    abrir_sessao(request, user)
    return RedirectResponse("/", status_code=303)


def abrir_sessao(request: Request, user) -> None:
    """Promove a sessão a autenticada. Ponto único de entrada no sistema."""
    request.session.pop("_2fa_user_id", None)
    request.session.pop("_2fa_email", None)
    request.session.pop("_2fa_expira", None)
    request.session["user"] = user["email"]
    request.session["_csrf"] = secrets.token_urlsafe(32)
    workspaces = available_workspaces(user["id"])
    if workspaces:
        request.session["org_id"] = workspaces[0]["id"]
    log_action(request, "Login efetuado", request.session.get("org_id"))


def usuario_em_segunda_etapa(request: Request):
    """Usuário que já provou a senha e ainda deve o código. None se expirou."""
    user_id = request.session.get("_2fa_user_id")
    if not user_id:
        return None
    expira = request.session.get("_2fa_expira", "")
    try:
        if datetime.now() > datetime.fromisoformat(expira):
            request.session.clear()
            return None
    except (TypeError, ValueError):
        request.session.clear()
        return None
    with db() as conn:
        return conn.execute(
            "SELECT id,name,email,role,is_superadmin,password_hash FROM users WHERE id=?", (user_id,)
        ).fetchone()


@app.get("/login/2fa", response_class=HTMLResponse)
def login_2fa_page(request: Request, error: str = ""):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    user = usuario_em_segunda_etapa(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return safe_template_response(
        "login_2fa.html",
        {"request": request, "email": user["email"], "error": error or None},
    )


@app.post("/login/2fa", response_class=HTMLResponse)
def login_2fa(request: Request, codigo: str = Form(""), csrf: str = Form("", alias="_csrf")):
    if not valid_csrf(request, csrf):
        return csrf_error()
    if login_is_blocked(request):
        return HTMLResponse("Muitas tentativas de acesso. Aguarde alguns minutos e tente novamente.", status_code=429)
    user = usuario_em_segunda_etapa(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not two_factor.verificar(user["id"], codigo):
        login_failure(request)
        return safe_template_response(
            "login_2fa.html",
            {"request": request, "email": user["email"],
             "error": "Código inválido, expirado ou já utilizado."},
            status_code=401,
        )
    login_success(request)
    abrir_sessao(request, user)
    return RedirectResponse("/", status_code=303)


@app.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    if os.getenv("JARBAS_PUBLIC_SIGNUP", "0") != "1":
        return HTMLResponse("Cadastros públicos temporariamente fechados. Entre em contato com o administrador da plataforma.", status_code=403)
    with db() as conn:
        plans = conn.execute("SELECT * FROM plans WHERE active=1 ORDER BY monthly_price").fetchall()
    return safe_template_response("signup.html", {"request": request, "plans": plans, "error": None})


@app.post("/signup", response_class=HTMLResponse)
def signup(
    request: Request,
    office_name: str = Form(...),
    document: str = Form(""),
    owner_name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(""),
    password: str = Form(...),
    plan_code: str = Form("pro"),
    csrf: str = Form("", alias="_csrf"),
):
    if not valid_csrf(request, csrf):
        return csrf_error()
    if os.getenv("JARBAS_PUBLIC_SIGNUP", "0") != "1":
        return HTMLResponse("Cadastros públicos temporariamente fechados.", status_code=403)
    email = email.strip().lower()
    if len(password) < 10:
        with db() as conn:
            plans = conn.execute("SELECT * FROM plans WHERE active=1 ORDER BY monthly_price").fetchall()
        return safe_template_response(
            "signup.html",
            {"request": request, "plans": plans, "error": "Use uma senha com pelo menos 10 caracteres."},
            status_code=400,
        )
    now = datetime.now().isoformat(timespec="seconds")
    with db() as conn:
        if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            plans = conn.execute("SELECT * FROM plans WHERE active=1 ORDER BY monthly_price").fetchall()
            return safe_template_response(
                "signup.html",
                {"request": request, "plans": plans, "error": "Este e-mail já possui cadastro."},
                status_code=400,
            )
        plan = conn.execute("SELECT * FROM plans WHERE code=? AND active=1", (plan_code,)).fetchone()
        if not plan:
            plan = conn.execute("SELECT * FROM plans WHERE code='pro'").fetchone()
        slug = unique_slug(conn, office_name)
        org_id = conn.insert_id(
            """INSERT INTO organizations
               (name,slug,document,email,phone,brand_name,logo_path,status,created_at)
               VALUES (?,?,?,?,?,?,NULL,'active',?)""",
            (office_name.strip(), slug, document.strip(), email, phone.strip(), office_name.strip(), now),
        )
        user_id = conn.insert_id(
            "INSERT INTO users (name,email,password_hash,role,created_at,is_superadmin) VALUES (?,?,?,?,?,0)",
            (owner_name.strip(), email, hash_password(password), "admin", now),
        )
        conn.execute(
            "INSERT INTO memberships (user_id,organization_id,role,is_active,created_at) VALUES (?,?,'owner',1,?)",
            (user_id, org_id, now),
        )
        conn.execute(
            """INSERT INTO subscriptions (organization_id,plan_id,status,started_at,current_period_end)
               VALUES (?,?,'trial',?,?)""",
            (org_id, plan["id"], now, (date.today() + timedelta(days=14)).isoformat()),
        )
    request.session["user"] = email
    request.session["org_id"] = org_id
    log_action(request, f"Workspace criado em teste: {office_name.strip()}", org_id)
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.post("/switch-workspace")
def switch_workspace(request: Request, organization_id: int = Form(...), csrf: str = Form("", alias="_csrf")):
    if not valid_csrf(request, csrf):
        return csrf_error()
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    allowed = {w["id"] for w in available_workspaces(user["id"])}
    if organization_id in allowed:
        request.session["org_id"] = organization_id
    return RedirectResponse("/", status_code=303)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    today = date.today()
    soon = today + timedelta(days=7)
    with db() as conn:
        ensure_financial_setup(conn, org["id"])
        snap = financial_snapshot(conn, org["id"])
        stats = {
            "clients": conn.execute("SELECT COUNT(*) c FROM clients WHERE organization_id=?", (org["id"],)).fetchone()["c"],
            "cases": conn.execute("SELECT COUNT(*) c FROM cases WHERE organization_id=? AND status!='Encerrado'", (org["id"],)).fetchone()["c"],
            "deadlines": conn.execute(
                "SELECT COUNT(*) c FROM deadlines WHERE organization_id=? AND status='Pendente' AND due_date<=?",
                (org["id"], soon.isoformat()),
            ).fetchone()["c"],
            "receivables": snap["receivable_open"],
            "payables": snap["payable_open"],
            "cash": snap["cash_balance"],
            "leads": conn.execute("SELECT COUNT(*) c FROM leads WHERE organization_id=? AND status NOT IN ('Contratado','Perdido')", (org["id"],)).fetchone()["c"],
            "tasks": conn.execute("SELECT COUNT(*) c FROM activities WHERE organization_id=? AND status='Pendente'", (org["id"],)).fetchone()["c"],
        }
        deadlines = conn.execute(
            """SELECT d.*,c.title case_title,c.number case_number
               FROM deadlines d LEFT JOIN cases c ON c.id=d.case_id AND c.organization_id=d.organization_id
               WHERE d.organization_id=? AND d.status='Pendente'
               ORDER BY d.due_date ASC LIMIT 8""",
            (org["id"],),
        ).fetchall()
        critical = conn.execute(
            """SELECT * FROM cases WHERE organization_id=? AND status!='Encerrado'
               ORDER BY CASE risk WHEN 'Crítico' THEN 1 WHEN 'Alto' THEN 2 WHEN 'Médio' THEN 3 ELSE 4 END,id DESC LIMIT 6""",
            (org["id"],),
        ).fetchall()
        activities = conn.execute(
            """SELECT a.*,c.title case_title FROM activities a LEFT JOIN cases c ON c.id=a.case_id AND c.organization_id=a.organization_id
               WHERE a.organization_id=? AND a.status='Pendente' ORDER BY COALESCE(a.due_at,a.start_at,'9999-12-31') LIMIT 6""",
            (org["id"],),
        ).fetchall()
        recent_docs = conn.execute(
            """SELECT d.id,d.case_id,d.original_name,d.page_count,d.status,c.title case_title FROM case_documents d
               JOIN cases c ON c.id=d.case_id AND c.organization_id=d.organization_id WHERE d.organization_id=? ORDER BY d.id DESC LIMIT 6""",
            (org["id"],),
        ).fetchall()
    return safe_template_response(
        "dashboard.html",
        common_context(request, user, org, stats=stats, deadlines=deadlines, critical=critical, activities=activities,
                       recent_docs=recent_docs, finance_snapshot=snap, today=today.isoformat()),
    )


@app.get("/clients", response_class=HTMLResponse)
def clients(request: Request):
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    with db() as conn:
        rows = conn.execute("SELECT * FROM clients WHERE organization_id=? ORDER BY name", (org["id"],)).fetchall()
    return safe_template_response("clients.html", common_context(request, user, org, clients=rows))


@app.post("/clients")
def create_client(
    request: Request,
    name: str = Form(...),
    person_type: str = Form("Pessoa Física"),
    document: str = Form(""),
    rg: str = Form(""),
    nationality: str = Form(""),
    marital_status: str = Form(""),
    profession: str = Form(""),
    birth_date: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    address: str = Form(""),
    address_number: str = Form(""),
    complement: str = Form(""),
    neighborhood: str = Form(""),
    city: str = Form(""),
    state: str = Form(""),
    zip_code: str = Form(""),
    responsible_name: str = Form(""),
    notes: str = Form(""),
    csrf: str = Form("", alias="_csrf"),
):
    if not valid_csrf(request, csrf):
        return csrf_error()
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    now=datetime.now().isoformat(timespec="seconds")
    with db() as conn:
        client_id=conn.insert_id(
            """INSERT INTO clients
               (organization_id,name,person_type,document,rg,nationality,marital_status,profession,birth_date,phone,email,address,address_number,complement,neighborhood,city,state,zip_code,responsible_name,notes,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (org["id"],name.strip(),person_type,document.strip(),rg.strip(),nationality.strip(),marital_status.strip(),profession.strip(),birth_date or None,phone.strip(),email.strip(),address.strip(),address_number.strip(),complement.strip(),neighborhood.strip(),city.strip(),state.strip().upper(),zip_code.strip(),responsible_name.strip(),notes.strip(),now,now),
        )
    log_action(request, f"Cliente cadastrado: {name.strip()}")
    return RedirectResponse(f"/clients/{client_id}?created=1", status_code=303)


@app.get("/clients/{client_id}", response_class=HTMLResponse)
def client_detail(request: Request, client_id: int):
    user,org=require_workspace(request)
    if isinstance(org,RedirectResponse): return org
    with db() as conn:
        client=conn.execute("SELECT * FROM clients WHERE id=? AND organization_id=?",(client_id,org["id"])).fetchone()
        if not client: return RedirectResponse("/clients",status_code=303)
        cases_rows=conn.execute("SELECT * FROM cases WHERE client_id=? AND organization_id=? ORDER BY id DESC",(client_id,org["id"])).fetchall()
        all_fin=financial_transaction_rows(conn,org["id"],5000); financial=[e for e in all_fin if e["client_id"]==client_id]
        activities=conn.execute("SELECT * FROM activities WHERE client_id=? AND organization_id=? ORDER BY COALESCE(due_at,start_at,'9999-12-31') LIMIT 50",(client_id,org["id"])).fetchall()
        contracts=conn.execute("""SELECT fc.*,
             COALESCE((SELECT SUM(t.original_amount) FROM financial_transactions t WHERE t.contract_id=fc.id AND t.organization_id=fc.organization_id),0) scheduled_amount,
             COALESCE((SELECT SUM(p.amount) FROM financial_payments p JOIN financial_transactions t ON t.id=p.transaction_id AND t.organization_id=p.organization_id WHERE t.contract_id=fc.id AND p.organization_id=fc.organization_id),0) paid_amount
             FROM fee_contracts fc WHERE fc.client_id=? AND fc.organization_id=? ORDER BY fc.id DESC""",(client_id,org["id"])).fetchall()
        generated_docs=conn.execute("SELECT * FROM generated_documents WHERE client_id=? AND organization_id=? ORDER BY id DESC LIMIT 50",(client_id,org["id"])).fetchall()
        categories=conn.execute("SELECT * FROM financial_categories WHERE organization_id=? AND active=1 ORDER BY COALESCE(code,'999'),name",(org["id"],)).fetchall()
        cost_centers=conn.execute("SELECT * FROM cost_centers WHERE organization_id=? AND active=1 ORDER BY code",(org["id"],)).fetchall()
        payments=conn.execute("""SELECT p.*,t.description,t.direction,a.name account_name FROM financial_payments p
            JOIN financial_transactions t ON t.id=p.transaction_id AND t.organization_id=p.organization_id
            LEFT JOIN financial_accounts a ON a.id=p.account_id AND a.organization_id=p.organization_id
            WHERE p.organization_id=? AND t.client_id=? ORDER BY p.paid_at DESC,p.id DESC LIMIT 50""",(org["id"],client_id)).fetchall()
    outstanding=sum(max(money(e["original_amount"])-money(e["paid_amount"]),0) for e in financial if e["direction"]=="receivable" and e["status"] not in ("Pago","Cancelado"))
    total_contracted=sum(money(c["contract_value"]) for c in contracts if c["status"]!="Cancelado")
    received=sum(money(e["paid_amount"]) for e in financial if e["direction"]=="receivable")
    expenses_paid=sum(money(e["paid_amount"]) for e in financial if e["direction"]=="payable")
    overdue=sum(max(money(e["original_amount"])-money(e["paid_amount"]),0) for e in financial if e["direction"]=="receivable" and e["status"]=="Vencido")
    fin_summary={"contracted":round(total_contracted,2),"received":round(received,2),"outstanding":round(outstanding,2),"overdue":round(overdue,2),"expenses_paid":round(expenses_paid,2),"margin":round(received-expenses_paid,2)}
    return safe_template_response("client_detail.html",common_context(request,user,org,client=client,cases=cases_rows,financial=financial,activities=activities,contracts=contracts,outstanding=outstanding,generated_docs=generated_docs,categories=categories,cost_centers=cost_centers,payments=payments,fin_summary=fin_summary))


@app.get("/cases", response_class=HTMLResponse)
def cases(request: Request):
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    with db() as conn:
        rows = conn.execute(
            """SELECT c.*,cl.name client_name FROM cases c
               LEFT JOIN clients cl ON cl.id=c.client_id AND cl.organization_id=c.organization_id
               WHERE c.organization_id=? ORDER BY c.id DESC""",
            (org["id"],),
        ).fetchall()
        clients_rows = conn.execute("SELECT id,name FROM clients WHERE organization_id=? ORDER BY name", (org["id"],)).fetchall()
    return safe_template_response("cases.html", common_context(request, user, org, cases=rows, clients=clients_rows))


@app.post("/cases")
def create_case(
    request: Request,
    title: str = Form(...),
    area: str = Form(...),
    client_id: str = Form(""),
    number: str = Form(""),
    court: str = Form(""),
    case_class: str = Form(""),
    subject: str = Form(""),
    claim_value: float = Form(0),
    risk: str = Form("Médio"),
    facts: str = Form(""),
    evidence: str = Form(""),
    strategy: str = Form(""),
    next_step: str = Form(""),
    files: Optional[list[UploadFile]] = File(None),
    csrf: str = Form("", alias="_csrf"),
):
    if not valid_csrf(request, csrf):
        return csrf_error()
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    subscription = current_subscription(org["id"])
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM cases WHERE organization_id=?", (org["id"],)).fetchone()["c"]
        if subscription and count >= subscription["case_limit"]:
            return RedirectResponse("/billing?limit=cases", status_code=303)
        cid = int(client_id) if client_id else None
        if cid and not conn.execute("SELECT 1 FROM clients WHERE id=? AND organization_id=?", (cid, org["id"])).fetchone():
            cid = None
        case_id = conn.insert_id(
            """INSERT INTO cases
               (organization_id,client_id,number,title,area,court,case_class,subject,claim_value,status,risk,facts,evidence,strategy,next_step,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,'Ativo',?,?,?,?,?,?,?)""",
            (org["id"], cid, number.strip(), title.strip(), area.strip(), court.strip(), case_class.strip(), subject.strip(), max(money(claim_value),0), risk, facts.strip(), evidence.strip(), strategy.strip(), next_step.strip(), datetime.now().isoformat(timespec="seconds"), datetime.now().isoformat(timespec="seconds")),
        )
        if cid:
            cli=conn.execute("SELECT * FROM clients WHERE id=? AND organization_id=?",(cid,org["id"])).fetchone()
            if cli:
                conn.execute("INSERT INTO case_parties (organization_id,case_id,client_id,name,role,document,person_type,phone,email,is_client,created_at) VALUES (?,?,?,?,?,?,?,?,?,1,?)",
                             (org["id"],case_id,cid,cli["name"],"Cliente principal",cli["document"],cli["person_type"],cli["phone"],cli["email"],datetime.now().isoformat(timespec="seconds")))
    uploaded, errors = ingest_case_files(request, org_id=org["id"], case_id=case_id, files=files)
    if errors:
        request.session["copilot_upload_errors"] = errors[:8]
    log_action(request, f"Processo/caso cadastrado: {title.strip()}")
    return RedirectResponse(f"/cases/{case_id}?created=1&upload={uploaded}&errors={len(errors)}", status_code=303)


@app.get("/cases/{case_id}", response_class=HTMLResponse)
def case_detail(request: Request, case_id: int):
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    with db() as conn:
        item = conn.execute(
            """SELECT c.*,cl.name client_name,cl.phone client_phone,cl.email client_email
               FROM cases c LEFT JOIN clients cl ON cl.id=c.client_id AND cl.organization_id=c.organization_id
               WHERE c.id=? AND c.organization_id=?""",
            (case_id, org["id"]),
        ).fetchone()
        if not item:
            return RedirectResponse("/cases", status_code=303)
        deadlines = conn.execute(
            "SELECT * FROM deadlines WHERE case_id=? AND organization_id=? ORDER BY due_date", (case_id, org["id"])
        ).fetchall()
        all_finance = financial_transaction_rows(conn, org["id"], 1000)
        entries = [e for e in all_finance if e["case_id"] == case_id][:50]
        documents = conn.execute(
            "SELECT * FROM case_documents WHERE case_id=? AND organization_id=? ORDER BY id DESC", (case_id, org["id"])
        ).fetchall()
        recent_drafts = conn.execute(
            "SELECT id,title,draft_type,created_at FROM drafts WHERE case_id=? AND organization_id=? ORDER BY id DESC LIMIT 5",
            (case_id, org["id"]),
        ).fetchall()
        activities = conn.execute(
            "SELECT * FROM activities WHERE case_id=? AND organization_id=? ORDER BY COALESCE(due_at,start_at,'9999-12-31') LIMIT 20",
            (case_id, org["id"]),
        ).fetchall()
        contracts = conn.execute(
            "SELECT * FROM fee_contracts WHERE case_id=? AND organization_id=? ORDER BY id DESC LIMIT 10", (case_id, org["id"])
        ).fetchall()
        parties = conn.execute(
            "SELECT * FROM case_parties WHERE case_id=? AND organization_id=? ORDER BY is_client DESC,id", (case_id, org["id"])
        ).fetchall()
        clients_rows = conn.execute("SELECT id,name FROM clients WHERE organization_id=? ORDER BY name", (org["id"],)).fetchall()
        stats = case_stats(conn, org_id=org["id"], case_id=case_id)
    return safe_template_response(
        "case_detail.html", common_context(request, user, org, case=item, deadlines=deadlines, entries=entries,
                                            documents=documents, document_stats=stats, recent_drafts=recent_drafts,
                                            activities=activities, contracts=contracts, parties=parties, clients=clients_rows,
                                            ai_connected=ai_available(), ai_model_name=ai_model(),
                                            created=request.query_params.get("created"), upload_ok=request.query_params.get("upload") or request.query_params.get("ok"),
                                            upload_error=request.query_params.get("errors") or request.query_params.get("error"),
                                            upload_error_details=request.session.pop("copilot_upload_errors", []),
                                            upload_notices=request.session.pop("copilot_upload_notices", []))
    )



@app.post("/cases/{case_id}/update")
def update_case(
    request: Request, case_id: int, title: str = Form(...), area: str = Form(...), client_id: str = Form(""),
    number: str = Form(""), court: str = Form(""), case_class: str = Form(""), subject: str = Form(""),
    claim_value: float = Form(0), status: str = Form("Ativo"), risk: str = Form("Médio"),
    facts: str = Form(""), evidence: str = Form(""), strategy: str = Form(""), next_step: str = Form(""),
    csrf: str = Form("", alias="_csrf"),
):
    if not valid_csrf(request, csrf): return csrf_error()
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse): return org
    allowed_status={"Ativo","Suspenso","Arquivado","Encerrado"}; allowed_risk={"Baixo","Médio","Alto","Crítico"}
    cid=int(client_id) if client_id else None
    with db() as conn:
        if not conn.execute("SELECT 1 FROM cases WHERE id=? AND organization_id=?", (case_id,org["id"])).fetchone():
            return RedirectResponse("/cases",status_code=303)
        if cid and not conn.execute("SELECT 1 FROM clients WHERE id=? AND organization_id=?", (cid,org["id"])).fetchone(): cid=None
        conn.execute(
            """UPDATE cases SET client_id=?,number=?,title=?,area=?,court=?,case_class=?,subject=?,claim_value=?,status=?,risk=?,facts=?,evidence=?,strategy=?,next_step=?,updated_at=?
               WHERE id=? AND organization_id=?""",
            (cid,number.strip(),title.strip(),area.strip(),court.strip(),case_class.strip(),subject.strip(),max(money(claim_value),0),status if status in allowed_status else "Ativo",risk if risk in allowed_risk else "Médio",facts.strip(),evidence.strip(),strategy.strip(),next_step.strip(),datetime.now().isoformat(timespec="seconds"),case_id,org["id"]),
        )
        conn.execute("UPDATE case_parties SET is_client=0,client_id=NULL WHERE case_id=? AND organization_id=? AND is_client=1", (case_id,org["id"]))
        if cid:
            client=conn.execute("SELECT * FROM clients WHERE id=? AND organization_id=?",(cid,org["id"])).fetchone()
            existing=conn.execute("SELECT id FROM case_parties WHERE case_id=? AND organization_id=? AND client_id=?",(case_id,org["id"],cid)).fetchone()
            if existing:
                conn.execute("UPDATE case_parties SET is_client=1 WHERE id=?",(existing["id"],))
            elif client:
                conn.execute("INSERT INTO case_parties (organization_id,case_id,client_id,name,role,document,person_type,phone,email,is_client,created_at) VALUES (?,?,?,?,?,?,?,?,?,1,?)",(org["id"],case_id,cid,client["name"],"Cliente",client["document"],client["person_type"],client["phone"],client["email"],datetime.now().isoformat(timespec="seconds")))
    log_action(request,f"Processo #{case_id} atualizado")
    return RedirectResponse(f"/cases/{case_id}?saved=1",status_code=303)


@app.post("/deadlines/{deadline_id}/complete")
def complete_deadline(request: Request, deadline_id: int, csrf: str = Form("", alias="_csrf")):
    if not valid_csrf(request,csrf): return csrf_error()
    user,org=require_workspace(request)
    if isinstance(org,RedirectResponse): return org
    with db() as conn:
        row=conn.execute("SELECT case_id FROM deadlines WHERE id=? AND organization_id=?",(deadline_id,org["id"])).fetchone()
        if not row: return RedirectResponse("/agenda",status_code=303)
        conn.execute("UPDATE deadlines SET status='Concluído' WHERE id=? AND organization_id=?",(deadline_id,org["id"]))
    log_action(request,f"Prazo #{deadline_id} concluído")
    return RedirectResponse(f"/cases/{row['case_id']}",status_code=303)


def _case_or_redirect(org_id: int, case_id: int):
    with db() as conn:
        item = conn.execute(
            """SELECT c.*,cl.name client_name FROM cases c
               LEFT JOIN clients cl ON cl.id=c.client_id AND cl.organization_id=c.organization_id
               WHERE c.id=? AND c.organization_id=?""",
            (case_id, org_id),
        ).fetchone()
    return item


def _consume_copilot_credits(org_id: int, user_id: int, kind: str, cost: int, description: str) -> bool:
    """Debita créditos. Leitura e escrita na MESMA transação.

    A versão anterior lia o consumo numa conexão e inseria noutra: duas abas
    simultâneas passavam as duas pela checagem e o teto era furado. Agora o
    SELECT e o INSERT compartilham a transação, e o BEGIN IMMEDIATE do SQLite
    serializa gravadores concorrentes.
    """
    mes = datetime.now().strftime("%Y-%m")
    agora = datetime.now().isoformat(timespec="seconds")
    with db() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except Exception:
            pass  # PostgreSQL já abre transação sozinho
        limite = conn.execute(
            """SELECT p.monthly_credits mc FROM subscriptions s
               JOIN plans p ON p.id=s.plan_id
               WHERE s.organization_id=? AND s.status='active'
               ORDER BY s.id DESC LIMIT 1""", (org_id,)).fetchone()
        if limite and limite["mc"]:
            usado = conn.execute(
                """SELECT COALESCE(SUM(credits),0) t FROM usage_ledger
                   WHERE organization_id=? AND substr(created_at,1,7)=?""",
                (org_id, mes)).fetchone()["t"]
            if int(usado or 0) + cost > int(limite["mc"]):
                return False
        conn.execute(
            "INSERT INTO usage_ledger (organization_id,user_id,kind,credits,description,created_at) VALUES (?,?,?,?,?,?)",
            (org_id, user_id, kind, cost, description, agora),
        )
    return True


@app.get("/cases/{case_id}/copilot", response_class=HTMLResponse)
def copilot_home(request: Request, case_id: int):
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    item = _case_or_redirect(org["id"], case_id)
    if not item:
        return RedirectResponse("/cases", status_code=303)
    with db() as conn:
        documents = conn.execute(
            "SELECT * FROM case_documents WHERE case_id=? AND organization_id=? ORDER BY id DESC", (case_id, org["id"])
        ).fetchall()
        stats = case_stats(conn, org_id=org["id"], case_id=case_id)
        recent_runs = conn.execute(
            "SELECT * FROM copilot_runs WHERE case_id=? AND organization_id=? ORDER BY id DESC LIMIT 8",
            (case_id, org["id"]),
        ).fetchall()
        drafts = conn.execute(
            "SELECT id,title,draft_type,created_at FROM drafts WHERE case_id=? AND organization_id=? ORDER BY id DESC LIMIT 8",
            (case_id, org["id"]),
        ).fetchall()
        tl = timeline(conn, org_id=org["id"], case_id=case_id, limit=25)
        recent = recent_context(conn, org_id=org["id"], case_id=case_id, limit=18)
    measure = infer_measure(recent)
    return safe_template_response(
        "copilot.html",
        common_context(request, user, org, case=item, documents=documents, document_stats=stats,
                       recent_runs=recent_runs, drafts=drafts, timeline=tl, measure=measure,
                       ai_connected=ai_available(), ai_model_name=ai_model(), ocr_status=ocr_capability(), answer=None, hard_truth_result=None,
                       deep_result=None, upload_error=request.query_params.get("error"), upload_ok=request.query_params.get("ok"),
                       upload_error_details=request.session.pop("copilot_upload_errors", []),
                       upload_notices=request.session.pop("copilot_upload_notices", []),
                       reindex_all=request.query_params.get("reindex_all")),
    )


@app.post("/cases/{case_id}/documents")
def upload_case_documents(request: Request, case_id: int, files: list[UploadFile] = File(...), csrf: str = Form("", alias="_csrf")):
    if not valid_csrf(request, csrf):
        return csrf_error()
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    item = _case_or_redirect(org["id"], case_id)
    if not item:
        return RedirectResponse("/cases", status_code=303)
    uploaded, errors = ingest_case_files(request, org_id=org["id"], case_id=case_id, files=files)
    if errors:
        request.session["copilot_upload_errors"] = errors[:8]
    destination = request.query_params.get("return", "copilot")
    target = f"/cases/{case_id}" if destination == "case" else f"/cases/{case_id}/copilot"
    return RedirectResponse(f"{target}?ok={uploaded}&error={len(errors)}", status_code=303)


@app.get("/cases/{case_id}/documents/{document_id}", response_class=HTMLResponse)
def document_detail(request: Request, case_id: int, document_id: int):
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    item = _case_or_redirect(org["id"], case_id)
    if not item:
        return RedirectResponse("/cases", status_code=303)
    with db() as conn:
        doc = conn.execute(
            "SELECT * FROM case_documents WHERE id=? AND case_id=? AND organization_id=?",
            (document_id, case_id, org["id"]),
        ).fetchone()
        if not doc:
            return RedirectResponse(f"/cases/{case_id}/copilot", status_code=303)
        pages = conn.execute(
            "SELECT page_number,text,label FROM document_pages WHERE document_id=? AND organization_id=? ORDER BY page_number LIMIT 250",
            (document_id, org["id"]),
        ).fetchall()
        pdf_path = heal_case_document_path(conn, doc, org_id=org["id"], case_id=case_id)
    return safe_template_response("document_detail.html", common_context(request, user, org, case=item, document=doc, pages=pages, pdf_available=bool(pdf_path)))


@app.post("/cases/{case_id}/documents/reindex-all")
def reindex_all_case_documents(request: Request, case_id: int, csrf: str = Form("", alias="_csrf")):
    if not valid_csrf(request, csrf):
        return csrf_error()
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    item = _case_or_redirect(org["id"], case_id)
    if not item:
        return RedirectResponse("/cases", status_code=303)
    ok = missing = failed = 0
    with db() as conn:
        docs = conn.execute(
            "SELECT * FROM case_documents WHERE case_id=? AND organization_id=? ORDER BY id",
            (case_id, org["id"]),
        ).fetchall()
        for doc in docs:
            try:
                path = heal_case_document_path(conn, doc, org_id=org["id"], case_id=case_id)
                if not path or not path.is_file():
                    missing += 1
                    continue
                index_pdf(conn, org_id=org["id"], case_id=case_id, document_id=doc["id"], path=path)
                ok += 1
            except Exception as exc:
                failed += 1
                conn.execute(
                    "UPDATE case_documents SET status='error',extraction_note=? WHERE id=? AND organization_id=?",
                    (f"Falha ao reprocessar: {type(exc).__name__}: {str(exc)[:300]}", doc["id"], org["id"]),
                )
    log_action(request, f"Reprocessamento integral de PDFs no caso #{case_id}: ok={ok}, ausentes={missing}, falhas={failed}")
    return RedirectResponse(f"/cases/{case_id}/copilot?reindex_all={ok}-{missing}-{failed}", status_code=303)


@app.post("/cases/{case_id}/documents/{document_id}/reindex")
def reindex_document(request: Request, case_id: int, document_id: int, csrf: str = Form("", alias="_csrf")):
    if not valid_csrf(request, csrf):
        return csrf_error()
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    item = _case_or_redirect(org["id"], case_id)
    if not item:
        return RedirectResponse("/cases", status_code=303)
    try:
        with db() as conn:
            doc = conn.execute(
                "SELECT * FROM case_documents WHERE id=? AND case_id=? AND organization_id=?",
                (document_id, case_id, org["id"]),
            ).fetchone()
            if not doc:
                return RedirectResponse(f"/cases/{case_id}/copilot?reindex=missing", status_code=303)
            path = heal_case_document_path(conn, doc, org_id=org["id"], case_id=case_id)
            if not path or not path.is_file():
                return RedirectResponse(f"/cases/{case_id}/documents/{document_id}?reindex=file_missing", status_code=303)
            result = index_pdf(conn, org_id=org["id"], case_id=case_id, document_id=document_id, path=path)
        log_action(request, f"Documento reindexado com o pipeline de PDF no caso #{case_id}: documento #{document_id} — {result.get('status')}")
        return RedirectResponse(f"/cases/{case_id}/documents/{document_id}?reindex=ok", status_code=303)
    except Exception as exc:
        with db() as conn:
            conn.execute("UPDATE case_documents SET status='error',extraction_note=? WHERE id=? AND organization_id=?",
                         (f"Falha ao reprocessar: {type(exc).__name__}: {str(exc)[:300]}", document_id, org["id"]))
        return RedirectResponse(f"/cases/{case_id}/documents/{document_id}?reindex=error", status_code=303)


@app.post("/cases/{case_id}/documents/{document_id}/delete")
def delete_document(request: Request, case_id: int, document_id: int, csrf: str = Form("", alias="_csrf")):
    if not valid_csrf(request, csrf):
        return csrf_error()
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    with db() as conn:
        doc = conn.execute(
            "SELECT * FROM case_documents WHERE id=? AND case_id=? AND organization_id=?",
            (document_id, case_id, org["id"]),
        ).fetchone()
        if doc:
            actual = heal_case_document_path(conn, doc, org_id=org["id"], case_id=case_id)
            remove_document_files(str(actual) if actual else doc["stored_path"])
            conn.execute("DELETE FROM case_documents WHERE id=? AND organization_id=?", (document_id, org["id"]))
    log_action(request, f"Documento removido do caso #{case_id}: documento #{document_id}")
    return RedirectResponse(f"/cases/{case_id}/copilot", status_code=303)


@app.post("/cases/{case_id}/copilot/ask", response_class=HTMLResponse)
def copilot_ask(request: Request, case_id: int, question: str = Form(...), csrf: str = Form("", alias="_csrf")):
    if not valid_csrf(request, csrf):
        return csrf_error()
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    item = _case_or_redirect(org["id"], case_id)
    if not item:
        return RedirectResponse("/cases", status_code=303)
    question = question.strip()[:3000]
    if not question:
        return RedirectResponse(f"/cases/{case_id}/copilot", status_code=303)
    cost = 5 if ai_available() else 1
    if not _consume_copilot_credits(org["id"], user["id"], "copilot_question", cost, f"Pergunta aos autos #{case_id}"):
        return RedirectResponse("/billing?limit=credits", status_code=303)
    with db() as conn:
        excerpts = search_case(conn, org_id=org["id"], case_id=case_id, query=question, limit=12)
        pdf_rows = conn.execute(
            "SELECT id,stored_path,stored_name,status,page_count,text_chars FROM case_documents WHERE organization_id=? AND case_id=? ORDER BY id DESC LIMIT 6",
            (org["id"], case_id),
        ).fetchall()
    pdf_paths = resolve_case_pdf_rows(pdf_rows, org_id=org["id"], case_id=case_id, limit=6)
    pdf_review_required = case_documents_require_pdf_review(pdf_rows)
    case_meta = {"title": item["title"], "number": item["number"], "area": item["area"], "court": item["court"], "client": item["client_name"]}
    try:
        answer = answer_question(question, excerpts, case_meta, pdf_paths=pdf_paths, pdf_review_required=pdf_review_required)
    except Exception as exc:
        answer = f"Falha ao consultar o módulo de IA: {str(exc)}\n\nOs trechos documentais continuam disponíveis abaixo para revisão manual."
    with db() as conn:
        conn.execute(
            """INSERT INTO copilot_runs (organization_id,case_id,user_id,kind,question,answer,sources_json,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (org["id"], case_id, user["id"], "question", question, answer,
             json.dumps([{"document": e["document"], "page": e["page"], "citation": e["citation"]} for e in excerpts], ensure_ascii=False),
             datetime.now().isoformat(timespec="seconds")),
        )
        documents = conn.execute("SELECT * FROM case_documents WHERE case_id=? AND organization_id=? ORDER BY id DESC", (case_id, org["id"])).fetchall()
        stats = case_stats(conn, org_id=org["id"], case_id=case_id)
        tl = timeline(conn, org_id=org["id"], case_id=case_id, limit=25)
        recent = recent_context(conn, org_id=org["id"], case_id=case_id, limit=18)
        recent_runs = conn.execute("SELECT * FROM copilot_runs WHERE case_id=? AND organization_id=? ORDER BY id DESC LIMIT 8", (case_id, org["id"])).fetchall()
        drafts = conn.execute("SELECT id,title,draft_type,created_at FROM drafts WHERE case_id=? AND organization_id=? ORDER BY id DESC LIMIT 8", (case_id, org["id"])).fetchall()
    log_action(request, f"Pergunta aos autos no caso #{case_id}")
    return safe_template_response("copilot.html", common_context(request, user, org, case=item, documents=documents,
        document_stats=stats, timeline=tl, measure=infer_measure(recent), recent_runs=recent_runs, drafts=drafts,
        ai_connected=ai_available(), ai_model_name=ai_model(), ocr_status=ocr_capability(), answer={"question": question, "text": answer, "sources": excerpts},
        hard_truth_result=None, deep_result=None, upload_error=None, upload_ok=None))


@app.post("/cases/{case_id}/copilot/analyze", response_class=HTMLResponse)
def copilot_analyze(request: Request, case_id: int, csrf: str = Form("", alias="_csrf")):
    if not valid_csrf(request, csrf):
        return csrf_error()
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    item = _case_or_redirect(org["id"], case_id)
    if not item:
        return RedirectResponse("/cases", status_code=303)
    cost = 15 if ai_available() else 2
    if not _consume_copilot_credits(org["id"], user["id"], "copilot_analysis", cost, f"Análise aprofundada dos autos #{case_id}"):
        return RedirectResponse("/billing?limit=credits", status_code=303)
    with db() as conn:
        recent = recent_context(conn, org_id=org["id"], case_id=case_id, limit=28)
        # Amostra adicional orientada a risco/prova, sem assumir que são fatos do caso.
        risk = search_case(conn, org_id=org["id"], case_id=case_id,
                           query="prescrição intempestividade ausência prova não comprovou improcedência indeferimento contradição ônus prova", limit=12)
        merged = []
        seen = set()
        for e in recent + risk:
            if e["chunk_id"] not in seen:
                seen.add(e["chunk_id"]); merged.append(e)
        stats = case_stats(conn, org_id=org["id"], case_id=case_id)
        documents = conn.execute("SELECT * FROM case_documents WHERE case_id=? AND organization_id=? ORDER BY id DESC", (case_id, org["id"])).fetchall()
        pdf_paths = resolve_case_pdf_rows(documents, org_id=org["id"], case_id=case_id, limit=6)
        tl = timeline(conn, org_id=org["id"], case_id=case_id, limit=25)
        recent_runs = conn.execute("SELECT * FROM copilot_runs WHERE case_id=? AND organization_id=? ORDER BY id DESC LIMIT 8", (case_id, org["id"])).fetchall()
        drafts = conn.execute("SELECT id,title,draft_type,created_at FROM drafts WHERE case_id=? AND organization_id=? ORDER BY id DESC LIMIT 8", (case_id, org["id"])).fetchall()
    meta = {"title": item["title"], "number": item["number"], "area": item["area"], "court": item["court"], "client": item["client_name"], "risk_cadastrado": item["risk"]}
    matrix = {"facts": item["facts"], "evidence": item["evidence"], "strategy": item["strategy"], "next_step": item["next_step"]}
    try:
        result = deep_analysis(merged, meta, matrix, pdf_paths=pdf_paths)
    except Exception as exc:
        result = f"Falha no módulo de IA: {str(exc)}\nA indexação documental permaneceu íntegra."
    with db() as conn:
        conn.execute("""INSERT INTO copilot_runs (organization_id,case_id,user_id,kind,question,answer,sources_json,created_at)
                        VALUES (?,?,?,?,?,?,?,?)""",
                     (org["id"], case_id, user["id"], "deep_analysis", "Análise estratégica dos autos", result,
                      json.dumps([{"document": e["document"], "page": e["page"], "citation": e["citation"]} for e in merged[:16]], ensure_ascii=False),
                      datetime.now().isoformat(timespec="seconds")))
    log_action(request, f"Análise aprofundada do Copiloto no caso #{case_id}")
    return safe_template_response("copilot.html", common_context(request, user, org, case=item, documents=documents,
        document_stats=stats, timeline=tl, measure=infer_measure(recent), recent_runs=recent_runs, drafts=drafts,
        ai_connected=ai_available(), ai_model_name=ai_model(), ocr_status=ocr_capability(), answer=None, hard_truth_result=None,
        deep_result=result, upload_error=None, upload_ok=None))


@app.post("/cases/{case_id}/copilot/hard-truth", response_class=HTMLResponse)
def copilot_hard_truth(request: Request, case_id: int, csrf: str = Form("", alias="_csrf")):
    if not valid_csrf(request, csrf):
        return csrf_error()
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    item = _case_or_redirect(org["id"], case_id)
    if not item:
        return RedirectResponse("/cases", status_code=303)
    if not _consume_copilot_credits(org["id"], user["id"], "hard_truth", 2, f"Hard Truth #{case_id}"):
        return RedirectResponse("/billing?limit=credits", status_code=303)
    with db() as conn:
        risk = search_case(conn, org_id=org["id"], case_id=case_id,
                           query="prescrição prescrito intempestivo improcedente indeferido ausência prova falta prova não comprovou não demonstrou ilegitimidade incompetência preclusão revelia confissão contradição ônus prova sucumbência", limit=30)
        recent = recent_context(conn, org_id=org["id"], case_id=case_id, limit=18)
        merged=[]; seen=set()
        for e in risk+recent:
            if e["chunk_id"] not in seen: seen.add(e["chunk_id"]); merged.append(e)
        documents = conn.execute("SELECT * FROM case_documents WHERE case_id=? AND organization_id=? ORDER BY id DESC", (case_id, org["id"])).fetchall()
        pdf_paths = resolve_case_pdf_rows(documents, org_id=org["id"], case_id=case_id, limit=6)
        meta = {"title": item["title"], "number": item["number"], "area": item["area"], "court": item["court"], "client": item["client_name"]}
        result = hard_truth_enhanced(merged, meta, pdf_paths=pdf_paths)
        stats = case_stats(conn, org_id=org["id"], case_id=case_id)
        tl = timeline(conn, org_id=org["id"], case_id=case_id, limit=25)
        recent_runs = conn.execute("SELECT * FROM copilot_runs WHERE case_id=? AND organization_id=? ORDER BY id DESC LIMIT 8", (case_id, org["id"])).fetchall()
        drafts = conn.execute("SELECT id,title,draft_type,created_at FROM drafts WHERE case_id=? AND organization_id=? ORDER BY id DESC LIMIT 8", (case_id, org["id"])).fetchall()
    log_action(request, f"Hard Truth executado no caso #{case_id}")
    return safe_template_response("copilot.html", common_context(request, user, org, case=item, documents=documents,
        document_stats=stats, timeline=tl, measure=infer_measure(recent), recent_runs=recent_runs, drafts=drafts,
        ai_connected=ai_available(), ai_model_name=ai_model(), ocr_status=ocr_capability(), answer=None, hard_truth_result=result,
        deep_result=None, upload_error=None, upload_ok=None))


@app.post("/cases/{case_id}/copilot/draft")
def copilot_draft(request: Request, case_id: int, draft_type: str = Form(...), objective: str = Form(""), csrf: str = Form("", alias="_csrf")):
    if not valid_csrf(request, csrf):
        return csrf_error()
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    item = _case_or_redirect(org["id"], case_id)
    if not item:
        return RedirectResponse("/cases", status_code=303)
    cost = 20 if ai_available() else 3
    if not _consume_copilot_credits(org["id"], user["id"], "draft", cost, f"Minuta {draft_type} #{case_id}"):
        return RedirectResponse("/billing?limit=credits", status_code=303)
    query = f"{draft_type} {objective} intimação decisão sentença prova pedido manifestação".strip()
    with db() as conn:
        relevant = search_case(conn, org_id=org["id"], case_id=case_id, query=query, limit=22)
        recent = recent_context(conn, org_id=org["id"], case_id=case_id, limit=16)
        pdf_rows = conn.execute(
            "SELECT id,stored_path,stored_name FROM case_documents WHERE organization_id=? AND case_id=? ORDER BY id DESC LIMIT 6",
            (org["id"], case_id),
        ).fetchall()
        org_full = conn.execute("SELECT * FROM organizations WHERE id=?", (org["id"],)).fetchone()
        client = conn.execute("SELECT * FROM clients WHERE id=? AND organization_id=?", (item["client_id"], org["id"])).fetchone() if item["client_id"] else None
        parties = conn.execute("SELECT * FROM case_parties WHERE case_id=? AND organization_id=? ORDER BY is_client DESC,id", (case_id, org["id"])).fetchall()
    pdf_paths = resolve_case_pdf_rows(pdf_rows, org_id=org["id"], case_id=case_id, limit=6)
    merged=[]; seen=set()
    for e in relevant+recent:
        if e["chunk_id"] not in seen: seen.add(e["chunk_id"]); merged.append(e)
    identity = build_identity_context(case=item, client=client, parties=parties, organization=org_full)
    meta = {
        "title": item["title"], "number": item["number"], "area": item["area"], "court": item["court"],
        "case_class": item["case_class"], "subject": item["subject"], "client": item["client_name"],
        "partes": [{"nome": p.get("name"), "papel": p.get("role"), "cliente": bool(p.get("is_client")), "qualificacao": p.get("qualification")} for p in identity.get("parties", [])],
        "advogado": identity.get("office", {}).get("lawyer_name"), "oab": identity.get("office", {}).get("oab_number"),
        "escritorio": identity.get("office", {}).get("name"),
    }
    matrix = {"facts": item["facts"], "evidence": item["evidence"], "strategy": item["strategy"], "next_step": item["next_step"]}
    try:
        content = draft_petition(draft_type=draft_type, objective=objective.strip(), excerpts=merged, case_meta=meta, matrix=matrix, pdf_paths=pdf_paths, identity=identity)
    except Exception as exc:
        content = complete_local_draft(draft_type=draft_type, objective=objective.strip(), excerpts=merged, identity=identity, matrix=matrix)
        content += f"\n\n[ALERTA: A IA EXTERNA NÃO CONCLUIU ESTA GERAÇÃO: {type(exc).__name__}. A peça acima foi montada com dados estruturados e deve ser revisada.]"
    title = f"{draft_type} — {item['number'] or item['title']}"
    stamp=datetime.now().strftime("%Y%m%d-%H%M%S")
    pdf_name=safe_filename(f"{draft_type}_{item['number'] or item['title']}_{stamp}.pdf")
    draft_dir=GENERATED_ROOT/"drafts"/str(org["id"])/str(case_id); draft_dir.mkdir(parents=True,exist_ok=True)
    pdf_target=draft_dir/pdf_name
    pdf_error=""
    try:
        create_draft_pdf(content=content,draft_title=title,case=item,organization=org_full,parties=parties,target=pdf_target)
    except Exception as exc:
        pdf_error=f"{type(exc).__name__}: {str(exc)[:300]}"
        pdf_target=None
    with db() as conn:
        draft_id = conn.insert_id(
            """INSERT INTO drafts (organization_id,case_id,user_id,title,draft_type,content,sources_json,created_at,pdf_path,pdf_file_name)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (org["id"], case_id, user["id"], title, draft_type, content,
             json.dumps([{"document": e["document"], "page": e["page"], "citation": e["citation"]} for e in merged], ensure_ascii=False),
             datetime.now().isoformat(timespec="seconds"), str(pdf_target) if pdf_target else None, pdf_name if pdf_target else None),
        )
        if pdf_target and item["client_id"]:
            try:
                conn.execute(
                    "INSERT INTO generated_documents (organization_id,client_id,case_id,user_id,document_type,title,stored_path,file_name,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (org["id"],item["client_id"],case_id,user["id"],"minuta_pdf",title,str(pdf_target),pdf_name,datetime.now().isoformat(timespec="seconds")),
                )
            except Exception:
                pass
    log_action(request, f"Minuta completa criada pelo Copiloto no caso #{case_id}: {draft_type}" + (f"; PDF falhou: {pdf_error}" if pdf_error else "; PDF institucional gerado"))
    return RedirectResponse(f"/cases/{case_id}/drafts/{draft_id}" + ("?pdf_error=1" if pdf_error else ""), status_code=303)


@app.get("/cases/{case_id}/drafts/{draft_id}", response_class=HTMLResponse)
def draft_detail(request: Request, case_id: int, draft_id: int):
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    item = _case_or_redirect(org["id"], case_id)
    if not item:
        return RedirectResponse("/cases", status_code=303)
    with db() as conn:
        draft = conn.execute("SELECT * FROM drafts WHERE id=? AND case_id=? AND organization_id=?", (draft_id, case_id, org["id"])).fetchone()
    if not draft:
        return RedirectResponse(f"/cases/{case_id}/copilot", status_code=303)
    try:
        sources = json.loads(draft["sources_json"] or "[]")
    except Exception:
        sources = []
    pdf_path=safe_data_file(draft["pdf_path"],GENERATED_ROOT) if draft["pdf_path"] else None
    return safe_template_response("draft_detail.html", common_context(request, user, org, case=item, draft=draft, sources=sources, pdf_available=bool(pdf_path and pdf_path.is_file()), pdf_error=request.query_params.get("pdf_error")))


@app.get("/cases/{case_id}/drafts/{draft_id}/view-pdf")
def draft_pdf_view(request: Request, case_id: int, draft_id: int):
    user,org=require_workspace(request)
    if isinstance(org,RedirectResponse): return org
    with db() as conn:
        draft=conn.execute("SELECT * FROM drafts WHERE id=? AND case_id=? AND organization_id=?",(draft_id,case_id,org["id"])).fetchone()
    if not draft or not draft["pdf_path"]: return PlainTextResponse("PDF da minuta não encontrado.",status_code=404)
    path=safe_data_file(draft["pdf_path"],GENERATED_ROOT)
    if not path or not path.is_file(): return PlainTextResponse("Arquivo PDF da minuta não localizado.",status_code=404)
    response=FileResponse(path,media_type="application/pdf")
    response.headers["Content-Disposition"]=f'inline; filename="{draft["pdf_file_name"] or "minuta.pdf"}"'
    response.headers["X-Frame-Options"]="SAMEORIGIN"
    response.headers["Content-Security-Policy"]="default-src 'self'; frame-ancestors 'self'; object-src 'self'"
    response.headers["Cache-Control"]="private, no-store"
    return response


@app.get("/cases/{case_id}/drafts/{draft_id}/download")
def draft_download(request: Request, case_id: int, draft_id: int):
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    with db() as conn:
        draft = conn.execute("SELECT * FROM drafts WHERE id=? AND case_id=? AND organization_id=?", (draft_id, case_id, org["id"])).fetchone()
    if not draft:
        return PlainTextResponse("Minuta não encontrada.", status_code=404)
    path=safe_data_file(draft["pdf_path"],GENERATED_ROOT) if draft["pdf_path"] else None
    if path and path.is_file():
        return FileResponse(path,media_type="application/pdf",filename=draft["pdf_file_name"] or safe_filename(draft["title"]+".pdf"))
    return PlainTextResponse("PDF institucional não localizado. Gere novamente a minuta nesta versão do JARBAS.",status_code=404)


@app.get("/cases/{case_id}/drafts/{draft_id}/download-text")
def draft_download_text(request: Request, case_id: int, draft_id: int):
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse): return org
    with db() as conn:
        draft = conn.execute("SELECT * FROM drafts WHERE id=? AND case_id=? AND organization_id=?", (draft_id, case_id, org["id"])).fetchone()
    if not draft: return PlainTextResponse("Minuta não encontrada.", status_code=404)
    filename=safe_filename(draft["title"]+".pdf").replace(".pdf",".txt")
    return PlainTextResponse(draft["content"],headers={"Content-Disposition":f'attachment; filename="{filename}"'})


@app.post("/cases/{case_id}/deadlines")
def add_deadline(request: Request, case_id: int, title: str = Form(...), due_date: str = Form(...), priority: str = Form("Normal"), csrf: str = Form("", alias="_csrf")):
    if not valid_csrf(request, csrf):
        return csrf_error()
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    with db() as conn:
        if not conn.execute("SELECT 1 FROM cases WHERE id=? AND organization_id=?", (case_id, org["id"])).fetchone():
            return RedirectResponse("/cases", status_code=303)
        conn.execute(
            """INSERT INTO deadlines (organization_id,case_id,title,due_date,priority,status,created_at)
               VALUES (?,?,?,?,?,'Pendente',?)""",
            (org["id"], case_id, title.strip(), due_date, priority, datetime.now().isoformat(timespec="seconds")),
        )
    log_action(request, f"Prazo criado no caso #{case_id}: {title.strip()}")
    return RedirectResponse(f"/cases/{case_id}", status_code=303)


@app.post("/cases/{case_id}/finance")
def add_finance(
    request: Request,
    case_id: int,
    description: str = Form(...),
    amount: float = Form(...),
    due_date: str = Form(""),
    kind: str = Form("Honorário"),
    csrf: str = Form("", alias="_csrf"),
):
    """Compatibilidade com o formulário antigo: cria uma conta a receber/pagar no novo ERP."""
    if not valid_csrf(request, csrf):
        return csrf_error()
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    with db() as conn:
        ensure_financial_setup(conn, org["id"])
        case_row = conn.execute("SELECT client_id FROM cases WHERE id=? AND organization_id=?", (case_id, org["id"])).fetchone()
        if not case_row:
            return RedirectResponse("/cases", status_code=303)
        if money(amount) <= 0:
            return RedirectResponse(f"/cases/{case_id}?finance_error=amount", status_code=303)
        direction = "payable" if kind.lower() in ("despesa", "custa", "custas") else "receivable"
        category_name = "Custas processuais" if direction == "payable" else (
            "Honorários de êxito" if "êxito" in kind.lower() else "Consultas" if "consulta" in kind.lower() else "Honorários contratuais"
        )
        category = conn.execute("SELECT id FROM financial_categories WHERE organization_id=? AND name=? LIMIT 1", (org["id"], category_name)).fetchone()
        conn.execute(
            """INSERT INTO financial_transactions
               (organization_id,client_id,case_id,contract_id,category_id,direction,description,original_amount,due_date,competence_date,status,payment_method,notes,legacy_finance_id,created_at)
               VALUES (?,?,?,NULL,?,?,?,?,?,?,'Pendente','','',NULL,?)""",
            (org["id"], case_row["client_id"], case_id, category["id"] if category else None, direction,
             description.strip(), money(amount), due_date or None, date.today().isoformat(), datetime.now().isoformat(timespec="seconds")),
        )
    log_action(request, f"Lançamento financeiro no caso #{case_id}: {description.strip()}")
    return RedirectResponse(f"/cases/{case_id}", status_code=303)


@app.get("/cases/{case_id}/analysis", response_class=HTMLResponse)
def analyze_case(request: Request, case_id: int):
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    subscription = current_subscription(org["id"])
    used = month_usage(org["id"])
    cost = 10
    if subscription and used + cost > subscription["monthly_credits"]:
        return RedirectResponse("/billing?limit=credits", status_code=303)

    with db() as conn:
        item = conn.execute("SELECT * FROM cases WHERE id=? AND organization_id=?", (case_id, org["id"])).fetchone()
        deadlines = conn.execute(
            "SELECT * FROM deadlines WHERE case_id=? AND organization_id=? AND status='Pendente' ORDER BY due_date",
            (case_id, org["id"]),
        ).fetchall()
    if not item:
        return RedirectResponse("/cases", status_code=303)

    missing = []
    for field, label in (("facts", "fatos"), ("evidence", "provas"), ("strategy", "estratégia"), ("next_step", "próximo passo")):
        if not (item[field] or "").strip():
            missing.append(label)

    alerts = []
    today = date.today()
    for d in deadlines:
        try:
            due = date.fromisoformat(d["due_date"])
            days = (due - today).days
            if days < 0:
                alerts.append(f"Prazo vencido: {d['title']} ({d['due_date']}).")
            elif days <= 3:
                alerts.append(f"Prazo crítico em {days} dia(s): {d['title']} ({d['due_date']}).")
        except ValueError:
            pass

    analysis = {
        "risk": item["risk"],
        "strength": "Há registro de provas no caso." if (item["evidence"] or "").strip() else "Não há prova cadastrada suficiente para apontar ponto forte.",
        "weakness": "Campos essenciais incompletos: " + ", ".join(missing) + "." if missing else "A matriz mínima está preenchida; ainda exige revisão jurídica humana.",
        "recommendation": item["next_step"] or "Definir próximo passo processual após conferência documental e normativa.",
        "alerts": alerts,
        "disclaimer": "Análise interna de apoio. Não cria fatos, não substitui leitura integral dos autos, pesquisa oficial de legislação/jurisprudência nem revisão do advogado responsável.",
        "cost": cost,
    }
    with db() as conn:
        conn.execute(
            "INSERT INTO usage_ledger (organization_id,user_id,kind,credits,description,created_at) VALUES (?,?,?,?,?,?)",
            (org["id"], user["id"], "analysis", cost, f"Análise do caso #{case_id}", datetime.now().isoformat(timespec="seconds")),
        )
    log_action(request, f"Análise JARBAS executada no caso #{case_id} ({cost} créditos)")
    return safe_template_response("analysis.html", common_context(request, user, org, case=item, analysis=analysis))


@app.get("/finance", response_class=HTMLResponse)
def finance(request: Request):
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    with db() as conn:
        ensure_financial_setup(conn, org["id"])
        migrate_legacy_finance(conn, org["id"])
        snapshot = financial_snapshot(conn, org["id"])
        categories = conn.execute("SELECT * FROM financial_categories WHERE organization_id=? AND active=1 ORDER BY COALESCE(code,'999'),name", (org["id"],)).fetchall()
        cost_centers = conn.execute("SELECT * FROM cost_centers WHERE organization_id=? AND active=1 ORDER BY code", (org["id"],)).fetchall()
        clients_rows = conn.execute("SELECT id,name FROM clients WHERE organization_id=? ORDER BY name", (org["id"],)).fetchall()
        cases_rows = conn.execute("SELECT id,title,number,client_id FROM cases WHERE organization_id=? ORDER BY id DESC", (org["id"],)).fetchall()
        contracts = conn.execute(
            """SELECT fc.*,cl.name client_name,c.title case_title,
               COALESCE((SELECT SUM(t.original_amount) FROM financial_transactions t WHERE t.contract_id=fc.id AND t.organization_id=fc.organization_id),0) scheduled_amount,
               COALESCE((SELECT SUM(p.amount) FROM financial_payments p JOIN financial_transactions t ON t.id=p.transaction_id AND t.organization_id=p.organization_id WHERE t.contract_id=fc.id AND p.organization_id=fc.organization_id),0) paid_amount
               FROM fee_contracts fc
               LEFT JOIN clients cl ON cl.id=fc.client_id AND cl.organization_id=fc.organization_id
               LEFT JOIN cases c ON c.id=fc.case_id AND c.organization_id=fc.organization_id
               WHERE fc.organization_id=? ORDER BY fc.id DESC LIMIT 100""",
            (org["id"],),
        ).fetchall()
        payments = conn.execute(
            """SELECT p.*,t.description,t.direction,a.name account_name
               FROM financial_payments p JOIN financial_transactions t ON t.id=p.transaction_id AND t.organization_id=p.organization_id
               LEFT JOIN financial_accounts a ON a.id=p.account_id AND a.organization_id=p.organization_id
               WHERE p.organization_id=? ORDER BY p.paid_at DESC,p.id DESC LIMIT 30""",
            (org["id"],),
        ).fetchall()
    # Fluxo de caixa realizado dos últimos 6 meses, calculado sem SQL específico de banco.
    today = date.today()
    month_keys=[]
    for offset in range(5,-1,-1):
        absolute=today.year*12+(today.month-1)-offset
        y,m0=divmod(absolute,12); m=m0+1
        month_keys.append(f"{y:04d}-{m:02d}")
    cashflow={k:{"income":0.0,"expense":0.0} for k in month_keys}
    for pmt in payments:
        key=(pmt["paid_at"] or "")[:7]
        if key in cashflow:
            if pmt["direction"]=="receivable": cashflow[key]["income"]+=money(pmt["amount"])
            else: cashflow[key]["expense"]+=money(pmt["amount"])
    cashflow_rows=[{"month":k,"income":round(v["income"],2),"expense":round(v["expense"],2),"result":round(v["income"]-v["expense"],2)} for k,v in cashflow.items()]
    return safe_template_response(
        "finance.html",
        common_context(request, user, org, snapshot=snapshot, entries=snapshot["rows"], accounts=snapshot["accounts"],
                       categories=categories, cost_centers=cost_centers, clients=clients_rows, cases=cases_rows, contracts=contracts,
                       payments=payments, cashflow=cashflow_rows),
    )


@app.post("/finance/transactions")
def create_financial_transaction(
    request: Request,
    direction: str = Form(...), description: str = Form(...), amount: float = Form(...), due_date: str = Form(""),
    competence_date: str = Form(""), client_id: str = Form(""), case_id: str = Form(""), category_id: str = Form(""),
    cost_center_id: str = Form(""), counterparty: str = Form(""), document_number: str = Form(""),
    notes: str = Form(""), csrf: str = Form("", alias="_csrf"),
):
    if not valid_csrf(request, csrf): return csrf_error()
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse): return org
    direction = direction if direction in ("receivable","payable") else "receivable"
    if money(amount) <= 0: return RedirectResponse("/finance?error=amount", status_code=303)
    with db() as conn:
        ensure_financial_setup(conn, org["id"])
        cid=int(client_id) if client_id else None; caseid=int(case_id) if case_id else None; catid=int(category_id) if category_id else None; ccid=int(cost_center_id) if cost_center_id else None
        if cid and not conn.execute("SELECT 1 FROM clients WHERE id=? AND organization_id=?",(cid,org["id"])).fetchone(): cid=None
        if caseid:
            c=conn.execute("SELECT client_id FROM cases WHERE id=? AND organization_id=?",(caseid,org["id"])).fetchone()
            if not c: caseid=None
            elif c["client_id"]: cid=c["client_id"]
        if catid and not conn.execute("SELECT 1 FROM financial_categories WHERE id=? AND organization_id=?",(catid,org["id"])).fetchone(): catid=None
        if ccid and not conn.execute("SELECT 1 FROM cost_centers WHERE id=? AND organization_id=?",(ccid,org["id"])).fetchone(): ccid=None
        conn.execute(
            """INSERT INTO financial_transactions
               (organization_id,client_id,case_id,contract_id,category_id,direction,description,original_amount,due_date,competence_date,status,payment_method,notes,legacy_finance_id,created_at,cost_center_id,counterparty,document_number)
               VALUES (?,?,?,NULL,?,?,?,?,?,?,'Pendente','',?,NULL,?,?,?,?)""",
            (org["id"],cid,caseid,catid,direction,description.strip(),money(amount),due_date or None,competence_date or date.today().isoformat(),notes.strip(),datetime.now().isoformat(timespec="seconds"),ccid,counterparty.strip(),document_number.strip()),
        )
    log_action(request, f"Financeiro: novo {'recebível' if direction=='receivable' else 'pagamento'} — {description.strip()}")
    return RedirectResponse("/finance?created=1", status_code=303)


@app.post("/finance/transactions/{transaction_id}/payments")
def register_financial_payment(
    request: Request, transaction_id: int, amount: float = Form(...), paid_at: str = Form(...), account_id: str = Form(""),
    payment_method: str = Form("PIX"), notes: str = Form(""), csrf: str = Form("", alias="_csrf"),
):
    if not valid_csrf(request, csrf): return csrf_error()
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse): return org
    with db() as conn:
        tx=conn.execute(
            """SELECT t.*,COALESCE((SELECT SUM(p.amount) FROM financial_payments p WHERE p.transaction_id=t.id AND p.organization_id=t.organization_id),0) paid_amount
               FROM financial_transactions t WHERE t.id=? AND t.organization_id=?""",(transaction_id,org["id"])).fetchone()
        if not tx: return RedirectResponse("/finance", status_code=303)
        remaining=max(money(tx["original_amount"])-money(tx["paid_amount"]),0)
        pay=money(amount)
        if pay<=0 or pay>remaining+0.01: return RedirectResponse("/finance?error=payment",status_code=303)
        aid=int(account_id) if account_id else None
        if aid and not conn.execute("SELECT 1 FROM financial_accounts WHERE id=? AND organization_id=? AND active=1",(aid,org["id"])).fetchone(): aid=None
        conn.execute(
            "INSERT INTO financial_payments (organization_id,transaction_id,account_id,amount,paid_at,payment_method,notes,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (org["id"],transaction_id,aid,pay,paid_at,payment_method.strip(),notes.strip(),datetime.now().isoformat(timespec="seconds")),
        )
        refresh_financial_statuses(conn,org["id"])
    log_action(request,f"Financeiro: baixa de R$ {pay:.2f} no lançamento #{transaction_id}")
    return RedirectResponse("/finance?paid=1",status_code=303)


@app.post("/finance/transactions/{transaction_id}/cancel")
def cancel_financial_transaction(request: Request, transaction_id: int, csrf: str = Form("", alias="_csrf")):
    if not valid_csrf(request, csrf): return csrf_error()
    user,org=require_workspace(request)
    if isinstance(org,RedirectResponse): return org
    with db() as conn:
        paid=conn.execute("SELECT COALESCE(SUM(amount),0) s FROM financial_payments WHERE transaction_id=? AND organization_id=?",(transaction_id,org["id"])).fetchone()["s"]
        if money(paid)==0:
            conn.execute("UPDATE financial_transactions SET status='Cancelado' WHERE id=? AND organization_id=?",(transaction_id,org["id"]))
    log_action(request,f"Financeiro: lançamento #{transaction_id} cancelado")
    return RedirectResponse("/finance",status_code=303)


@app.post("/finance/accounts")
def create_financial_account(request: Request, name: str = Form(...), account_type: str = Form("Banco"), bank_name: str = Form(""), opening_balance: float = Form(0), csrf: str = Form("", alias="_csrf")):
    if not valid_csrf(request, csrf): return csrf_error()
    user,org=require_workspace(request)
    if isinstance(org,RedirectResponse): return org
    with db() as conn:
        conn.execute("INSERT INTO financial_accounts (organization_id,name,account_type,bank_name,opening_balance,active,created_at) VALUES (?,?,?,?,?,1,?)",
                     (org["id"],name.strip(),account_type.strip(),bank_name.strip(),money(opening_balance),datetime.now().isoformat(timespec="seconds")))
    log_action(request,f"Financeiro: conta criada — {name.strip()}")
    return RedirectResponse("/finance#contas",status_code=303)


@app.post("/finance/categories")
def create_financial_category(request: Request, name: str = Form(...), code: str = Form(""), direction: str = Form("both"), csrf: str = Form("", alias="_csrf")):
    if not valid_csrf(request, csrf): return csrf_error()
    user,org=require_workspace(request)
    if isinstance(org,RedirectResponse): return org
    direction=direction if direction in ("income","expense","both") else "both"
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO financial_categories (organization_id,name,direction,active,created_at,code,parent_id) VALUES (?,?,?,1,?,?,NULL)",
                     (org["id"],name.strip(),direction,datetime.now().isoformat(timespec="seconds"),code.strip()))
    return RedirectResponse("/finance#categorias",status_code=303)


@app.post("/finance/contracts")
def create_fee_contract(
    request: Request, title: str = Form(...), client_id: str = Form(""), case_id: str = Form(""), contract_value: float = Form(0),
    success_percent: float = Form(0), entry_amount: float = Form(0), installment_count: int = Form(0), first_due_date: str = Form(""),
    notes: str = Form(""), csrf: str = Form("", alias="_csrf"),
):
    if not valid_csrf(request, csrf): return csrf_error()
    user,org=require_workspace(request)
    if isinstance(org,RedirectResponse): return org
    contract_value=max(money(contract_value),0); success_percent=min(max(money(success_percent),0),100); entry_amount=min(max(money(entry_amount),0),contract_value); installment_count=min(max(int(installment_count or 0),0),120)
    with db() as conn:
        ensure_financial_setup(conn,org["id"])
        cid=int(client_id) if client_id else None; caseid=int(case_id) if case_id else None
        if caseid:
            c=conn.execute("SELECT client_id FROM cases WHERE id=? AND organization_id=?",(caseid,org["id"])).fetchone()
            if not c: caseid=None
            elif c["client_id"]: cid=c["client_id"]
        if cid and not conn.execute("SELECT 1 FROM clients WHERE id=? AND organization_id=?",(cid,org["id"])).fetchone(): cid=None
        contract_id=conn.insert_id(
            """INSERT INTO fee_contracts (organization_id,client_id,case_id,title,contract_value,success_percent,entry_amount,installment_count,first_due_date,status,notes,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,'Ativo',?,?)""",
            (org["id"],cid,caseid,title.strip(),contract_value,success_percent,entry_amount,installment_count,first_due_date or None,notes.strip(),datetime.now().isoformat(timespec="seconds")),
        )
        cat=conn.execute("SELECT id FROM financial_categories WHERE organization_id=? AND name='Honorários contratuais' LIMIT 1",(org["id"],)).fetchone()
        now=datetime.now().isoformat(timespec="seconds")
        if entry_amount>0:
            conn.execute(
                """INSERT INTO financial_transactions (organization_id,client_id,case_id,contract_id,category_id,direction,description,original_amount,due_date,competence_date,status,payment_method,notes,legacy_finance_id,created_at)
                   VALUES (?,?,?,?,?,'receivable',?,?,?,?,'Pendente','','Entrada contratual',NULL,?)""",
                (org["id"],cid,caseid,contract_id,cat["id"] if cat else None,f"{title.strip()} — Entrada",entry_amount,date.today().isoformat(),date.today().isoformat(),now),
            )
        balance=max(contract_value-entry_amount,0)
        if balance>0 and installment_count>0:
            first=first_due_date or date.today().isoformat()
            base=round(balance/installment_count,2); allocated=0.0
            for i in range(installment_count):
                value=base if i<installment_count-1 else round(balance-allocated,2); allocated+=value
                due=add_months_iso(first,i)
                conn.execute(
                    """INSERT INTO financial_transactions (organization_id,client_id,case_id,contract_id,category_id,direction,description,original_amount,due_date,competence_date,status,payment_method,notes,legacy_finance_id,created_at)
                       VALUES (?,?,?,?,?,'receivable',?,?,?,?,'Pendente','','Parcela de contrato de honorários',NULL,?)""",
                    (org["id"],cid,caseid,contract_id,cat["id"] if cat else None,f"{title.strip()} — Parcela {i+1}/{installment_count}",value,due,due,now),
                )
    log_action(request,f"Financeiro: contrato de honorários criado — {title.strip()}")
    return RedirectResponse("/finance#contratos",status_code=303)


@app.get("/crm", response_class=HTMLResponse)
def crm(request: Request):
    user,org=require_workspace(request)
    if isinstance(org,RedirectResponse): return org
    with db() as conn:
        leads=conn.execute("SELECT * FROM leads WHERE organization_id=? ORDER BY CASE status WHEN 'Novo' THEN 1 WHEN 'Qualificação' THEN 2 WHEN 'Consulta' THEN 3 WHEN 'Proposta' THEN 4 WHEN 'Contratado' THEN 5 ELSE 6 END,COALESCE(next_action_date,'9999-12-31'),id DESC",(org["id"],)).fetchall()
    stages=["Novo","Qualificação","Consulta","Proposta","Contratado","Perdido"]
    pipeline={stage:[r for r in leads if r["status"]==stage] for stage in stages}
    total_est=sum(money(r["estimated_fee"]) for r in leads if r["status"] not in ("Contratado","Perdido"))
    return safe_template_response("crm.html",common_context(request,user,org,leads=leads,pipeline=pipeline,stages=stages,total_estimated=total_est))


@app.post("/crm/leads")
def create_lead(request: Request, name: str = Form(...), contact_name: str = Form(""), phone: str = Form(""), email: str = Form(""), source: str = Form(""), area: str = Form(""), estimated_fee: float = Form(0), next_action_date: str = Form(""), notes: str = Form(""), csrf: str = Form("", alias="_csrf")):
    if not valid_csrf(request,csrf): return csrf_error()
    user,org=require_workspace(request)
    if isinstance(org,RedirectResponse): return org
    now=datetime.now().isoformat(timespec="seconds")
    with db() as conn:
        conn.execute("""INSERT INTO leads (organization_id,name,contact_name,phone,email,source,area,status,estimated_fee,next_action_date,notes,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?,'Novo',?,?,?,?,?)""",(org["id"],name.strip(),contact_name.strip(),phone.strip(),email.strip(),source.strip(),area.strip(),money(estimated_fee),next_action_date or None,notes.strip(),now,now))
    log_action(request,f"CRM: novo lead — {name.strip()}")
    return RedirectResponse("/crm",status_code=303)


@app.post("/crm/leads/{lead_id}/status")
def update_lead_status(request: Request, lead_id: int, status: str = Form(...), next_action_date: str = Form(""), csrf: str = Form("", alias="_csrf")):
    if not valid_csrf(request,csrf): return csrf_error()
    user,org=require_workspace(request)
    if isinstance(org,RedirectResponse): return org
    allowed={"Novo","Qualificação","Consulta","Proposta","Contratado","Perdido"}; status=status if status in allowed else "Novo"
    with db() as conn:
        conn.execute("UPDATE leads SET status=?,next_action_date=?,updated_at=? WHERE id=? AND organization_id=?",(status,next_action_date or None,datetime.now().isoformat(timespec="seconds"),lead_id,org["id"]))
    return RedirectResponse("/crm",status_code=303)


@app.post("/crm/leads/{lead_id}/convert")
def convert_lead(request: Request, lead_id: int, csrf: str = Form("", alias="_csrf")):
    if not valid_csrf(request,csrf): return csrf_error()
    user,org=require_workspace(request)
    if isinstance(org,RedirectResponse): return org
    with db() as conn:
        lead=conn.execute("SELECT * FROM leads WHERE id=? AND organization_id=?",(lead_id,org["id"])).fetchone()
        if not lead: return RedirectResponse("/crm",status_code=303)
        existing=None
        if lead["email"]: existing=conn.execute("SELECT id FROM clients WHERE organization_id=? AND lower(email)=lower(?) LIMIT 1",(org["id"],lead["email"])).fetchone()
        client_id=existing["id"] if existing else conn.insert_id("INSERT INTO clients (organization_id,name,document,phone,email,notes,created_at) VALUES (?,?, '',?,?,?,?)",(org["id"],lead["contact_name"] or lead["name"],lead["phone"],lead["email"],f"Convertido do CRM. {lead['notes'] or ''}",datetime.now().isoformat(timespec="seconds")))
        conn.execute("UPDATE leads SET status='Contratado',updated_at=? WHERE id=? AND organization_id=?",(datetime.now().isoformat(timespec="seconds"),lead_id,org["id"]))
    log_action(request,f"CRM: lead #{lead_id} convertido em cliente #{client_id}")
    return RedirectResponse(f"/clients?converted={client_id}",status_code=303)


@app.get("/agenda", response_class=HTMLResponse)
def agenda(request: Request):
    user,org=require_workspace(request)
    if isinstance(org,RedirectResponse): return org
    with db() as conn:
        activities=conn.execute("""SELECT a.*,c.title case_title,c.number case_number,cl.name client_name,u.name user_name
                                   FROM activities a LEFT JOIN cases c ON c.id=a.case_id AND c.organization_id=a.organization_id
                                   LEFT JOIN clients cl ON cl.id=a.client_id AND cl.organization_id=a.organization_id
                                   LEFT JOIN users u ON u.id=a.user_id WHERE a.organization_id=?
                                   ORDER BY CASE a.status WHEN 'Pendente' THEN 1 ELSE 2 END,COALESCE(a.due_at,a.start_at,'9999-12-31'),a.id DESC LIMIT 250""",(org["id"],)).fetchall()
        deadlines=conn.execute("""SELECT d.*,c.title case_title,c.number case_number FROM deadlines d LEFT JOIN cases c ON c.id=d.case_id AND c.organization_id=d.organization_id
                                  WHERE d.organization_id=? AND d.status='Pendente' ORDER BY d.due_date LIMIT 100""",(org["id"],)).fetchall()
        cases_rows=conn.execute("SELECT id,title,number,client_id FROM cases WHERE organization_id=? AND status!='Encerrado' ORDER BY title",(org["id"],)).fetchall()
        clients_rows=conn.execute("SELECT id,name FROM clients WHERE organization_id=? ORDER BY name",(org["id"],)).fetchall()
        members=conn.execute("SELECT u.id,u.name FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.organization_id=? AND m.is_active=1 ORDER BY u.name",(org["id"],)).fetchall()
    return safe_template_response("agenda.html",common_context(request,user,org,activities=activities,deadlines=deadlines,cases=cases_rows,clients=clients_rows,members=members,today=date.today().isoformat()))


@app.post("/agenda/activities")
def create_activity(request: Request, title: str = Form(...), activity_type: str = Form("Tarefa"), start_at: str = Form(""), due_at: str = Form(""), priority: str = Form("Normal"), case_id: str = Form(""), client_id: str = Form(""), user_id: str = Form(""), notes: str = Form(""), csrf: str = Form("", alias="_csrf")):
    if not valid_csrf(request,csrf): return csrf_error()
    user,org=require_workspace(request)
    if isinstance(org,RedirectResponse): return org
    with db() as conn:
        caseid=int(case_id) if case_id else None; cid=int(client_id) if client_id else None; uid=int(user_id) if user_id else user["id"]
        if caseid:
            c=conn.execute("SELECT client_id FROM cases WHERE id=? AND organization_id=?",(caseid,org["id"])).fetchone()
            if not c: caseid=None
            elif not cid: cid=c["client_id"]
        if cid and not conn.execute("SELECT 1 FROM clients WHERE id=? AND organization_id=?",(cid,org["id"])).fetchone(): cid=None
        if caseid and cid and not conn.execute("SELECT 1 FROM cases WHERE id=? AND organization_id=? AND (client_id=? OR client_id IS NULL)",(caseid,org["id"],cid)).fetchone():
            cid=conn.execute("SELECT client_id FROM cases WHERE id=? AND organization_id=?",(caseid,org["id"])).fetchone()["client_id"]
        if uid and not conn.execute("SELECT 1 FROM memberships WHERE organization_id=? AND user_id=? AND is_active=1",(org["id"],uid)).fetchone(): uid=user["id"]
        conn.execute("""INSERT INTO activities (organization_id,case_id,client_id,user_id,title,activity_type,start_at,due_at,status,priority,notes,completed_at,created_at)
                        VALUES (?,?,?,?,?,?,?,?,'Pendente',?,?,NULL,?)""",(org["id"],caseid,cid,uid,title.strip(),activity_type,start_at or None,due_at or None,priority,notes.strip(),datetime.now().isoformat(timespec="seconds")))
    log_action(request,f"Agenda: atividade criada — {title.strip()}")
    return RedirectResponse("/agenda",status_code=303)


@app.post("/agenda/activities/{activity_id}/complete")
def complete_activity(request: Request, activity_id: int, csrf: str = Form("", alias="_csrf")):
    if not valid_csrf(request,csrf): return csrf_error()
    user,org=require_workspace(request)
    if isinstance(org,RedirectResponse): return org
    with db() as conn:
        conn.execute("UPDATE activities SET status='Concluída',completed_at=? WHERE id=? AND organization_id=?",(datetime.now().isoformat(timespec="seconds"),activity_id,org["id"]))
    return RedirectResponse("/agenda",status_code=303)


@app.get("/documents", response_class=HTMLResponse)
def documents_center(request: Request, q: str = ""):
    user,org=require_workspace(request)
    if isinstance(org,RedirectResponse): return org
    with db() as conn:
        docs=conn.execute("""SELECT d.*,c.title case_title,c.number case_number,cl.name client_name
                             FROM case_documents d JOIN cases c ON c.id=d.case_id AND c.organization_id=d.organization_id
                             LEFT JOIN clients cl ON cl.id=c.client_id AND cl.organization_id=c.organization_id
                             WHERE d.organization_id=? ORDER BY d.id DESC LIMIT 500""",(org["id"],)).fetchall()
    if q.strip():
        qq=q.strip().lower(); docs=[d for d in docs if qq in (d["original_name"] or "").lower() or qq in (d["case_title"] or "").lower() or qq in (d["case_number"] or "").lower() or qq in (d["client_name"] or "").lower()]
    total_bytes=sum(int(d["size_bytes"] or 0) for d in docs); total_pages=sum(int(d["page_count"] or 0) for d in docs)
    return safe_template_response("documents.html",common_context(request,user,org,documents=docs,q=q,total_bytes=total_bytes,total_pages=total_pages))


@app.get("/cases/{case_id}/documents/{document_id}/view")
def view_case_document(request: Request, case_id: int, document_id: int):
    user,org=require_workspace(request)
    if isinstance(org,RedirectResponse): return org
    with db() as conn:
        doc=conn.execute("SELECT * FROM case_documents WHERE id=? AND case_id=? AND organization_id=?",(document_id,case_id,org["id"])).fetchone()
        if not doc: return PlainTextResponse("Documento não encontrado.",status_code=404)
        path=heal_case_document_path(conn,doc,org_id=org["id"],case_id=case_id)
    if not path or not path.is_file():
        return PlainTextResponse("Arquivo físico não localizado. Reenvie o PDF ao processo ou execute a reparação de caminhos.",status_code=404)
    display_name=safe_filename(doc["original_name"] or "processo.pdf")
    response=FileResponse(path,media_type="application/pdf")
    response.headers["Content-Disposition"]=f'inline; filename="{display_name}"'
    response.headers["X-Frame-Options"]="SAMEORIGIN"
    response.headers["Content-Security-Policy"]="default-src 'self'; frame-ancestors 'self'; object-src 'self'"
    response.headers["Cache-Control"]="private, no-store"
    return response


@app.get("/cases/{case_id}/documents/{document_id}/download")
def download_case_document(request: Request, case_id: int, document_id: int):
    user,org=require_workspace(request)
    if isinstance(org,RedirectResponse): return org
    with db() as conn:
        doc=conn.execute("SELECT * FROM case_documents WHERE id=? AND case_id=? AND organization_id=?",(document_id,case_id,org["id"])).fetchone()
        if not doc: return PlainTextResponse("Documento não encontrado.",status_code=404)
        path=heal_case_document_path(conn,doc,org_id=org["id"],case_id=case_id)
    if not path or not path.is_file(): return PlainTextResponse("Arquivo físico não localizado. Reenvie o PDF ao processo ou execute a reparação de caminhos.",status_code=404)
    return FileResponse(path,media_type="application/pdf",filename=doc["original_name"] or "processo.pdf")


@app.get("/timesheet", response_class=HTMLResponse)
def timesheet(request: Request):
    user,org=require_workspace(request)
    if isinstance(org,RedirectResponse): return org
    with db() as conn:
        entries=conn.execute("""SELECT te.*,c.title case_title,c.number case_number,cl.name client_name,u.name user_name
                                FROM time_entries te LEFT JOIN cases c ON c.id=te.case_id AND c.organization_id=te.organization_id
                                LEFT JOIN clients cl ON cl.id=te.client_id AND cl.organization_id=te.organization_id
                                LEFT JOIN users u ON u.id=te.user_id WHERE te.organization_id=? ORDER BY te.work_date DESC,te.id DESC LIMIT 300""",(org["id"],)).fetchall()
        cases_rows=conn.execute("SELECT id,title,number,client_id FROM cases WHERE organization_id=? ORDER BY id DESC",(org["id"],)).fetchall()
        members=conn.execute("SELECT u.id,u.name FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.organization_id=? AND m.is_active=1 ORDER BY u.name",(org["id"],)).fetchall()
    total_minutes=sum(int(e["minutes"] or 0) for e in entries); billable_value=sum((int(e["minutes"] or 0)/60)*money(e["hourly_rate"]) for e in entries if e["billable"])
    return safe_template_response("timesheet.html",common_context(request,user,org,entries=entries,cases=cases_rows,members=members,total_minutes=total_minutes,billable_value=round(billable_value,2)))


@app.post("/timesheet")
def create_time_entry(request: Request, work_date: str = Form(...), minutes: int = Form(...), description: str = Form(...), case_id: str = Form(""), user_id: str = Form(""), billable: str = Form("1"), hourly_rate: float = Form(0), csrf: str = Form("", alias="_csrf")):
    if not valid_csrf(request,csrf): return csrf_error()
    user,org=require_workspace(request)
    if isinstance(org,RedirectResponse): return org
    with db() as conn:
        caseid=int(case_id) if case_id else None; cid=None
        if caseid:
            c=conn.execute("SELECT client_id FROM cases WHERE id=? AND organization_id=?",(caseid,org["id"])).fetchone(); cid=c["client_id"] if c else None
            if not c: caseid=None
        uid=int(user_id) if user_id else user["id"]
        if not conn.execute("SELECT 1 FROM memberships WHERE organization_id=? AND user_id=? AND is_active=1",(org["id"],uid)).fetchone(): uid=user["id"]
        conn.execute("INSERT INTO time_entries (organization_id,case_id,client_id,user_id,work_date,minutes,description,billable,hourly_rate,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (org["id"],caseid,cid,uid,work_date,max(int(minutes),1),description.strip(),1 if str(billable)=="1" else 0,money(hourly_rate),datetime.now().isoformat(timespec="seconds")))
    return RedirectResponse("/timesheet",status_code=303)


@app.get("/reports", response_class=HTMLResponse)
def reports(request: Request):
    user,org=require_workspace(request)
    if isinstance(org,RedirectResponse): return org
    with db() as conn:
        cases_rows=conn.execute("SELECT area,risk,status FROM cases WHERE organization_id=?",(org["id"],)).fetchall()
        clients_rows=conn.execute("SELECT id,name FROM clients WHERE organization_id=?",(org["id"],)).fetchall()
        leads=conn.execute("SELECT status,estimated_fee FROM leads WHERE organization_id=?",(org["id"],)).fetchall()
        time_rows=conn.execute("SELECT minutes,billable,hourly_rate FROM time_entries WHERE organization_id=?",(org["id"],)).fetchall()
        snap=financial_snapshot(conn,org["id"])
    def counts(field, rows):
        out={}
        for r in rows: out[r[field] or "Não informado"]=out.get(r[field] or "Não informado",0)+1
        return sorted(out.items(),key=lambda x:(-x[1],x[0]))
    case_areas=counts("area",cases_rows); risks=counts("risk",cases_rows); lead_stages=counts("status",leads)
    debtors={}
    for tx in snap["rows"]:
        if tx["direction"]=="receivable" and tx["status"]!="Pago" and tx["client_name"]:
            debtors[tx["client_name"]]=debtors.get(tx["client_name"],0)+max(money(tx["original_amount"])-money(tx["paid_amount"]),0)
    debtors=sorted(debtors.items(),key=lambda x:-x[1])[:15]
    total_hours=sum(int(r["minutes"] or 0) for r in time_rows)/60
    billable_hours=sum(int(r["minutes"] or 0) for r in time_rows if r["billable"])/60
    return safe_template_response("reports.html",common_context(request,user,org,case_areas=case_areas,risks=risks,lead_stages=lead_stages,debtors=debtors,finance_snapshot=snap,total_hours=total_hours,billable_hours=billable_hours,clients_count=len(clients_rows)))


@app.get("/audit", response_class=HTMLResponse)
def audit(request: Request):
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    if not can_manage_workspace(org) and not user["is_superadmin"]:
        return RedirectResponse("/", status_code=303)
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM action_logs WHERE organization_id=? ORDER BY id DESC LIMIT 100", (org["id"],)
        ).fetchall()
    return safe_template_response("audit.html", common_context(request, user, org, logs=rows))


@app.get("/team", response_class=HTMLResponse)
def team(request: Request):
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    with db() as conn:
        members = conn.execute(
            """SELECT u.id,u.name,u.email,m.role,m.is_active,m.created_at
               FROM memberships m JOIN users u ON u.id=m.user_id
               WHERE m.organization_id=? ORDER BY CASE m.role WHEN 'owner' THEN 1 WHEN 'admin' THEN 2 ELSE 3 END,u.name""",
            (org["id"],),
        ).fetchall()
    return safe_template_response(
        "team.html", common_context(request, user, org, members=members, can_manage=can_manage_workspace(org))
    )


@app.post("/team")
def add_team_member(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form("member"),
    csrf: str = Form("", alias="_csrf"),
):
    if not valid_csrf(request, csrf):
        return csrf_error()
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    if not can_manage_workspace(org):
        return RedirectResponse("/team?error=permission", status_code=303)
    subscription = current_subscription(org["id"])
    email = email.strip().lower()
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM memberships WHERE organization_id=? AND is_active=1", (org["id"],)).fetchone()["c"]
        if subscription and count >= subscription["user_limit"]:
            return RedirectResponse("/billing?limit=users", status_code=303)
        existing = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if existing:
            user_id = existing["id"]
        else:
            if len(password) < 10:
                return RedirectResponse("/team?error=password", status_code=303)
            user_id = conn.insert_id(
                "INSERT INTO users (name,email,password_hash,role,created_at,is_superadmin) VALUES (?,?,?,?,?,0)",
                (name.strip(), email, hash_password(password), role, datetime.now().isoformat(timespec="seconds")),
            )
        conn.execute(
            """INSERT OR IGNORE INTO memberships (user_id,organization_id,role,is_active,created_at)
               VALUES (?,?,?,?,?)""",
            (user_id, org["id"], role if role in ("admin", "member") else "member", 1, datetime.now().isoformat(timespec="seconds")),
        )
    log_action(request, f"Usuário adicionado à equipe: {email}")
    return RedirectResponse("/team", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
def workspace_settings(request: Request, saved: str = "", error: str = ""):
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    ai_flash = request.session.pop("ai_flash", "")
    return safe_template_response(
        "settings.html",
        common_context(
            request, user, org, can_manage=can_manage_workspace(org), saved=saved, error=error, ai_flash=ai_flash,
            can_configure_ai=bool(user["is_superadmin"] and os.getenv("JARBAS_ALLOW_SECRET_CONFIG", "0") == "1"),
            ai_legal_model=office_legal_model(), ai_intake_model=office_intake_model(), ai_routine_model=office_routine_model(),
        ),
    )


def _valid_image_signature(payload: bytes, ext: str) -> bool:
    if ext == ".jpg": return payload.startswith(b"\xff\xd8\xff")
    if ext == ".png": return payload.startswith(b"\x89PNG\r\n\x1a\n")
    if ext == ".webp": return len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP"
    return False


@app.post("/settings")
async def update_workspace_settings(
    request: Request,
    brand_name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    address: str = Form(""),
    city: str = Form(""),
    lawyer_name: str = Form(""),
    oab_number: str = Form(""),
    website: str = Form(""),
    primary_color: str = Form("#9f2948"),
    timezone: str = Form("America/Sao_Paulo"),
    logo: UploadFile | None = File(None),
    csrf: str = Form("", alias="_csrf"),
):
    if not valid_csrf(request, csrf):
        return csrf_error()
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    if not can_manage_workspace(org):
        return RedirectResponse("/settings?error=permission", status_code=303)

    primary_color = primary_color.strip()
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", primary_color):
        primary_color = "#9f2948"

    logo_path = org["logo_path"]
    if logo and logo.filename:
        allowed = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
        ext = allowed.get((logo.content_type or "").lower())
        if not ext:
            return RedirectResponse("/settings?error=logo_type", status_code=303)
        payload = await logo.read(2 * 1024 * 1024 + 1)
        if len(payload) > 2 * 1024 * 1024:
            return RedirectResponse("/settings?error=logo_size", status_code=303)
        if not _valid_image_signature(payload, ext):
            return RedirectResponse("/settings?error=logo_content", status_code=303)
        target_dir = WORKSPACE_ASSET_DIR / str(org["id"])
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"logo{ext}"
        target.write_bytes(payload)
        logo_path = f"/static/workspaces/{org['id']}/logo{ext}"

    with db() as conn:
        conn.execute(
            """UPDATE organizations
               SET brand_name=?,email=?,phone=?,address=?,city=?,lawyer_name=?,oab_number=?,website=?,primary_color=?,timezone=?,logo_path=?
               WHERE id=?""",
            (
                brand_name.strip() or org["name"], email.strip(), phone.strip(), address.strip(), city.strip(), lawyer_name.strip(), oab_number.strip(),
                website.strip(), primary_color, timezone.strip() or "America/Sao_Paulo", logo_path, org["id"]
            ),
        )
    log_action(request, "Identidade e configurações do workspace atualizadas")
    return RedirectResponse("/settings?saved=1", status_code=303)


def _qr_svg(uri: str) -> str:
    """QR Code gerado LOCALMENTE, em SVG embutido na página.

    O segredo TOTP não pode sair da máquina: mandá-lo a um serviço externo de
    geração de QR entregaria a terceiro a credencial que protege os autos.
    Se o segno não estiver instalado, devolvemos vazio e a tela cai na
    digitação manual do segredo — que funciona em qualquer aplicativo.
    """
    try:
        import io
        import segno
    except Exception:
        return ""
    try:
        # O segno escreve BYTES, mesmo em SVG. Passar um StringIO levanta
        # TypeError, que o except engoliria: a tela cairia no cadastro manual
        # sem QR e sem ninguém entender por quê.
        buffer = io.BytesIO()
        segno.make(uri, error="m").save(
            buffer, kind="svg", scale=5, border=2,
            dark="#2b0d16", light="#ffffff", xmldecl=False, svgns=True,
        )
        return buffer.getvalue().decode("utf-8")
    except Exception:
        return ""


@app.get("/settings/2fa", response_class=HTMLResponse)
def two_factor_page(request: Request, obrigatorio: str = "", error: str = "", saved: str = ""):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    ativo = two_factor.ativo(user["id"])
    org = current_workspace(request, user)
    exigido = auth_2fa.exige_2fa(user, org)
    codigos = request.session.pop("_2fa_codigos", None)

    segredo = uri = qr = ""
    if not ativo:
        try:
            segredo, uri = two_factor.iniciar_inscricao(user["id"], user["email"])
        except ValueError:
            ativo = True
        else:
            qr = _qr_svg(uri)
    return safe_template_response(
        "settings_2fa.html",
        {
            "request": request, "user": user, "organization": org,
            "workspaces": available_workspaces(user["id"]),
            "ativo": ativo, "exigido": exigido,
            "obrigatorio": obrigatorio == "1",
            "segredo": segredo, "otpauth_uri": uri, "qr_svg": qr,
            "codigos": codigos,
            "codigos_restantes": two_factor.codigos_restantes(user["id"]) if ativo else 0,
            "error": error or None, "saved": saved or None,
        },
    )


@app.post("/settings/2fa/ativar")
def two_factor_ativar(request: Request, codigo: str = Form(""), csrf: str = Form("", alias="_csrf")):
    if not valid_csrf(request, csrf):
        return csrf_error()
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    codigos = two_factor.confirmar_inscricao(user["id"], codigo)
    if codigos is None:
        return RedirectResponse("/settings/2fa?error=codigo", status_code=303)
    # Os códigos em claro existem só nesta passagem: o banco guarda hash.
    request.session["_2fa_codigos"] = codigos
    log_action(request, "Segundo fator ativado")
    return RedirectResponse("/settings/2fa?saved=ativado", status_code=303)


@app.post("/settings/2fa/desativar")
def two_factor_desativar(request: Request, password: str = Form(""), codigo: str = Form(""), csrf: str = Form("", alias="_csrf")):
    if not valid_csrf(request, csrf):
        return csrf_error()
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    org = current_workspace(request, user)
    if auth_2fa.exige_2fa(user, org):
        return RedirectResponse("/settings/2fa?error=obrigatorio", status_code=303)
    # Desligar proteção exige provar as DUAS credenciais: só a sessão aberta
    # não basta, senão um computador desbloqueado derruba o segundo fator.
    with db() as conn:
        atual = conn.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
    if not atual or not verify_password(password, atual["password_hash"]):
        return RedirectResponse("/settings/2fa?error=senha", status_code=303)
    if not two_factor.verificar(user["id"], codigo):
        return RedirectResponse("/settings/2fa?error=codigo", status_code=303)
    two_factor.desativar(user["id"])
    log_action(request, "Segundo fator desativado")
    return RedirectResponse("/settings/2fa?saved=desativado", status_code=303)


@app.post("/settings/2fa/codigos")
def two_factor_regerar_codigos(request: Request, password: str = Form(""), csrf: str = Form("", alias="_csrf")):
    if not valid_csrf(request, csrf):
        return csrf_error()
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    with db() as conn:
        atual = conn.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
    if not atual or not verify_password(password, atual["password_hash"]):
        return RedirectResponse("/settings/2fa?error=senha", status_code=303)
    codigos = two_factor.regerar_codigos_recuperacao(user["id"])
    if codigos is None:
        return RedirectResponse("/settings/2fa?error=inativo", status_code=303)
    request.session["_2fa_codigos"] = codigos
    log_action(request, "Códigos de recuperação regerados")
    return RedirectResponse("/settings/2fa?saved=codigos", status_code=303)


@app.post("/account/password")
def change_own_password(request: Request, current_password: str = Form(...), new_password: str = Form(...), confirm_password: str = Form(...), csrf: str = Form("", alias="_csrf")):
    if not valid_csrf(request, csrf): return csrf_error()
    user=require_user(request)
    if isinstance(user,RedirectResponse): return user
    if len(new_password) < 12 or new_password != confirm_password:
        return RedirectResponse("/settings?password_error=policy",status_code=303)
    with db() as conn:
        fresh=conn.execute("SELECT * FROM users WHERE id=?",(user["id"],)).fetchone()
        if not fresh or not verify_password(current_password,fresh["password_hash"]):
            return RedirectResponse("/settings?password_error=current",status_code=303)
        conn.execute("UPDATE users SET password_hash=? WHERE id=?",(hash_password(new_password),user["id"]))
    request.session.clear()
    return RedirectResponse("/login?password_changed=1",status_code=303)


@app.get("/billing", response_class=HTMLResponse)
def billing(request: Request, limit: str = ""):
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    with db() as conn:
        plans = conn.execute("SELECT * FROM plans WHERE active=1 ORDER BY monthly_price").fetchall()
        users_count = conn.execute("SELECT COUNT(*) c FROM memberships WHERE organization_id=? AND is_active=1", (org["id"],)).fetchone()["c"]
        cases_count = conn.execute("SELECT COUNT(*) c FROM cases WHERE organization_id=?", (org["id"],)).fetchone()["c"]
        orders = conn.execute("SELECT * FROM subscription_orders WHERE organization_id=? ORDER BY id DESC LIMIT 20", (org["id"],)).fetchall()
    return safe_template_response(
        "billing.html",
        common_context(request, user, org, plans=plans, users_count=users_count, cases_count=cases_count, orders=orders, limit=limit, can_manage=can_manage_workspace(org)),
    )


@app.post("/billing/change-plan")
def change_plan(request: Request, plan_code: str = Form(...), csrf: str = Form("", alias="_csrf")):
    """Compatibilidade da UI antiga: cria pedido; não ativa plano sem confirmação de pagamento."""
    if not valid_csrf(request, csrf): return csrf_error()
    user, org = require_workspace(request)
    if isinstance(org, RedirectResponse): return org
    if not can_manage_workspace(org): return RedirectResponse("/billing", status_code=303)
    with db() as conn:
        plan=conn.execute("SELECT * FROM plans WHERE code=? AND active=1",(plan_code,)).fetchone()
        if not plan: return RedirectResponse("/billing?error=plan",status_code=303)
        regular=money(plan["regular_price"] or plan["monthly_price"]); promo=money(plan["monthly_price"])
        existing=conn.execute("SELECT id FROM subscription_orders WHERE organization_id=? AND plan_code=? AND status='pending' ORDER BY id DESC LIMIT 1",(org["id"],plan_code)).fetchone()
        if existing: order_id=existing["id"]
        else:
            order_id=conn.insert_id("INSERT INTO subscription_orders (organization_id,plan_code,regular_price,promo_price,discount_percent,billing_cycle,status,provider,external_id,created_at) VALUES (?,?,?,?,50,'monthly','pending','manual','',?)",(org["id"],plan_code,regular,promo,datetime.now().isoformat(timespec="seconds")))
    log_action(request,f"Pedido de alteração de plano #{order_id} criado para {plan_code}; aguardando ativação/pagamento")
    return RedirectResponse(f"/billing?order={order_id}",status_code=303)


@app.get("/platform", response_class=HTMLResponse)
def platform_admin(request: Request):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    if not user["is_superadmin"]:
        return RedirectResponse("/", status_code=303)
    with db() as conn:
        offices = conn.execute(
            """SELECT o.*,p.name plan_name,p.monthly_price,s.status subscription_status,s.current_period_end,
                      (SELECT COUNT(*) FROM memberships m WHERE m.organization_id=o.id AND m.is_active=1) users_count,
                      (SELECT COUNT(*) FROM cases c WHERE c.organization_id=o.id) cases_count,
                      (SELECT COALESCE(SUM(credits),0) FROM usage_ledger ul WHERE ul.organization_id=o.id AND ul.created_at>=?) credits_used
               FROM organizations o
               LEFT JOIN subscriptions s ON s.organization_id=o.id
               LEFT JOIN plans p ON p.id=s.plan_id
               ORDER BY o.id DESC""",
            (date.today().replace(day=1).isoformat(),),
        ).fetchall()
        stats = {
            "offices": conn.execute("SELECT COUNT(*) c FROM organizations WHERE status='active'").fetchone()["c"],
            "users": conn.execute("SELECT COUNT(*) c FROM memberships WHERE is_active=1").fetchone()["c"],
            "mrr": conn.execute(
                """SELECT COALESCE(SUM(p.monthly_price),0) s FROM subscriptions s
                   JOIN plans p ON p.id=s.plan_id WHERE s.status='active'"""
            ).fetchone()["s"],
            "trials": conn.execute("SELECT COUNT(*) c FROM subscriptions WHERE status='trial'").fetchone()["c"],
        }
        plans = conn.execute("SELECT * FROM plans WHERE active=1 ORDER BY monthly_price").fetchall()
        subscription_orders = conn.execute("""SELECT so.*,o.name organization_name FROM subscription_orders so JOIN organizations o ON o.id=so.organization_id ORDER BY so.id DESC LIMIT 100""").fetchall()
    org = current_workspace(request, user)
    return safe_template_response(
        "platform.html",
        common_context(request, user, org, offices=offices, platform_stats=stats, plans=plans, subscription_orders=subscription_orders),
    )


@app.post("/platform/offices")
def platform_create_office(
    request: Request,
    office_name: str = Form(...),
    owner_name: str = Form(...),
    owner_email: str = Form(...),
    temporary_password: str = Form(...),
    plan_code: str = Form("pro"),
    csrf: str = Form("", alias="_csrf"),
):
    if not valid_csrf(request, csrf):
        return csrf_error()
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    if not user["is_superadmin"]:
        return RedirectResponse("/", status_code=303)
    if len(temporary_password) < 10:
        return RedirectResponse("/platform?error=password", status_code=303)
    now = datetime.now().isoformat(timespec="seconds")
    owner_email = owner_email.strip().lower()
    with db() as conn:
        plan = conn.execute("SELECT * FROM plans WHERE code=?", (plan_code,)).fetchone()
        if not plan:
            return RedirectResponse("/platform", status_code=303)
        slug = unique_slug(conn, office_name)
        org_id = conn.insert_id(
            "INSERT INTO organizations (name,slug,email,brand_name,status,created_at) VALUES (?,?,?,?,'active',?)",
            (office_name.strip(), slug, owner_email, office_name.strip(), now),
        )
        owner = conn.execute("SELECT * FROM users WHERE email=?", (owner_email,)).fetchone()
        if owner:
            owner_id = owner["id"]
        else:
            owner_id = conn.insert_id(
                "INSERT INTO users (name,email,password_hash,role,created_at,is_superadmin) VALUES (?,?,?,?,?,0)",
                (owner_name.strip(), owner_email, hash_password(temporary_password), "admin", now),
            )
        conn.execute(
            "INSERT OR IGNORE INTO memberships (user_id,organization_id,role,is_active,created_at) VALUES (?,?,'owner',1,?)",
            (owner_id, org_id, now),
        )
        conn.execute(
            "INSERT INTO subscriptions (organization_id,plan_id,status,started_at,current_period_end) VALUES (?,?,'active',?,?)",
            (org_id, plan["id"], now, (date.today() + timedelta(days=30)).isoformat()),
        )
    log_action(request, f"Super Admin criou escritório: {office_name.strip()}", None)
    return RedirectResponse("/platform", status_code=303)


@app.get("/produto", response_class=HTMLResponse)
def product_page(request: Request):
    with db() as conn:
        plans = conn.execute("SELECT * FROM plans WHERE active=1 ORDER BY monthly_price").fetchall()
    return safe_template_response("product.html", {"request": request, "plans": plans})


@app.get("/robots.txt", response_class=HTMLResponse)
def robots():
    if os.getenv("JARBAS_ALLOW_INDEXING", "0") == "1":
        return HTMLResponse("User-agent: *\nAllow: /\n", media_type="text/plain")
    return HTMLResponse("User-agent: *\nDisallow: /\n", media_type="text/plain")


@app.get("/no-workspace", response_class=HTMLResponse)
def no_workspace(request: Request):
    user = require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    return safe_template_response("no_workspace.html", {"request": request, "user": user})

# JARBAS 8.0 — módulos interligados (intake inteligente, documentos, financeiro avançado, Central IA).
from .v7 import router as v7_router
app.include_router(v7_router)

# JARBAS 8.5 — Conselho tri-IA (OpenAI + Anthropic + Google).
from .council_routes import router as council_router
app.include_router(council_router)

# JARBAS 8.7 — cobrança recorrente (Mercado Pago).
from .billing_routes import router as billing_router
app.include_router(billing_router)

# JARBAS 8.9 — prazos processuais.
from .prazo_routes import router as prazo_router
app.include_router(prazo_router)
