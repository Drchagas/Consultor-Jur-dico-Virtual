"""Cobrança via Mercado Pago.

O endpoint de webhook é público: sem validação de assinatura, qualquer um na
internet ativa assinaturas mandando um POST. Estes testes são a garantia de
que isso não passa. Rodam sem rede e sem credencial real.
"""

import hashlib
import hmac
import re
import sqlite3
import sys
import time
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
RAIZ = _RAIZ / "payload" if (_RAIZ / "payload" / "app").is_dir() else _RAIZ
sys.path.insert(0, str(RAIZ))

import pytest

from app import billing_mercadopago as MP

APP = RAIZ / "app"
SEGREDO = "segredo-de-teste-do-webhook"


def assinar(data_id, request_id, ts=None, secret=SEGREDO):
    ts = str(int(time.time())) if ts is None else str(ts)
    manifesto = MP.montar_manifesto(data_id, request_id, ts)
    v1 = hmac.new(secret.encode(), manifesto.encode(), hashlib.sha256).hexdigest()
    return f"ts={ts},v1={v1}"


# ---------------------------------------------------------- manifesto

def test_manifesto_segue_o_formato_do_mercado_pago():
    assert MP.montar_manifesto("123", "req-9", "1700000000") == \
        "id:123;request-id:req-9;ts:1700000000;"


def test_manifesto_poe_o_data_id_em_minusculas():
    assert MP.montar_manifesto("ABC-Def", "r", "1") == "id:abc-def;request-id:r;ts:1;"


def test_manifesto_omite_segmento_ausente_em_vez_de_deixar_vazio():
    """Deixar 'id:;' em vez de omitir gera hash diferente e recusa tudo."""
    assert MP.montar_manifesto(None, "r", "1") == "request-id:r;ts:1;"
    assert MP.montar_manifesto("x", None, "1") == "id:x;ts:1;"
    assert MP.montar_manifesto(None, None, "1") == "ts:1;"


# ------------------------------------------------------ validação HMAC

def test_assinatura_valida_passa():
    MP.validar_assinatura(assinar("123", "req-9"), "req-9", "123", secret=SEGREDO)


def test_assinatura_ausente_e_recusada():
    with pytest.raises(MP.AssinaturaInvalida, match="ausente"):
        MP.validar_assinatura(None, "req", "123", secret=SEGREDO)


def test_header_malformado_e_recusado():
    for ruim in ("", "abc", "v1=deadbeef", "ts=123", "ts=,v1="):
        with pytest.raises(MP.AssinaturaInvalida):
            MP.validar_assinatura(ruim, "req", "123", secret=SEGREDO)


def test_hash_adulterado_e_recusado():
    sig = assinar("123", "req-9")
    with pytest.raises(MP.AssinaturaInvalida, match="não confere"):
        MP.validar_assinatura(sig[:-1] + ("0" if sig[-1] != "0" else "1"),
                              "req-9", "123", secret=SEGREDO)


def test_data_id_trocado_invalida_a_assinatura():
    """Impede reaproveitar assinatura de um recurso para atacar outro."""
    sig = assinar("123", "req-9")
    with pytest.raises(MP.AssinaturaInvalida):
        MP.validar_assinatura(sig, "req-9", "999", secret=SEGREDO)


def test_segredo_errado_e_recusado():
    sig = assinar("123", "req-9", secret="outro-segredo")
    with pytest.raises(MP.AssinaturaInvalida):
        MP.validar_assinatura(sig, "req-9", "123", secret=SEGREDO)


def test_assinatura_antiga_e_recusada_por_replay():
    antiga = int(time.time()) - 4000
    with pytest.raises(MP.AssinaturaInvalida, match="replay"):
        MP.validar_assinatura(assinar("123", "req", ts=antiga), "req", "123",
                              secret=SEGREDO)


def test_timestamp_em_milissegundos_e_aceito():
    agora = time.time()
    ms = int(agora) * 1000
    manifesto = MP.montar_manifesto("123", "req", str(ms))
    v1 = hmac.new(SEGREDO.encode(), manifesto.encode(), hashlib.sha256).hexdigest()
    MP.validar_assinatura(f"ts={ms},v1={v1}", "req", "123", secret=SEGREDO, agora=agora)


def test_comparacao_de_hash_e_resistente_a_timing():
    fonte = (APP / "billing_mercadopago.py").read_text(encoding="utf-8")
    assert "compare_digest" in fonte, "comparação simples de hash vaza por timing"


# ------------------------------------------------- máquina de estados

def test_mapa_de_status_cobre_todos_os_estados_do_mercado_pago():
    for bruto, esperado in (("pending", "pending"), ("authorized", "active"),
                            ("paused", "past_due"), ("cancelled", "canceled"),
                            ("finished", "canceled")):
        assert MP.status_interno(bruto) == esperado


def test_status_desconhecido_nao_libera_acesso():
    """Padrão seguro: status novo do gateway não pode virar acesso liberado."""
    assert MP.status_interno("estado_que_nao_existe") == "pending"
    assert not MP.tem_acesso(MP.status_interno("estado_que_nao_existe"))


def test_apenas_assinatura_ativa_da_acesso():
    assert MP.tem_acesso("active")
    for s in ("pending", "past_due", "canceled"):
        assert not MP.tem_acesso(s), f"{s} não pode dar acesso"


def test_pagamento_falho_suspende_o_acesso():
    """paused = cobrança falhou. Continuar liberado seria serviço de graça."""
    assert not MP.tem_acesso(MP.status_interno("paused"))


# --------------------------------------------------- referência externa

def test_referencia_externa_identifica_escritorio_e_plano():
    ref = "jarbas:org:42:plano:profissional"
    assert MP.org_da_referencia(ref) == 42
    assert MP.plano_da_referencia(ref) == "profissional"


def test_referencia_invalida_nao_vira_escritorio_qualquer():
    for ruim in ("", "lixo", "jarbas:org:abc:plano:x", "outro:org:1:plano:x"):
        assert MP.org_da_referencia(ruim) is None


# ------------------------------------------------------- credenciais

def test_nenhuma_credencial_no_codigo():
    """Access token em código-fonte vaza em todo zip e todo commit."""
    for arq in sorted(APP.glob("*.py")):
        texto = arq.read_text(encoding="utf-8")
        achados = re.findall(r"(?:TEST|APP_USR)-[0-9A-Za-z_-]{8,}", texto)
        assert not achados, f"{arq.name} contém credencial embutida"


def test_token_e_lido_do_ambiente(monkeypatch):
    monkeypatch.delenv("MERCADOPAGO_ACCESS_TOKEN", raising=False)
    with pytest.raises(MP.BillingError, match="MERCADOPAGO_ACCESS_TOKEN"):
        MP.access_token()
    monkeypatch.setenv("MERCADOPAGO_ACCESS_TOKEN", "TEST-xyz")
    assert MP.access_token() == "TEST-xyz"


def test_sandbox_detectado_pelo_prefixo(monkeypatch):
    monkeypatch.setenv("MERCADOPAGO_ACCESS_TOKEN", "TEST-abc")
    assert MP.em_sandbox()
    monkeypatch.setenv("MERCADOPAGO_ACCESS_TOKEN", "APP_USR-abc")
    assert not MP.em_sandbox()


def test_status_do_gateway_nao_expoe_o_token(monkeypatch):
    monkeypatch.setenv("MERCADOPAGO_ACCESS_TOKEN", "TEST-1234567890abcdef")
    s = MP.status_gateway()
    assert "1234567890" not in str(s)
    assert s["token_dica"].startswith("••••")


def test_webhook_secret_e_diferente_do_access_token():
    """Erro comum: usar o access token como segredo do webhook. São dois."""
    fonte = (APP / "billing_mercadopago.py").read_text(encoding="utf-8")
    assert "MERCADOPAGO_WEBHOOK_SECRET" in fonte
    assert "distinto do" in fonte or "NÃO é o access token" in fonte


def test_url_publica_exige_https(monkeypatch):
    monkeypatch.setenv("JARBAS_PUBLIC_URL", "http://exemplo.com.br")
    with pytest.raises(MP.BillingError, match="https"):
        MP.url_publica()
    monkeypatch.setenv("JARBAS_PUBLIC_URL", "https://exemplo.com.br/")
    assert MP.url_publica() == "https://exemplo.com.br"


# -------------------------------------------------------- rota do webhook

def _rota_webhook() -> str:
    src = (APP / "billing_routes.py").read_text(encoding="utf-8")
    i = src.index('@router.post("/webhooks/mercadopago")')
    j = src.find("\ndef aplicar_evento", i)
    return src[i:j if j > 0 else len(src)]


def test_webhook_valida_assinatura_antes_de_tocar_o_banco():
    corpo = _rota_webhook()
    pos_val = corpo.index("validar_assinatura")
    pos_db = corpo.index("with db()")
    assert pos_val < pos_db, "webhook mexe no banco antes de autenticar"


def test_webhook_recusa_assinatura_invalida_com_401():
    assert "status_code=401" in _rota_webhook()


def test_webhook_nao_confia_no_status_vindo_no_corpo():
    """O corpo diz QUAL recurso mudou; o status vem de consulta autenticada.
    Confiar no corpo deixaria forjar 'authorized' e obter acesso de graça."""
    corpo = _rota_webhook()
    assert "consultar_preapproval" in corpo
    assert 'corpo.get("status")' not in corpo


def test_webhook_e_idempotente():
    """O Mercado Pago reenvia notificações. Processar duas vezes duplica efeito."""
    corpo = _rota_webhook()
    assert "event_key" in corpo and "duplicado" in corpo
    esquema = (APP / "database.py").read_text(encoding="utf-8")
    assert "UNIQUE INDEX IF NOT EXISTS idx_billing_events_key" in esquema


def test_retorno_do_checkout_nao_ativa_assinatura():
    """A URL de retorno é digitável pelo usuário. Ativar ali é acesso de graça."""
    src = (APP / "billing_routes.py").read_text(encoding="utf-8")
    i = src.index('@router.get("/billing/retorno"')
    j = src.index('@router.post("/webhooks/mercadopago")')
    trecho = src[i:j]
    for proibido in ("UPDATE subscriptions", "INSERT INTO subscriptions", "'active'"):
        assert proibido not in trecho, f"/billing/retorno faz {proibido}"


def test_rota_de_assinar_exige_csrf_e_permissao():
    src = (APP / "billing_routes.py").read_text(encoding="utf-8")
    i = src.index('@router.post("/billing/assinar/{plan_code}")')
    j = src.index('@router.get("/billing/retorno"')
    trecho = src[i:j]
    assert "valid_csrf" in trecho
    assert "can_manage_workspace" in trecho


def test_assinatura_nasce_pendente_e_nao_ativa():
    src = (APP / "billing_routes.py").read_text(encoding="utf-8")
    i = src.index('@router.post("/billing/assinar/{plan_code}")')
    j = src.index('@router.get("/billing/retorno"')
    assert "'pending'" in src[i:j]


# --------------------------------------------------------------- schema

def test_tabela_de_eventos_existe_nos_dois_bancos():
    fonte = (APP / "database.py").read_text(encoding="utf-8")
    c = sqlite3.connect(":memory:")
    for n in ("SQLITE_SCHEMA", "V7_SQLITE_EXTRA", "V81_SQLITE_EXTRA",
              "V85_SQLITE_EXTRA", "V87_SQLITE_EXTRA"):
        c.executescript(re.search(rf'{n} = r?"""(.*?)"""', fonte, re.S).group(1))
    cols = {r[1] for r in c.execute("PRAGMA table_info(billing_events)")}
    assert {"event_key", "preapproval_id", "organization_id",
            "status_internal", "processed"} <= cols
    assert "V87_POSTGRES_EXTRA" in fonte


def test_initialize_schema_aplica_a_migracao_87():
    fonte = (APP / "database.py").read_text(encoding="utf-8")
    ultimo = fonte.rindex("def initialize_schema")
    assert "V87_POSTGRES_EXTRA if IS_POSTGRES else V87_SQLITE_EXTRA" in fonte[ultimo:]
    assert 'ensure_column(conn, "subscriptions", "external_id TEXT")' in fonte[ultimo:]
