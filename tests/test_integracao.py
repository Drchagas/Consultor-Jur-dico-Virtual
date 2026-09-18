"""Fumaça de integração: a fiação 8.5 está de pé?

Não sobe o servidor (exigiria FastAPI instalado). Verifica que o router foi
incluído, o schema migra, os templates existem e as dependências batem com
o que o código importa.
"""

import re
import sqlite3
import sys
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
# A árvore de fontes tem app/ na raiz; o pacote de instalação tem payload/app/.
RAIZ = _RAIZ / "payload" if (_RAIZ / "payload" / "app").is_dir() else _RAIZ
sys.path.insert(0, str(RAIZ))

import pytest



def ler(rel: str) -> str:
    return (RAIZ / rel).read_text(encoding="utf-8")


def _ps1() -> str:
    """Lê o instalador seja qual for a versão no nome do arquivo."""
    for base in (RAIZ / "installer", RAIZ, _RAIZ):
        achados = sorted(base.glob("INSTALAR_JARBAS_*.ps1")) if base.is_dir() else []
        if achados:
            return achados[0].read_text(encoding="utf-8-sig")
    raise AssertionError("instalador INSTALAR_JARBAS_*.ps1 não encontrado")


# ------------------------------------------------------------------- fiação

def test_router_do_conselho_incluido_no_main():
    m = ler("app/main.py")
    assert "from .council_routes import router as council_router" in m
    assert "app.include_router(council_router)" in m


def test_council_routes_nao_importa_main_no_topo():
    """Import de main no topo criaria ciclo — tem de ser tardio, dentro de _main()."""
    fonte = ler("app/council_routes.py")
    topo = fonte.split("router = APIRouter()")[0]
    assert "from . import main" not in topo, "import de main no topo cria ciclo"
    assert "def _main()" in fonte


def test_menu_tem_link_para_o_conselho():
    assert '/conselho' in ler("app/templates/base.html")


def test_templates_do_conselho_existem():
    for t in ("conselho.html", "conselho_resultado.html"):
        assert (RAIZ / "app" / "templates" / t).is_file(), f"template {t} ausente"


def test_todos_os_templates_referenciados_existem():
    """Um safe_template_response apontando para arquivo inexistente só quebra
    em produção, na hora que o usuário clica."""
    disponiveis = {p.name for p in (RAIZ / "app" / "templates").glob("*.html")}
    faltando = set()
    for arq in (RAIZ / "app").glob("*.py"):
        for nome in re.findall(r'safe_template_response\(\s*["\']([\w./-]+\.html)["\']',
                               arq.read_text(encoding="utf-8")):
            if nome not in disponiveis:
                faltando.add(f"{arq.name} -> {nome}")
    assert not faltando, f"templates referenciados e ausentes: {sorted(faltando)}"


# ------------------------------------------------------------------- schema

def test_migracao_85_cria_a_tabela_do_conselho():
    fonte = ler("app/database.py")
    c = sqlite3.connect(":memory:")
    for bloco in ("SQLITE_SCHEMA", "V7_SQLITE_EXTRA", "V81_SQLITE_EXTRA", "V85_SQLITE_EXTRA"):
        m = re.search(rf'{bloco} = r?"""(.*?)"""', fonte, re.S)
        assert m, f"bloco {bloco} ausente"
        c.executescript(m.group(1))
    cols = {r[1] for r in c.execute("PRAGMA table_info(ai_council_runs)")}
    for obrigatoria in ("organization_id", "role", "provider", "model",
                        "input_tokens", "output_tokens", "cost_usd"):
        assert obrigatoria in cols, f"ai_council_runs sem {obrigatoria}"


def test_initialize_schema_aplica_a_migracao_85():
    """O rebind final tem de incluir o bloco 8.5, senão a tabela nunca nasce."""
    fonte = ler("app/database.py")
    ultimo = fonte.rindex("def initialize_schema")
    assert "V85_POSTGRES_EXTRA if IS_POSTGRES else V85_SQLITE_EXTRA" in fonte[ultimo:]


def test_insert_do_conselho_bate_com_o_schema_de_drafts():
    """Regressão: draft_type é NOT NULL e foi esquecido na primeira versão."""
    fonte = ler("app/database.py")
    c = sqlite3.connect(":memory:")
    c.executescript(re.search(r'SQLITE_SCHEMA = r?"""(.*?)"""', fonte, re.S).group(1))
    obrigatorias = {r[1] for r in c.execute("PRAGMA table_info(drafts)")
                    if r[3] and r[4] is None and r[1] != "id"}
    insert = re.search(r"INSERT INTO drafts\s*\(([^)]*)\)",
                       ler("app/council_routes.py"), re.S).group(1)
    fornecidas = {c.strip() for c in insert.replace("\n", "").split(",")}
    faltando = obrigatorias - fornecidas
    assert not faltando, f"INSERT em drafts sem colunas obrigatórias: {faltando}"


# ------------------------------------------------------------- dependências

def test_requirements_traz_apenas_o_sdk_do_claude():
    """Provedor único a partir da 9.0."""
    opc = ler("requirements-ia.txt")
    assert "anthropic" in opc
    for removido in ("openai", "google-genai", "google-generativeai"):
        assert removido not in opc, f"{removido} deveria ter sido removido"
    assert "openai" not in ler("requirements.txt")


def test_sdks_de_ia_sao_opcionais_e_nao_abortam_a_instalacao():
    """Um SDK de IA que não resolve não pode impedir o escritório de instalar."""
    obrig = [l.split("=")[0].split(">")[0].strip()
             for l in ler("requirements.txt").splitlines()
             if l.strip() and not l.startswith("#")]
    assert "anthropic" not in obrig, "anthropic no requirements.txt torna a IA obrigatória"
    assert "google-genai" not in obrig


def test_faixas_dos_sdks_nao_travam_em_major_antigo():
    """anthropic 1.0 saiu em 20/08/2026; faixas <1 pegariam uma 0.x."""
    opc = ler("requirements-ia.txt")
    assert re.search(r"anthropic>=1\.", opc), "faixa do anthropic presa em 0.x"


def test_httpx_nao_esta_pinado_em_versao_exata():
    """Pino exato conflitava com o google-genai e derrubava a resolução do pip."""
    for linha in ler("requirements.txt").splitlines():
        if linha.strip().startswith("httpx"):
            assert "==" not in linha, f"httpx pinado: {linha.strip()}"


def test_requirements_nao_traz_o_sdk_descontinuado():
    req = ler("requirements.txt")
    linhas = [l.strip() for l in req.splitlines()
              if l.strip() and not l.strip().startswith("#")]
    assert not any(l.startswith("google-generativeai") for l in linhas), (
        "'google-generativeai' foi descontinuado pelo Google; use 'google-genai'")


def test_nenhum_provedor_removido_sobrou_no_codigo():
    """Resquício de provedor removido vira chamada morta ou custo errado."""
    # Só código e configuração ativa. As notas de versão CITAM os provedores
    # removidos de propósito — é o registro de o que mudou e por quê.
    alvos = (list((RAIZ / "app").glob("*.py"))
             + list((RAIZ / "app" / "templates").glob("*.html"))
             + [RAIZ / ".env.example", RAIZ / "requirements.txt",
                RAIZ / "requirements-ia.txt"])
    for arq in alvos:
        if not arq.is_file():
            continue
        # Comentários que EXPLICAM a migração citam os nomes antigos de
        # propósito. O que não pode sobrar é referência executável.
        codigo = "\n".join(
            l for l in arq.read_text(encoding="utf-8").splitlines()
            if not l.lstrip().startswith(("#", "//")))
        codigo = re.sub(r'"""(?:.|\n)*?"""', "", codigo)
        for termo in ("gemini-", "gpt-5.", "google-genai", "GEMINI_API_KEY"):
            assert termo not in codigo, f"{arq.name} ainda usa {termo}"


# ------------------------------------------------------------------- config

def test_env_example_documenta_teto_e_papeis():
    env = ler(".env.example")
    assert "JARBAS_AI_TETO_USD_MES" in env
    for papel in ("EXTRACAO", "ESTRATEGIA", "REDACAO", "CRITICA", "ROTINA"):
        assert f"JARBAS_PAPEL_{papel}" in env


def test_instalador_coleta_so_a_chave_do_claude():
    ps = _ps1()
    assert "ANTHROPIC_API_KEY" in ps
    for removida in ("OPENAI_API_KEY", "GEMINI_API_KEY"):
        assert removida not in ps, f"instalador ainda pede {removida}"


def test_instalador_nao_deixa_testserver_em_producao():
    assert "testserver" not in _ps1()


def test_versao_atualizada():
    """8.5.x = linha do conselho tri-IA. O patch varia; a linha, não."""
    versao = ler("VERSION.txt").strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", versao), f"versão inesperada: {versao}"


def test_notas_da_versao_acompanham_o_version_txt():
    versao = ler("VERSION.txt").strip()
    esperado = f"NOTAS_DA_VERSAO_{versao.replace('.', '_')}.txt"
    assert (RAIZ / "docs" / esperado).is_file() or (RAIZ / esperado).is_file(), (
        f"faltam as notas {esperado} para a versão {versao}")
