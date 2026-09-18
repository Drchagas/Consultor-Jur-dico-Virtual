"""Correções da 8.8: busca indexada, 2FA, recuperação, retenção, RLS.

Cada bloco corresponde a um item que estava na lista de pendências para
vender assinatura, e a maioria a uma falha real de segurança ou de escala.
"""

import base64
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
RAIZ = _RAIZ / "payload" if (_RAIZ / "payload" / "app").is_dir() else _RAIZ
sys.path.insert(0, str(RAIZ))

import pytest

from app import auth_2fa as A
from app import search_index as SI

APP = RAIZ / "app"
FONTE_DB = (APP / "database.py").read_text(encoding="utf-8")


def _banco():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    for n in ("SQLITE_SCHEMA", "V7_SQLITE_EXTRA", "V81_SQLITE_EXTRA",
              "V85_SQLITE_EXTRA", "V87_SQLITE_EXTRA", "V88_SQLITE_EXTRA"):
        c.executescript(re.search(rf'{n} = r?"""(.*?)"""', FONTE_DB, re.S).group(1))
    for col, tipo in (("deleted_at", "TEXT"), ("deleted_by", "INTEGER")):
        c.execute(f"ALTER TABLE case_documents ADD COLUMN {col} {tipo}")
    c.execute("ALTER TABLE document_chunks ADD COLUMN deleted_at TEXT")
    return c


# ==========================================================  TOTP (RFC 6238)

# Vetores oficiais do Apêndice B da RFC, seed "12345678901234567890".
SEED = base64.b32encode(b"12345678901234567890").decode()
VETORES = [(59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"),
           (1234567890, "89005924"), (2000000000, "69279037"),
           (20000000000, "65353130")]


def test_totp_bate_com_os_vetores_oficiais_da_rfc_6238():
    """Sem isto, o código pode ser plausível e ainda assim incompatível com
    Google Authenticator — descoberto só quando o usuário não consegue entrar."""
    for t, esperado in VETORES:
        assert A.codigo_atual(SEED, agora=t) == esperado[-6:], f"t={t}"


def test_totp_aceita_deriva_de_um_periodo():
    t = 1111111109
    assert A.verificar_codigo(SEED, A.codigo_atual(SEED, t - 30), agora=t)
    assert A.verificar_codigo(SEED, A.codigo_atual(SEED, t + 30), agora=t)


def test_totp_recusa_codigo_fora_da_janela():
    t = 1111111109
    assert not A.verificar_codigo(SEED, A.codigo_atual(SEED, t - 300), agora=t)


def test_totp_recusa_formato_invalido():
    for ruim in ("", "abc", "12345", "1234567", None, "12 34 56 78"):
        assert not A.verificar_codigo(SEED, ruim, agora=59)


def test_totp_ignora_espacos_e_hifens_do_usuario():
    t = 59
    codigo = A.codigo_atual(SEED, t)
    assert A.verificar_codigo(SEED, f"{codigo[:3]} {codigo[3:]}", agora=t)
    assert A.verificar_codigo(SEED, f"{codigo[:3]}-{codigo[3:]}", agora=t)


def test_segredo_gerado_tem_entropia_suficiente():
    s = A.gerar_segredo()
    assert len(s) >= 32                      # 160 bits em base32
    assert len({A.gerar_segredo() for _ in range(50)}) == 50


def test_uri_otpauth_e_valida_para_o_aplicativo():
    uri = A.uri_otpauth("ABCDEFGHIJKLMNOP", "dr@exemplo.br")
    assert uri.startswith("otpauth://totp/")
    for p in ("secret=ABCDEFGHIJKLMNOP", "issuer=", "digits=6", "period=30"):
        assert p in uri


def test_comparacao_de_codigo_e_em_tempo_constante():
    fonte = (APP / "auth_2fa.py").read_text(encoding="utf-8")
    assert "compare_digest" in fonte


# ================================================  códigos de recuperação

def test_codigos_de_recuperacao_sao_unicos():
    cs = A.gerar_codigos_recuperacao(10)
    assert len(cs) == 10 and len(set(cs)) == 10


def test_codigo_de_recuperacao_e_guardado_como_hash():
    c = "AB12-CD34"
    h = A.hash_codigo(c)
    assert c.replace("-", "") not in h and len(h) == 64


def test_conferencia_de_recuperacao_ignora_caixa_e_espaco():
    c = "ab12-cd34"
    hashes = [A.hash_codigo("AB12-CD34")]
    assert A.conferir_codigo(c, hashes) == hashes[0]
    assert A.conferir_codigo(" AB12-CD34 ", hashes) == hashes[0]


def test_codigo_errado_nao_casa():
    assert A.conferir_codigo("ZZZZ-ZZZZ", [A.hash_codigo("AB12-CD34")]) is None


# =====================================================  redefinição de senha

def test_token_de_reset_nao_e_guardado_em_claro():
    token, h, _ = A.gerar_token_reset()
    assert token != h and A.hash_token_reset(token) == h
    assert len(token) >= 32


def test_token_de_reset_expira():
    passado = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    futuro = (datetime.now() + timedelta(minutes=30)).isoformat(timespec="seconds")
    assert A.token_expirado(passado)
    assert not A.token_expirado(futuro)


def test_token_malformado_conta_como_expirado():
    """Padrão seguro: o que não dá para validar não vale."""
    for ruim in ("", "ontem", "2026-13-45"):
        assert A.token_expirado(ruim)


def test_tokens_de_reset_nao_se_repetem():
    assert len({A.gerar_token_reset()[0] for _ in range(50)}) == 50


# ==============================================================  busca FTS5

@pytest.fixture(autouse=True)
def _sem_estado():
    yield


def _povoar(c, quantos=400):
    c.execute("INSERT INTO organizations (id,name,slug,status,created_at) VALUES (1,'A','a','active','2026-01-01')")
    c.execute("INSERT INTO organizations (id,name,slug,status,created_at) VALUES (2,'B','b','active','2026-01-01')")
    for o in (1, 2):
        c.execute("INSERT INTO clients (id,organization_id,name,created_at) VALUES (?,?,'C','2026-01-01')", (o, o))
        c.execute("INSERT INTO cases (id,organization_id,client_id,title,area,created_at) VALUES (?,?,?,'T','Cível','2026-01-01')", (o, o, o))
        c.execute("""INSERT INTO case_documents (id,organization_id,case_id,original_name,stored_name,stored_path,sha256,size_bytes,created_at)
                     VALUES (?,?,?,'autos.pdf',?,?,?,1000,'2026-01-01')""", (o, o, o, f"s{o}.pdf", f"/p/{o}.pdf", f"h{o}"))
    textos = ["Alega-se a nulidade da prova obtida sem autorizacao judicial.",
              "A ilicitude probatoria contamina os atos subsequentes.",
              "O contrato de locacao foi rescindido em marco de 2025."]
    for i in range(quantos):
        c.execute("""INSERT INTO document_chunks (organization_id,case_id,document_id,page_number,chunk_index,text,created_at)
                     VALUES (1,1,1,?,?,?,'2026-01-01')""", (i // 3 + 1, i, textos[i % 3]))
    c.execute("""INSERT INTO document_chunks (organization_id,case_id,document_id,page_number,chunk_index,text,created_at)
                 VALUES (2,2,2,1,0,'SEGREDO DO ESCRITORIO RIVAL sobre nulidade','2026-01-01')""")
    c.commit()


def test_indice_fts_e_criado_e_populado_por_gatilho():
    c = _banco()
    assert SI.preparar_indice(c), "FTS5 indisponível neste SQLite"
    _povoar(c, 60)
    n = c.execute("SELECT COUNT(*) n FROM document_chunks_fts").fetchone()["n"]
    assert n == c.execute("SELECT COUNT(*) n FROM document_chunks").fetchone()["n"]


def test_busca_indexada_encontra_por_radical():
    """'probatoria' precisa casar 'probatorio'; era a limitação do método antigo."""
    c = _banco(); SI.preparar_indice(c); _povoar(c, 30)
    r = SI.buscar(c, org_id=1, case_id=1, consulta="probatoria", limite=5)
    assert r and "probatoria" in r[0]["text"].lower()


def test_busca_indexada_respeita_o_isolamento_entre_escritorios():
    c = _banco(); SI.preparar_indice(c); _povoar(c, 30)
    r = SI.buscar(c, org_id=1, case_id=1, consulta="nulidade", limite=50)
    assert r
    assert all("RIVAL" not in x["text"] for x in r), "busca vazou entre escritórios"


def test_consulta_hostil_nao_quebra_a_busca():
    """Operador do FTS5 vindo do usuário derrubaria a query com erro."""
    c = _banco(); SI.preparar_indice(c); _povoar(c, 20)
    for hostil in ("AND OR NOT", 'prova AND (nulidade', '"aspas soltas',
                   "termo*special", "NEAR(a b)", "^inicio"):
        assert SI.buscar(c, org_id=1, case_id=1, consulta=hostil, limite=3) is not None


def test_expressao_fts_escapa_todo_termo():
    e = SI.consulta_fts("prova AND (nulidade)")
    assert e.count('"') == e.count("*") * 2, "termo sem aspas vira sintaxe do FTS5"


def test_palavras_vazias_sao_descartadas():
    assert SI.termos("a nulidade da prova") == ["nulidade", "prova"]


def test_acentos_sao_normalizados():
    assert SI.termos("nulidade probatória") == ["nulidade", "probatoria"]


def test_busca_devolve_none_quando_nao_ha_indice():
    """Contrato com o chamador: None manda usar a varredura antiga."""
    c = _banco()   # sem preparar_indice
    _povoar(c, 5)
    assert SI.buscar(c, org_id=1, case_id=1, consulta="nulidade", limite=3) is None


def test_copilot_usa_o_indice_e_mantem_a_reserva():
    fonte = (APP / "copilot.py").read_text(encoding="utf-8")
    assert "search_index.buscar" in fonte
    assert "_search_case_varredura" in fonte, "sem reserva, SQLite sem FTS5 quebra"


def test_busca_nao_carrega_todos_os_chunks_em_memoria():
    """A varredura antiga fazia SELECT sem LIMIT e pontuava em Python."""
    fonte = (APP / "search_index.py").read_text(encoding="utf-8")
    assert "LIMIT ?" in fonte


# ============================================================  retenção

def test_exclusao_e_reversivel():
    fonte = (APP / "retencao.py").read_text(encoding="utf-8")
    assert "def excluir_documento" in fonte and "def restaurar_documento" in fonte
    i = fonte.index("def excluir_documento")
    j = fonte.index("def restaurar_documento")
    assert "DELETE FROM" not in fonte[i:j], "exclusão destrutiva não é reversível"


def test_exclusao_marca_os_chunks_junto():
    """Senão o Copiloto continua citando documento que o usuário apagou."""
    fonte = (APP / "retencao.py").read_text(encoding="utf-8")
    i = fonte.index("def excluir_documento")
    j = fonte.index("def restaurar_documento")
    assert "UPDATE document_chunks" in fonte[i:j]


def test_expurgo_remove_de_verdade():
    fonte = (APP / "retencao.py").read_text(encoding="utf-8")
    i = fonte.index("def expurgar")
    trecho = fonte[i:]
    for t in ("document_chunks", "document_pages", "case_documents"):
        assert f"DELETE FROM {t}" in trecho, f"{t} sobrevive ao expurgo"
    assert ".unlink()" in trecho, "expurgo não remove o arquivo do disco"


def test_expurgo_valida_o_caminho_antes_de_apagar():
    """stored_path fora da raiz de uploads = apagar arquivo arbitrário."""
    fonte = (APP / "retencao.py").read_text(encoding="utf-8")
    assert "resolve()).startswith" in fonte


def test_eliminacao_a_pedido_do_titular_nao_espera_carencia():
    fonte = (APP / "retencao.py").read_text(encoding="utf-8")
    i = fonte.index("def eliminar_a_pedido_do_titular")
    assert "forcar=True" in fonte[i:]


def test_schema_tem_as_colunas_de_exclusao_reversivel():
    fonte = FONTE_DB
    ultimo = fonte.rindex("def initialize_schema")
    for t, c, tp in (("case_documents", "deleted_at", "TEXT"),
                     ("case_documents", "deleted_by", "INTEGER"),
                     ("document_chunks", "deleted_at", "TEXT")):
        # assinatura de TRÊS argumentos: nome e tipo na mesma string
        assert f'ensure_column(conn, "{t}", "{c} {tp}")' in fonte[ultimo:]


# =================================================================  RLS

def test_rls_cobre_todas_as_tabelas_com_tenant():
    from app.database import TABELAS_COM_TENANT, rls_sql
    sql = rls_sql()
    for t in TABELAS_COM_TENANT:
        assert f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;" in sql
        assert f"CREATE POLICY {t}_tenant" in sql


def test_rls_usa_force_para_valer_tambem_para_o_dono():
    """Sem FORCE, o dono da tabela — que costuma ser o usuário da aplicação —
    ignora a política inteira e o RLS vira decoração."""
    from app.database import TABELAS_COM_TENANT, rls_sql
    sql = rls_sql()
    for t in TABELAS_COM_TENANT:
        assert f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY;" in sql


def test_rls_tem_with_check_e_nao_so_using():
    """USING filtra leitura; sem WITH CHECK dá para INSERIR em outro tenant."""
    from app.database import rls_sql
    sql = rls_sql()
    assert sql.count("WITH CHECK") == sql.count("CREATE POLICY")


def test_rls_cobre_as_tabelas_que_o_teste_de_isolamento_protege():
    from app.database import TABELAS_COM_TENANT
    for t in ("clients", "cases", "case_documents", "document_chunks"):
        assert t in TABELAS_COM_TENANT


def test_org_da_sessao_usa_set_local():
    """SET LOCAL morre com a transação. Sem isso, uma conexão devolvida ao
    pool carregaria o tenant do request anterior."""
    assert "set_config('jarbas.org_id', ?, true)" in FONTE_DB


# ==========================================  pool e concorrência de créditos

def test_postgres_usa_pool_de_conexoes():
    assert "ConnectionPool" in FONTE_DB
    assert "putconn" in FONTE_DB, "conexão fechada em vez de devolvida ao pool"


def test_pool_tem_reserva_se_a_biblioteca_faltar():
    i = FONTE_DB.index("self._pooled = True")
    assert "psycopg.connect" in FONTE_DB[i:i + 600]


def test_debito_de_creditos_e_uma_transacao_so():
    """Regressão do TOCTOU: ler numa conexão e inserir noutra deixava duas
    abas simultâneas passarem as duas pela checagem."""
    fonte = (APP / "main.py").read_text(encoding="utf-8")
    i = fonte.index("def _consume_copilot_credits")
    j = fonte.index("\n@app.", i)
    corpo = fonte[i:j]
    assert corpo.count("with db()") == 1, "mais de uma transação no débito"
    assert "BEGIN IMMEDIATE" in corpo
    assert "current_subscription(" not in corpo and "month_usage(" not in corpo, \
        "leitura fora da transação reintroduz a corrida"
