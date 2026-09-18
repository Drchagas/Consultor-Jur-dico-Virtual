"""Retenção e eliminação de dados (LGPD, art. 16 e art. 18, VI).

Três problemas que existiam:

1. Exclusão de documento era destrutiva e imediata. Um clique errado apagava
   PDF de processo em andamento, sem volta.
2. Nada era jamais eliminado de fato. "Não apagar nunca" não é conservador:
   é reter dado pessoal de terceiros — vítimas, testemunhas, menores — sem
   prazo nem finalidade, o que a LGPD não permite.
3. Um documento excluído deixava vestígio nas minutas e no índice.

O desenho: exclusão marca `deleted_at` e some da interface; o expurgo remove
para valer depois da carência. Entre os dois há uma janela de arrependimento.

A carência padrão é 30 dias. O prazo de guarda de documento de cliente após o
fim do mandato é decisão do advogado, não deste código — o Provimento
188/2018 do CFOAB e as normas de guarda documental é que orientam isso.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

from .database import db


def dias_carencia() -> int:
    """Janela entre excluir e eliminar definitivamente."""
    try:
        return max(0, int(os.getenv("JARBAS_RETENCAO_LIXEIRA_DIAS", "30")))
    except ValueError:
        return 30


def _agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------- exclusão reversível

def excluir_documento(org_id: int, document_id: int, user_id: int | None = None) -> bool:
    """Move para a lixeira. O arquivo e os chunks continuam até o expurgo.

    Os chunks são marcados junto para o Copiloto parar de citar o documento
    imediatamente — quem excluiu não espera vê-lo citado numa análise feita
    logo depois.
    """
    agora = _agora()
    with db() as conn:
        alvo = conn.execute(
            "SELECT id FROM case_documents WHERE id=? AND organization_id=? AND deleted_at IS NULL",
            (document_id, org_id)).fetchone()
        if not alvo:
            return False
        conn.execute(
            "UPDATE case_documents SET deleted_at=?,deleted_by=? WHERE id=? AND organization_id=?",
            (agora, user_id, document_id, org_id))
        conn.execute(
            "UPDATE document_chunks SET deleted_at=? WHERE document_id=? AND organization_id=?",
            (agora, document_id, org_id))
    return True


def restaurar_documento(org_id: int, document_id: int) -> bool:
    with db() as conn:
        alvo = conn.execute(
            "SELECT id FROM case_documents WHERE id=? AND organization_id=? AND deleted_at IS NOT NULL",
            (document_id, org_id)).fetchone()
        if not alvo:
            return False
        conn.execute(
            "UPDATE case_documents SET deleted_at=NULL,deleted_by=NULL WHERE id=? AND organization_id=?",
            (document_id, org_id))
        conn.execute(
            "UPDATE document_chunks SET deleted_at=NULL WHERE document_id=? AND organization_id=?",
            (document_id, org_id))
    return True


def lixeira(org_id: int) -> list[dict]:
    limite = (datetime.now() - timedelta(days=dias_carencia())).isoformat(timespec="seconds")
    with db() as conn:
        linhas = conn.execute(
            """SELECT id,case_id,original_name,size_bytes,deleted_at,stored_path
               FROM case_documents
               WHERE organization_id=? AND deleted_at IS NOT NULL
               ORDER BY deleted_at DESC""", (org_id,)).fetchall()
    saida = []
    for r in linhas:
        saida.append({
            "id": r["id"], "case_id": r["case_id"], "nome": r["original_name"],
            "bytes": r["size_bytes"], "excluido_em": r["deleted_at"],
            "expurgo_iminente": (r["deleted_at"] or "") <= limite,
        })
    return saida


# ------------------------------------------------------------------ expurgo

def expurgar(org_id: int | None = None, *, forcar: bool = False,
             upload_root: Path | None = None) -> dict:
    """Remove definitivamente o que passou da carência.

    forcar=True ignora a carência. Usado pela eliminação a pedido do titular
    (art. 18, VI), que não espera 30 dias.
    """
    limite = (datetime.now() - timedelta(days=0 if forcar else dias_carencia())
              ).isoformat(timespec="seconds")
    raiz = upload_root or Path(os.getenv("JARBAS_UPLOAD_ROOT", "")) or None

    onde = "deleted_at IS NOT NULL AND deleted_at<=?"
    args: list = [limite]
    if org_id is not None:
        onde += " AND organization_id=?"
        args.append(org_id)

    with db() as conn:
        alvos = conn.execute(
            f"SELECT id,organization_id,stored_path,size_bytes FROM case_documents WHERE {onde}",
            tuple(args)).fetchall()

        arquivos_removidos = bytes_liberados = 0
        erros: list[str] = []
        for r in alvos:
            caminho = r["stored_path"] or ""
            if caminho:
                try:
                    p = Path(caminho)
                    if raiz is not None and not str(p.resolve()).startswith(str(Path(raiz).resolve())):
                        erros.append(f"documento {r['id']}: caminho fora da raiz de uploads")
                        continue
                    if p.is_file():
                        p.unlink()
                        arquivos_removidos += 1
                        bytes_liberados += int(r["size_bytes"] or 0)
                except OSError as exc:
                    erros.append(f"documento {r['id']}: {exc}")

        ids = [r["id"] for r in alvos]
        for i in range(0, len(ids), 500):     # lote: evita SQL gigante
            lote = ids[i:i + 500]
            marcas = ",".join("?" * len(lote))
            conn.execute(f"DELETE FROM document_chunks WHERE document_id IN ({marcas})", tuple(lote))
            conn.execute(f"DELETE FROM document_pages WHERE document_id IN ({marcas})", tuple(lote))
            conn.execute(f"DELETE FROM case_documents WHERE id IN ({marcas})", tuple(lote))

    return {
        "documentos": len(ids),
        "arquivos_removidos": arquivos_removidos,
        "bytes_liberados": bytes_liberados,
        "erros": erros,
        "forcado": forcar,
    }


def eliminar_a_pedido_do_titular(org_id: int, case_id: int) -> dict:
    """Art. 18, VI: eliminação imediata dos documentos de um processo.

    Não apaga o registro do caso nem os lançamentos financeiros: há dever
    legal de guarda contábil e fiscal que a LGPD ressalva no art. 16, I.
    """
    agora = _agora()
    with db() as conn:
        conn.execute(
            """UPDATE case_documents SET deleted_at=?
               WHERE organization_id=? AND case_id=? AND deleted_at IS NULL""",
            (agora, org_id, case_id))
        conn.execute(
            """UPDATE document_chunks SET deleted_at=?
               WHERE organization_id=? AND case_id=? AND deleted_at IS NULL""",
            (agora, org_id, case_id))
    return expurgar(org_id, forcar=True)


def resumo(org_id: int) -> dict:
    with db() as conn:
        ativos = conn.execute(
            """SELECT COUNT(*) n, COALESCE(SUM(size_bytes),0) b FROM case_documents
               WHERE organization_id=? AND deleted_at IS NULL""", (org_id,)).fetchone()
        na_lixeira = conn.execute(
            """SELECT COUNT(*) n, COALESCE(SUM(size_bytes),0) b FROM case_documents
               WHERE organization_id=? AND deleted_at IS NOT NULL""", (org_id,)).fetchone()
    return {
        "ativos": {"documentos": ativos["n"], "bytes": ativos["b"]},
        "lixeira": {"documentos": na_lixeira["n"], "bytes": na_lixeira["b"]},
        "carencia_dias": dias_carencia(),
    }
