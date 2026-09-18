from __future__ import annotations
import os,re,sys,compileall,tempfile,shutil
from pathlib import Path
ROOT=Path(os.environ.get('JARBAS_ROOT',Path(__file__).resolve().parent.parent)).resolve();sys.path.insert(0,str(ROOT))
admin_email=os.environ.get('JARBAS_ADMIN_EMAIL','admin@chagasadvogados.local');admin_password=os.environ.get('JARBAS_BOOTSTRAP_ADMIN_PASSWORD','')
assert compileall.compile_dir(str(ROOT/'app'),quiet=1),'compileall falhou'
from app import main
from app.smart_intake import detect_case_metadata
from app.petition_generator import build_identity_context, create_draft_pdf, complete_local_draft
from app.copilot import resolve_uploaded_pdf_path
from app.pdf_pipeline import extract_pdf as pipeline_extract, ocr_capability
from fastapi.testclient import TestClient
import reportlab
import pymupdf
main.init_db()
with main.db() as conn:
    ok=conn.execute('PRAGMA integrity_check').fetchone()[0];assert ok=='ok',ok
    _v=(ROOT/'VERSION.txt').read_text(encoding='utf-8').strip()
    meta=conn.execute("SELECT value FROM app_meta WHERE key='schema_version'").fetchone();assert meta and meta['value']==_v,('schema_version',meta['value'] if meta else None,_v)
    cols={r[1] for r in conn.execute('PRAGMA table_info(drafts)').fetchall()};assert {'pdf_path','pdf_file_name'}<=cols,cols
    if admin_password:
        row=conn.execute('SELECT password_hash FROM users WHERE lower(email)=lower(?)',(admin_email,)).fetchone();assert row and main.verify_password(admin_password,row['password_hash']),'senha admin nao corresponde ao banco ativo'
# routes
routes=[]
for route in main.app.routes:
    for method in (getattr(route,'methods',None) or []):
        if method not in {'HEAD','OPTIONS'} and getattr(route,'path',None):routes.append((method,route.path))
assert len(routes)==len(set(routes)),'rotas duplicadas detectadas'
required_paths={'/','/clients','/cases','/intake','/finance','/crm','/agenda','/documents','/timesheet','/reports','/ai','/conselho','/settings','/billing','/search','/cases/{case_id}/documents/{document_id}/view','/cases/{case_id}/documents/{document_id}/reindex','/cases/{case_id}/documents/reindex-all','/cases/{case_id}/drafts/{draft_id}/view-pdf','/cases/{case_id}/drafts/{draft_id}/download'}
actual={p for _,p in routes};missing=required_paths-actual;assert not missing,('rotas principais ausentes',missing)
# Intake sample
sample=ROOT/'tools'/'test_intake.pdf'
if sample.exists():
    data=detect_case_metadata(sample,prefer_ai=False);assert data.get('number')=='5001234-56.2026.8.21.0041',data
    px=pipeline_extract(sample,max_pages=12,tail_pages=2,max_chars=80000);assert px.page_count>=1 and px.text_chars>100,(px.page_count,px.text_chars,px.note)
    assert px.status in {'indexed','partial_ocr'},px.status

# Testa a cadeia de fallback sem depender de um PDF defeituoso real.
import app.pdf_pipeline as _pp
if sample.exists():
    _orig_py=_pp._extract_pymupdf_page
    try:
        _pp._extract_pymupdf_page=lambda doc,idx: ''
        fx=_pp.extract_pdf(sample,max_pages=3,max_chars=20000,allow_local_ocr=False)
        assert fx.text_chars>100 and 'pypdf' in fx.engine_summary,(fx.status,fx.engine_summary,fx.text_chars)
    finally:
        _pp._extract_pymupdf_page=_orig_py
    _orig_py=_pp._extract_pymupdf_page; _orig_pp=_pp._extract_pypdf_page
    try:
        _pp._extract_pymupdf_page=lambda doc,idx: ''
        _pp._extract_pypdf_page=lambda reader,idx: ''
        fx=_pp.extract_pdf(sample,max_pages=3,max_chars=20000,allow_local_ocr=False)
        assert fx.text_chars>100 and 'pdfplumber' in fx.engine_summary,(fx.status,fx.engine_summary,fx.text_chars)
    finally:
        _pp._extract_pymupdf_page=_orig_py; _pp._extract_pypdf_page=_orig_pp

# Verifica dependências do novo pipeline de PDF.
import pymupdf, pdfplumber
assert getattr(pymupdf,'VersionBind',None) or getattr(pymupdf,'__version__',None),'PyMuPDF indisponível'
assert getattr(pdfplumber,'__version__',None),'pdfplumber indisponível'
# Teste de PDF somente-imagem: deve ser classificado de forma explícita, nunca silenciosamente como "lido".
scanwork=ROOT/'data'/'_selftest_scan_830';shutil.rmtree(scanwork,ignore_errors=True);scanwork.mkdir(parents=True,exist_ok=True)
try:
    import pymupdf as _pm
    base=scanwork/'base.pdf'
    d=_pm.open();pg=d.new_page(width=595,height=842);pg.insert_text((60,100),'PROCESSO 5001234-56.2026.8.21.0041 - PAGINA DIGITALIZADA',fontsize=14);d.save(str(base));d.close()
    d=_pm.open(str(base));pix=d[0].get_pixmap(matrix=_pm.Matrix(2,2),alpha=False);png=scanwork/'scan.png';pix.save(str(png));d.close()
    imgpdf=scanwork/'scan_only.pdf';out=_pm.open();pg=out.new_page(width=595,height=842);pg.insert_image(pg.rect,filename=str(png));out.save(str(imgpdf));out.close()
    sx=pipeline_extract(imgpdf,max_pages=3,max_chars=40000,allow_local_ocr=True)
    assert sx.page_count==1,sx
    assert sx.status in {'indexed','partial_ocr','needs_ocr'},sx.status
    if sx.status=='indexed':
        assert sx.text_chars>20,sx
finally:
    shutil.rmtree(scanwork,ignore_errors=True)
# PDF institucional sem tocar no banco real
work=ROOT/'data'/'_selftest_820';shutil.rmtree(work,ignore_errors=True);work.mkdir(parents=True,exist_ok=True)
try:
    fake_case={'id':999,'number':'5000000-00.2026.8.21.0001','title':'Teste JARBAS','area':'Cível','court':'Vara Judicial de Teste','case_class':'Procedimento Comum','subject':'Teste'}
    fake_client={'id':999,'name':'Cliente Teste','person_type':'Pessoa Física','document':'000.000.000-00','nationality':'brasileiro','marital_status':'solteiro','profession':'empresário','rg':'0000000000','address':'Rua Teste','address_number':'1','city':'Canela','state':'RS','zip_code':'95680-000'}
    fake_party={'name':'Parte Adversa Ltda','role':'Ré','person_type':'Pessoa Jurídica','document':'00.000.000/0001-00','address':'Rua Oposta, 2','city':'Gramado','state':'RS','zip_code':'95670-000','is_client':0}
    fake_org={'name':'Escritório Teste','brand_name':'Escritório Teste','lawyer_name':'Dr. Teste','oab_number':'OAB/RS 000.000','address':'Rua Profissional, 3','city':'Canela/RS','phone':'(54) 0000-0000','email':'teste@example.com','primary_color':'#7b1836','logo_path':'/static/chagas_logo.jpeg'}
    identity=build_identity_context(case=fake_case,client=fake_client,parties=[dict(fake_client,role='Autor',is_client=1,client_id=999),fake_party],organization=fake_org)
    content=complete_local_draft(draft_type='Manifestação',objective='Teste de geração institucional.',excerpts=[{'text':'Trecho documental de teste.','citation':'[teste.pdf · p. 1]'}],identity=identity,matrix={})
    pdf=work/'minuta.pdf';create_draft_pdf(content=content,draft_title='Manifestação - Teste',case=fake_case,organization=fake_org,parties=[],target=pdf)
    assert pdf.is_file() and pdf.read_bytes()[:5]==b'%PDF-', 'PDF institucional inválido'
    doccheck=pymupdf.open(str(pdf)); text='\n'.join((pg.get_text('text') or '') for pg in doccheck); doccheck.close()
    for token in ('Cliente Teste','Parte Adversa Ltda','Dr. Teste','OAB/RS 000.000','5000000-00.2026.8.21.0001'):
        assert token in text,(token,text[:1000])
    # caminho legado deve resolver pelo stored_name na raiz canônica
    expected=ROOT/'data'/'uploads'/'999'/'999';expected.mkdir(parents=True,exist_ok=True);legacy_target=expected/'legacy.pdf';legacy_target.write_bytes(pdf.read_bytes())
    resolved=resolve_uploaded_pdf_path('C:/OLD/JARBAS/data/uploads/999/999/legacy.pdf',org_id=999,case_id=999,stored_name='legacy.pdf');assert resolved==legacy_target.resolve(),resolved
finally:
    shutil.rmtree(work,ignore_errors=True)
    shutil.rmtree(ROOT/'data'/'uploads'/'999',ignore_errors=True)
# base_url=localhost: o padrao do TestClient e 'http://testserver', host que o
# TrustedHostMiddleware recusa em producao (400). Nao se coloca host de teste
# no JARBAS_ALLOWED_HOSTS de producao so para o self-test passar.
web=TestClient(main.app,base_url='http://localhost')
_versao=(ROOT/'VERSION.txt').read_text(encoding='utf-8').strip()
r=web.get('/health');assert r.status_code==200,(r.status_code,r.text[:300])
assert r.json().get('version')==_versao,('versao divergente',r.json().get('version'),_versao)
r=web.get('/login');assert r.status_code==200,(r.status_code,r.text[:300])
if admin_password:
    m=re.search(r'name="_csrf"[^>]*value="([^"]+)"',r.text) or re.search(r'value="([^"]+)"[^>]*name="_csrf"',r.text);assert m,'csrf nao localizado'
    r=web.post('/login',data={'email':admin_email,'password':admin_password,'_csrf':m.group(1)},follow_redirects=False);assert r.status_code==303,('login falhou',r.status_code)
    for path in sorted({'/','/clients','/cases','/intake','/finance','/crm','/agenda','/documents','/timesheet','/reports','/ai','/conselho','/settings','/billing','/search'}):
        rr=web.get(path);assert rr.status_code==200,(path,rr.status_code,rr.text[:200])
print('REPORTLAB_OK',getattr(reportlab,'Version','?'))
print('PYMUPDF_OK',getattr(pymupdf,'VersionBind',getattr(pymupdf,'__version__','?')))
print('PDFPLUMBER_OK',getattr(pdfplumber,'__version__','?'))
print('OCR_CAPABILITY',ocr_capability())
print('JARBAS_8_3_1_INSTALL_SELFTEST_OK')
