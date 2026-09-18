"""Busca textual indexada nos autos.

O que havia antes: `search_case` carregava TODOS os chunks do caso para a
memória do Python e pontuava um por um, a cada consulta. Num caso de 766
páginas já pesa; em alguns milhares, trava o processo inteiro. E a pontuação
era puramente lexical: "nulidade da prova" não encontrava "ilicitude
probatória".

O que há agora: FTS5 no SQLite e `to_tsvector`/`ts_rank` no PostgreSQL, os
dois com stemming em português (radicalização: "probatória" casa com
"probatório", "nulidades" com "nulidade"). O ranking sai do banco, que lê só
as linhas que casam.

Se o FTS não estiver disponível — SQLite compilado sem FTS5, índice ainda não
construído — a busca cai para a varredura antiga. Melhor devolver resultado
lento que devolver erro no meio de uma análise.
"""

from __future__ import annotations

import re
import unicodedata

from .database import IS_POSTGRES, db

# Palavras que só adicionam ruído numa consulta jurídica.
VAZIAS = {
    "a", "o", "as", "os", "de", "da", "do", "das", "dos", "e", "ou", "que",
    "em", "no", "na", "nos", "nas", "um", "uma", "por", "para", "com", "se",
    "ao", "aos", "à", "às", "pelo", "pela", "como", "mais", "foi", "ser",
}


def fts_disponivel(conn) -> bool:
    if IS_POSTGRES:
        return True
    try:
        conn.execute("SELECT 1 FROM document_chunks_fts LIMIT 1").fetchone()
        return True
    except Exception:
        return False


# --------------------------------------------------------------------- SQLite

SQLITE_FTS = r"""
CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts USING fts5(
    text,
    content='document_chunks',
    content_rowid='id',
    tokenize="unicode61 remove_diacritics 2"
);

CREATE TRIGGER IF NOT EXISTS document_chunks_ai AFTER INSERT ON document_chunks BEGIN
    INSERT INTO document_chunks_fts(rowid,text) VALUES (new.id,new.text);
END;
CREATE TRIGGER IF NOT EXISTS document_chunks_ad AFTER DELETE ON document_chunks BEGIN
    INSERT INTO document_chunks_fts(document_chunks_fts,rowid,text)
    VALUES ('delete',old.id,old.text);
END;
CREATE TRIGGER IF NOT EXISTS document_chunks_au AFTER UPDATE ON document_chunks BEGIN
    INSERT INTO document_chunks_fts(document_chunks_fts,rowid,text)
    VALUES ('delete',old.id,old.text);
    INSERT INTO document_chunks_fts(rowid,text) VALUES (new.id,new.text);
END;
"""


def preparar_indice(conn) -> bool:
    """Cria a tabela FTS e os gatilhos. Idempotente; devolve se deu certo."""
    if IS_POSTGRES:
        try:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chunks_fts ON document_chunks "
                "USING GIN (to_tsvector('portuguese', text))")
            return True
        except Exception:
            return False
    try:
        conn.executescript(SQLITE_FTS)
        return True
    except Exception:
        return False   # SQLite sem FTS5 compilado


def reconstruir_indice(conn) -> int:
    """Repovoa o índice a partir dos chunks. Necessário uma vez ao migrar,
    porque os gatilhos só pegam linhas gravadas depois deles."""
    if IS_POSTGRES:
        return 0
    if not preparar_indice(conn):
        return 0
    conn.execute("INSERT INTO document_chunks_fts(document_chunks_fts) VALUES ('rebuild')")
    row = conn.execute("SELECT COUNT(*) n FROM document_chunks_fts").fetchone()
    return int(row["n"] if hasattr(row, "keys") else row[0])


# ------------------------------------------------------------------ consulta

def _normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in texto if not unicodedata.combining(c))


def termos(consulta: str) -> list[str]:
    brutos = re.findall(r"[0-9A-Za-zÀ-ÿ]{2,}", consulta or "")
    saida = []
    for t in brutos:
        if t.lower() in VAZIAS:
            continue
        limpo = _normalizar(t).lower()
        if limpo and limpo not in saida:
            saida.append(limpo)
    return saida


def consulta_fts(consulta: str) -> str:
    """Monta a expressão MATCH do FTS5.

    Cada termo vira prefixo (`nulidad*`) para aproximar flexão, e todos são
    aspeados: sem isso, um termo como `AND` ou um parêntese vindo do usuário
    é interpretado como sintaxe e derruba a query com erro.
    """
    ts = termos(consulta)
    if not ts:
        return ""
    return " OR ".join(f'"{t}"*' for t in ts)


def buscar(conn, *, org_id: int, case_id: int, consulta: str,
           limite: int = 10) -> list[dict] | None:
    """Busca indexada. Devolve None quando o índice não está disponível,
    para o chamador cair no método antigo."""
    if not consulta or not consulta.strip():
        return []

    if IS_POSTGRES:
        linhas = conn.execute(
            """SELECT ch.id,ch.document_id,ch.page_number,ch.chunk_index,ch.text,
                      ch.label,d.original_name,
                      ts_rank(to_tsvector('portuguese',ch.text),
                              plainto_tsquery('portuguese',?)) AS rank
               FROM document_chunks ch
               JOIN case_documents d
                 ON d.id=ch.document_id AND d.organization_id=ch.organization_id
               WHERE ch.organization_id=? AND ch.case_id=?
                 AND to_tsvector('portuguese',ch.text) @@ plainto_tsquery('portuguese',?)
               ORDER BY rank DESC, ch.page_number, ch.chunk_index
               LIMIT ?""",
            (consulta, org_id, case_id, consulta, limite),
        ).fetchall()
    else:
        expressao = consulta_fts(consulta)
        if not expressao:
            return []
        try:
            linhas = conn.execute(
                """SELECT ch.id,ch.document_id,ch.page_number,ch.chunk_index,ch.text,
                          ch.label,d.original_name,
                          -bm25(document_chunks_fts) AS rank
                   FROM document_chunks_fts f
                   JOIN document_chunks ch ON ch.id=f.rowid
                   JOIN case_documents d
                     ON d.id=ch.document_id AND d.organization_id=ch.organization_id
                   WHERE document_chunks_fts MATCH ?
                     AND ch.organization_id=? AND ch.case_id=?
                   ORDER BY rank DESC, ch.page_number, ch.chunk_index
                   LIMIT ?""",
                (expressao, org_id, case_id, limite),
            ).fetchall()
        except Exception:
            return None   # índice ausente: chamador usa o método antigo

    return [{
        "score": round(float(r["rank"] or 0), 4),
        "chunk_id": r["id"],
        "document_id": r["document_id"],
        "document": r["original_name"],
        "page": r["page_number"],
        "label": r["label"],
        "text": (r["text"] or "").strip(),
        "snippet": (r["text"] or "").strip()[:1500],
        "citation": f"[{r['original_name']} · p. {r['page_number']}]",
    } for r in linhas]


def estatisticas(org_id: int | None = None) -> dict:
    """Para a tela de diagnóstico."""
    with db() as conn:
        disponivel = fts_disponivel(conn)
        chunks = conn.execute("SELECT COUNT(*) n FROM document_chunks").fetchone()["n"]
        indexados = 0
        if disponivel and not IS_POSTGRES:
            try:
                indexados = conn.execute(
                    "SELECT COUNT(*) n FROM document_chunks_fts").fetchone()["n"]
            except Exception:
                indexados = 0
    return {
        "motor": "postgresql" if IS_POSTGRES else ("fts5" if disponivel else "varredura"),
        "disponivel": disponivel,
        "chunks": chunks,
        "indexados": indexados if not IS_POSTGRES else chunks,
        "completo": IS_POSTGRES or indexados >= chunks,
    }
