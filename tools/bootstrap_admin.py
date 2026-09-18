from __future__ import annotations
import os, sys, traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(os.environ.get('JARBAS_ROOT', Path(__file__).resolve().parent.parent)).resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app import main

admin_email = os.environ.get('JARBAS_ADMIN_EMAIL', 'admin@chagasadvogados.local').strip().lower()
admin_password = os.environ.get('JARBAS_BOOTSTRAP_ADMIN_PASSWORD', '')
dev_email = os.environ.get('JARBAS_DEVELOPER_EMAIL', 'developer@jarbas.local').strip().lower()
dev_password = os.environ.get('JARBAS_BOOTSTRAP_DEVELOPER_PASSWORD', '')
if len(admin_password) < 12:
    raise SystemExit('ERRO: senha administrativa ausente ou menor que 12 caracteres.')
if dev_password and len(dev_password) < 16:
    raise SystemExit('ERRO: senha de desenvolvedor deve ter ao menos 16 caracteres.')

# Compatibilidade com init_db: a senha de bootstrap é efêmera e não é gravada
# no .env.local. Ela existe apenas neste processo de instalação.
os.environ['JARBAS_ADMIN_PASSWORD'] = admin_password
try:
    main.init_db()
except Exception:
    print('JARBAS_BOOTSTRAP_INIT_ERROR', file=sys.stderr)
    traceback.print_exc()
    raise SystemExit(41)
now = datetime.now().isoformat(timespec='seconds')
with main.db() as conn:
    admin = conn.execute('SELECT * FROM users WHERE lower(email)=lower(?)', (admin_email,)).fetchone()
    if not admin:
        admin_id = conn.insert_id(
            'INSERT INTO users (name,email,password_hash,role,created_at,is_superadmin) VALUES (?,?,?,?,?,1)',
            ('Administrador JARBAS', admin_email, main.hash_password(admin_password), 'admin', now),
        )
    else:
        admin_id = admin['id']
        conn.execute('UPDATE users SET email=?,password_hash=?,is_superadmin=1,role=? WHERE id=?',
                     (admin_email, main.hash_password(admin_password), 'admin', admin_id))
    org = conn.execute("SELECT * FROM organizations WHERE slug='chagas-advogados' ORDER BY id LIMIT 1").fetchone()
    if not org:
        org = conn.execute('SELECT * FROM organizations ORDER BY id LIMIT 1').fetchone()
    if org:
        conn.execute("INSERT OR IGNORE INTO memberships (user_id,organization_id,role,is_active,created_at) VALUES (?,?,'owner',1,?)",
                     (admin_id, org['id'], now))
        conn.execute("UPDATE memberships SET role='owner',is_active=1 WHERE user_id=? AND organization_id=?", (admin_id, org['id']))
    if dev_password:
        dev = conn.execute('SELECT * FROM users WHERE lower(email)=lower(?)', (dev_email,)).fetchone()
        if dev:
            dev_id = dev['id']
            conn.execute('UPDATE users SET email=?,password_hash=?,is_superadmin=1,role=? WHERE id=?',
                         (dev_email, main.hash_password(dev_password), 'admin', dev_id))
        else:
            dev_id = conn.insert_id(
                'INSERT INTO users (name,email,password_hash,role,created_at,is_superadmin) VALUES (?,?,?,?,?,1)',
                ('Desenvolvedor JARBAS Local', dev_email, main.hash_password(dev_password), 'admin', now),
            )
        if org:
            conn.execute("INSERT OR IGNORE INTO memberships (user_id,organization_id,role,is_active,created_at) VALUES (?,?,'owner',1,?)",
                         (dev_id, org['id'], now))
            conn.execute("UPDATE memberships SET role='owner',is_active=1 WHERE user_id=? AND organization_id=?", (dev_id, org['id']))
print('JARBAS_BOOTSTRAP_ADMIN_OK')
