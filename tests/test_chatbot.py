"""Atendimento JARBAS — o chatbot do escritório.

Este arquivo testa o que pode virar dano real:

1. Uma resposta errada ao cliente já saiu. Não há revisão do advogado
   entre a resposta e o WhatsApp dele. Por isso os testes de conteúdo
   (sem promessa, sem precedente, sem honorário inventado) são os mais
   duros daqui.
2. A triagem decide a ORDEM da fila. Se "preso em flagrante" não subir
   para CRÍTICO, o caso fica atrás de uma dúvida de contrato.
3. A agenda proposta tem de existir. Oferecer sábado ou 17h30 gera um
   cliente na porta em horário em que não há ninguém.

Vale a regra do CLAUDE.md: teste que só casa string no arquivo não vale.
Aqui as funções são CHAMADAS, e as rotas sobem com banco de verdade.
"""

import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

_RAIZ = Path(__file__).resolve().parent.parent
RAIZ = _RAIZ / "payload" if (_RAIZ / "payload" / "app").is_dir() else _RAIZ
sys.path.insert(0, str(RAIZ))

from app import chatbot  # noqa: E402

EMAIL = "chatbot@chagas.local"
SENHA = "SenhaDeTesteMuitoLonga1!"

# Uma sexta-feira útil, fora do recesso forense (art. 220 do CPC).
SEXTA = datetime(2026, 3, 6, 8, 0)


# ------------------------------------------------------------------ triagem

@pytest.mark.parametrize("relato,area,risco", [
    ("Meu marido foi preso em flagrante ontem à noite", "criminal", "CRÍTICO"),
    ("Tenho audiência de custódia amanhã de manhã", "criminal", "CRÍTICO"),
    ("Fui demitido por justa causa e não recebi as verbas rescisórias", "trabalhista", "MÉDIO"),
    ("Quero entrar com divórcio e definir a guarda dos meus filhos", "familia", "MÉDIO"),
    ("O banco fez busca e apreensão do meu carro", "bancario", "ALTO"),
    ("Comprei uma geladeira com defeito e a loja não troca", "consumidor", "MÉDIO"),
    ("O INSS indeferiu meu auxílio-doença", "previdenciario", "MÉDIO"),
    ("Recebi um auto de infração ambiental da FEPAM com prazo de defesa", "ambiental", "ALTO"),
])
def test_a_triagem_acerta_area_e_risco(relato, area, risco):
    c = chatbot.classificar(relato)
    assert c.area.codigo == area, f"{relato!r} caiu em {c.area.codigo}"
    assert c.risco == risco, f"{relato!r} classificado como {c.risco} ({c.motivos})"


def test_risco_alto_ou_critico_sempre_declara_o_motivo():
    """Risco sem motivo não é classificação, é palpite — e não se audita."""
    for relato in ("Meu filho está preso", "Recebi uma intimação com prazo",
                   "Tem leilão do meu imóvel marcado"):
        c = chatbot.classificar(relato)
        assert c.prioritario, relato
        assert c.motivos, f"{relato!r} subiu de risco sem motivo declarado"


def test_saudacao_sozinha_nao_inventa_risco_nem_motivo():
    """'bom dia' não é um caso. Herdar o risco mínimo da área cível faria a
    fila encher de MÉDIO com um motivo que ninguém escreveu."""
    c = chatbot.classificar("bom dia")
    assert c.fluxo == "#INICIO"
    assert c.risco == "BAIXO"
    assert c.motivos == ()


def test_relato_sem_gatilho_nao_desce_para_baixo():
    """Relato longo que não acendeu gatilho é risco NÃO IDENTIFICADO, e o
    piso honesto disso é médio — nunca baixo."""
    c = chatbot.classificar("Preciso de ajuda com uma situação chata que "
                            "estou vivendo com o meu vizinho já faz meses")
    assert c.risco == "MÉDIO"
    assert c.motivos


def test_numero_de_processo_identifica_cliente_existente():
    c = chatbot.classificar("como está o 5001234-56.2026.8.21.0041?")
    assert c.fluxo == "#CLIENTE_EXISTENTE"
    assert c.processo == "5001234-56.2026.8.21.0041"


def test_os_quatro_fluxos_da_diretriz_existem_e_sao_alcancaveis():
    alcancados = {
        chatbot.detectar_fluxo("bom dia"),
        chatbot.detectar_fluxo("fui demitido sem justa causa"),
        chatbot.detectar_fluxo("quero saber do meu processo"),
        chatbot.detectar_fluxo("", historico=[{"role": "user", "content": "oi"}]),
    }
    assert alcancados == set(chatbot.FLUXOS)


# ------------------------------------------------------------------- agenda

def test_agenda_so_propoe_dia_util_dentro_da_janela():
    """Diretriz §15: dia útil, 08h–17h, 60 minutos."""
    for horario in chatbot.horarios_disponiveis(SEXTA, quantidade=8):
        inicio = horario.inicio
        assert inicio.weekday() < 5, f"{inicio} cai em fim de semana"
        assert chatbot.JANELA_INICIO <= inicio.hour, inicio
        assert horario.fim.hour <= chatbot.JANELA_FIM, f"{inicio} termina depois das 17h"
        assert horario.fim.date() == inicio.date()


def test_a_consulta_de_60_minutos_precisa_caber_antes_das_17h():
    """16h30 está dentro da janela e mesmo assim é inválido: a consulta
    terminaria 17h30. Foi por isso que a checagem olha o FIM, não o início."""
    assert chatbot.horario_valido(datetime(2026, 3, 6, 16, 0))
    assert not chatbot.horario_valido(datetime(2026, 3, 6, 16, 30))
    assert not chatbot.horario_valido(datetime(2026, 3, 6, 17, 0))


def test_agenda_pula_sabado_domingo_e_feriado():
    sexta = datetime(2026, 3, 6, 16, 30)   # depois do último horário do dia
    proximo = chatbot.horarios_disponiveis(sexta, quantidade=1)[0]
    assert proximo.inicio.date() == date(2026, 3, 9), proximo.inicio  # segunda

    # 21/04 é Tiradentes: a agenda tem de saltar para 22/04.
    vespera = datetime(2026, 4, 20, 16, 30)
    assert chatbot.horarios_disponiveis(vespera, quantidade=1)[0].inicio.date() == date(2026, 4, 22)


def test_agenda_respeita_horario_ja_ocupado():
    livres = chatbot.horarios_disponiveis(SEXTA, quantidade=3)
    ocupado = livres[0].inicio
    depois = chatbot.horarios_disponiveis(SEXTA, quantidade=3, ocupados=[ocupado])
    assert ocupado not in [h.inicio for h in depois]


def test_agenda_nunca_propoe_horario_no_passado():
    tarde = datetime(2026, 3, 6, 14, 30)
    for h in chatbot.horarios_disponiveis(tarde, quantidade=4):
        assert h.inicio > tarde


# --------------------------------------------------- conteúdo da resposta

def _resposta_roteiro(relato, **kw):
    kw.setdefault("agora", SEXTA)
    return chatbot.responder(relato, forcar_roteiro=True, **kw)


RELATOS = [
    "Meu marido foi preso em flagrante",
    "Fui demitido por justa causa",
    "Quero me divorciar e discutir a guarda",
    "O banco me cobrou tarifa que não contratei",
    "bom dia",
    "como está o processo 5001234-56.2026.8.21.0041?",
]


@pytest.mark.parametrize("relato", RELATOS)
def test_o_roteiro_nunca_promete_resultado_nem_cita_precedente(relato):
    """O teste mais importante do arquivo: o texto vai para o cliente."""
    r = _resposta_roteiro(relato)
    assert r.alertas == (), f"{relato!r} produziu texto com alerta: {r.alertas}"


@pytest.mark.parametrize("relato", RELATOS)
def test_toda_resposta_do_roteiro_traz_o_rodape_institucional(relato):
    r = _resposta_roteiro(relato)
    assert chatbot.ESCRITORIO["telefone"] in r.texto
    assert chatbot.ESCRITORIO["email"] in r.texto
    assert chatbot.ESCRITORIO["endereco"] in r.texto


def test_o_roteiro_informa_a_consulta_de_250_e_nao_estipula_honorario():
    r = _resposta_roteiro("Fui demitido por justa causa")
    assert "R$ 250,00" in r.texto
    assert len(re.findall(r"R\$\s*[\d.]+,\d{2}", r.texto)) == 1, (
        "apareceu outro valor além da consulta — o chatbot não orça serviço")


def test_o_roteiro_faz_as_perguntas_da_area_triada():
    r = _resposta_roteiro("Fui demitido por justa causa e não recebi nada")
    area = chatbot.AREAS_POR_CODIGO["trabalhista"]
    for pergunta in area.perguntas:
        assert pergunta in r.texto


def test_caso_critico_aciona_o_advogado_no_texto():
    r = _resposta_roteiro("Meu marido foi preso em flagrante ontem")
    assert chatbot.ESCRITORIO["advogado"] in r.texto
    assert "urgente" in r.texto.lower()


def test_cliente_existente_nao_recebe_andamento_inventado():
    r = _resposta_roteiro("e aí, meu processo andou?")
    assert r.classificacao.fluxo == "#CLIENTE_EXISTENTE"
    baixo = r.texto.lower()
    assert "não vou adiantar prazo de decisão" in baixo or "depende do juízo" in baixo
    for inventado in ("sentença", "deferido", "julgado procedente", "audiência marcada para"):
        assert inventado not in baixo, f"o roteiro afirmou {inventado!r} sem ler os autos"


def test_a_resposta_so_oferece_horario_que_a_agenda_devolveu():
    r = _resposta_roteiro("Quero marcar uma consulta sobre um contrato")
    for horario in r.horarios:
        assert str(horario) in r.texto
    assert "sábado" not in r.texto and "domingo" not in r.texto


def test_sem_horario_livre_a_resposta_nao_inventa_data():
    texto = chatbot.texto_dos_horarios([])
    assert "Confirme a agenda" in texto
    assert not re.search(r"\d{2}/\d{2}", texto)


# ------------------------------------------------------ revisão da saída

def test_revisar_saida_pega_promessa_de_resultado():
    alertas = chatbot.revisar_saida("Pode ficar tranquilo, garanto que você vai ganhar essa ação.")
    assert any("promessa de resultado" in a for a in alertas)


def test_revisar_saida_pega_promessa_de_prazo_do_judiciario():
    alertas = chatbot.revisar_saida("A decisão sai em 30 dias, no máximo.")
    assert any("prazo de decisão" in a for a in alertas)


def test_revisar_saida_pega_citacao_de_precedente_no_atendimento():
    alertas = chatbot.revisar_saida("O STJ já decidiu isso no REsp 1.234.567.")
    assert any("citação" in a for a in alertas)


def test_revisar_saida_pega_valor_que_nao_e_o_da_consulta():
    alertas = chatbot.revisar_saida("Seu caso vale uns R$ 40.000,00 de dano moral.")
    assert any("valor em reais" in a for a in alertas)


def test_revisar_saida_aceita_o_valor_da_consulta():
    assert chatbot.revisar_saida("A consulta é de R$ 250,00 e dura 60 minutos.") == ()


# ------------------------------------------------------------------- LGPD

def test_mascaramento_cobre_os_identificadores_diretos():
    texto = ("Meu CPF é 123.456.789-09, o CNPJ da empresa é 12.345.678/0001-90, "
             "meu telefone é (54) 99110-1959, e-mail joao@exemplo.br, "
             "cartão 4111 1111 1111 1111.")
    limpo = chatbot.mascarar_sensiveis(texto)
    for cru in ("123.456.789-09", "12.345.678/0001-90", "99110-1959",
                "joao@exemplo.br", "4111 1111 1111 1111"):
        assert cru not in limpo, f"{cru} vazou para a trilha"
    assert "[CPF]" in limpo and "[CNPJ]" in limpo and "[cartão]" in limpo


def test_o_resumo_que_vai_ao_crm_ja_sai_mascarado():
    resumo = chatbot.resumo_do_atendimento(
        [{"role": "user", "content": "Sou o João, CPF 123.456.789-09, fui demitido"}],
        chatbot.classificar("fui demitido"))
    assert "123.456.789-09" not in resumo
    assert "[CPF]" in resumo


# ------------------------------------------------------------ erro de IA

def test_falha_da_ia_levanta_erro_em_vez_de_virar_atendimento(monkeypatch):
    """Diretriz do projeto: erro de IA nunca vira conteúdo. Um pedido de
    desculpas do modelo gravado como resposta do escritório já é uma
    mensagem enviada ao cliente em nome do advogado."""
    monkeypatch.setattr(chatbot, "ia_disponivel", lambda: True)

    def explode(*a, **k):
        raise RuntimeError("401 chave recusada")

    monkeypatch.setattr(chatbot, "gateway_ask", explode)
    with pytest.raises(chatbot.ChatbotError):
        chatbot.responder("fui demitido", agora=SEXTA)


def test_resposta_vazia_da_ia_tambem_e_erro(monkeypatch):
    class _R:
        text = "   "
        model = "claude-x"
        input_tokens = output_tokens = 0

    monkeypatch.setattr(chatbot, "ia_disponivel", lambda: True)
    monkeypatch.setattr(chatbot, "gateway_ask", lambda *a, **k: _R())
    with pytest.raises(chatbot.ChatbotError):
        chatbot.responder("fui demitido", agora=SEXTA)


def test_a_ia_recebe_apenas_os_horarios_reais_no_contexto(monkeypatch):
    """Se a agenda não entrar no contexto, o modelo inventa data. O teste
    prova que ela entra — e que o prompt proíbe inventar quando não há."""
    capturado = {}

    class _R:
        text = "resposta"
        model = "claude-x"
        input_tokens = output_tokens = 0

    def espiao(instrucoes, entrada, **k):
        capturado["instrucoes"] = instrucoes
        capturado["entrada"] = entrada
        return _R()

    monkeypatch.setattr(chatbot, "ia_disponivel", lambda: True)
    monkeypatch.setattr(chatbot, "gateway_ask", espiao)
    r = chatbot.responder("quero marcar consulta", agora=SEXTA)

    for horario in r.horarios:
        assert str(horario) in capturado["entrada"]
    assert "ZERO INVENÇÃO" in capturado["instrucoes"]
    assert "Não invente horário" in capturado["instrucoes"]
    # Rodapé ausente no texto do modelo: o código anexa, não deixa sair sem.
    assert chatbot.ESCRITORIO["telefone"] in r.texto


def test_sem_chave_o_chatbot_atende_pelo_roteiro(monkeypatch):
    monkeypatch.setattr(chatbot, "ia_disponivel", lambda: False)
    r = chatbot.responder("fui demitido por justa causa", agora=SEXTA)
    assert r.modelo == "roteiro"
    assert not r.por_ia
    assert chatbot.ESCRITORIO["nome"] in r.texto


# ------------------------------------------------------------------ rotas

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
def nav(sistema):
    import asyncio
    import httpx

    class Navegador:
        def __init__(self, app):
            self._loop = asyncio.new_event_loop()
            self._c = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://localhost", follow_redirects=True, timeout=60)

        def get(self, caminho):
            return self._loop.run_until_complete(self._c.get(caminho))

        def post(self, caminho, dados):
            dados = dict(dados)
            dados["_csrf"] = self.token()
            return self._loop.run_until_complete(self._c.post(caminho, data=dados))

        def token(self):
            corpo = self.get("/atendimento").text
            return re.search(r'name="_csrf" value="([^"]+)"', corpo).group(1)

        def entrar(self):
            corpo = self.get("/login").text
            token = re.search(r'name="_csrf" value="([^"]+)"', corpo).group(1)
            return self._loop.run_until_complete(self._c.post(
                "/login", data={"email": EMAIL, "password": SENHA, "_csrf": token}))

        def fechar(self):
            self._loop.run_until_complete(self._c.aclose())
            self._loop.close()

    navegador = Navegador(sistema.app)
    navegador.entrar()
    yield navegador, sistema
    navegador.fechar()


def _abrir_conversa(navegador, nome="Maria de Souza"):
    navegador.post("/atendimento/nova",
                   {"contato_nome": nome, "contato_telefone": "(54) 98888-0000",
                    "canal": "whatsapp", "observacoes": ""})
    corpo = navegador.get("/atendimento").text
    return int(re.search(r"/atendimento\?conversa=(\d+)", corpo).group(1))


def test_a_tela_de_atendimento_abre_vazia(nav):
    navegador, _ = nav
    r = navegador.get("/atendimento")
    assert r.status_code == 200
    assert "Atendimento JARBAS" in r.text


def test_o_ciclo_completo_grava_conversa_triagem_e_resposta(nav):
    navegador, main = nav
    conversa = _abrir_conversa(navegador)
    navegador.post(f"/atendimento/{conversa}/mensagem",
                   {"mensagem": "Meu marido foi preso em flagrante ontem em Canela"})

    with main.db() as c:
        linha = c.execute("SELECT * FROM chat_conversations WHERE id=?", (conversa,)).fetchone()
        mensagens = c.execute(
            "SELECT * FROM chat_messages WHERE conversation_id=? ORDER BY id", (conversa,)).fetchall()

    assert linha["risco"] == "CRÍTICO"
    assert linha["area"] == "criminal"
    assert [m["role"] for m in mensagens] == ["user", "assistant"]
    assert chatbot.ESCRITORIO["telefone"] in mensagens[1]["content"]

    corpo = navegador.get(f"/atendimento?conversa={conversa}").text
    assert "CRÍTICO" in corpo
    assert "Criminal e execução penal" in corpo


def test_sem_chave_de_ia_o_atendimento_nao_debita_credito(nav):
    """O roteiro não chama ninguém. Cobrar por ele faria o escritório pagar
    exatamente quando o sistema está sem chave."""
    navegador, main = nav
    conversa = _abrir_conversa(navegador)
    navegador.post(f"/atendimento/{conversa}/mensagem", {"mensagem": "fui demitido"})
    with main.db() as c:
        usados = c.execute("SELECT COUNT(*) t FROM usage_ledger WHERE kind='atendimento'").fetchone()["t"]
    assert usados == 0


def test_a_conversa_vira_lead_no_crm_com_resumo_mascarado(nav):
    navegador, main = nav
    conversa = _abrir_conversa(navegador, nome="João da Silva")
    navegador.post(f"/atendimento/{conversa}/mensagem",
                   {"mensagem": "Sou o João, CPF 123.456.789-09, fui demitido por justa causa"})
    navegador.post(f"/atendimento/{conversa}/lead", {})

    with main.db() as c:
        lead = c.execute("SELECT * FROM leads ORDER BY id DESC LIMIT 1").fetchone()
        linha = c.execute("SELECT * FROM chat_conversations WHERE id=?", (conversa,)).fetchone()

    assert lead["name"] == "João da Silva"
    assert lead["area"] == "Trabalhista"
    assert "123.456.789-09" not in (lead["notes"] or "")
    assert linha["lead_id"] == lead["id"]
    assert linha["status"] == "encaminhado"


def test_encaminhar_duas_vezes_nao_duplica_o_lead(nav):
    navegador, main = nav
    conversa = _abrir_conversa(navegador)
    navegador.post(f"/atendimento/{conversa}/mensagem", {"mensagem": "fui demitido"})
    navegador.post(f"/atendimento/{conversa}/lead", {})
    navegador.post(f"/atendimento/{conversa}/lead", {})
    with main.db() as c:
        total = c.execute("SELECT COUNT(*) t FROM leads").fetchone()["t"]
    assert total == 1


def test_encerrar_e_reabrir_mudam_a_situacao(nav):
    navegador, main = nav
    conversa = _abrir_conversa(navegador)
    navegador.post(f"/atendimento/{conversa}/encerrar", {"status": "encerrado"})
    with main.db() as c:
        assert c.execute("SELECT status FROM chat_conversations WHERE id=?",
                         (conversa,)).fetchone()["status"] == "encerrado"
    navegador.post(f"/atendimento/{conversa}/encerrar", {"status": "aberto"})
    with main.db() as c:
        assert c.execute("SELECT status FROM chat_conversations WHERE id=?",
                         (conversa,)).fetchone()["status"] == "aberto"


def test_csrf_invalido_e_recusado(nav):
    navegador, main = nav
    conversa = _abrir_conversa(navegador)
    r = navegador._loop.run_until_complete(navegador._c.post(
        f"/atendimento/{conversa}/mensagem",
        data={"mensagem": "teste", "_csrf": "token-falso"}))
    assert r.status_code == 403
    with main.db() as c:
        total = c.execute("SELECT COUNT(*) t FROM chat_messages").fetchone()["t"]
    assert total == 0


def test_conversa_de_outro_escritorio_nao_e_alcancavel(nav):
    """Isolamento multi-tenant: a conversa existe, mas não para este org."""
    navegador, main = nav
    agora = datetime.now().isoformat(timespec="seconds")
    with main.db() as c:
        outro = c.insert_id(
            "INSERT INTO organizations (name,slug,created_at) VALUES (?,?,?)",
            ("Outro Escritório", "outro-escritorio", agora))
        alheia = c.insert_id(
            """INSERT INTO chat_conversations (organization_id,canal,contato_nome,
               status,created_at,updated_at) VALUES (?,?,?,'aberto',?,?)""",
            (outro, "whatsapp", "Contato alheio", agora, agora))

    corpo = navegador.get(f"/atendimento?conversa={alheia}").text
    assert "Contato alheio" not in corpo

    navegador.post(f"/atendimento/{alheia}/mensagem", {"mensagem": "vazou?"})
    with main.db() as c:
        total = c.execute("SELECT COUNT(*) t FROM chat_messages WHERE conversation_id=?",
                          (alheia,)).fetchone()["t"]
    assert total == 0, "gravou mensagem em conversa de outro escritório"


def test_a_fila_coloca_o_caso_critico_na_frente(nav):
    navegador, main = nav
    calmo = _abrir_conversa(navegador, nome="Contato Calmo")
    navegador.post(f"/atendimento/{calmo}/mensagem",
                   {"mensagem": "queria tirar uma dúvida sobre um contrato de aluguel"})
    urgente = _abrir_conversa(navegador, nome="Contato Urgente")
    navegador.post(f"/atendimento/{urgente}/mensagem",
                   {"mensagem": "meu filho foi preso em flagrante agora"})

    corpo = navegador.get("/atendimento").text
    assert corpo.index("Contato Urgente") < corpo.index("Contato Calmo"), (
        "a fila ignorou o risco: caso crítico atrás de dúvida de contrato")


def test_resposta_com_ia_debita_credito_uma_vez_so(nav, monkeypatch):
    navegador, main = nav
    import app.chatbot as cb

    class _R:
        text = "Bom dia! Vamos analisar seu caso."
        model = "claude-teste"
        input_tokens = output_tokens = 10

    monkeypatch.setattr(cb, "ia_disponivel", lambda: True)
    monkeypatch.setattr(cb, "gateway_ask", lambda *a, **k: _R())

    conversa = _abrir_conversa(navegador)
    navegador.post(f"/atendimento/{conversa}/mensagem", {"mensagem": "fui demitido"})

    with main.db() as c:
        linhas = c.execute("SELECT credits FROM usage_ledger WHERE kind='atendimento'").fetchall()
        msgs = c.execute("SELECT * FROM chat_messages WHERE role='assistant'").fetchall()
    assert [l["credits"] for l in linhas] == [2]
    assert msgs[0]["model"] == "claude-teste"
    # Rodapé ausente na resposta do modelo: o sistema anexa antes de gravar.
    assert cb.ESCRITORIO["telefone"] in msgs[0]["content"]


def test_credito_e_reservado_antes_de_chamar_a_ia(nav, monkeypatch):
    """Debitar depois é pagar a API para só então descobrir que o teto do
    plano já tinha estourado. Sem crédito, a IA não chega a ser chamada e o
    atendimento continua pelo roteiro."""
    navegador, main = nav
    import app.chatbot as cb

    chamadas = []
    monkeypatch.setattr(cb, "ia_disponivel", lambda: True)
    monkeypatch.setattr(cb, "gateway_ask", lambda *a, **k: chamadas.append(1))
    monkeypatch.setattr(main, "_consume_copilot_credits", lambda *a, **k: False)

    conversa = _abrir_conversa(navegador)
    navegador.post(f"/atendimento/{conversa}/mensagem", {"mensagem": "fui demitido"})

    assert chamadas == [], "a IA foi chamada mesmo sem crédito disponível"
    with main.db() as c:
        resposta = c.execute("SELECT * FROM chat_messages WHERE role='assistant'").fetchone()
    assert resposta["model"] == "roteiro"
    assert "R$ 250,00" in resposta["content"]


def test_falha_da_ia_nao_grava_resposta_na_conversa(nav, monkeypatch):
    navegador, main = nav
    import app.chatbot as cb

    def explode(*a, **k):
        raise RuntimeError("401 chave recusada")

    monkeypatch.setattr(cb, "ia_disponivel", lambda: True)
    monkeypatch.setattr(cb, "gateway_ask", explode)

    conversa = _abrir_conversa(navegador)
    navegador.post(f"/atendimento/{conversa}/mensagem", {"mensagem": "fui demitido"})

    with main.db() as c:
        papeis = [m["role"] for m in c.execute(
            "SELECT role FROM chat_messages WHERE conversation_id=? ORDER BY id",
            (conversa,)).fetchall()]
    assert papeis == ["user"], "gravou resposta apesar da falha de IA"


def test_o_botao_de_roteiro_ignora_a_ia_e_nao_debita(nav, monkeypatch):
    navegador, main = nav
    import app.chatbot as cb

    chamadas = []
    monkeypatch.setattr(cb, "ia_disponivel", lambda: True)
    monkeypatch.setattr(cb, "gateway_ask", lambda *a, **k: chamadas.append(1))

    conversa = _abrir_conversa(navegador)
    navegador.post(f"/atendimento/{conversa}/mensagem",
                   {"mensagem": "fui demitido", "roteiro": "1"})

    assert chamadas == []
    with main.db() as c:
        total = c.execute("SELECT COUNT(*) t FROM usage_ledger WHERE kind='atendimento'").fetchone()["t"]
        resposta = c.execute("SELECT * FROM chat_messages WHERE role='assistant'").fetchone()
    assert total == 0
    assert resposta["model"] == "roteiro"
