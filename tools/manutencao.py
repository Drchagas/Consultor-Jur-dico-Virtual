#!/usr/bin/env python3
"""Manutencao periodica do JARBAS: expurgo, indice e RLS.

Rode semanalmente (Agendador de Tarefas do Windows / cron):
    python tools/manutencao.py --expurgo --reindexar
"""
from __future__ import annotations
import argparse, os, sys
from pathlib import Path

ROOT = Path(os.environ.get("JARBAS_ROOT", Path(__file__).resolve().parent.parent)).resolve()
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description="Manutencao do JARBAS")
    ap.add_argument("--expurgo", action="store_true", help="elimina o que passou da carencia")
    ap.add_argument("--reindexar", action="store_true", help="reconstroi o indice de busca")
    ap.add_argument("--rls", action="store_true", help="aplica RLS (PostgreSQL)")
    ap.add_argument("--renomear", action="store_true",
                    help="troca nomes de download de tribunal pelo numero do processo")
    ap.add_argument("--org", type=int, default=None, help="limitar a um escritorio")
    a = ap.parse_args()
    if not (a.expurgo or a.reindexar or a.rls or a.renomear):
        ap.print_help()
        return 1

    from app import main as m
    from app.database import db, aplicar_rls

    m.init_db()

    if a.reindexar:
        from app import search_index
        with db() as conn:
            n = search_index.reconstruir_indice(conn)
        print(f"indice de busca: {n} chunks indexados")

    if a.rls:
        with db() as conn:
            print("RLS aplicado" if aplicar_rls(conn) else "RLS ignorado (nao e PostgreSQL)")

    if a.renomear:
        from app import nome_documento as ND
        with db() as conn:
            onde = "WHERE organization_id=?" if a.org else "WHERE 1=1"
            args = (a.org,) if a.org else ()
            docs = conn.execute(
                f"SELECT id,organization_id,case_id,original_name FROM case_documents {onde}",
                args).fetchall()
            trocados = 0
            for d in docs:
                if not ND.precisa_renomear(d["original_name"]):
                    continue
                pgs = conn.execute(
                    """SELECT text FROM document_pages
                       WHERE organization_id=? AND document_id=? ORDER BY page_number LIMIT 3""",
                    (d["organization_id"], d["id"])).fetchall()
                texto = "\n".join((r["text"] or "") for r in pgs)
                caso = conn.execute("SELECT number FROM cases WHERE id=? AND organization_id=?",
                                    (d["case_id"], d["organization_id"])).fetchone()
                novo = ND.nome_amigavel(d["original_name"], texto,
                                        (caso["number"] if caso else "") or "")
                if novo and novo != d["original_name"]:
                    conn.execute("UPDATE case_documents SET original_name=? WHERE id=?",
                                 (novo, d["id"]))
                    print(f"  #{d['id']}: {d['original_name'][:48]}... -> {novo}")
                    trocados += 1
        print(f"renomeados: {trocados} de {len(docs)} documentos")

    if a.expurgo:
        from app import retencao
        r = retencao.expurgar(a.org, upload_root=m.UPLOAD_ROOT)
        print(f"expurgo: {r['documentos']} documentos, {r['arquivos_removidos']} arquivos, "
              f"{r['bytes_liberados']/1024/1024:.1f} MB liberados")
        for e in r["erros"]:
            print("  erro:", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
