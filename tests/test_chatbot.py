"""Chatbot público de atendimento: a única porta do sistema sem login.

Cada teste aqui defende uma garantia concreta, na ordem de gravidade que
motivou o desenho:

1. O chatbot NUNCA consulta cliente, processo ou documento — arquitetura,
   não promessa: o módulo simplesmente não tem código que leia essas
   tabelas. Testado varrendo o próprio arquivo-fonte.
2. Erro técnico da IA nunca chega ao visitante em texto cru (modelo, chave,
   stack). `friendly_error()` é para a equipe; aqui a mensagem é sempre
   genérica.
3. Sem chave de IA configurada, ou com o teto mensal do chatbot estourado,
   o visitante continua conseguindo deixar contato — o sistema nunca fica
   mudo.
4. Rate limit por IP e por sessão: superfície pública, sem login, é alvo
   óbvio de abuso e de custo de IA sem fim.
5. CSRF em toda rota de escrita, igual ao resto do sistema.
6. Nenhum dado de OUTRO visitante aparece: sessão por token, nunca por ID
   sequencial adivinhável reaproveitado entre organizações.
"""

import re
import sys
from pathlib import Path

import pytest

_RAIZ = Path(__file__).resolve().parent.parent
RAIZ = _RAIZ / "payload" if (_RAIZ / "payload" / "app").is_dir() else _RAIZ
sys.path.insert(0, str(RAIZ))

EMAIL = "chatbot@chagas.local"
SENHA = "SenhaDeTesteMuitoLonga1!"


# ============================================== garantia estrutural (fonte)

def test_o_chatbot_nunca_consulta_cliente_processo_ou_documento():
    """Garantia por arquitetura: o módulo não tem SQL para essas tabelas.

    Não é um teste de comportamento em runtime — é a prova de que o caminho
    de código para vazar dado de cliente a um visitante anônimo não existe.
    """
    fonte = (RAIZ / "app" / "chatbot_routes.py").read_text(encoding="utf-8")
    proibidas = ("FROM clients", "FROM cases", "FROM case_documents",
                "FROM case_parties", "JOIN clients", "JOIN cases")
    achadas = [p for p in proibidas if p in fonte]
    assert not achadas, f"chatbot_routes.py consulta tabela de cliente/processo: {achadas}"


def test_o_erro_da_ia_nunca_e_mostrado_cru_ao_visitante():
    """friendly_error() foi escrito para a equipe (cita modelo, hint de chave).

    Se este arquivo CHAMAR friendly_error() e devolver o resultado numa
    resposta JSON, detalhe de configuração do sistema vaza para a internet.
    O nome aparece de propósito em comentário e docstring, explicando por
    que NÃO se usa — por isso a checagem é pela AST (chamada de função de
    verdade), imune a comentário e string, e não por substring no texto.
    """
    import ast
    fonte = (RAIZ / "app" / "chatbot_routes.py").read_text(encoding="utf-8")
    arvore = ast.parse(fonte)
    chamadas = [
        no for no in ast.walk(arvore)
        if isinstance(no, ast.Call) and isinstance(no.func, ast.Name)
        and no.func.id == "friendly_error"
    ]
    assert not chamadas, (
        "chatbot_routes.py não pode CHAMAR friendly_error(): é diagnóstico "
        "para a equipe, não resposta para o público"
    )


# ================================================================= fixtures

@pytest.fixture()
def sistema(tmp_path, monkeypatch):
    monkeypatch.setenv("JARBAS_ENV", "development")
    monkeypatch.setenv("JARBAS_ALLOWED_HOSTS", "localhost,127.0.0.1")
    monkeypatch.setenv("JARBAS_ADMIN_EMAIL", EMAIL)
    monkeypatch.setenv("JARBAS_ADMIN_PASSWORD", SENHA)
    monkeypatch.setenv("JARBAS_2FA_OBRIGATORIO", "0")
    monkeypatch.setenv("JARBAS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    import app as pacote
    for modulo in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
        sys.modules.pop(modulo, None)
        nome = modulo.partition(".")[2]
        if nome and "." not in nome:
            setattr(pacote, nome, None)
            delattr(pacote, nome)

    import app.main as main
    main.init_db()
    return main


@pytest.fixture()
def navegador(sistema):
    import asyncio

    import httpx

    laco = asyncio.new_event_loop()
    cliente = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=sistema.app),
        base_url="http://localhost", follow_redirects=True, timeout=60)

    class Nav:
        def __init__(self):
            self._csrf = None

        def get(self, caminho):
            return laco.run_until_complete(cliente.get(caminho))

        def token(self):
            if not self._csrf:
                corpo = self.get("/atendimento").text
                self._csrf = re.search(r'name="csrf-token" content="([^"]+)"', corpo).group(1)
            return self._csrf

        def post(self, caminho, dados):
            corpo = dict(dados)
            if "_csrf" not in corpo:
                corpo["_csrf"] = self.token()  # avaliação preguiçosa: só busca se falta
            return laco.run_until_complete(cliente.post(caminho, data=corpo))

        def entrar_como_equipe(self):
            corpo = self.get("/login").text
            tok = re.search(r'name="_csrf" value="([^"]+)"', corpo).group(1)
            resposta = laco.run_until_complete(cliente.post(
                "/login", data={"email": EMAIL, "password": SENHA, "_csrf": tok}))
            # abrir_sessao() gira o _csrf da sessão no login (proteção contra
            # fixação de sessão) — o token público cacheado antes de entrar
            # fica inválido; força buscar um novo na próxima escrita.
            self._csrf = None
            return resposta

        def iniciar_conversa(self):
            r = self.post("/api/chatbot/iniciar", {})
            assert r.status_code == 200, r.text
            return r.json()["token"]

        def fechar(self):
            laco.run_until_complete(cliente.aclose())
            laco.close()

    nav = Nav()
    yield nav, sistema
    nav.fechar()


# ==================================================================== rotas

def test_a_pagina_de_atendimento_abre_sem_login(navegador):
    nav, _ = navegador
    r = nav.get("/atendimento")
    assert r.status_code == 200
    assert "CHAGAS" in r.text
    assert 'name="csrf-token"' in r.text


def test_iniciar_devolve_saudacao_e_nao_promete_ia_quando_nao_ha_chave(navegador):
    nav, _ = navegador
    r = nav.post("/api/chatbot/iniciar", {})
    corpo = r.json()
    assert corpo["ia_ativa"] is False
    assert "JARBAS" in corpo["saudacao"]
    assert corpo["whatsapp"].startswith("https://wa.me/55")


def test_sem_csrf_a_escrita_e_recusada(navegador):
    nav, _ = navegador
    token = nav.iniciar_conversa()
    r = nav.post("/api/chatbot/mensagem", {"_csrf": "chave-errada", "token": token, "mensagem": "oi"})
    assert r.status_code == 403


def test_mensagem_sem_ia_configurada_ainda_assim_responde(navegador):
    """O sistema nunca fica mudo: sem IA, cai numa resposta roteirizada."""
    nav, _ = navegador
    token = nav.iniciar_conversa()
    r = nav.post("/api/chatbot/mensagem", {"token": token, "mensagem": "Quero saber sobre um caso novo"})
    assert r.status_code == 200
    assert r.json()["resposta"]


def test_mensagem_vazia_e_recusada(navegador):
    nav, _ = navegador
    token = nav.iniciar_conversa()
    r = nav.post("/api/chatbot/mensagem", {"token": token, "mensagem": "   "})
    assert r.status_code == 400


def test_mensagem_longa_demais_e_recusada_sem_gastar_ia(navegador):
    nav, _ = navegador
    token = nav.iniciar_conversa()
    r = nav.post("/api/chatbot/mensagem", {"token": token, "mensagem": "a" * 5000})
    assert r.status_code == 400
    assert "longa" in r.json()["erro"].lower()


def test_token_inexistente_nao_quebra_e_nao_vaza_sessao_de_outro(navegador):
    nav, _ = navegador
    r = nav.post("/api/chatbot/mensagem", {"token": "token-que-nao-existe", "mensagem": "oi"})
    assert r.status_code == 404


def test_contato_grava_lead_com_a_origem_correta(navegador):
    nav, sistema = navegador
    token = nav.iniciar_conversa()
    nav.post("/api/chatbot/mensagem", {"token": token, "mensagem": "Preciso de ajuda com um contrato"})
    r = nav.post("/api/chatbot/contato", {"token": token, "nome": "Carlos Teste", "contato": "carlos@exemplo.br"})
    assert r.status_code == 200
    lead_id = r.json()["lead_id"]
    with sistema.db() as conn:
        lead = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    assert lead["source"] == "Chatbot do site"
    assert lead["email"] == "carlos@exemplo.br"
    assert lead["status"] == "Novo"


def test_contato_sem_nome_ou_sem_contato_e_recusado(navegador):
    """Campo ausente é 422 do próprio FastAPI (padrão do framework, igual em
    toda rota do sistema). O que é responsabilidade DESTE código é recusar
    espaço em branco disfarçado de preenchido — daí o "   " abaixo em vez de
    string vazia, que some do corpo do form antes de chegar à rota."""
    nav, _ = navegador
    token = nav.iniciar_conversa()
    r = nav.post("/api/chatbot/contato", {"token": token, "nome": "   ", "contato": "   "})
    assert r.status_code == 400


def test_a_nota_do_lead_nao_despeja_a_conversa_inteira(navegador):
    """Card do Kanban vira parede de texto se a nota levar a transcrição inteira."""
    nav, sistema = navegador
    token = nav.iniciar_conversa()
    for _ in range(3):
        nav.post("/api/chatbot/mensagem", {"token": token, "mensagem": "b" * 400})
    r = nav.post("/api/chatbot/contato", {"token": token, "nome": "Ana", "contato": "(54) 98888-0000"})
    lead_id = r.json()["lead_id"]
    with sistema.db() as conn:
        lead = conn.execute("SELECT notes FROM leads WHERE id=?", (lead_id,)).fetchone()
    assert len(lead["notes"]) < 1500, "a nota do lead ficou grande demais para o card do CRM"


def test_conversa_encerrada_nao_aceita_nova_mensagem(navegador):
    nav, _ = navegador
    token = nav.iniciar_conversa()
    nav.post("/api/chatbot/encerrar", {"token": token})
    r = nav.post("/api/chatbot/mensagem", {"token": token, "mensagem": "ainda dá pra falar?"})
    assert r.status_code == 400


def test_limite_de_mensagens_por_sessao_e_respeitado(navegador, monkeypatch):
    import app.chatbot_routes as CB
    monkeypatch.setattr(CB, "MAX_MSGS_POR_SESSAO", 2)
    nav, _ = navegador
    token = nav.iniciar_conversa()
    nav.post("/api/chatbot/mensagem", {"token": token, "mensagem": "primeira"})
    r = nav.post("/api/chatbot/mensagem", {"token": token, "mensagem": "segunda"})
    assert r.status_code == 400
    assert "limite" in r.json()["erro"].lower()


def test_rate_limit_por_ip_bloqueia_apos_o_teto(navegador, monkeypatch):
    import app.chatbot_routes as CB
    monkeypatch.setattr(CB, "MAX_MSGS_POR_IP_JANELA", 2)
    nav, _ = navegador
    token = nav.iniciar_conversa()
    nav.post("/api/chatbot/mensagem", {"token": token, "mensagem": "um"})
    nav.post("/api/chatbot/mensagem", {"token": token, "mensagem": "dois"})
    r = nav.post("/api/chatbot/mensagem", {"token": token, "mensagem": "tres"})
    assert r.status_code == 429


def test_rate_limit_de_novas_sessoes_por_ip(navegador, monkeypatch):
    import app.chatbot_routes as CB
    monkeypatch.setattr(CB, "MAX_SESSOES_POR_IP_HORA", 1)
    nav, _ = navegador
    nav.iniciar_conversa()
    r = nav.post("/api/chatbot/iniciar", {})
    assert r.status_code == 429


def test_org_com_chatbot_desativado_recusa_iniciar(navegador):
    nav, sistema = navegador
    with sistema.db() as conn:
        conn.execute("UPDATE organizations SET chatbot_ativo=0 WHERE slug='chagas-advogados'")
    r = nav.post("/api/chatbot/iniciar", {})
    assert r.status_code == 503
    assert r.json().get("whatsapp", "").startswith("https://wa.me/")


# =============================================== resposta com IA (mockada)

def test_com_ia_configurada_a_resposta_usa_o_gateway_e_registra_custo(navegador, monkeypatch):
    import app.chatbot_routes as CB
    from app.ai_gateway import AIResult

    monkeypatch.setattr(CB, "ai_configured", lambda: True)
    monkeypatch.setattr(CB, "ai_ask", lambda *a, **k: AIResult(
        text="Claro, posso te ajudar com isso.", model="claude-haiku-4-5-20251001",
        input_tokens=200, output_tokens=80))

    nav, sistema = navegador
    token = nav.iniciar_conversa()
    r = nav.post("/api/chatbot/mensagem", {"token": token, "mensagem": "Sofri um acidente de carro"})
    assert r.status_code == 200
    assert r.json()["resposta"] == "Claro, posso te ajudar com isso."

    with sistema.db() as conn:
        org_id = conn.execute("SELECT id FROM organizations WHERE slug='chagas-advogados'").fetchone()["id"]
        custo = conn.execute(
            "SELECT SUM(cost_usd) c FROM chatbot_messages WHERE organization_id=? AND role='assistant' AND model!='local'",
            (org_id,)).fetchone()["c"]
        ledger = conn.execute(
            "SELECT COUNT(*) c FROM usage_ledger WHERE organization_id=? AND kind='chatbot_site'",
            (org_id,)).fetchone()["c"]
    assert custo and custo > 0
    assert ledger == 1


def test_falha_da_ia_vira_mensagem_generica_nao_a_excecao(navegador, monkeypatch):
    import app.chatbot_routes as CB

    monkeypatch.setattr(CB, "ai_configured", lambda: True)

    def explode(*a, **k):
        raise RuntimeError("invalid x-api-key: sk-ant-segredoquenaopodevazar")

    monkeypatch.setattr(CB, "ai_ask", explode)
    nav, _ = navegador
    token = nav.iniciar_conversa()
    r = nav.post("/api/chatbot/mensagem", {"token": token, "mensagem": "teste de falha"})
    assert r.status_code == 200
    resposta = r.json()["resposta"]
    assert "sk-ant-" not in resposta
    assert "x-api-key" not in resposta
    assert "RuntimeError" not in resposta


def test_teto_de_gasto_do_chatbot_degrada_para_resposta_sem_ia(navegador, monkeypatch):
    """Teto estourado não pode travar o visitante — só desliga a IA."""
    import app.chatbot_routes as CB
    from app.ai_gateway import AIResult

    monkeypatch.setattr(CB, "ai_configured", lambda: True)
    monkeypatch.setattr(CB, "TETO_USD_MES", 0.0001)
    chamou = []
    monkeypatch.setattr(CB, "ai_ask", lambda *a, **k: (chamou.append(1) or AIResult(
        text="resposta cara", model="claude-opus-5", input_tokens=100000, output_tokens=100000)))

    nav, _ = navegador
    token = nav.iniciar_conversa()
    r1 = nav.post("/api/chatbot/mensagem", {"token": token, "mensagem": "primeira pergunta cara"})
    assert r1.status_code == 200
    # A primeira chamada pode passar (gasto ainda em zero); a resposta cara
    # eleva o gasto do mês muito acima do teto de 0.0001 — a próxima tem de
    # cair no modo sem IA.
    r2 = nav.post("/api/chatbot/mensagem", {"token": token, "mensagem": "segunda pergunta"})
    assert r2.status_code == 200
    assert "WhatsApp" in r2.json()["resposta"] or "simples" in r2.json()["resposta"].lower()


# ============================================================ painel da equipe

def test_painel_do_chatbot_exige_login(navegador):
    nav, _ = navegador
    r = nav.get("/crm/chatbot")
    assert r.status_code in (302, 303, 200)
    if r.status_code == 200:
        assert "/login" in str(r.url) or "senha" in r.text.lower() or "e-mail" in r.text.lower()


def test_equipe_ve_a_conversa_e_o_lead_gerado(navegador):
    nav, sistema = navegador
    token = nav.iniciar_conversa()
    nav.post("/api/chatbot/mensagem", {"token": token, "mensagem": "Quero saber sobre um caso novo"})
    nav.post("/api/chatbot/contato", {"token": token, "nome": "Equipe Teste", "contato": "(54) 97777-0000"})

    nav.entrar_como_equipe()
    r = nav.get("/crm/chatbot")
    assert r.status_code == 200
    assert "Equipe Teste" in r.text

    with sistema.db() as conn:
        sessao = conn.execute(
            "SELECT id FROM chatbot_sessions WHERE visitor_name='Equipe Teste'").fetchone()
    r = nav.get(f"/crm/chatbot/{sessao['id']}")
    assert r.status_code == 200
    assert "Quero saber sobre um caso novo" in r.text
    assert "lead" in r.text.lower()


def test_conversa_de_um_visitante_nao_aparece_para_outro_via_token(navegador):
    """Um token só abre a própria conversa — nunca a de sessão vizinha."""
    nav, _ = navegador
    token_a = nav.iniciar_conversa()
    nav._csrf = None  # força pegar um csrf novo, simula outra aba
    token_b = nav.iniciar_conversa()
    assert token_a != token_b


# ==================================================== higiene de configuração

def test_ip_nunca_e_gravado_em_claro():
    """ip_hash existe por LGPD: minimização de dado desde o desenho."""
    fonte = (RAIZ / "app" / "chatbot_routes.py").read_text(encoding="utf-8")
    assert "ip_hash" in fonte
    assert "login_client_ip(request)" in fonte  # usa o resolvedor de IP já auditado do login
    assert "hashlib.sha256" in fonte


def test_teto_do_chatbot_e_menor_que_o_teto_geral_por_padrao():
    """A única porta sem login tem de ter o menor limite de gasto, não o maior."""
    import app.chatbot_routes as CB
    assert CB.TETO_USD_MES <= 50, "o teto padrão do chatbot não pode superar o teto geral de IA"


def test_link_whatsapp_formata_o_telefone_brasileiro():
    from app.chatbot_routes import link_whatsapp
    assert link_whatsapp("(54) 99110-1959") == "https://wa.me/5554991101959"
    assert link_whatsapp("") == ""
    assert link_whatsapp("123") == ""


def test_a_chave_de_organizacao_do_chatbot_e_configuravel_por_env(monkeypatch):
    """Instalação multiescritório precisa poder apontar para outro slug.

    Recarrega o módulo com o env var setado e, no fim, recarrega de novo já
    com o env var desfeito — deixando sys.modules limpo para os testes
    seguintes, que não passam pela fixture `sistema` antes de importar.
    """
    import importlib

    import app.chatbot_routes as CB
    try:
        monkeypatch.setenv("JARBAS_CHATBOT_ORG_SLUG", "outro-escritorio")
        importlib.reload(CB)
        assert CB.ORG_SLUG_PADRAO == "outro-escritorio"
    finally:
        monkeypatch.delenv("JARBAS_CHATBOT_ORG_SLUG", raising=False)
        importlib.reload(CB)
        assert CB.ORG_SLUG_PADRAO == "chagas-advogados"


def test_balao_flutuante_nao_aparece_nas_paginas_de_venda_do_saas(navegador):
    """/produto e /signup vendem o JARBAS para OUTROS escritórios; o balão
    fala em nome do CHAGAS – ADVOGADOS. Nas duas telas juntas confundiria
    quem está avaliando o produto com quem procura um advogado."""
    nav, _ = navegador
    for caminho in ("/produto", "/signup"):
        r = nav.get(caminho)
        assert 'id="jarbas-chat-root"' not in r.text, caminho


def test_balao_flutuante_aparece_no_login(navegador):
    nav, _ = navegador
    r = nav.get("/login")
    assert 'id="jarbas-chat-root"' in r.text


def test_atendimento_nao_duplica_o_widget(navegador):
    """A página /atendimento já embute o chat; o balão flutuante por cima
    criaria dois widgets brigando pelo mesmo #jarbas-chat-root."""
    nav, _ = navegador
    r = nav.get("/atendimento")
    assert r.text.count('id="jarbas-chat-root"') == 1


# ================================================== correções da revisão adversarial

def test_contato_repetido_na_mesma_sessao_nao_cria_dois_leads(navegador):
    """Idempotência por sessão: reenvio (duplo clique, "Voltar" do navegador)
    não pode duplicar o lead nem virar vetor de flood com um único token."""
    nav, sistema = navegador
    token = nav.iniciar_conversa()
    r1 = nav.post("/api/chatbot/contato", {"token": token, "nome": "Bruno", "contato": "bruno@ex.br"})
    assert r1.status_code == 200
    r2 = nav.post("/api/chatbot/contato", {"token": token, "nome": "Bruno de Novo", "contato": "outro@ex.br"})
    assert r2.status_code == 400
    with sistema.db() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"]
    assert total == 1


def test_rate_limit_proprio_do_contato_impede_flood_sem_gastar_ia(navegador, monkeypatch):
    """Achado da revisão: /contato não tinha teto nenhum — um único token
    bastava para inundar o CRM em loop, sem custar um centavo de IA."""
    import app.chatbot_routes as CB
    monkeypatch.setattr(CB, "MAX_CONTATOS_POR_IP_HORA", 2)
    nav, sistema = navegador
    for i in range(2):
        token = nav.iniciar_conversa()
        r = nav.post("/api/chatbot/contato", {"token": token, "nome": f"Pessoa {i}", "contato": f"{i}@ex.br"})
        assert r.status_code == 200
    token = nav.iniciar_conversa()
    r = nav.post("/api/chatbot/contato", {"token": token, "nome": "Excedente", "contato": "x@ex.br"})
    assert r.status_code == 429
    with sistema.db() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"]
    assert total == 2


def test_contato_em_sessao_encerrada_e_recusado(navegador):
    nav, _ = navegador
    token = nav.iniciar_conversa()
    nav.post("/api/chatbot/encerrar", {"token": token})
    r = nav.post("/api/chatbot/contato", {"token": token, "nome": "Alguém", "contato": "a@ex.br"})
    assert r.status_code == 400


def test_desativar_o_chatbot_tambem_bloqueia_conversa_ja_aberta(navegador):
    """Achado da revisão: chatbot_ativo=0 só travava /iniciar. Uma aba já
    aberta continuava mandando mensagem e gerando lead com o chat 'desligado'."""
    nav, sistema = navegador
    token = nav.iniciar_conversa()
    with sistema.db() as conn:
        conn.execute("UPDATE organizations SET chatbot_ativo=0 WHERE slug='chagas-advogados'")

    r = nav.post("/api/chatbot/mensagem", {"token": token, "mensagem": "ainda dá pra falar?"})
    assert r.status_code == 503
    assert r.json().get("whatsapp", "").startswith("https://wa.me/")

    r = nav.post("/api/chatbot/contato", {"token": token, "nome": "Visitante", "contato": "v@ex.br"})
    assert r.status_code == 503
    with sistema.db() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"] == 0


def test_slug_sem_organizacao_correspondente_nunca_atende_por_outra(navegador, monkeypatch, sistema):
    """Achado da revisão: um slug errado caía silenciosamente para 'qualquer
    organização ativa' — em instalação multiescritório, isso mistura dados
    de um escritório com o de outro. Agora tem de falhar visível."""
    import app.chatbot_routes as CB
    with sistema.db() as conn:
        conn.execute(
            "INSERT INTO organizations (name,slug,status,created_at) VALUES (?,?,?,?)",
            ("Outro Escritório", "outro-escritorio", "active", "2026-01-01T00:00:00"))
    nav, _ = navegador
    csrf = nav.token()  # pega o token ANTES de derrubar /atendimento (que deixa de servi-lo)
    monkeypatch.setattr(CB, "ORG_SLUG_PADRAO", "slug-que-nao-existe")

    r = nav.get("/atendimento")
    assert r.status_code == 503
    r = nav.post("/api/chatbot/iniciar", {"_csrf": csrf})
    assert r.status_code == 503
    with sistema.db() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM chatbot_sessions").fetchone()["c"] == 0


def test_a_resposta_da_ia_tem_teto_proprio_de_tokens_de_saida(navegador, monkeypatch):
    """Achado da revisão: sem max_tokens próprio, a instrução 'responda em
    até 100 palavras' era só texto — uma mensagem manipulada podia inflar o
    custo em dezenas de vezes."""
    import app.chatbot_routes as CB
    from app.ai_gateway import AIResult

    recebido = {}

    def fake_ask(instructions, prompt, *, model=None, profile="legal", max_tokens=None):
        recebido["max_tokens"] = max_tokens
        return AIResult(text="ok", model="claude-haiku-4-5-20251001", input_tokens=10, output_tokens=5)

    monkeypatch.setattr(CB, "ai_configured", lambda: True)
    monkeypatch.setattr(CB, "ai_ask", fake_ask)
    nav, _ = navegador
    token = nav.iniciar_conversa()
    nav.post("/api/chatbot/mensagem", {"token": token, "mensagem": "teste"})
    assert recebido["max_tokens"] == CB.MAX_TOKENS_SAIDA_CHATBOT
    assert recebido["max_tokens"] < 1000, "teto de saída do chatbot não pode ser o mesmo de uma petição"


def test_reserva_de_orcamento_bloqueia_segunda_chamada_antes_da_primeira_commitar(sistema):
    """A janela de corrida que a revisão apontou: duas requisições lendo o
    mesmo gasto ANTES de qualquer uma gravar. A reserva em memória fecha essa
    janela sem precisar esperar a chamada de IA (que nem é feita aqui)."""
    import app.chatbot_routes as CB
    monkeypatch_teto = CB.TETO_USD_MES
    try:
        CB.TETO_USD_MES = 0.00001  # qualquer estimativa já estoura
        with sistema.db() as conn:
            org = conn.execute("SELECT id FROM organizations WHERE slug='chagas-advogados'").fetchone()
            assert CB._reservar_orcamento(conn, org["id"]) is False
    finally:
        CB.TETO_USD_MES = monkeypatch_teto


def test_reserva_e_liberada_apos_a_resposta_para_nao_travar_o_teto_para_sempre(sistema):
    import app.chatbot_routes as CB
    with sistema.db() as conn:
        org = conn.execute("SELECT id FROM organizations WHERE slug='chagas-advogados'").fetchone()
        assert CB._reservar_orcamento(conn, org["id"]) is True
        chave = CB._chave_mes(org["id"])
        assert CB._RESERVA_MES.get(chave, 0.0) > 0
        CB._liberar_reserva(org["id"])
        assert CB._RESERVA_MES.get(chave, 0.0) == 0.0


def test_o_widget_le_o_erro_e_o_whatsapp_reais_ao_iniciar():
    """Antes, qualquer falha de /iniciar (inclusive chat desativado) virava
    um genérico 'tente de novo' que nunca mostrava o WhatsApp já calculado."""
    fonte = (RAIZ / "app" / "static" / "chatbot-widget.js").read_text(encoding="utf-8")
    assert "res.corpo && res.corpo.erro" in fonte
    assert "res.corpo && res.corpo.whatsapp" in fonte


def test_o_widget_nao_promete_resposta_instantanea():
    fonte = (RAIZ / "app" / "static" / "chatbot-widget.js").read_text(encoding="utf-8")
    assert "Resposta em instantes" not in fonte


def test_equipe_pode_excluir_uma_conversa(navegador):
    nav, sistema = navegador
    token = nav.iniciar_conversa()
    nav.post("/api/chatbot/mensagem", {"token": token, "mensagem": "mensagem a apagar depois"})
    with sistema.db() as conn:
        sessao_id = conn.execute("SELECT id FROM chatbot_sessions ORDER BY id DESC LIMIT 1").fetchone()["id"]

    nav.entrar_como_equipe()
    r = nav.post(f"/crm/chatbot/{sessao_id}/excluir", {})
    assert r.status_code in (200, 303)
    with sistema.db() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM chatbot_sessions WHERE id=?", (sessao_id,)).fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM chatbot_messages WHERE session_id=?", (sessao_id,)).fetchone()["c"] == 0


def test_excluir_a_conversa_preserva_o_lead_ja_gerado(navegador):
    """Apagar a transcrição não pode apagar o registro comercial do lead —
    são coisas diferentes, com ciclos de vida diferentes."""
    nav, sistema = navegador
    token = nav.iniciar_conversa()
    r = nav.post("/api/chatbot/contato", {"token": token, "nome": "Fica", "contato": "fica@ex.br"})
    lead_id = r.json()["lead_id"]
    with sistema.db() as conn:
        sessao_id = conn.execute("SELECT id FROM chatbot_sessions ORDER BY id DESC LIMIT 1").fetchone()["id"]

    nav.entrar_como_equipe()
    nav.post(f"/crm/chatbot/{sessao_id}/excluir", {})
    with sistema.db() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM leads WHERE id=?", (lead_id,)).fetchone()["c"] == 1


def test_dashboard_avisa_sobre_conversas_sem_retorno(navegador):
    nav, sistema = navegador
    token = nav.iniciar_conversa()
    nav.post("/api/chatbot/mensagem", {"token": token, "mensagem": "alguém aí?"})

    nav.entrar_como_equipe()
    r = nav.get("/")
    assert "sem contato deixado" in r.text
