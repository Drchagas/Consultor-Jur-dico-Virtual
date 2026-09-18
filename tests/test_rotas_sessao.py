"""Sessão expirada não pode virar HTTP 500.

Falhas reais de produção em 04/09, quatro rotas de uma vez:

    GET /intake    TypeError: 'NoneType' object is not subscriptable
    GET /intake/14 TypeError: 'NoneType' object is not subscriptable
    GET /billing   TypeError: 'NoneType' object is not subscriptable
    GET /audit     TypeError: 'RedirectResponse' object is not subscriptable

Causa: require_workspace devolvia (redirect, None) quando a sessão expirava.
As rotas checam `isinstance(org, RedirectResponse)` — e None não é
RedirectResponse, então a checagem passava batido e a linha seguinte
estourava. Quem usava org["id"] via NoneType; quem usava user["..."] via
RedirectResponse, porque o redirect estava no lugar do usuário.

Corrigido na fonte: o redirect vai nos DOIS lugares. Uma linha conserta as
58 rotas de uma vez, em vez de depender de cada uma lembrar de uma checagem
extra que nenhuma tinha.
"""

import re
import sys
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
RAIZ = _RAIZ / "payload" if (_RAIZ / "payload" / "app").is_dir() else _RAIZ
sys.path.insert(0, str(RAIZ))

import pytest

APP = RAIZ / "app"
MAIN = (APP / "main.py").read_text(encoding="utf-8")

GUARDA = re.compile(r"isinstance\(\s*org\s*,\s*RedirectResponse\s*\)")
USO = re.compile(r"\b(?:user|org)\s*\[")
MODULOS = ("main.py", "v7.py", "council_routes.py", "billing_routes.py",
           "prazo_routes.py", "chatbot_routes.py")


# v7.py usa _org_user(), que apenas repassa require_workspace.
HELPERS = ("require_workspace", "_org_user")


def _rotas():
    """(arquivo, rota, corpo) de toda rota que resolve o workspace.

    O corpo termina na PRÓXIMA definição de nível superior — sem isso, as
    funções auxiliares declaradas entre rotas caem no bloco da rota anterior
    e o detector acusa a rota errada.
    """
    for nome in MODULOS:
        arq = APP / nome
        if not arq.is_file():
            continue
        src = arq.read_text(encoding="utf-8")
        for bloco in re.split(r"\n(?=@(?:app|router)\.(?:get|post))", src)[1:]:
            m = re.search(r'(get|post)\("([^"]+)"', bloco)
            if not m:
                continue
            # corta na segunda def de coluna zero (a primeira é a própria rota)
            defs = [d.start() for d in re.finditer(r"^def ", bloco, re.M)]
            corpo = bloco[:defs[1]] if len(defs) > 1 else bloco
            if not any(h in corpo for h in HELPERS):
                continue
            yield nome, f"{m.group(1).upper()} {m.group(2)}", corpo


# ------------------------------------------------------ contrato da função

def test_require_workspace_nunca_devolve_org_none():
    """(redirect, None) é o formato que causou os quatro 500 de produção."""
    i = MAIN.index("def require_workspace(")
    j = MAIN.index("\ndef ", i + 10)
    corpo = MAIN[i:j]
    assert "return user, None" not in corpo, (
        "org=None escapa da checagem isinstance(org, RedirectResponse)")


def test_require_workspace_devolve_o_redirect_nos_dois_lugares():
    i = MAIN.index("def require_workspace(")
    j = MAIN.index("\ndef ", i + 10)
    corpo = MAIN[i:j]
    assert "return user, user" in corpo, (
        "o redirect precisa ir também no lugar de org, senão as rotas que "
        "guardam org continuam quebrando quando a sessão expira")


def test_helper_do_v7_apenas_repassa_require_workspace():
    """_org_user precisa herdar a correção; se divergir, /intake volta a 500."""
    v7 = (APP / "v7.py").read_text(encoding="utf-8")
    i = v7.index("def _org_user(")
    corpo = v7[i:v7.index("\ndef ", i + 10)]
    assert "require_workspace" in corpo
    assert "None" not in corpo, "_org_user não pode inventar um retorno próprio"


def test_nenhum_helper_de_sessao_devolve_none_no_lugar_do_org():
    for nome in MODULOS:
        arq = APP / nome
        if arq.is_file():
            texto = arq.read_text(encoding="utf-8")
            assert "return user, None" not in texto, f"{nome} reintroduz (user, None)"


# ---------------------------------------------------- guarda em cada rota

def test_toda_rota_com_workspace_guarda_o_sentinela():
    sem = [f"{a}: {r}" for a, r, c in _rotas() if not GUARDA.search(c)]
    assert not sem, "rotas sem guarda (HTTP 500 em sessão expirada):\n  " + "\n  ".join(sem)


def test_nenhuma_rota_usa_user_ou_org_antes_de_guardar():
    """Foi assim que /audit quebrou: guardava org, mas lia user na linha
    seguinte — e user era o RedirectResponse."""
    ruins = []
    for a, r, c in _rotas():
        g = GUARDA.search(c)
        u = USO.search(c)
        if g and u and u.start() < g.start():
            ruins.append(f"{a}: {r}")
    assert not ruins, "uso de user/org antes da guarda:\n  " + "\n  ".join(ruins)


def test_as_quatro_rotas_que_quebraram_estao_cobertas():
    """Regressão nominal das falhas de 04/09."""
    encontradas = {r for _, r, _ in _rotas()}
    for alvo in ("GET /intake", "GET /billing", "GET /audit"):
        assert any(x.startswith(alvo) for x in encontradas), f"{alvo} não auditada"


def test_ha_rotas_suficientes_sendo_auditadas():
    """Se o recorte parar de casar, o teste vira vitória vazia."""
    assert sum(1 for _ in _rotas()) >= 50


# --------------------------------------- regressão do TypeError no schema

def test_ensure_column_e_chamado_com_a_assinatura_certa():
    """ensure_column(conn, tabela, "coluna TIPO") tem TRÊS parâmetros.

    Chamar com quatro levanta TypeError, aborta initialize_schema e derruba
    init_db inteiro — foi o que produziu JARBAS_BOOTSTRAP_INIT_ERROR e travou
    o schema em 8.5.2 nas versões 8.6.0, 8.7.0 e 8.8.0.
    """
    import inspect
    from app.database import ensure_column
    params = list(inspect.signature(ensure_column).parameters)
    assert len(params) == 3, f"assinatura mudou: {params}"

    ruins = []
    for nome in ("database.py", "main.py"):
        arq = APP / nome
        if not arq.is_file():
            continue
        for n, linha in enumerate(arq.read_text(encoding="utf-8").splitlines(), 1):
            if "ensure_column(" not in linha or "def ensure_column" in linha:
                continue
            dentro = linha[linha.index("ensure_column(") + len("ensure_column("):]
            profundidade, args, atual = 1, [], ""
            for ch in dentro:
                if ch in "([":
                    profundidade += 1
                elif ch in ")]":
                    profundidade -= 1
                    if profundidade == 0:
                        break
                if ch == "," and profundidade == 1:
                    args.append(atual); atual = ""
                else:
                    atual += ch
            args.append(atual)
            if len([a for a in args if a.strip()]) > 3:
                ruins.append(f"{nome}:{n}: {linha.strip()[:90]}")
    assert not ruins, "ensure_column com 4 argumentos (TypeError em runtime):\n  " + "\n  ".join(ruins)


# ------------------------------------- migração executada, não só grepada

def test_initialize_schema_roda_de_verdade_nos_dois_caminhos(tmp=None):
    """CHAMA initialize_schema, em vez de procurar strings no arquivo.

    Meus testes anteriores verificavam se o texto 'ensure_column(conn, ...)'
    aparecia em database.py. Passavam com a assinatura errada e a instalação
    quebrava em runtime com TypeError. Teste que só casa texto dá confiança
    falsa — este executa.
    """
    import sqlite3
    import tempfile
    from app import database as DB

    fonte = (APP / "database.py").read_text(encoding="utf-8")

    class _Conn:
        backend = "sqlite"

        def __init__(self, raw):
            self.raw = raw

        def execute(self, sql, p=()):
            return self.raw.execute(sql, tuple(p))

        def executescript(self, s):
            self.raw.executescript(s)

    def _migrar(preexistente: bool):
        d = tempfile.mkdtemp()
        raw = sqlite3.connect(f"{d}/j.db")
        raw.row_factory = sqlite3.Row
        if preexistente:
            # estado real da máquina do escritório antes da atualização
            for n in ("SQLITE_SCHEMA", "V7_SQLITE_EXTRA", "V81_SQLITE_EXTRA",
                      "V85_SQLITE_EXTRA"):
                raw.executescript(re.search(rf'{n} = r?"""(.*?)"""', fonte, re.S).group(1))
            raw.execute("INSERT INTO app_meta (key,value,updated_at) "
                        "VALUES ('schema_version','8.5.2','x')")
            raw.commit()
        DB.initialize_schema(_Conn(raw))
        raw.commit()
        return raw

    for preexistente, rotulo in ((False, "banco novo"), (True, "banco em 8.5.2")):
        raw = _migrar(preexistente)
        cols_plans = {r[1] for r in raw.execute("PRAGMA table_info(plans)")}
        cols_docs = {r[1] for r in raw.execute("PRAGMA table_info(case_documents)")}
        tabelas = {r[0] for r in raw.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "ai_tier" in cols_plans, rotulo
        assert "monthly_ai_usd" in cols_plans, rotulo
        assert "deleted_at" in cols_docs, rotulo
        assert {"billing_events", "user_totp", "password_resets"} <= tabelas, rotulo
        raw.close()
