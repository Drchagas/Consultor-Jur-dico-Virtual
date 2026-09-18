from __future__ import annotations
import os, sys
from pathlib import Path
ROOT=Path(os.environ.get('JARBAS_ROOT',Path(__file__).resolve().parent)).resolve();sys.path.insert(0,str(ROOT))
from app.database import db
from app.copilot import resolve_uploaded_pdf_path
from app.pdf_pipeline import extract_pdf, ocr_capability
from app.ai_gateway import connection_status

print('--- PDF ENGINE 8.3.1 ---')
print('OCR_LOCAL=',ocr_capability())
print('OPENAI=',connection_status())
with db() as conn:
    rows=conn.execute("SELECT id,organization_id,case_id,original_name,stored_name,stored_path,status,page_count,text_chars,extraction_note FROM case_documents ORDER BY id DESC LIMIT 100").fetchall()
    print('DOCUMENTOS=',len(rows))
    for r in rows:
        path=resolve_uploaded_pdf_path(r['stored_path'] or '',org_id=r['organization_id'],case_id=r['case_id'],stored_name=r['stored_name'] or '')
        exists=bool(path and path.is_file())
        dbs=conn.execute("SELECT COUNT(*) pages,COALESCE(SUM(CASE WHEN LENGTH(TRIM(COALESCE(text,'')))>=30 THEN 1 ELSE 0 END),0) text_pages,COALESCE(SUM(LENGTH(COALESCE(text,''))),0) chars FROM document_pages WHERE organization_id=? AND case_id=? AND document_id=?",(r['organization_id'],r['case_id'],r['id'])).fetchone()
        chunks=conn.execute("SELECT COUNT(*) c FROM document_chunks WHERE organization_id=? AND case_id=? AND document_id=?",(r['organization_id'],r['case_id'],r['id'])).fetchone()['c']
        db_pages=int(dbs['pages'] or 0); db_text_pages=int(dbs['text_pages'] or 0); db_chars=int(dbs['chars'] or 0); db_cov=(db_text_pages/db_pages if db_pages else 0)
        print(f"PDF id={r['id']} case={r['case_id']} name={r['original_name']} status_db={r['status']} declared_pages={r['page_count']} indexed_pages={db_pages} indexed_text_pages={db_text_pages} db_coverage={db_cov:.2f} chars_db={db_chars} chunks={chunks} file={exists} path={path or ''}")
        if r['extraction_note']:
            print(f"  DB_NOTE {r['extraction_note']}")
        if exists:
            try:
                x=extract_pdf(path,max_pages=8,tail_pages=3,max_chars=50000,allow_local_ocr=True)
                print(f"  LIVE_SAMPLE status={x.status} total_pages={x.page_count} sampled={len(x.pages)} chars={x.text_chars} coverage={x.coverage:.2f} engines={x.engine_summary}")
                print(f"  LIVE_NOTE {x.note}")
                if db_pages != int(r['page_count'] or 0) or (db_pages and db_cov < 0.55 and x.coverage >= 0.80):
                    print('  ACTION REINDEX_RECOMMENDED=1')
            except Exception as exc:
                print(f"  ERROR {type(exc).__name__}: {str(exc)[:500]}")
