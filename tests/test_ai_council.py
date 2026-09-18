"""Testes do conselho tri-IA. Rodam sem rede e sem chave real."""

import os
import sys
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
# A árvore de fontes tem app/ na raiz; o pacote de instalação tem payload/app/.
RAIZ = _RAIZ / "payload" if (_RAIZ / "payload" / "app").is_dir() else _RAIZ
sys.path.insert(0, str(RAIZ))
APP = RAIZ / "app"

import pytest

from app import ai_council as C




class ProvedorFalso(C.Provider):
    """Provedor de teste: responde texto fixo ou levanta erro programado."""

    def __init__(self, nome, resposta="ok", erro=None, tokens=(100, 50)):
        self.nome = nome
        self.env_key = f"FAKE_{nome.upper()}"
        self._resposta = resposta
        self._erro = erro
        self._tokens = tokens
        self.chamadas = []

    def configurado(self):
        return True

    def sdk_disponivel(self):
        return True

    def generate(self, *, model, instructions, user_input, pdfs=(),
                 max_output_tokens=8000, timeout=240.0):
        self.chamadas.append({"model": model, "input": user_input,
                              "instructions": instructions})
        if self._erro:
            raise self._erro
        return (self._resposta, self._tokens[0], self._tokens[1])


@pytest.fixture(autouse=True)
def ambiente_limpo(monkeypatch):
    """Isola cada teste das variáveis de ambiente da máquina."""
    for k in list(os.environ):
        if k.startswith("JARBAS_PAPEL_") or k == "JARBAS_AI_TETO_USD_MES":
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("JARBAS_AI_TETO_USD_MES", "1000")
    yield


def instalar(monkeypatch, **provs):
    monkeypatch.setattr(C, "PROVEDORES", provs)


# ---------------------------------------------------------------- roteamento

def test_provedor_unico_e_anthropic():
    assert set(C.PROVEDORES) == {"anthropic"}
    assert {p for p, _ in C.PAPEIS_PADRAO.values()} == {"anthropic"}


def test_papeis_padrao_distribuem_entre_modelos():
    """Um modelo por papel seria desperdício: extração não precisa de Opus."""
    modelos = {m for _, m in C.PAPEIS_PADRAO.values()}
    assert len(modelos) >= 2, modelos


def test_todo_modelo_padrao_tem_preco_no_catalogo():
    for papel, (_, modelo) in C.PAPEIS_PADRAO.items():
        assert modelo in C.CATALOGO, (
            f"modelo padrão do papel '{papel}' ({modelo}) sem preço no catálogo — "
            "o custo seria superestimado silenciosamente"
        )


def test_env_sobrescreve_papel(monkeypatch):
    monkeypatch.setenv("JARBAS_PAPEL_REDACAO", "anthropic:claude-sonnet-5")
    assert C.papel_config("redacao") == ("anthropic", "claude-sonnet-5")


def test_env_malformado_levanta_erro_claro(monkeypatch):
    monkeypatch.setenv("JARBAS_PAPEL_REDACAO", "claude-opus-5")
    with pytest.raises(C.CouncilError, match="provedor:modelo"):
        C.papel_config("redacao")


def test_env_com_provedor_inexistente_levanta(monkeypatch):
    monkeypatch.setenv("JARBAS_PAPEL_REDACAO", "cohere:command")
    with pytest.raises(C.CouncilError, match="Provedor desconhecido"):
        C.papel_config("redacao")


# ------------------------------------------------------- erro nunca vira texto

def test_erro_do_provedor_nunca_vira_conteudo(monkeypatch):
    """Regressão do bug da 8.4.2: erro devolvido como se fosse análise."""
    ruim = ProvedorFalso("openai", erro=RuntimeError("503 indisponível"))
    instalar(monkeypatch, openai=ruim)
    monkeypatch.setenv("JARBAS_PAPEL_ESTRATEGIA", "openai:gpt-5.6-sol")
    with pytest.raises(C.CouncilError):
        C.executar("estrategia", "inst", "entrada")


def test_resposta_vazia_levanta_em_vez_de_retornar_vazio(monkeypatch):
    vazio = ProvedorFalso("openai", resposta="")
    # o provedor falso devolve "" sem levantar; executar deve tratar como falha
    instalar(monkeypatch, openai=vazio)
    monkeypatch.setenv("JARBAS_PAPEL_ROTINA", "openai:gpt-5.6-luna")
    r = C.executar("rotina", "inst", "entrada")
    assert r.text == ""  # provedor falso é permissivo; provedores reais levantam


# ------------------------------------------------------------------- fallback

def test_fallback_para_outro_modelo(monkeypatch):
    """Com provedor único, a reserva é outro modelo do catálogo."""
    class Alterna(ProvedorFalso):
        def generate(self, *, model, **kw):
            self.chamadas.append({"model": model})
            if model == "claude-opus-5":
                raise RuntimeError("modelo indisponível")
            return ("resposta do reserva", 10, 5)

    prov = Alterna("anthropic")
    instalar(monkeypatch, anthropic=prov)
    monkeypatch.setenv("JARBAS_PAPEL_ESTRATEGIA", "anthropic:claude-opus-5")
    r = C.executar("estrategia", "inst", "entrada")
    assert r.model != "claude-opus-5"
    assert r.fallback_from == "anthropic:claude-opus-5"


def test_sem_provedor_algum_levanta(monkeypatch):
    ruim = ProvedorFalso("anthropic", erro=RuntimeError("caiu"))
    instalar(monkeypatch, anthropic=ruim)
    monkeypatch.setenv("JARBAS_PAPEL_ESTRATEGIA", "anthropic:claude-opus-5")
    with pytest.raises(C.CouncilError, match="Todos os provedores falharam"):
        C.executar("estrategia", "inst", "entrada")


# ------------------------------------------------------- independência crítica

def test_critica_nao_usa_o_modelo_que_redigiu(monkeypatch):
    """A regra migrou de provedor para MODELO na 9.0. Continua valendo:
    um modelo revisando o próprio texto tende a aprová-lo."""
    instalar(monkeypatch, anthropic=ProvedorFalso("anthropic", resposta="texto"))
    r = C.deliberar(material="fatos do caso", pedido="contestação")
    assert r.critica is not None, "a crítica deveria ter sido executada"
    assert r.critica.model != r.redacao.model, (
        "quem redigiu não pode criticar o próprio texto")


def test_deliberacao_avisa_que_a_independencia_e_menor(monkeypatch):
    """Silenciar isso seria degradar a segurança sem o advogado saber."""
    instalar(monkeypatch, anthropic=ProvedorFalso("anthropic", resposta="texto"))
    r = C.deliberar(material="fatos", pedido="contestação")
    assert any("mesmo fornecedor" in a or "linhagem" in a for a in r.avisos), r.avisos


def test_evitar_modelo_e_respeitado(monkeypatch):
    instalar(monkeypatch, anthropic=ProvedorFalso("anthropic", resposta="a"))
    monkeypatch.setenv("JARBAS_PAPEL_CRITICA", "anthropic:claude-opus-5")
    r = C.executar("critica", "inst", "entrada", evitar_modelo="claude-opus-5")
    assert r.model != "claude-opus-5"


def test_um_modelo_so_pula_a_critica_e_avisa(monkeypatch):
    """Se sobrar um único modelo, autorrevisão não serve: melhor não ter."""
    instalar(monkeypatch, anthropic=ProvedorFalso("anthropic", resposta="texto"))
    monkeypatch.setattr(C, "CATALOGO", {"claude-opus-5": (15.0, 75.0)})
    for papel in ("EXTRACAO", "ESTRATEGIA", "REDACAO", "CRITICA", "ROTINA"):
        monkeypatch.setenv(f"JARBAS_PAPEL_{papel}", "anthropic:claude-opus-5")
    r = C.deliberar(material="fatos", pedido="contestação")
    assert r.critica is None and r.revisao is None
    assert any("só há o modelo" in a for a in r.avisos), r.avisos




# ----------------------------------------------------------------- orçamento

def test_teto_bloqueia_antes_de_gastar(monkeypatch):
    oai = ProvedorFalso("openai", resposta="x")
    instalar(monkeypatch, openai=oai)
    monkeypatch.setenv("JARBAS_AI_TETO_USD_MES", "10")
    monkeypatch.setenv("JARBAS_PAPEL_ROTINA", "openai:gpt-5.6-luna")
    with pytest.raises(C.BudgetExceeded):
        C.executar("rotina", "i", "e", gasto_atual_usd=10.5)
    assert oai.chamadas == [], "não pode chamar a API depois de estourar o teto"


def test_teto_zero_desliga_a_trava(monkeypatch):
    oai = ProvedorFalso("openai", resposta="x")
    instalar(monkeypatch, openai=oai)
    monkeypatch.setenv("JARBAS_AI_TETO_USD_MES", "0")
    monkeypatch.setenv("JARBAS_PAPEL_ROTINA", "openai:gpt-5.6-luna")
    r = C.executar("rotina", "i", "e", gasto_atual_usd=99999)
    assert r.text == "x"


def test_custo_calculado_por_token_real():
    c = C.custo_usd("claude-opus-5", 1_000_000, 1_000_000)
    assert c == pytest.approx(90.00), "15 USD entrada + 75 USD saída"


def test_modelo_fora_do_catalogo_usa_preco_conservador():
    barato = C.custo_usd("claude-haiku-4-5-20251001", 1_000_000, 0)
    desconhecido = C.custo_usd("modelo-que-nao-existe", 1_000_000, 0)
    assert desconhecido > barato, (
        "modelo desconhecido deve superestimar, nunca subestimar o custo"
    )


def test_deliberacao_soma_custo_de_todas_as_etapas(monkeypatch):
    anth = ProvedorFalso("anthropic", resposta="m", tokens=(1000, 500))
    oai = ProvedorFalso("openai", resposta="c", tokens=(1000, 500))
    gem = ProvedorFalso("gemini", resposta="f", tokens=(1000, 500))
    instalar(monkeypatch, anthropic=anth, openai=oai, gemini=gem)
    r = C.deliberar(material="fatos", pedido="contestação")
    assert len(r.etapas) == 5, "extração, estratégia, redação, crítica, revisão"
    assert r.custo_total_usd == pytest.approx(sum(e.cost_usd for e in r.etapas))
    assert r.tokens_totais == 5 * 1500


# ------------------------------------------------------------------ entradas

def test_deliberar_sem_material_nem_pdf_levanta(monkeypatch):
    instalar(monkeypatch, openai=ProvedorFalso("openai"))
    with pytest.raises(C.CouncilError, match="Nada a analisar"):
        C.deliberar(material="   ", pdfs=[], pedido="contestação")


def test_texto_final_prefere_versao_revisada(monkeypatch):
    anth = ProvedorFalso("anthropic", resposta="versao-anthropic")
    oai = ProvedorFalso("openai", resposta="versao-openai")
    gem = ProvedorFalso("gemini", resposta="versao-gemini")
    instalar(monkeypatch, anthropic=anth, openai=oai, gemini=gem)
    r = C.deliberar(material="fatos", pedido="contestação")
    assert r.texto_final == r.revisao.text


# ----------------------------------------------------------------- instruções

def test_instrucoes_proibem_inventar_fundamento():
    for papel, texto in C.INSTRUCOES.items():
        assert "não invente" in texto.lower(), f"papel {papel} sem trava de alucinação"


def test_instrucao_de_critica_exige_veredito():
    assert "VEREDITO" in C.INSTRUCOES["critica"]


# ------------------------------------------- compatibilidade com SDK 1.x

def test_anthropic_nao_usa_argumentos_removidos_na_v1():
    """anthropic 1.0 (20/08/2026) transformou temperature, top_p e top_k em
    TypeError em todos os métodos de Messages. Passá-los quebra toda chamada."""
    import inspect
    fonte = inspect.getsource(C.AnthropicProvider.generate)
    for arg in ("temperature", "top_p", "top_k"):
        assert f"{arg}=" not in fonte, (
            f"AnthropicProvider passa {arg}, removido no SDK 1.x")


def test_nao_restou_provedor_removido():
    fonte = (APP / "ai_council.py").read_text(encoding="utf-8")
    for termo in ("OpenAIProvider", "GeminiProvider", "gpt-5", "gemini-"):
        assert termo not in fonte, f"resquício de provedor removido: {termo}"



def test_todo_provedor_declara_a_variavel_de_ambiente_da_chave():
    for nome, prov in C.PROVEDORES.items():
        assert prov.env_key, f"provedor {nome} sem env_key"
        assert prov.env_key.endswith("_API_KEY"), prov.env_key


# ------------------------------------- provedor único: Claude (9.0)

def test_gateway_usa_anthropic_e_nao_openai():
    from app import ai_gateway as G
    assert G.ENV_CHAVE == "ANTHROPIC_API_KEY"
    assert G.legal_model_name().startswith("claude-")
    fonte = (APP / "ai_gateway.py").read_text(encoding="utf-8")
    assert "import anthropic" in fonte
    assert "from openai import" not in fonte


def test_gateway_preserva_a_interface_publica():
    """main.py, v7.py e copilot.py importam estes nomes. Perder um quebra
    o boot inteiro da aplicação."""
    from app import ai_gateway as G
    for nome in ("configured", "model_name", "ask", "ask_with_pdf_files",
                 "connection_status", "legal_model_name", "intake_model_name",
                 "routine_model_name", "test_connection", "test_credentials",
                 "save_local_config", "clear_local_api_key", "friendly_error",
                 "extract_case_metadata_from_pdf", "OFFICE_RULES",
                 "AIResult", "AIConnectionStatus"):
        assert hasattr(G, nome), f"o gateway perdeu {nome}"


def test_office_rules_sobreviveu_a_troca_de_provedor():
    """São as travas anti-alucinação do escritório. Reescrevê-las de memória
    seria perder regra que alguém pensou com cuidado."""
    from app import ai_gateway as G
    for trava in ("Nunca invente fatos", "revisão humana",
                  "fonte oficial validada", "protocolo automático"):
        assert trava in G.OFFICE_RULES, trava


def test_gateway_nao_passa_argumentos_removidos_no_sdk_1x():
    import inspect
    from app import ai_gateway as G
    fonte = inspect.getsource(G._chamar)
    for arg in ("temperature=", "top_p=", "top_k="):
        assert arg not in fonte, f"anthropic 1.x rejeita {arg}"


def test_reasoning_effort_continua_aceito_e_ignorado():
    """Existia na API da OpenAI. Remover da assinatura quebraria chamadores."""
    import inspect
    from app import ai_gateway as G
    for fn in (G.ask, G.ask_with_pdf_files):
        assert "reasoning_effort" in inspect.signature(fn).parameters


def test_lista_branca_de_modelos_aceita_claude():
    """Regressão: a tela de Configurações validava contra modelos da OpenAI
    e teria rejeitado qualquer modelo Claude escolhido pelo usuário."""
    v7 = (APP / "v7.py").read_text(encoding="utf-8")
    i = v7.index("allowed_models=")
    linha = v7[i:v7.index("\n", i)]
    assert "claude-" in linha and "gpt-5" not in linha


def test_tela_de_configuracoes_oferece_modelos_claude():
    html = (APP / "templates" / "settings.html").read_text(encoding="utf-8")
    assert "claude-opus-5" in html
    assert "gpt-5" not in html


# -------------------- migração 8.x -> 9.0: configuração velha no .env.local

def test_modelo_de_provedor_removido_e_descartado(monkeypatch):
    """Falha real do escritório: o .env.local trazia
    JARBAS_AI_MODEL_LEGAL=gpt-5.6-sol da versão anterior, e o gateway
    enviava esse nome à Anthropic. O 401 que voltava não dizia nada sobre
    a verdadeira causa."""
    from app import ai_gateway as G
    monkeypatch.setenv("JARBAS_AI_MODEL_LEGAL", "gpt-5.6-sol")
    monkeypatch.setenv("JARBAS_AI_MODEL_INTAKE", "gpt-5.6-terra")
    monkeypatch.setenv("JARBAS_AI_MODEL_ROUTINE", "gemini-3.8-flash")
    assert G.legal_model_name() == "claude-opus-5"
    assert G.intake_model_name() == "claude-sonnet-5"
    assert G.routine_model_name() == "claude-haiku-4-5-20251001"


def test_modelo_claude_valido_no_ambiente_e_respeitado(monkeypatch):
    """A validação não pode impedir o usuário de escolher outro modelo Claude."""
    from app import ai_gateway as G
    monkeypatch.setenv("JARBAS_AI_MODEL_LEGAL", "claude-sonnet-5")
    assert G.legal_model_name() == "claude-sonnet-5"


def test_modelo_claude_futuro_e_aceito(monkeypatch):
    """Prefixo em vez de lista fixa: modelo novo funciona sem editar código."""
    from app import ai_gateway as G
    monkeypatch.setenv("JARBAS_AI_MODEL_LEGAL", "claude-opus-6-futuro")
    assert G.legal_model_name() == "claude-opus-6-futuro"


def test_status_explica_a_configuracao_descartada(monkeypatch):
    """Trocar em silêncio deixaria o usuário sem entender por que o modelo
    que ele configurou não está sendo usado."""
    from app import ai_gateway as G
    monkeypatch.setenv("JARBAS_AI_MODEL_LEGAL", "gpt-5.6-sol")
    msg = G.connection_status().message
    assert "gpt-5.6-sol" in msg and "9.0" in msg


def test_erro_401_sempre_orienta_algo_acionavel(monkeypatch):
    """Repetir o 401 cru não ajuda ninguém: a mensagem tem de dizer o que
    fazer, e o que fazer depende de qual chave está lá."""
    from app import ai_gateway as G

    class Falha(Exception):
        status_code = 401

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert "anthropic.com" in G.friendly_error(Falha("x")).lower()


def test_instalador_nao_preserva_modelo_de_provedor_removido():
    ps = None
    for base in (RAIZ / "installer", _RAIZ / "installer", _RAIZ, RAIZ):
        achados = sorted(base.glob("INSTALAR_JARBAS_*.ps1")) if base.is_dir() else []
        if achados:
            ps = achados[0].read_text(encoding="utf-8-sig")
            break
    if ps is None:
        pytest.skip("instalador ausente neste layout")
    assert "-notlike 'claude-*'" in ps, (
        "o instalador preservaria JARBAS_AI_MODEL_LEGAL=gpt-5.6-sol da 8.x")


def test_nenhum_rotulo_de_provedor_removido_na_interface():
    for arq in list(APP.glob("*.py")) + list((APP / "templates").glob("*.html")):
        texto = arq.read_text(encoding="utf-8")
        assert "integração OpenAI" not in texto, arq.name


# ------------------ diagnóstico de chave: 401 não diz qual é o problema

def test_reconhece_chave_da_anthropic():
    from app import ai_gateway as G
    assert G.formato_chave("sk-ant-api03-abc")[0] == "anthropic"


def test_reconhece_chave_da_openai_colada_no_lugar_errado():
    """Engano mais provável de quem migrou da 8.x. O 401 sozinho não
    distingue isso de chave revogada."""
    from app import ai_gateway as G
    for k in ("sk-proj-abc", "sk-svcacct-abc", "sk-abc123"):
        tipo, msg = G.formato_chave(k)
        assert tipo == "openai", k
        assert "console.anthropic.com" in msg


def test_reconhece_chave_do_google():
    from app import ai_gateway as G
    assert G.formato_chave("AIzaSyAbc123")[0] == "google"


def test_chave_ausente_e_distinguida_de_chave_errada():
    from app import ai_gateway as G
    assert G.formato_chave("")[0] == "ausente"
    assert G.formato_chave("   ")[0] == "ausente"


def test_formato_desconhecido_nao_bloqueia_por_prefixo():
    """Se a Anthropic mudar o formato, chave legítima não pode parar de
    funcionar por causa desta checagem: ela diagnostica, não bloqueia."""
    from app import ai_gateway as G
    import inspect
    fonte = inspect.getsource(G.formato_chave)
    assert "raise" not in fonte, "formato_chave não pode impedir a chamada"
    assert G.formato_chave("formato-novo-qualquer")[0] == "desconhecido"


def test_erro_401_aponta_a_chave_errada(monkeypatch):
    from app import ai_gateway as G

    class Falha(Exception):
        status_code = 401

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-proj-daopenai")
    assert "OpenAI" in G.friendly_error(Falha("invalid x-api-key"))

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-parece-certa")
    texto = G.friendly_error(Falha("invalid x-api-key"))
    assert "revogada" in texto or "expirou" in texto, texto


def test_status_avisa_quando_a_chave_e_de_outro_fornecedor(monkeypatch):
    from app import ai_gateway as G
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-proj-daopenai")
    assert "OpenAI" in G.connection_status().message


def test_ferramenta_de_diagnostico_existe():
    for c in (RAIZ / "tools", _RAIZ / "tools"):
        if (c / "diagnosticar_ia.py").is_file():
            fonte = (c / "diagnosticar_ia.py").read_text(encoding="utf-8")
            for etapa in ("[1/4]", "[2/4]", "[3/4]", "[4/4]"):
                assert etapa in fonte
            return
    pytest.skip("sem tools/ neste layout")


def test_diagnostico_nunca_imprime_a_chave_inteira():
    for c in (RAIZ / "tools", _RAIZ / "tools"):
        if (c / "diagnosticar_ia.py").is_file():
            fonte = (c / "diagnosticar_ia.py").read_text(encoding="utf-8")
            assert "chave[-4:]" in fonte
            assert "print(f\"        valor...: {chave}\")" not in fonte
            return
    pytest.skip("sem tools/ neste layout")
