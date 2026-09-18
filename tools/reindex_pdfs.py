from __future__ import annotations
import os,sys
from pathlib import Path
ROOT=Path(os.environ.get('JARBAS_ROOT',Path(__file__).resolve().parent)).resolve();sys.path.insert(0,str(ROOT))
from app.database import db
from app.copilot import index_pdf, resolve_uploaded_pdf_path
reindexed=skipped=missing=failed=0
with db() as conn:
    rows=conn.execute("SELECT id,organization_id,case_id,stored_path,stored_name,status FROM case_documents ORDER BY id").fetchall()
    for row in rows:
        path=resolve_uploaded_pdf_path(row['stored_path'] or '',org_id=row['organization_id'],case_id=row['case_id'],stored_name=row['stored_name'] or '')
        if not path or not path.is_file():
            missing+=1;print(f"PDF_MISSING id={row['id']} name={row['stored_name']}");continue
        try:
            result=index_pdf(conn,org_id=row['organization_id'],case_id=row['case_id'],document_id=row['id'],path=path)
            reindexed+=1
            print(f"PDF_REINDEX_OK id={row['id']} pages={result['page_count']} chars={result['text_chars']} chunks={result['chunks']} status={result['status']}")
        except Exception as exc:
            failed+=1
            conn.execute("UPDATE case_documents SET status='error',extraction_note=? WHERE id=? AND organization_id=?",(f"Falha no pipeline 8.3.1: {type(exc).__name__}: {str(exc)[:260]}",row['id'],row['organization_id']))
            print(f"PDF_REINDEX_ERROR id={row['id']} error={type(exc).__name__}:{str(exc)[:220]}")
print(f"PDF_REINDEX_SUMMARY reindexed={reindexed} missing={missing} failed={failed}")
if failed: sys.exit(3)
