from __future__ import annotations
import sqlite3,sys
from pathlib import Path
TABLE_WEIGHTS={'clients':50,'cases':80,'case_documents':60,'document_pages':1,'document_chunks':1,'financial_transactions':35,'finance':20,'fee_contracts':50,'financial_payments':25,'generated_documents':30,'ai_messages':2,'activities':8,'leads':8,'time_entries':5}
def score(path:Path):
    s=int(path.stat().st_size/1024);details=[]
    try:
        con=sqlite3.connect(str(path));cur=con.cursor();tables={r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table,w in TABLE_WEIGHTS.items():
            if table in tables:
                try:n=int(cur.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]);s+=n*w;details.append(f'{table}:{n}')
                except Exception:pass
        ok=cur.execute('PRAGMA integrity_check').fetchone()[0];con.close()
        if ok!='ok':return -1,f'integrity:{ok}'
    except Exception as exc:return -1,f'erro:{type(exc).__name__}'
    return s,','.join(details)
best=None
for arg in sys.argv[1:]:
    p=Path(arg)
    if p.is_file():
        sc,detail=score(p);mtime=p.stat().st_mtime;stable_bonus=1 if p.parent.parent.name=='JARBAS_Enterprise' else 0;print(f'CANDIDATE={p}|{sc}|{detail}')
        key=(sc,stable_bonus,mtime)
        if sc>=0 and (best is None or key>best[0]):best=(key,p)
print(f'BEST={best[1]}' if best else 'BEST=')
