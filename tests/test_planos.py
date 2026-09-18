"""Um plano só é vendável se os limites que ele promete forem aplicados.

Antes desta camada, `plans.storage_gb` existia no banco e nunca era conferido:
o assinante de 20 GB podia subir 500 GB. Em escala de 2.000 assinantes, é a
diferença entre ~70 TB previstos e crescimento sem teto.
"""

import re
import sqlite3
import sys
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
RAIZ = _RAIZ / "payload" if (_RAIZ / "payload" / "app").is_dir() else _RAIZ
sys.path.insert(0, str(RAIZ))

import pytest

from app import ai_council as C
from app import plan_limits as PL

APP = RAIZ / "app"
FONTE_DB = (APP / "database.py").read_text(encoding="utf-8")


def _schema(nome: str) -> str:
    return re.search(rf'{nome} = r?"""(.*?)"""', FONTE_DB, re.S).group(1)


@pytest.fixture(autouse=True)
def banco(monkeypatch):
    """Banco real com dois escritórios em planos diferentes."""
    global conn
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    for n in ("SQLITE_SCHEMA", "V7_SQLITE_EXTRA", "V81_SQLITE_EXTRA", "V85_SQLITE_EXTRA"):
        conn.executescript(_schema(n))
    conn.execute("ALTER TABLE plans ADD COLUMN ai_tier TEXT")
    conn.execute("ALTER TABLE plans ADD COLUMN monthly_ai_usd REAL")

    planos = [(1, "essencial", "Essencial", 50, 2, 50, 20, 100, "basico", 5.0),
              (2, "escritorio", "Escritório", 400, 20, 2000, 200, 5000, "premium", 120.0)]
    for pid, code, nome, preco, ul, cl, gb, cred, tier, usd in planos:
        conn.execute(
            """INSERT INTO plans (id,code,name,monthly_price,user_limit,case_limit,
                                  storage_gb,monthly_credits,active,ai_tier,monthly_ai_usd)
               VALUES (?,?,?,?,?,?,?,?,1,?,?)""",
            (pid, code, nome, preco, ul, cl, gb, cred, tier, usd))
    for oid, pid in ((1, 1), (2, 2)):
        conn.execute(
            "INSERT INTO organizations (id,name,slug,status,created_at) VALUES (?,?,?,'active','2026-01-01')",
            (oid, f"Org {oid}", f"org{oid}"))
        conn.execute(
            """INSERT INTO subscriptions (organization_id,plan_id,status,started_at)
               VALUES (?,?,'active','2026-01-01')""", (oid, pid))
        conn.execute(
            """INSERT INTO clients (id,organization_id,name,created_at)
               VALUES (?,?,?,'2026-01-01')""", (oid, oid, f"Cliente {oid}"))
        conn.execute(
            """INSERT INTO cases (id,organization_id,client_id,title,area,created_at)
               VALUES (?,?,?,?,'Cível','2026-01-01')""", (oid, oid, oid, f"Caso {oid}"))
    conn.commit()

    import contextlib

    @contextlib.contextmanager
    def fake_db():
        yield conn

    monkeypatch.setattr(PL, "db", fake_db)
    yield
    conn.close()


def _doc(org, case, ident, size_bytes):
    conn.execute(
        """INSERT INTO case_documents
           (organization_id,case_id,original_name,stored_name,stored_path,sha256,size_bytes,created_at)
           VALUES (?,?,?,?,?,?,?,'2026-01-01')""",
        (org, case, f"d{ident}.pdf", f"s{ident}.pdf", f"/p/{ident}.pdf", f"h{ident}", size_bytes))
    conn.commit()


GB = 1024 ** 3
MB = 1024 ** 2


# ------------------------------------------------------ leitura do plano

def test_limites_vem_do_plano_assinado():
    a, b = PL.limites(1), PL.limites(2)
    assert a.plan_code == "essencial" and a.storage_gb == 20 and a.ai_tier == "basico"
    assert b.plan_code == "escritorio" and b.storage_gb == 200 and b.ai_tier == "premium"


def test_sem_assinatura_ativa_cai_no_minimo():
    conn.execute("UPDATE subscriptions SET status='canceled' WHERE organization_id=1")
    conn.commit()
    lim = PL.limites(1)
    assert lim.plan_code == "sem-plano"
    assert lim.storage_bytes == 1 * GB, "escritório sem plano não pode ter cota generosa"


# ------------------------------------------------- cota de armazenamento

def test_upload_dentro_da_cota_passa():
    _doc(1, 1, 1, 5 * GB)
    PL.checar_cota_armazenamento(1, 1 * GB)   # 5 + 1 <= 20


def test_upload_que_estoura_a_cota_e_recusado():
    _doc(1, 1, 1, 19 * GB)
    with pytest.raises(PL.CotaDeArmazenamentoExcedida):
        PL.checar_cota_armazenamento(1, 2 * GB)   # 19 + 2 > 20


def test_mensagem_de_cota_diz_o_que_fazer():
    _doc(1, 1, 1, 19 * GB)
    with pytest.raises(PL.CotaDeArmazenamentoExcedida) as e:
        PL.checar_cota_armazenamento(1, 2 * GB)
    msg = str(e.value)
    assert "Essencial" in msg and "20 GB" in msg
    assert "mude de plano" in msg or "Libere espaço" in msg


def test_cota_e_por_escritorio_e_nao_global():
    _doc(1, 1, 1, 19 * GB)   # org 1 quase cheia
    PL.checar_cota_armazenamento(2, 50 * GB)   # org 2 tem 200 GB, não é afetada


def test_cota_zero_significa_ilimitado():
    conn.execute("UPDATE plans SET storage_gb=0 WHERE id=2")
    conn.commit()
    PL.checar_cota_armazenamento(2, 500 * GB)


def test_upload_e_barrado_no_main_antes_de_registrar():
    """A checagem tem de estar no caminho de gravação, não só no módulo."""
    src = (APP / "main.py").read_text(encoding="utf-8")
    assert "plan_limits.checar_cota_armazenamento" in src
    pos_check = src.index("plan_limits.checar_cota_armazenamento")
    pos_insert = src.index("INSERT INTO case_documents")
    assert pos_check < pos_insert, "cota conferida depois do INSERT não serve"


# ------------------------------------------------------- orçamento de IA

def _run(org, custo):
    conn.execute(
        """INSERT INTO ai_council_runs
           (organization_id,role,provider,model,input_tokens,output_tokens,cost_usd,created_at)
           VALUES (?,'redacao','openai','gpt-5.6-sol',0,0,?,?)""",
        (org, custo, __import__("datetime").date.today().isoformat()))
    conn.commit()


def test_orcamento_de_ia_bloqueia_ao_esgotar():
    _run(1, 4.99)
    PL.checar_orcamento_ia(1)          # ainda cabe
    _run(1, 0.02)
    with pytest.raises(PL.OrcamentoDeIAExcedido):
        PL.checar_orcamento_ia(1)      # 5.01 > 5.00


def test_orcamento_e_por_escritorio():
    _run(1, 10.0)
    PL.checar_orcamento_ia(2)          # plano de US$120 não é afetado


# --------------------------------------------- roteamento de IA por plano

def test_cada_faixa_define_os_cinco_papeis():
    for nome, faixa in PL.FAIXAS.items():
        assert set(faixa) == set(C.PAPEIS_PADRAO), f"faixa {nome} incompleta"


def test_todo_modelo_de_toda_faixa_tem_preco_no_catalogo():
    for nome, faixa in PL.FAIXAS.items():
        for papel, (_, modelo) in faixa.items():
            assert modelo in C.CATALOGO, (
                f"faixa {nome}, papel {papel}: modelo {modelo} sem preço — "
                "o custo seria estimado errado e o teto não protegeria")


def test_faixa_do_plano_tem_precedencia_sobre_o_ambiente(monkeypatch):
    monkeypatch.setenv("JARBAS_PAPEL_REDACAO", "anthropic:claude-opus-5")
    faixa = PL.FAIXAS["basico"]
    assert C.papel_config("redacao", faixa) == faixa["redacao"]
    assert C.papel_config("redacao") == ("anthropic", "claude-opus-5")


def test_faixas_estao_ordenadas_por_custo():
    """basico < economico < padrao < premium, medido no catálogo real."""
    def custo(faixa):
        # análise completa: processo de 328 páginas, 5 etapas
        et = [("extracao", 86000, 8000), ("estrategia", 9000, 6000),
              ("redacao", 15000, 8000), ("critica", 17000, 5000),
              ("redacao", 14000, 8000)]
        return sum(C.custo_usd(faixa[p][1], i, o) for p, i, o in et)

    ordem = ["basico", "economico", "padrao", "premium"]
    custos = [custo(PL.FAIXAS[n]) for n in ordem]
    assert custos == sorted(custos), dict(zip(ordem, [round(c, 4) for c in custos]))


def test_toda_faixa_usa_modelo_diferente_para_criticar():
    """A regra migrou de provedor para MODELO na 9.0, mas não sumiu: um
    modelo revisando o próprio texto tende a aprová-lo."""
    for nome, faixa in PL.FAIXAS.items():
        assert faixa["redacao"][1] != faixa["critica"][1], (
            f"faixa {nome}: redação e crítica no mesmo modelo")


def test_toda_faixa_usa_apenas_claude():
    for nome, faixa in PL.FAIXAS.items():
        for papel, (prov, modelo) in faixa.items():
            assert prov == "anthropic", f"{nome}/{papel}"
            assert modelo.startswith("claude-"), f"{nome}/{papel}: {modelo}"


# ------------------------------------------------------------- resumo

def test_resumo_traz_percentual_de_cada_limite():
    _doc(1, 1, 1, 10 * GB)
    _run(1, 2.5)
    r = PL.resumo_consumo(1)
    assert r["armazenamento"]["pct"] == 50.0
    assert r["ia"]["pct"] == 50.0
    assert r["plano"] == "Essencial"


# --------------------------------------- integração com o gateway

def test_assinatura_pendente_nao_da_limites_do_plano():
    """Assinatura criada mas não autorizada não pode liberar cota cheia."""
    conn.execute("UPDATE subscriptions SET status='pending' WHERE organization_id=2")
    conn.commit()
    lim = PL.limites(2)
    assert lim.plan_code == "sem-plano"
    assert lim.storage_bytes == 1 * GB


def test_pagamento_falho_derruba_para_o_minimo():
    """past_due = cobrança recusada. 200 GB e faixa premium seriam serviço grátis."""
    conn.execute("UPDATE subscriptions SET status='past_due' WHERE organization_id=2")
    conn.commit()
    lim = PL.limites(2)
    assert lim.plan_code == "sem-plano"
    assert lim.ai_tier == "basico"


def test_estados_com_limites_batem_com_o_mapa_do_gateway():
    """Se o gateway ganhar um estado novo, ele não pode virar acesso por acidente."""
    from app import billing_mercadopago as MP
    src = (APP / "plan_limits.py").read_text(encoding="utf-8")
    for interno in set(MP.MAPA_STATUS.values()):
        if MP.tem_acesso(interno):
            assert f"'{interno}'" in src, (
                f"status '{interno}' dá acesso no gateway mas não consta em plan_limits")
