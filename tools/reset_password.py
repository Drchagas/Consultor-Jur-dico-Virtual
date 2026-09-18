from __future__ import annotations
import os,sys
from pathlib import Path
ROOT=Path(os.environ.get('JARBAS_ROOT',Path(__file__).resolve().parent.parent)).resolve(); sys.path.insert(0,str(ROOT))
from app import main
email=os.environ.get('JARBAS_RESET_EMAIL','admin@chagasadvogados.local').strip().lower(); pw=os.environ.get('JARBAS_RESET_PASSWORD','')
if len(pw)<12: raise SystemExit('Senha deve ter ao menos 12 caracteres.')
main.init_db()
with main.db() as conn:
    row=conn.execute('SELECT id FROM users WHERE lower(email)=lower(?)',(email,)).fetchone()
    if not row: raise SystemExit('Usuário não encontrado: '+email)
    conn.execute('UPDATE users SET password_hash=?,is_superadmin=CASE WHEN lower(email)=lower(?) THEN 1 ELSE is_superadmin END WHERE id=?',(main.hash_password(pw),'admin@chagasadvogados.local',row['id']))
print('JARBAS_PASSWORD_RESET_OK')
