from __future__ import annotations
import os,sqlite3,sys
from pathlib import Path
root=Path(os.environ.get("JARBAS_ROOT",Path(__file__).resolve().parent.parent))
db=root/"data"/"jarbas.db"
if not db.exists():
 print("DB=AUSENTE"); raise SystemExit(0)
con=sqlite3.connect(str(db));cur=con.cursor()
print("DB_INTEGRITY="+str(cur.execute("PRAGMA integrity_check").fetchone()[0]))
tables={r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
def count(t):
 return cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] if t in tables else 0
version="n/a"
if "app_meta" in tables:
 r=cur.execute("SELECT value FROM app_meta WHERE key='schema_version'").fetchone();version=r[0] if r else "n/a"
print("SCHEMA_VERSION="+str(version))
for t in ["users","organizations","clients","cases","case_documents","document_pages","document_chunks","financial_transactions","fee_contracts","generated_documents","ai_threads","ai_messages"]:
 print(t.upper()+"="+str(count(t)))
con.close()
