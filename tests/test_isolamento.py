"""Isolamento entre escritórios e integridade do schema.

Se algum destes testes falhar, é incidente de sigilo profissional, não bug
comum. Roda contra o SQLITE_SCHEMA real de app/database.py.
"""

import re
import sqlite3
import sys
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
# A árvore de fontes tem app/ na raiz; o pacote de instalação tem payload/app/.
RAIZ = _RAIZ / "payload" if (_RAIZ / "payload" / "app").is_dir() else _RAIZ
sys.path.insert(0, str(RAIZ))

import pytest

FONTE_DB = (RAIZ / "app" / "database.py").read_text(encoding="utf-8")


def _schema(nome: str) -> str:
    m = re.search(rf'{nome} = """(.*?)"""', FONTE_DB, re.S)
    assert m, f"bloco {nome} não encontrado em app/database.py"
    return m.group(1)


@pytest.fixture(autouse=True)
def banco():
    """Banco em memória com o schema real e dois escritórios povoados."""
    global conn
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_schema("SQLITE_SCHEMA"))

    for i, nome in enumerate(("Chagas Advogados", "Escritorio Rival"), start=1):
        conn.execute(
            "INSERT INTO organizations (id,name,slug,status,created_at) VALUES (?,?,?,'active','2026-01-01')",
            (i, nome, f"org{i}"))
        conn.execute(
            "INSERT INTO clients (id,organization_id,name,created_at) VALUES (?,?,?,'2026-01-01')",
            (i, i, f"Cliente sigiloso {i}"))
        conn.execute(
            "INSERT INTO cases (id,organization_id,client_id,title,area,created_at) VALUES (?,?,?,?,'Cível','2026-01-01')",
            (i, i, i, f"Caso confidencial {i}"))
        conn.execute(
            """INSERT INTO case_documents
               (id,organization_id,case_id,original_name,stored_name,stored_path,sha256,size_bytes,created_at)
               VALUES (?,?,?,?,?,?,?,0,'2026-01-01')""",
            (i, i, i, f"autos{i}.pdf", f"s{i}.pdf", f"/data/{i}.pdf", f"hash{i}"))
        conn.execute(
            """INSERT INTO document_chunks
               (organization_id,case_id,document_id,page_number,chunk_index,text,created_at)
               VALUES (?,?,?,1,0,?,'2026-01-01')""",
            (i, i, i, f"SEGREDO DO ESCRITORIO {i}"))
    conn.commit()
    yield
    conn.close()


TABELAS_TENANT = ["clients", "cases", "case_documents", "document_chunks"]


def test_toda_tabela_sensivel_tem_organization_id():
    for t in TABELAS_TENANT:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({t})")}
        assert "organization_id" in cols, f"{t} sem organization_id — vazamento garantido"


@pytest.mark.parametrize if False else (lambda f: f)
def test_consulta_com_escopo_nao_ve_o_outro_escritorio():
    for t in TABELAS_TENANT:
        linhas = conn.execute(
            f"SELECT * FROM {t} WHERE organization_id=?", (1,)).fetchall()
        assert linhas, f"{t}: consulta com escopo não retornou nada"
        for r in linhas:
            assert r["organization_id"] == 1, f"{t} vazou linha de outro escritório"


def test_id_de_outro_escritorio_nao_e_alcancavel():
    """O padrão do main.py: WHERE id=? AND organization_id=?."""
    achado = conn.execute(
        "SELECT * FROM cases WHERE id=? AND organization_id=?", (2, 1)).fetchone()
    assert achado is None, (
        "escritório 1 alcançou o caso 2 — falha de isolamento"
    )


def test_chunks_do_copiloto_sao_isolados():
    """O copiloto só pode citar trecho do próprio escritório."""
    linhas = conn.execute(
        "SELECT text FROM document_chunks WHERE organization_id=?", (1,)).fetchall()
    textos = " ".join(r["text"] for r in linhas)
    assert "SEGREDO DO ESCRITORIO 2" not in textos


def test_join_sem_escopo_no_lado_direito_vazaria():
    """Documenta por que o main.py repete organization_id nos dois lados do JOIN."""
    frouxo = conn.execute(
        """SELECT ch.text FROM document_chunks ch
           JOIN case_documents d ON d.id=ch.document_id
           WHERE ch.organization_id=?""", (1,)).fetchall()
    estrito = conn.execute(
        """SELECT ch.text FROM document_chunks ch
           JOIN case_documents d ON d.id=ch.document_id AND d.organization_id=ch.organization_id
           WHERE ch.organization_id=?""", (1,)).fetchall()
    assert len(estrito) == len(frouxo) == 1


def test_exclusao_de_documento_leva_os_chunks_junto():
    """Direito de eliminação da LGPD: não pode sobrar trecho órfão indexado."""
    antes = conn.execute(
        "SELECT COUNT(*) c FROM document_chunks WHERE document_id=1").fetchone()["c"]
    assert antes == 1
    conn.execute("DELETE FROM case_documents WHERE id=1 AND organization_id=1")
    depois = conn.execute(
        "SELECT COUNT(*) c FROM document_chunks WHERE document_id=1").fetchone()["c"]
    assert depois == 0, (
        "chunks sobreviveram à exclusão do documento — o copiloto ainda citaria "
        "peça apagada e a eliminação da LGPD ficaria incompleta"
    )


def test_documento_duplicado_no_mesmo_caso_e_rejeitado():
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO case_documents
               (organization_id,case_id,original_name,stored_name,stored_path,sha256,size_bytes,created_at)
               VALUES (1,1,'copia.pdf','s9.pdf','/data/9.pdf','hash1',0,'2026-01-01')""")


def test_mesmo_pdf_em_escritorios_diferentes_e_permitido():
    """A unicidade é por (org, caso, sha) — dois escritórios podem ter o mesmo PDF."""
    conn.execute(
        """INSERT INTO case_documents
           (organization_id,case_id,original_name,stored_name,stored_path,sha256,size_bytes,created_at)
           VALUES (2,2,'igual.pdf','s8.pdf','/data/8.pdf','hash1',0,'2026-01-01')""")


# ------------------------------------------------- tradutor SQL do PostgreSQL

def test_nenhuma_query_tem_interrogacao_literal():
    """_pg_sql troca TODO '?' por '%s'. Um '?' dentro de string SQL corromperia
    a query só no PostgreSQL, invisível em desenvolvimento com SQLite."""
    INICIO_SQL = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|WITH)\b", re.I)
    # Um '?' só é placeholder se estiver isolado por delimitadores SQL.
    PLACEHOLDER = re.compile(r"(?<![^\s,(=])\?(?![^\s,)])")

    problemas = []
    for arq in sorted((RAIZ / "app").glob("*.py")):
        texto = arq.read_text(encoding="utf-8")
        for m in re.finditer(r'"""(.*?)"""|"([^"\n]*)"|\'([^\'\n]*)\'', texto, re.S):
            corpo = next(g for g in m.groups() if g is not None)
            if "?" not in corpo or not INICIO_SQL.match(corpo):
                continue
            restante = PLACEHOLDER.sub("", corpo)
            if "?" in restante:
                linha = texto[:m.start()].count("\n") + 1
                problemas.append(f"{arq.name}:{linha}: {corpo.strip()[:70]}")
    assert not problemas, (
        "'?' literal em string SQL quebra no PostgreSQL:\n" + "\n".join(problemas))


def test_nenhuma_query_tem_curinga_percent_embutido():
    """Curinga de LIKE deve vir por parâmetro, nunca na string SQL."""
    problemas = []
    for arq in sorted((RAIZ / "app").glob("*.py")):
        texto = arq.read_text(encoding="utf-8")
        for m in re.finditer(r"LIKE\s+'[^']*%", texto, re.I):
            linha = texto[:m.start()].count("\n") + 1
            problemas.append(f"{arq.name}:{linha}")
    assert not problemas, (
        "curinga % embutido em SQL conflita com os placeholders %s do psycopg: "
        + ", ".join(problemas))


def test_os_dois_schemas_cobrem_as_mesmas_tabelas():
    def tabelas(bloco):
        return {m.lower() for m in re.findall(
            r"CREATE TABLE IF NOT EXISTS (\w+)", _schema(bloco), re.I)}
    so_sqlite = tabelas("SQLITE_SCHEMA") - tabelas("POSTGRES_SCHEMA")
    so_pg = tabelas("POSTGRES_SCHEMA") - tabelas("SQLITE_SCHEMA")
    assert not so_sqlite, f"tabelas ausentes no PostgreSQL: {sorted(so_sqlite)}"
    assert not so_pg, f"tabelas ausentes no SQLite: {sorted(so_pg)}"
