from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional

BASE_DIR = Path(__file__).resolve().parent.parent

# Onde vivem banco, uploads, PDFs importados e documentos gerados.
#
# O padrão continua sendo `data/` dentro da instalação — é o que o instalador
# do Windows espera, faz backup e migra entre versões, e nada disso muda se a
# variável não for definida.
#
# A variável existe porque em servidor os dados NÃO podem morar na árvore de
# código: em container, `data/` dentro da imagem é apagado a cada atualização,
# e o volume persistente precisa ser montado em outro lugar. Também separa o
# que é sigiloso (autos de clientes) do que é apenas código, o que simplifica
# backup, permissão de arquivo e resposta a incidente.
DATA_DIR = Path(os.getenv("JARBAS_DATA_DIR", "").strip() or (BASE_DIR / "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "jarbas.db"

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DB_PATH}")
IS_POSTGRES = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

if IS_POSTGRES:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - dependency checked in container build
        raise RuntimeError("DATABASE_URL aponta para PostgreSQL, mas psycopg não está instalado.") from exc


def _pg_sql(sql: str) -> str:
    converted = sql
    is_ignore = bool(re.match(r"\s*INSERT\s+OR\s+IGNORE\s+INTO\b", converted, flags=re.I))
    if is_ignore:
        converted = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", converted, count=1, flags=re.I)
    # A aplicação usa placeholders DB-API do SQLite. Não há '?' literais nas queries.
    converted = converted.replace("?", "%s")
    if is_ignore and "ON CONFLICT" not in converted.upper():
        converted = converted.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return converted


# Pool do PostgreSQL. Sem ele, cada request abria uma conexão nova — a
# operação mais cara do ciclo, e a que primeiro derruba o banco sob carga.
_POOL = None


def _pool():
    global _POOL
    if _POOL is None:
        from psycopg_pool import ConnectionPool
        _POOL = ConnectionPool(
            DATABASE_URL,
            min_size=int(os.getenv("JARBAS_DB_POOL_MIN", "2")),
            max_size=int(os.getenv("JARBAS_DB_POOL_MAX", "20")),
            timeout=float(os.getenv("JARBAS_DB_POOL_TIMEOUT", "15")),
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _POOL


class Connection:
    def __init__(self):
        self.backend = "postgres" if IS_POSTGRES else "sqlite"
        self._pooled = False
        if IS_POSTGRES:
            try:
                self.raw = _pool().getconn()
                self._pooled = True
            except Exception:
                # psycopg_pool ausente ou pool indisponível: conexão direta.
                self.raw = psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)
        else:
            self.raw = sqlite3.connect(DB_PATH, timeout=15)
            self.raw.row_factory = sqlite3.Row
            self.raw.execute("PRAGMA foreign_keys = ON")
            self.raw.execute("PRAGMA journal_mode = WAL")

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.raw.commit()
            else:
                self.raw.rollback()
        finally:
            if self._pooled:
                try:
                    _pool().putconn(self.raw)   # devolve, não fecha
                except Exception:
                    self.raw.close()
            else:
                self.raw.close()
        return False

    def execute(self, sql: str, params: Iterable[Any] = ()):
        if IS_POSTGRES:
            return self.raw.execute(_pg_sql(sql), tuple(params))
        return self.raw.execute(sql, tuple(params))

    def executescript(self, script: str) -> None:
        if not IS_POSTGRES:
            self.raw.executescript(script)
            return
        # O schema abaixo não contém procedures/strings com ';' internos.
        for statement in script.split(";"):
            if statement.strip():
                self.raw.execute(statement)

    def insert_id(self, sql: str, params: Iterable[Any] = ()) -> int:
        if IS_POSTGRES:
            statement = _pg_sql(sql).rstrip().rstrip(";")
            if "RETURNING" not in statement.upper():
                statement += " RETURNING id"
            row = self.raw.execute(statement, tuple(params)).fetchone()
            return int(row["id"])
        cur = self.raw.execute(sql, tuple(params))
        return int(cur.lastrowid)


def db() -> Connection:
    return Connection()


def has_column(conn: Connection, table: str, column: str) -> bool:
    if conn.backend == "postgres":
        row = conn.execute(
            """SELECT 1 FROM information_schema.columns
               WHERE table_schema='public' AND table_name=? AND column_name=?""",
            (table, column),
        ).fetchone()
        return bool(row)
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())


def ensure_column(conn: Connection, table: str, definition: str) -> None:
    """Acrescenta a coluna se ela ainda não existir.

    ATENÇÃO: TRÊS argumentos — nome e tipo vão juntos na mesma string
    (`ensure_column(conn, "deadlines", "count_start TEXT")`). Chamar com
    quatro derruba init_db inteiro com TypeError; já quebrou três releases.

    Verificar e depois alterar é uma corrida quando mais de um processo sobe
    ao mesmo tempo — e é exatamente o que acontece com uvicorn --workers 2 ou
    com duas instâncias da aplicação apontando para o mesmo banco. Os dois
    veem a coluna faltando, os dois emitem o ALTER, e o segundo morre com
    "duplicate column name" ANTES de servir a primeira requisição.

    No PostgreSQL o próprio banco resolve com IF NOT EXISTS. O SQLite não
    tem essa cláusula, então a corrida perdida é absorvida: se o erro for
    justamente o de coluna duplicada, o trabalho já foi feito por quem
    chegou primeiro e não há nada a corrigir. Qualquer outro erro sobe.
    """
    column = definition.split()[0]
    if has_column(conn, table, column):
        return
    if conn.backend == "postgres":
        conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {definition}")
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'admin',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    monthly_price REAL NOT NULL,
    user_limit INTEGER NOT NULL,
    case_limit INTEGER NOT NULL,
    storage_gb INTEGER NOT NULL,
    monthly_credits INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS organizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT UNIQUE NOT NULL,
    document TEXT,
    email TEXT,
    phone TEXT,
    brand_name TEXT,
    logo_path TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memberships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    organization_id INTEGER NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, organization_id),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY(organization_id) REFERENCES organizations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER UNIQUE NOT NULL,
    plan_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'trial',
    started_at TEXT NOT NULL,
    current_period_end TEXT,
    FOREIGN KEY(organization_id) REFERENCES organizations(id) ON DELETE CASCADE,
    FOREIGN KEY(plan_id) REFERENCES plans(id)
);
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER,
    name TEXT NOT NULL,
    document TEXT,
    phone TEXT,
    email TEXT,
    notes TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER,
    client_id INTEGER,
    number TEXT,
    title TEXT NOT NULL,
    area TEXT NOT NULL,
    court TEXT,
    status TEXT NOT NULL DEFAULT 'Ativo',
    risk TEXT NOT NULL DEFAULT 'Médio',
    facts TEXT,
    evidence TEXT,
    strategy TEXT,
    next_step TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS deadlines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER,
    case_id INTEGER,
    title TEXT NOT NULL,
    due_date TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'Normal',
    status TEXT NOT NULL DEFAULT 'Pendente',
    created_at TEXT NOT NULL,
    FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS finance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER,
    client_id INTEGER,
    case_id INTEGER,
    description TEXT NOT NULL,
    amount REAL NOT NULL,
    due_date TEXT,
    status TEXT NOT NULL DEFAULT 'Pendente',
    kind TEXT NOT NULL DEFAULT 'Honorário',
    created_at TEXT NOT NULL,
    FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE SET NULL,
    FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS action_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER,
    user_email TEXT,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    user_id INTEGER,
    kind TEXT NOT NULL,
    credits INTEGER NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(organization_id) REFERENCES organizations(id) ON DELETE CASCADE,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS case_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    case_id INTEGER NOT NULL,
    original_name TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    mime_type TEXT,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    page_count INTEGER NOT NULL DEFAULT 0,
    text_chars INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'uploaded',
    extraction_note TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE,
    UNIQUE(organization_id,case_id,sha256)
);
CREATE TABLE IF NOT EXISTS document_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    case_id INTEGER NOT NULL,
    document_id INTEGER NOT NULL,
    page_number INTEGER NOT NULL,
    text TEXT,
    label TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(document_id) REFERENCES case_documents(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS document_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    case_id INTEGER NOT NULL,
    document_id INTEGER NOT NULL,
    page_number INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    label TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(document_id) REFERENCES case_documents(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS copilot_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    case_id INTEGER NOT NULL,
    user_id INTEGER,
    kind TEXT NOT NULL,
    question TEXT,
    answer TEXT NOT NULL,
    sources_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    case_id INTEGER NOT NULL,
    user_id INTEGER,
    title TEXT NOT NULL,
    draft_type TEXT NOT NULL,
    content TEXT NOT NULL,
    sources_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_case_documents_case ON case_documents(organization_id,case_id);
CREATE INDEX IF NOT EXISTS idx_document_pages_case ON document_pages(organization_id,case_id,document_id,page_number);
CREATE INDEX IF NOT EXISTS idx_document_chunks_case ON document_chunks(organization_id,case_id,document_id,page_number);
CREATE INDEX IF NOT EXISTS idx_copilot_runs_case ON copilot_runs(organization_id,case_id,created_at);
CREATE INDEX IF NOT EXISTS idx_drafts_case ON drafts(organization_id,case_id,created_at);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    contact_name TEXT,
    phone TEXT,
    email TEXT,
    source TEXT,
    area TEXT,
    status TEXT NOT NULL DEFAULT 'Novo',
    estimated_fee REAL NOT NULL DEFAULT 0,
    next_action_date TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(organization_id) REFERENCES organizations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    case_id INTEGER,
    client_id INTEGER,
    user_id INTEGER,
    title TEXT NOT NULL,
    activity_type TEXT NOT NULL DEFAULT 'Tarefa',
    start_at TEXT,
    due_at TEXT,
    status TEXT NOT NULL DEFAULT 'Pendente',
    priority TEXT NOT NULL DEFAULT 'Normal',
    notes TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(organization_id) REFERENCES organizations(id) ON DELETE CASCADE,
    FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE,
    FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE SET NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS time_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    case_id INTEGER,
    client_id INTEGER,
    user_id INTEGER,
    work_date TEXT NOT NULL,
    minutes INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL,
    billable INTEGER NOT NULL DEFAULT 1,
    hourly_rate REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY(organization_id) REFERENCES organizations(id) ON DELETE CASCADE,
    FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE SET NULL,
    FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE SET NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS financial_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    account_type TEXT NOT NULL DEFAULT 'Banco',
    bank_name TEXT,
    opening_balance REAL NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    FOREIGN KEY(organization_id) REFERENCES organizations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS financial_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    direction TEXT NOT NULL DEFAULT 'both',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(organization_id,name,direction),
    FOREIGN KEY(organization_id) REFERENCES organizations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS fee_contracts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    client_id INTEGER,
    case_id INTEGER,
    title TEXT NOT NULL,
    contract_value REAL NOT NULL DEFAULT 0,
    success_percent REAL NOT NULL DEFAULT 0,
    entry_amount REAL NOT NULL DEFAULT 0,
    installment_count INTEGER NOT NULL DEFAULT 0,
    first_due_date TEXT,
    status TEXT NOT NULL DEFAULT 'Ativo',
    notes TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(organization_id) REFERENCES organizations(id) ON DELETE CASCADE,
    FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE SET NULL,
    FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS financial_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    client_id INTEGER,
    case_id INTEGER,
    contract_id INTEGER,
    category_id INTEGER,
    direction TEXT NOT NULL,
    description TEXT NOT NULL,
    original_amount REAL NOT NULL,
    due_date TEXT,
    competence_date TEXT,
    status TEXT NOT NULL DEFAULT 'Pendente',
    payment_method TEXT,
    notes TEXT,
    legacy_finance_id INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY(organization_id) REFERENCES organizations(id) ON DELETE CASCADE,
    FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE SET NULL,
    FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE SET NULL,
    FOREIGN KEY(contract_id) REFERENCES fee_contracts(id) ON DELETE SET NULL,
    FOREIGN KEY(category_id) REFERENCES financial_categories(id) ON DELETE SET NULL,
    UNIQUE(organization_id,legacy_finance_id)
);
CREATE TABLE IF NOT EXISTS financial_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    transaction_id INTEGER NOT NULL,
    account_id INTEGER,
    amount REAL NOT NULL,
    paid_at TEXT NOT NULL,
    payment_method TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(organization_id) REFERENCES organizations(id) ON DELETE CASCADE,
    FOREIGN KEY(transaction_id) REFERENCES financial_transactions(id) ON DELETE CASCADE,
    FOREIGN KEY(account_id) REFERENCES financial_accounts(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_org_status ON leads(organization_id,status,next_action_date);
CREATE INDEX IF NOT EXISTS idx_activities_org_due ON activities(organization_id,status,due_at);
CREATE INDEX IF NOT EXISTS idx_time_entries_org_date ON time_entries(organization_id,work_date);
CREATE INDEX IF NOT EXISTS idx_financial_transactions_org_due ON financial_transactions(organization_id,direction,status,due_date);
CREATE INDEX IF NOT EXISTS idx_financial_payments_org_date ON financial_payments(organization_id,paid_at);
CREATE INDEX IF NOT EXISTS idx_fee_contracts_org ON fee_contracts(organization_id,status);
"""

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'admin',
    created_at TEXT NOT NULL,
    is_superadmin INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS plans (
    id BIGSERIAL PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    monthly_price NUMERIC(12,2) NOT NULL,
    user_limit INTEGER NOT NULL,
    case_limit INTEGER NOT NULL,
    storage_gb INTEGER NOT NULL,
    monthly_credits INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS organizations (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT UNIQUE NOT NULL,
    document TEXT,
    email TEXT,
    phone TEXT,
    brand_name TEXT,
    logo_path TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    address TEXT,
    website TEXT,
    primary_color TEXT DEFAULT '#9f2948',
    timezone TEXT DEFAULT 'America/Sao_Paulo'
);
CREATE TABLE IF NOT EXISTS memberships (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    organization_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'member',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, organization_id)
);
CREATE TABLE IF NOT EXISTS subscriptions (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT UNIQUE NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    plan_id BIGINT NOT NULL REFERENCES plans(id),
    status TEXT NOT NULL DEFAULT 'trial',
    started_at TEXT NOT NULL,
    current_period_end TEXT
);
CREATE TABLE IF NOT EXISTS clients (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT REFERENCES organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    document TEXT,
    phone TEXT,
    email TEXT,
    notes TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cases (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT REFERENCES organizations(id) ON DELETE CASCADE,
    client_id BIGINT REFERENCES clients(id) ON DELETE SET NULL,
    number TEXT,
    title TEXT NOT NULL,
    area TEXT NOT NULL,
    court TEXT,
    status TEXT NOT NULL DEFAULT 'Ativo',
    risk TEXT NOT NULL DEFAULT 'Médio',
    facts TEXT,
    evidence TEXT,
    strategy TEXT,
    next_step TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deadlines (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT REFERENCES organizations(id) ON DELETE CASCADE,
    case_id BIGINT REFERENCES cases(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    due_date TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'Normal',
    status TEXT NOT NULL DEFAULT 'Pendente',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS finance (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT REFERENCES organizations(id) ON DELETE CASCADE,
    client_id BIGINT REFERENCES clients(id) ON DELETE SET NULL,
    case_id BIGINT REFERENCES cases(id) ON DELETE SET NULL,
    description TEXT NOT NULL,
    amount NUMERIC(14,2) NOT NULL,
    due_date TEXT,
    status TEXT NOT NULL DEFAULT 'Pendente',
    kind TEXT NOT NULL DEFAULT 'Honorário',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS action_logs (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT REFERENCES organizations(id) ON DELETE CASCADE,
    user_email TEXT,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_ledger (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    credits INTEGER NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clients_org ON clients(organization_id);
CREATE INDEX IF NOT EXISTS idx_cases_org ON cases(organization_id);
CREATE INDEX IF NOT EXISTS idx_deadlines_org_due ON deadlines(organization_id, due_date);
CREATE INDEX IF NOT EXISTS idx_finance_org_status ON finance(organization_id, status);
CREATE INDEX IF NOT EXISTS idx_logs_org_created ON action_logs(organization_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_org_created ON usage_ledger(organization_id, created_at);

CREATE TABLE IF NOT EXISTS case_documents (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    case_id BIGINT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    original_name TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    mime_type TEXT,
    size_bytes BIGINT NOT NULL DEFAULT 0,
    page_count INTEGER NOT NULL DEFAULT 0,
    text_chars BIGINT NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'uploaded',
    extraction_note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(organization_id,case_id,sha256)
);
CREATE TABLE IF NOT EXISTS document_pages (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    case_id BIGINT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    document_id BIGINT NOT NULL REFERENCES case_documents(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL,
    text TEXT,
    label TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS document_chunks (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    case_id BIGINT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    document_id BIGINT NOT NULL REFERENCES case_documents(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    label TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS copilot_runs (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    case_id BIGINT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    question TEXT,
    answer TEXT NOT NULL,
    sources_json TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    case_id BIGINT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    draft_type TEXT NOT NULL,
    content TEXT NOT NULL,
    sources_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_case_documents_case ON case_documents(organization_id,case_id);
CREATE INDEX IF NOT EXISTS idx_document_pages_case ON document_pages(organization_id,case_id,document_id,page_number);
CREATE INDEX IF NOT EXISTS idx_document_chunks_case ON document_chunks(organization_id,case_id,document_id,page_number);
CREATE INDEX IF NOT EXISTS idx_copilot_runs_case ON copilot_runs(organization_id,case_id,created_at);
CREATE INDEX IF NOT EXISTS idx_drafts_case ON drafts(organization_id,case_id,created_at);

CREATE TABLE IF NOT EXISTS leads (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    contact_name TEXT,
    phone TEXT,
    email TEXT,
    source TEXT,
    area TEXT,
    status TEXT NOT NULL DEFAULT 'Novo',
    estimated_fee NUMERIC(14,2) NOT NULL DEFAULT 0,
    next_action_date TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activities (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    case_id BIGINT REFERENCES cases(id) ON DELETE CASCADE,
    client_id BIGINT REFERENCES clients(id) ON DELETE SET NULL,
    user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    activity_type TEXT NOT NULL DEFAULT 'Tarefa',
    start_at TEXT,
    due_at TEXT,
    status TEXT NOT NULL DEFAULT 'Pendente',
    priority TEXT NOT NULL DEFAULT 'Normal',
    notes TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS time_entries (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    case_id BIGINT REFERENCES cases(id) ON DELETE SET NULL,
    client_id BIGINT REFERENCES clients(id) ON DELETE SET NULL,
    user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
    work_date TEXT NOT NULL,
    minutes INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL,
    billable INTEGER NOT NULL DEFAULT 1,
    hourly_rate NUMERIC(14,2) NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS financial_accounts (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    account_type TEXT NOT NULL DEFAULT 'Banco',
    bank_name TEXT,
    opening_balance NUMERIC(14,2) NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS financial_categories (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    direction TEXT NOT NULL DEFAULT 'both',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(organization_id,name,direction)
);
CREATE TABLE IF NOT EXISTS fee_contracts (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    client_id BIGINT REFERENCES clients(id) ON DELETE SET NULL,
    case_id BIGINT REFERENCES cases(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    contract_value NUMERIC(14,2) NOT NULL DEFAULT 0,
    success_percent NUMERIC(8,4) NOT NULL DEFAULT 0,
    entry_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
    installment_count INTEGER NOT NULL DEFAULT 0,
    first_due_date TEXT,
    status TEXT NOT NULL DEFAULT 'Ativo',
    notes TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS financial_transactions (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    client_id BIGINT REFERENCES clients(id) ON DELETE SET NULL,
    case_id BIGINT REFERENCES cases(id) ON DELETE SET NULL,
    contract_id BIGINT REFERENCES fee_contracts(id) ON DELETE SET NULL,
    category_id BIGINT REFERENCES financial_categories(id) ON DELETE SET NULL,
    direction TEXT NOT NULL,
    description TEXT NOT NULL,
    original_amount NUMERIC(14,2) NOT NULL,
    due_date TEXT,
    competence_date TEXT,
    status TEXT NOT NULL DEFAULT 'Pendente',
    payment_method TEXT,
    notes TEXT,
    legacy_finance_id BIGINT,
    created_at TEXT NOT NULL,
    UNIQUE(organization_id,legacy_finance_id)
);
CREATE TABLE IF NOT EXISTS financial_payments (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    transaction_id BIGINT NOT NULL REFERENCES financial_transactions(id) ON DELETE CASCADE,
    account_id BIGINT REFERENCES financial_accounts(id) ON DELETE SET NULL,
    amount NUMERIC(14,2) NOT NULL,
    paid_at TEXT NOT NULL,
    payment_method TEXT,
    notes TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_org_status ON leads(organization_id,status,next_action_date);
CREATE INDEX IF NOT EXISTS idx_activities_org_due ON activities(organization_id,status,due_at);
CREATE INDEX IF NOT EXISTS idx_time_entries_org_date ON time_entries(organization_id,work_date);
CREATE INDEX IF NOT EXISTS idx_financial_transactions_org_due ON financial_transactions(organization_id,direction,status,due_date);
CREATE INDEX IF NOT EXISTS idx_financial_payments_org_date ON financial_payments(organization_id,paid_at);
CREATE INDEX IF NOT EXISTS idx_fee_contracts_org ON fee_contracts(organization_id,status);
"""


def initialize_schema(conn: Connection) -> None:
    conn.executescript(POSTGRES_SCHEMA if IS_POSTGRES else SQLITE_SCHEMA)

# JARBAS 7.0 — esquema adicional para intake inteligente, documentos, IA e SaaS escalável.
V7_SQLITE_EXTRA = r"""
CREATE TABLE IF NOT EXISTS case_imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    user_id INTEGER,
    original_name TEXT NOT NULL,
    temp_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    parsed_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_client',
    created_at TEXT NOT NULL,
    FOREIGN KEY(organization_id) REFERENCES organizations(id) ON DELETE CASCADE,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS generated_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    client_id INTEGER NOT NULL,
    case_id INTEGER,
    user_id INTEGER,
    document_type TEXT NOT NULL,
    title TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    file_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(organization_id) REFERENCES organizations(id) ON DELETE CASCADE,
    FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE,
    FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE SET NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS document_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    body TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    UNIQUE(organization_id,code),
    FOREIGN KEY(organization_id) REFERENCES organizations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS cost_centers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(organization_id,code),
    FOREIGN KEY(organization_id) REFERENCES organizations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS ai_threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    user_id INTEGER,
    case_id INTEGER,
    client_id INTEGER,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(organization_id) REFERENCES organizations(id) ON DELETE CASCADE,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL,
    FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE,
    FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS ai_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    model TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY(organization_id) REFERENCES organizations(id) ON DELETE CASCADE,
    FOREIGN KEY(thread_id) REFERENCES ai_threads(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS subscription_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    plan_code TEXT NOT NULL,
    regular_price REAL NOT NULL DEFAULT 0,
    promo_price REAL NOT NULL DEFAULT 0,
    discount_percent REAL NOT NULL DEFAULT 0,
    billing_cycle TEXT NOT NULL DEFAULT 'monthly',
    status TEXT NOT NULL DEFAULT 'pending',
    provider TEXT,
    external_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(organization_id) REFERENCES organizations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_case_imports_org ON case_imports(organization_id,status,created_at);
CREATE INDEX IF NOT EXISTS idx_generated_docs_client ON generated_documents(organization_id,client_id,created_at);
CREATE INDEX IF NOT EXISTS idx_ai_threads_org ON ai_threads(organization_id,updated_at);
CREATE INDEX IF NOT EXISTS idx_ai_messages_thread ON ai_messages(organization_id,thread_id,id);
CREATE INDEX IF NOT EXISTS idx_cost_centers_org ON cost_centers(organization_id,code);
"""

V7_POSTGRES_EXTRA = r"""
CREATE TABLE IF NOT EXISTS case_imports (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
    original_name TEXT NOT NULL,
    temp_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    parsed_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_client',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS generated_documents (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    client_id BIGINT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    case_id BIGINT REFERENCES cases(id) ON DELETE SET NULL,
    user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
    document_type TEXT NOT NULL,
    title TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    file_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS document_templates (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    body TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    UNIQUE(organization_id,code)
);
CREATE TABLE IF NOT EXISTS cost_centers (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(organization_id,code)
);
CREATE TABLE IF NOT EXISTS ai_threads (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
    case_id BIGINT REFERENCES cases(id) ON DELETE CASCADE,
    client_id BIGINT REFERENCES clients(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_messages (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    thread_id BIGINT NOT NULL REFERENCES ai_threads(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    model TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscription_orders (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    plan_code TEXT NOT NULL,
    regular_price NUMERIC(12,2) NOT NULL DEFAULT 0,
    promo_price NUMERIC(12,2) NOT NULL DEFAULT 0,
    discount_percent NUMERIC(5,2) NOT NULL DEFAULT 0,
    billing_cycle TEXT NOT NULL DEFAULT 'monthly',
    status TEXT NOT NULL DEFAULT 'pending',
    provider TEXT,
    external_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_case_imports_org ON case_imports(organization_id,status,created_at);
CREATE INDEX IF NOT EXISTS idx_generated_docs_client ON generated_documents(organization_id,client_id,created_at);
CREATE INDEX IF NOT EXISTS idx_ai_threads_org ON ai_threads(organization_id,updated_at);
CREATE INDEX IF NOT EXISTS idx_ai_messages_thread ON ai_messages(organization_id,thread_id,id);
CREATE INDEX IF NOT EXISTS idx_cost_centers_org ON cost_centers(organization_id,code);
"""

# Rebind para que bancos novos e existentes recebam o núcleo 7.0.
def initialize_schema(conn: Connection) -> None:
    conn.executescript(POSTGRES_SCHEMA if IS_POSTGRES else SQLITE_SCHEMA)
    conn.executescript(V7_POSTGRES_EXTRA if IS_POSTGRES else V7_SQLITE_EXTRA)

# JARBAS 8.1 — integridade de dossiê, partes processuais e versionamento de esquema.
V81_SQLITE_EXTRA = r"""
CREATE TABLE IF NOT EXISTS case_parties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    case_id INTEGER NOT NULL,
    client_id INTEGER,
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'Parte',
    document TEXT,
    person_type TEXT,
    nationality TEXT,
    marital_status TEXT,
    profession TEXT,
    rg TEXT,
    address TEXT,
    city TEXT,
    state TEXT,
    zip_code TEXT,
    phone TEXT,
    email TEXT,
    source_page INTEGER,
    source_excerpt TEXT,
    is_client INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY(organization_id) REFERENCES organizations(id) ON DELETE CASCADE,
    FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE,
    FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_case_parties_case ON case_parties(organization_id,case_id,is_client,role);
CREATE INDEX IF NOT EXISTS idx_case_parties_document ON case_parties(organization_id,document);
"""

V81_POSTGRES_EXTRA = r"""
CREATE TABLE IF NOT EXISTS case_parties (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    case_id BIGINT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    client_id BIGINT REFERENCES clients(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'Parte',
    document TEXT,
    person_type TEXT,
    nationality TEXT,
    marital_status TEXT,
    profession TEXT,
    rg TEXT,
    address TEXT,
    city TEXT,
    state TEXT,
    zip_code TEXT,
    phone TEXT,
    email TEXT,
    source_page INTEGER,
    source_excerpt TEXT,
    is_client INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_case_parties_case ON case_parties(organization_id,case_id,is_client,role);
CREATE INDEX IF NOT EXISTS idx_case_parties_document ON case_parties(organization_id,document);
"""

# Rebind final: toda inicialização 8.1 recebe o núcleo anterior + tabelas novas.
def initialize_schema(conn: Connection) -> None:
    conn.executescript(POSTGRES_SCHEMA if IS_POSTGRES else SQLITE_SCHEMA)
    conn.executescript(V7_POSTGRES_EXTRA if IS_POSTGRES else V7_SQLITE_EXTRA)
    conn.executescript(V81_POSTGRES_EXTRA if IS_POSTGRES else V81_SQLITE_EXTRA)


# --------------------------------------------------------------------------
# JARBAS 8.5 — Conselho tri-IA.
# Registro por ETAPA (não por requisição): é o que permite cobrar por token
# real e aplicar teto de gasto em dólar, em vez de crédito de valor fixo.
# --------------------------------------------------------------------------

V85_SQLITE_EXTRA = r"""
CREATE TABLE IF NOT EXISTS ai_council_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    user_id INTEGER,
    case_id INTEGER,
    role TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    elapsed_s REAL NOT NULL DEFAULT 0,
    fallback_from TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_council_org_data ON ai_council_runs(organization_id,created_at);
CREATE INDEX IF NOT EXISTS idx_council_case ON ai_council_runs(organization_id,case_id);
"""

V85_POSTGRES_EXTRA = r"""
CREATE TABLE IF NOT EXISTS ai_council_runs (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id BIGINT,
    case_id BIGINT,
    role TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens BIGINT NOT NULL DEFAULT 0,
    output_tokens BIGINT NOT NULL DEFAULT 0,
    cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    elapsed_s DOUBLE PRECISION NOT NULL DEFAULT 0,
    fallback_from TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_council_org_data ON ai_council_runs(organization_id,created_at);
CREATE INDEX IF NOT EXISTS idx_council_case ON ai_council_runs(organization_id,case_id);
"""


# Rebind final 8.5: núcleo + 7.0 + 8.1 + conselho tri-IA.
def initialize_schema(conn: Connection) -> None:
    conn.executescript(POSTGRES_SCHEMA if IS_POSTGRES else SQLITE_SCHEMA)
    conn.executescript(V7_POSTGRES_EXTRA if IS_POSTGRES else V7_SQLITE_EXTRA)
    conn.executescript(V81_POSTGRES_EXTRA if IS_POSTGRES else V81_SQLITE_EXTRA)
    conn.executescript(V85_POSTGRES_EXTRA if IS_POSTGRES else V85_SQLITE_EXTRA)


# --------------------------------------------------------------------------
# JARBAS 8.6 — plano como contrato aplicável.
# ai_tier: faixa de modelos usada pelo Conselho (ver app/plan_limits.FAIXAS).
# monthly_ai_usd: orçamento mensal de IA em dólar, medido por token real.
# --------------------------------------------------------------------------

V86_SQLITE_EXTRA = r"""
ALTER TABLE plans ADD COLUMN ai_tier TEXT;
ALTER TABLE plans ADD COLUMN monthly_ai_usd REAL;
"""

V86_POSTGRES_EXTRA = r"""
ALTER TABLE plans ADD COLUMN IF NOT EXISTS ai_tier TEXT;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS monthly_ai_usd DOUBLE PRECISION;
"""


def initialize_schema(conn: Connection) -> None:
    conn.executescript(POSTGRES_SCHEMA if IS_POSTGRES else SQLITE_SCHEMA)
    conn.executescript(V7_POSTGRES_EXTRA if IS_POSTGRES else V7_SQLITE_EXTRA)
    conn.executescript(V81_POSTGRES_EXTRA if IS_POSTGRES else V81_SQLITE_EXTRA)
    conn.executescript(V85_POSTGRES_EXTRA if IS_POSTGRES else V85_SQLITE_EXTRA)
    # ALTER TABLE não tem IF NOT EXISTS no SQLite: ensure_column é idempotente.
    ensure_column(conn, "plans", "ai_tier TEXT")
    ensure_column(conn, "plans", "monthly_ai_usd " + ("DOUBLE PRECISION" if IS_POSTGRES else "REAL"))


# --------------------------------------------------------------------------
# JARBAS 8.7 — cobrança recorrente (Mercado Pago Preapproval).
# billing_events guarda TODA notificação recebida: é a trilha de auditoria
# financeira e a base da idempotência (o Mercado Pago reenvia notificações).
# --------------------------------------------------------------------------

V87_SQLITE_EXTRA = r"""
CREATE TABLE IF NOT EXISTS billing_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL DEFAULT 'mercadopago',
    event_key TEXT NOT NULL,
    preapproval_id TEXT,
    organization_id INTEGER,
    plan_code TEXT,
    status_provider TEXT,
    status_internal TEXT,
    amount REAL,
    payload TEXT,
    processed INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_billing_events_key ON billing_events(provider,event_key);
CREATE INDEX IF NOT EXISTS idx_billing_events_org ON billing_events(organization_id,created_at);
CREATE INDEX IF NOT EXISTS idx_billing_events_pre ON billing_events(preapproval_id);
"""

V87_POSTGRES_EXTRA = r"""
CREATE TABLE IF NOT EXISTS billing_events (
    id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL DEFAULT 'mercadopago',
    event_key TEXT NOT NULL,
    preapproval_id TEXT,
    organization_id BIGINT,
    plan_code TEXT,
    status_provider TEXT,
    status_internal TEXT,
    amount DOUBLE PRECISION,
    payload TEXT,
    processed INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_billing_events_key ON billing_events(provider,event_key);
CREATE INDEX IF NOT EXISTS idx_billing_events_org ON billing_events(organization_id,created_at);
CREATE INDEX IF NOT EXISTS idx_billing_events_pre ON billing_events(preapproval_id);
"""


def initialize_schema(conn: Connection) -> None:
    conn.executescript(POSTGRES_SCHEMA if IS_POSTGRES else SQLITE_SCHEMA)
    conn.executescript(V7_POSTGRES_EXTRA if IS_POSTGRES else V7_SQLITE_EXTRA)
    conn.executescript(V81_POSTGRES_EXTRA if IS_POSTGRES else V81_SQLITE_EXTRA)
    conn.executescript(V85_POSTGRES_EXTRA if IS_POSTGRES else V85_SQLITE_EXTRA)
    conn.executescript(V87_POSTGRES_EXTRA if IS_POSTGRES else V87_SQLITE_EXTRA)
    ensure_column(conn, "plans", "ai_tier TEXT")
    ensure_column(conn, "plans", "monthly_ai_usd " + ("DOUBLE PRECISION" if IS_POSTGRES else "REAL"))
    ensure_column(conn, "subscriptions", "provider TEXT")
    ensure_column(conn, "subscriptions", "external_id TEXT")


# --------------------------------------------------------------------------
# JARBAS 8.8 — 2FA, recuperação de senha, retenção e RLS.
# --------------------------------------------------------------------------

V88_SQLITE_EXTRA = r"""
CREATE TABLE IF NOT EXISTS user_totp (
    user_id INTEGER PRIMARY KEY,
    secret TEXT NOT NULL,
    confirmed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_recovery_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    code_hash TEXT NOT NULL,
    used_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recovery_user ON user_recovery_codes(user_id);
CREATE TABLE IF NOT EXISTS password_resets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    token_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    requested_ip TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reset_token ON password_resets(token_hash);
CREATE TABLE IF NOT EXISTS totp_used (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    period INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_totp_used ON totp_used(user_id,period);
"""

V88_POSTGRES_EXTRA = r"""
CREATE TABLE IF NOT EXISTS user_totp (
    user_id BIGINT PRIMARY KEY,
    secret TEXT NOT NULL,
    confirmed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_recovery_codes (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    code_hash TEXT NOT NULL,
    used_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recovery_user ON user_recovery_codes(user_id);
CREATE TABLE IF NOT EXISTS password_resets (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    token_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    requested_ip TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reset_token ON password_resets(token_hash);
CREATE TABLE IF NOT EXISTS totp_used (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    code TEXT NOT NULL,
    period BIGINT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_totp_used ON totp_used(user_id,period);
"""


def initialize_schema(conn: Connection) -> None:
    conn.executescript(POSTGRES_SCHEMA if IS_POSTGRES else SQLITE_SCHEMA)
    conn.executescript(V7_POSTGRES_EXTRA if IS_POSTGRES else V7_SQLITE_EXTRA)
    conn.executescript(V81_POSTGRES_EXTRA if IS_POSTGRES else V81_SQLITE_EXTRA)
    conn.executescript(V85_POSTGRES_EXTRA if IS_POSTGRES else V85_SQLITE_EXTRA)
    conn.executescript(V87_POSTGRES_EXTRA if IS_POSTGRES else V87_SQLITE_EXTRA)
    conn.executescript(V88_POSTGRES_EXTRA if IS_POSTGRES else V88_SQLITE_EXTRA)
    conn.executescript(V89_POSTGRES_EXTRA if IS_POSTGRES else V89_SQLITE_EXTRA)
    conn.executescript(CHATBOT_POSTGRES_EXTRA if IS_POSTGRES else CHATBOT_SQLITE_EXTRA)
    ensure_column(conn, "plans", "ai_tier TEXT")
    ensure_column(conn, "plans", "monthly_ai_usd " + ("DOUBLE PRECISION" if IS_POSTGRES else "REAL"))
    ensure_column(conn, "subscriptions", "provider TEXT")
    ensure_column(conn, "subscriptions", "external_id TEXT")
    # Exclusão reversível: sem estas colunas o expurgo não tem o que ler.
    ensure_column(conn, "case_documents", "deleted_at TEXT")
    ensure_column(conn, "case_documents", "deleted_by INTEGER")
    ensure_column(conn, "document_chunks", "deleted_at TEXT")
    ensure_column(conn, "organizations", "require_2fa INTEGER")
    for definicao in COLUNAS_DEADLINES:
        ensure_column(conn, "deadlines", definicao)


# --------------------------------------------------------------------------
# Row Level Security (PostgreSQL).
#
# O isolamento entre escritórios hoje vive na aplicação, e está correto e
# testado. Mas é uma disciplina: basta UMA query futura escrita sem
# `organization_id` para vazar autos de um escritório para outro.
#
# O RLS é a rede embaixo. O banco passa a recusar linhas de outro escritório
# mesmo que a aplicação esqueça o filtro. Só vale para PostgreSQL — o SQLite
# não tem RLS, o que é mais um motivo para não operar multi-tenant nele.
#
# A aplicação precisa definir jarbas.org_id por transação:
#     SET LOCAL jarbas.org_id = '42';
# --------------------------------------------------------------------------

TABELAS_COM_TENANT = [
    "clients", "cases", "case_documents", "document_pages", "document_chunks",
    "drafts", "copilot_runs", "ai_council_runs", "usage_ledger",
    "chat_conversations", "chat_messages",
]


def rls_sql() -> str:
    partes = [
        "CREATE OR REPLACE FUNCTION jarbas_org_atual() RETURNS BIGINT AS $$",
        "  SELECT NULLIF(current_setting('jarbas.org_id', true), '')::BIGINT;",
        "$$ LANGUAGE sql STABLE;",
    ]
    for t in TABELAS_COM_TENANT:
        partes += [
            f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;",
            # FORCE: a política vale inclusive para o dono da tabela, senão o
            # usuário da aplicação (que costuma ser o dono) a ignora por completo.
            f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY;",
            f"DROP POLICY IF EXISTS {t}_tenant ON {t};",
            f"CREATE POLICY {t}_tenant ON {t} USING "
            f"(jarbas_org_atual() IS NULL OR organization_id = jarbas_org_atual()) "
            f"WITH CHECK (jarbas_org_atual() IS NULL OR organization_id = jarbas_org_atual());",
        ]
    return "\n".join(partes)


def aplicar_rls(conn: Connection) -> bool:
    """Idempotente. Sem efeito fora do PostgreSQL."""
    if not IS_POSTGRES:
        return False
    try:
        conn.executescript(rls_sql())
        return True
    except Exception:
        return False


def definir_org_da_sessao(conn: Connection, org_id: Optional[int]) -> None:
    """Fixa o escritório da transação. SET LOCAL morre com a transação, então
    uma conexão devolvida ao pool não carrega o tenant do request anterior."""
    if not IS_POSTGRES:
        return
    try:
        conn.execute("SELECT set_config('jarbas.org_id', ?, true)",
                     ("" if org_id is None else str(org_id),))
    except Exception:
        pass


# --------------------------------------------------------------------------
# JARBAS 8.9 — prazos processuais, movimentações e linha do tempo.
# --------------------------------------------------------------------------

V89_SQLITE_EXTRA = r"""
CREATE TABLE IF NOT EXISTS case_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    case_id INTEGER NOT NULL,
    event_number TEXT,
    occurred_at TEXT,
    code TEXT,
    description TEXT NOT NULL,
    actor TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    is_intimacao INTEGER NOT NULL DEFAULT 0,
    deadline_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mov_case ON case_movements(organization_id,case_id,occurred_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mov_unico
    ON case_movements(organization_id,case_id,source,event_number,occurred_at);
"""

V89_POSTGRES_EXTRA = r"""
CREATE TABLE IF NOT EXISTS case_movements (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL,
    case_id BIGINT NOT NULL,
    event_number TEXT,
    occurred_at TEXT,
    code TEXT,
    description TEXT NOT NULL,
    actor TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    is_intimacao INTEGER NOT NULL DEFAULT 0,
    deadline_id BIGINT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mov_case ON case_movements(organization_id,case_id,occurred_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mov_unico
    ON case_movements(organization_id,case_id,source,event_number,occurred_at);
"""

# Colunas que faltavam em deadlines. Sem elas o prazo era só um título e uma
# data: não dava para recalcular, auditar o fundamento nem alertar a tempo.
COLUNAS_DEADLINES = [
    "term_type TEXT",            # código do catálogo (contestacao, apelacao...)
    "start_date TEXT",           # termo inicial (data da intimação)
    "count_start TEXT",          # início efetivo da contagem (art. 224)
    "days INTEGER",              # quantidade de dias do prazo
    "business_days INTEGER",     # 1 = dias úteis (art. 219), 0 = corridos
    "legal_basis TEXT",          # fundamento: art. 335 do CPC, etc.
    "doubled INTEGER",           # prazo em dobro
    "alert_days INTEGER",        # antecedência do alerta, em dias úteis
    "responsible_user_id INTEGER",
    "protocol_date TEXT",        # quando foi efetivamente protocolado
    "notes TEXT",
    "source TEXT",               # manual | intake | datajud
    "movement_id INTEGER",       # movimentação que originou o prazo
]


# --------------------------------------------------------------------------
# Atendimento JARBAS — o chatbot do escritório (tela /atendimento).
#
# Duas tabelas, pelo mesmo motivo de sempre: a conversa é o registro que o
# advogado lê; a mensagem é o que foi dito. Guardar só o resumo perderia a
# transcrição, e a transcrição é o que sustenta a abertura do caso depois.
#
# `risco`, `area` e `fluxo` ficam desnormalizados na conversa de propósito:
# a fila de atendimento é ordenada por risco, e ordenar por um campo que só
# existe dentro do texto da última mensagem não é fila, é varredura.
# --------------------------------------------------------------------------

CHATBOT_SQLITE_EXTRA = r"""
CREATE TABLE IF NOT EXISTS chat_conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    user_id INTEGER,
    client_id INTEGER,
    lead_id INTEGER,
    canal TEXT NOT NULL DEFAULT 'whatsapp',
    contato_nome TEXT,
    contato_telefone TEXT,
    contato_email TEXT,
    fluxo TEXT,
    area TEXT,
    risco TEXT,
    status TEXT NOT NULL DEFAULT 'aberto',
    resumo TEXT,
    observacoes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(organization_id) REFERENCES organizations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chat_conv_org ON chat_conversations(organization_id,updated_at);
CREATE INDEX IF NOT EXISTS idx_chat_conv_status ON chat_conversations(organization_id,status,risco);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    conversation_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    model TEXT,
    alertas TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY(organization_id) REFERENCES organizations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chat_msg_conv ON chat_messages(organization_id,conversation_id,id);
"""

CHATBOT_POSTGRES_EXTRA = r"""
CREATE TABLE IF NOT EXISTS chat_conversations (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id BIGINT,
    client_id BIGINT,
    lead_id BIGINT,
    canal TEXT NOT NULL DEFAULT 'whatsapp',
    contato_nome TEXT,
    contato_telefone TEXT,
    contato_email TEXT,
    fluxo TEXT,
    area TEXT,
    risco TEXT,
    status TEXT NOT NULL DEFAULT 'aberto',
    resumo TEXT,
    observacoes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_conv_org ON chat_conversations(organization_id,updated_at);
CREATE INDEX IF NOT EXISTS idx_chat_conv_status ON chat_conversations(organization_id,status,risco);
CREATE TABLE IF NOT EXISTS chat_messages (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    conversation_id BIGINT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    model TEXT,
    alertas TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_msg_conv ON chat_messages(organization_id,conversation_id,id);
"""
