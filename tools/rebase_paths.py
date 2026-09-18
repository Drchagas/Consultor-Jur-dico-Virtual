from __future__ import annotations
import sqlite3, sys
from pathlib import Path
if len(sys.argv) != 4: raise SystemExit('uso: rebase_paths.py DB OLD_ROOT NEW_ROOT')
db_path, old_root, new_root = map(str, sys.argv[1:])
old_root=str(Path(old_root)); new_root=str(Path(new_root))
con=sqlite3.connect(db_path); cur=con.cursor(); tables={r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
for table,column in [('case_documents','stored_path'),('generated_documents','stored_path'),('case_imports','temp_path'),('drafts','pdf_path')]:
    if table not in tables: continue
    try:
        rows=cur.execute(f'SELECT id,{column} FROM {table} WHERE {column} IS NOT NULL').fetchall()
        for rid,val in rows:
            if isinstance(val,str) and val.lower().startswith(old_root.lower()):
                cur.execute(f'UPDATE {table} SET {column}=? WHERE id=?',(new_root + val[len(old_root):],rid))
    except Exception: pass
con.commit(); con.close(); print('JARBAS_PATH_REBASE_OK')
