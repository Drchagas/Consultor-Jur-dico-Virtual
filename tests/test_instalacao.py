"""O self-test do instalador consegue passar?

Regressão de duas falhas reais de instalação, ambas introduzidas por mim:

1. Removi 'testserver' do JARBAS_ALLOWED_HOSTS de produção (correto em si),
   sem notar que o TestClient do Starlette usa exatamente esse host por
   padrão. O TrustedHostMiddleware devolvia 400 e o instalador abortava com
   "Self-test integrado falhou. O sistema nao sera considerado instalado."

2. A versão vivia hardcoded em quatro lugares. /health devolvia "8.3.1"
   literal — mudar `FastAPI(version=...)` não tinha efeito nenhum sobre ele.
   Subir o VERSION.txt para 8.5.1 quebraria o assert do self-test.

A regra que estes testes impõem: VERSION.txt é a única fonte de verdade, e
o host do self-test tem de estar nos hosts que o instalador grava.
"""

import re
import sys
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
RAIZ = _RAIZ / "payload" if (_RAIZ / "payload" / "app").is_dir() else _RAIZ
sys.path.insert(0, str(RAIZ))

import pytest

APP = RAIZ / "app"
VERSAO = (RAIZ / "VERSION.txt").read_text(encoding="utf-8").strip()


def _tools_dir() -> Path:
    for c in (RAIZ / "tools", _RAIZ / "tools"):
        if c.is_dir():
            return c
    pytest.skip("sem tools/ neste layout")


def _validate() -> str:
    arq = _tools_dir() / "validate_install.py"
    if not arq.is_file():
        pytest.skip("validate_install.py ausente")
    return arq.read_text(encoding="utf-8")


def _instalador() -> str:
    for base in (RAIZ / "installer", _RAIZ / "installer", _RAIZ, RAIZ):
        achados = sorted(base.glob("INSTALAR_JARBAS_*.ps1")) if base.is_dir() else []
        if achados:
            return achados[0].read_text(encoding="utf-8-sig")
    pytest.skip("instalador ausente neste layout")


# ------------------------------------------------- host do TestClient

def test_host_do_selftest_esta_nos_allowed_hosts_do_instalador():
    """O TrustedHostMiddleware recusa qualquer host fora da lista, com 400."""
    ps1 = _instalador()
    m = re.search(r"'JARBAS_ALLOWED_HOSTS=([^']+)'", ps1)
    assert m, "instalador não grava JARBAS_ALLOWED_HOSTS"
    permitidos = {h.strip() for h in m.group(1).split(",") if h.strip()}

    vi = _validate()
    usados = re.findall(r"TestClient\(\s*main\.app\s*(?:,\s*base_url=['\"]https?://([^'\"/:]+))?",
                        vi)
    assert usados, "não encontrei nenhuma construção de TestClient"
    for host in usados:
        efetivo = host or "testserver"   # padrão do Starlette
        assert efetivo in permitidos, (
            f"o self-test fala com o host '{efetivo}', que não está em "
            f"{sorted(permitidos)} — HTTP 400 e instalação abortada")


def test_producao_nao_confia_em_host_de_teste():
    """A solução não é colocar 'testserver' no ALLOWED_HOSTS de produção."""
    m = re.search(r"'JARBAS_ALLOWED_HOSTS=([^']+)'", _instalador())
    assert "testserver" not in m.group(1)


# --------------------------------------------------- versão única

def test_nenhuma_versao_fixa_no_codigo_da_aplicacao():
    """Versão literal em app/*.py sai de sincronia em silêncio."""
    achados = []
    for arq in sorted(APP.glob("*.py")):
        for n, linha in enumerate(arq.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r'["\']\d+\.\d+\.\d+["\']', linha) and "VERSION" not in linha:
                achados.append(f"{arq.name}:{n}: {linha.strip()[:80]}")
    assert not achados, "versão fixa no código:\n" + "\n".join(achados)


def test_health_reporta_a_versao_do_version_txt():
    """/health é o que o self-test consulta; tem de refletir o build real."""
    src = (APP / "main.py").read_text(encoding="utf-8")
    m = re.search(r'def health\(\).*?return (\{.*?\})', src, re.S)
    assert m, "rota /health não localizada"
    assert "APP_VERSION" in m.group(1), (
        "/health devolve versão literal; mudar FastAPI(version=...) não a afeta")


def test_app_version_vem_do_arquivo():
    src = (APP / "main.py").read_text(encoding="utf-8")
    assert 'APP_VERSION = (BASE_DIR / "VERSION.txt")' in src


def test_schema_version_acompanha_a_versao():
    src = (APP / "main.py").read_text(encoding="utf-8")
    assert "version = version or APP_VERSION" in src, (
        "schema_version fixo faz o self-test comparar com valor errado")


def test_selftest_compara_com_version_txt_e_nao_com_literal():
    vi = _validate()
    literais = re.findall(r"==['\"]\d+\.\d+\.\d+['\"]", vi)
    assert not literais, f"assert de versão fixa no self-test: {literais}"
    assert "VERSION.txt" in vi, "o self-test precisa ler VERSION.txt"


# ------------------------------------------- cobertura de rotas novas

def test_selftest_varre_a_rota_do_conselho():
    """Rota nova que o self-test não visita é rota que quebra sem aviso."""
    vi = _validate()
    assert "'/conselho'" in vi, "/conselho fora da varredura do self-test"


def test_version_txt_existe_onde_o_selftest_procura():
    """ROOT/VERSION.txt: o instalador precisa copiá-lo para a pasta de instalação."""
    assert (RAIZ / "VERSION.txt").is_file()
    ps1 = _instalador()
    assert "'VERSION.txt'" in ps1, "instalador não copia VERSION.txt"


def test_selftest_inicializa_o_banco_antes_de_conferir_a_versao():
    """Ordem importa: sem init_db() antes, o schema_version lido ainda é o
    da versão anterior e o assert falha em toda atualização."""
    vi = _validate()
    pos_init = vi.find("main.init_db()")
    pos_check = vi.find("key='schema_version'")
    assert pos_init != -1, "self-test não chama init_db()"
    assert pos_init < pos_check, (
        "init_db() precisa vir antes da checagem de schema_version")


def test_init_db_marca_a_versao_do_schema():
    import ast
    src = (APP / "main.py").read_text(encoding="utf-8")
    for no in ast.walk(ast.parse(src)):
        if isinstance(no, ast.FunctionDef) and no.name == "init_db":
            chamadas = {n.func.id for n in ast.walk(no)
                        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            assert "initialize_schema" in chamadas
            assert "_mark_schema_version" in chamadas
            return
    pytest.fail("init_db não encontrada em main.py")
