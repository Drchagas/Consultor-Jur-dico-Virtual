from __future__ import annotations
import os,re,sys,time
import httpx
port=os.environ.get('JARBAS_PORT','8765'); email=os.environ.get('JARBAS_ADMIN_EMAIL','admin@chagasadvogados.local'); pw=os.environ.get('JARBAS_LIVE_TEST_PASSWORD','')
if not pw: raise SystemExit('senha de teste ausente')
base=f'http://127.0.0.1:{port}'
last=''
for _ in range(45):
    try:
        with httpx.Client(base_url=base,follow_redirects=False,timeout=3) as c:
            h=c.get('/health')
            if h.status_code!=200: raise RuntimeError(f'health {h.status_code}')
            r=c.get('/login'); m=re.search(r'name="_csrf"[^>]*value="([^"]+)"',r.text) or re.search(r'value="([^"]+)"[^>]*name="_csrf"',r.text)
            if not m: raise RuntimeError('csrf não localizado')
            x=c.post('/login',data={'email':email,'password':pw,'_csrf':m.group(1)})
            if x.status_code!=303: raise RuntimeError(f'login HTTP {x.status_code}')
            y=c.get('/')
            if y.status_code!=200: raise RuntimeError(f'dashboard HTTP {y.status_code}')
            print('JARBAS_LIVE_LOGIN_OK'); raise SystemExit(0)
    except SystemExit: raise
    except Exception as exc:
        last=str(exc); time.sleep(1)
print('JARBAS_LIVE_LOGIN_ERROR='+last); raise SystemExit(3)
