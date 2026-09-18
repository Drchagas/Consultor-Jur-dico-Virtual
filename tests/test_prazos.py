"""Prazos processuais, linha do tempo e documentos.

Prazo errado é responsabilidade civil e disciplinar do advogado. Estes testes
verificam as regras contra datas reais do calendário, não contra o que o
código faz.
"""

import re
import sys
import tempfile
from datetime import date
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
RAIZ = _RAIZ / "payload" if (_RAIZ / "payload" / "app").is_dir() else _RAIZ
sys.path.insert(0, str(RAIZ))

import pytest

from app import movimentacoes as MV
from app import prazos as P

APP = RAIZ / "app"


# ==========================================================  calendário

def test_pascoa_bate_com_datas_conhecidas():
    """Dela derivam Carnaval, Sexta Santa e Corpus Christi. Errar aqui erra
    cinco feriados de uma vez."""
    for ano, m, d in ((2023, 4, 9), (2024, 3, 31), (2025, 4, 20),
                      (2026, 4, 5), (2027, 3, 28), (2028, 4, 16)):
        assert P.pascoa(ano) == date(ano, m, d), ano


def test_feriados_moveis_de_2026():
    mv = P.moveis(2026)
    assert mv[date(2026, 2, 17)].startswith("Carnaval")
    assert mv[date(2026, 4, 3)] == "Sexta-feira Santa"
    assert mv[date(2026, 6, 4)] == "Corpus Christi"


def test_feriado_estadual_do_rs():
    assert date(2026, 9, 20) in P.feriados(2026, "RS")
    assert date(2026, 9, 20) not in P.feriados(2026, "SP")


def test_recesso_forense_cobre_20_12_a_20_01():
    """Art. 220 do CPC."""
    assert P.em_recesso(date(2025, 12, 20))
    assert P.em_recesso(date(2025, 12, 31))
    assert P.em_recesso(date(2026, 1, 20))
    assert not P.em_recesso(date(2025, 12, 19))
    assert not P.em_recesso(date(2026, 1, 21))


def test_fim_de_semana_nao_e_dia_util():
    assert not P.e_dia_util(date(2026, 3, 7))    # sábado
    assert not P.e_dia_util(date(2026, 3, 8))    # domingo
    assert P.e_dia_util(date(2026, 3, 9))        # segunda


def test_proximo_dia_util_pula_feriado_prolongado():
    # 2026-04-03 é Sexta Santa; 04 e 05 fim de semana
    assert P.proximo_dia_util(date(2026, 4, 3)) == date(2026, 4, 6)


# ======================================================  contagem (art. 219)

def test_prazo_conta_em_dias_uteis_e_nao_corridos():
    p = P.calcular(date(2026, 3, 6), 15, uteis=True)
    assert p.vencimento == date(2026, 3, 27)
    assert (p.vencimento - p.inicio_contagem).days == 21, "15 úteis = 21 corridos"


def test_exclui_o_dia_do_comeco():
    """Art. 224: 1 dia útil a partir de segunda vence na terça."""
    p = P.calcular(date(2026, 3, 9), 1, uteis=True)
    assert p.vencimento == date(2026, 3, 10)


def test_termo_inicial_em_dia_sem_expediente_adia_o_inicio():
    p = P.calcular(date(2026, 3, 7), 5, uteis=True)   # sábado
    assert p.inicio_contagem == date(2026, 3, 9)
    assert any("sábado" in s for s in p.suspensoes)


def test_prazo_atravessando_o_carnaval():
    """5 dias úteis a partir de 13/02/2026 caem depois do Carnaval."""
    p = P.calcular(date(2026, 2, 13), 5, uteis=True)
    assert p.vencimento == date(2026, 2, 25)
    assert (p.vencimento - p.inicio_contagem).days == 12


def test_prazo_atravessando_o_recesso_estica_muito():
    """Caso que mais induz erro: 15 úteis viram 51 dias de calendário."""
    p = P.calcular(date(2025, 12, 15), 15, uteis=True)
    assert p.vencimento == date(2026, 2, 4)
    assert (p.vencimento - p.inicio_contagem).days == 51
    assert any("recesso" in s for s in p.suspensoes)


def test_intimacao_dentro_do_recesso_so_comeca_em_21_01():
    p = P.calcular(date(2025, 12, 26), 15, uteis=True)
    assert p.inicio_contagem == date(2026, 1, 21)
    assert any("recesso" in s for s in p.suspensoes)


def test_prazo_material_conta_corrido():
    """Decadencial e prescricional não seguem o art. 219."""
    p = P.calcular(date(2026, 3, 6), 120, uteis=False)
    assert (p.vencimento - p.inicio_contagem).days >= 120


def test_prazo_material_prorroga_vencimento_em_dia_sem_expediente():
    """Art. 224, §1º."""
    p = P.calcular(date(2026, 3, 5), 2, uteis=False)   # cairia no sábado 07
    assert P.e_dia_util(p.vencimento)


def test_prazo_em_dobro_dobra_os_dias():
    simples = P.calcular(date(2026, 3, 6), 15, uteis=True)
    dobro = P.calcular(date(2026, 3, 6), 15, uteis=True, dobro=True)
    assert dobro.dias == 30
    assert dobro.vencimento > simples.vencimento
    assert any("dobro" in s for s in dobro.suspensoes)


def test_prazo_zero_ou_negativo_e_recusado():
    for n in (0, -5):
        with pytest.raises(ValueError):
            P.calcular(date(2026, 3, 6), n)


# ==============================================================  catálogo

def test_catalogo_cobre_os_prazos_do_dia_a_dia():
    for cod in ("contestacao", "apelacao", "embargos_declaracao",
                "agravo_instrumento", "recurso_inominado", "replica",
                "embargos_execucao", "impugnacao_cumprimento"):
        assert cod in P.POR_CODIGO, cod


def test_todo_prazo_do_catalogo_tem_fundamento_legal():
    """Prazo sem fundamento não dá para conferir nem para justificar."""
    for t in P.CATALOGO:
        assert t.fundamento, t.codigo
        assert re.search(r"art\.|Lei", t.fundamento), t.fundamento


def test_prazos_do_catalogo_batem_com_o_cpc():
    esperado = {"contestacao": 15, "apelacao": 15, "embargos_declaracao": 5,
                "agravo_instrumento": 15, "recurso_inominado": 10,
                "manifestacao_geral": 5, "custas_preparo": 5}
    for cod, dias in esperado.items():
        assert P.POR_CODIGO[cod].dias == dias, cod


def test_prazos_materiais_marcados_como_corridos():
    for cod in ("rescisoria", "mandado_seguranca"):
        assert not P.POR_CODIGO[cod].uteis, cod


def test_tipo_desconhecido_levanta():
    with pytest.raises(ValueError):
        P.calcular_do_catalogo("inexistente", date(2026, 3, 6))


# =============================================================  semáforo

def test_criticidade_marca_vencido():
    nivel, texto = P.criticidade(date(2026, 3, 6), hoje=date(2026, 3, 10))
    assert nivel == "vencido" and "VENCIDO" in texto


def test_criticidade_marca_hoje():
    nivel, _ = P.criticidade(date(2026, 3, 10), hoje=date(2026, 3, 10))
    assert nivel == "hoje"


def test_criticidade_conta_em_dias_uteis():
    """Sexta com prazo na segunda: 1 dia útil, não 3 corridos."""
    nivel, texto = P.criticidade(date(2026, 3, 9), hoje=date(2026, 3, 6))
    assert nivel == "critico"


# =========================================  linha do tempo do eproc

def test_extrai_eventos_da_listagem_do_eproc():
    texto = """Listagem dos Eventos do Processo
Evento Data/Hora Descrição Usuário
22 11/09/2026 14:32 Expedida/certificada a intimação eletrônica  SCHALELA
20 11/09/2026 10:05 Proferido despacho de mero expediente  SCHALELA
16 08/09/2026 09:12 PETIÇÃO  RS105040
"""
    mvs = MV.extrair_eventos_eproc(texto)
    assert len(mvs) == 3
    assert mvs[0].evento == "22"
    assert mvs[0].data == date(2026, 9, 11)


def test_detecta_intimacao_mas_nao_marca_tudo():
    """Marcar demais gera prazo fantasma, e prazo fantasma treina o
    advogado a ignorar o alerta."""
    texto = """1 01/09/2026 Expedida/certificada a intimação eletrônica  X
2 02/09/2026 Proferido despacho de mero expediente  X
3 03/09/2026 Juntada de petição  X
4 04/09/2026 Citação realizada  X
5 05/09/2026 Conclusos para decisão  X
"""
    mvs = MV.extrair_eventos_eproc(texto)
    intimacoes = [m for m in mvs if m.intimacao]
    assert len(intimacoes) == 2
    assert {m.evento for m in intimacoes} == {"1", "4"}


def test_eventos_repetidos_sao_deduplicados():
    linha = "10 01/09/2026 Juntada de petição  X\n"
    assert len(MV.extrair_eventos_eproc(linha * 3)) == 1


def test_texto_sem_eventos_devolve_lista_vazia():
    assert MV.extrair_eventos_eproc("documento qualquer sem tabela") == []
    assert MV.extrair_eventos_eproc("") == []


def test_eventos_saem_do_mais_recente_para_o_mais_antigo():
    texto = ("1 01/01/2026 Distribuição  X\n"
             "9 15/06/2026 Sentença  X\n"
             "5 10/03/2026 Contestação  X\n")
    mvs = MV.extrair_eventos_eproc(texto)
    assert [m.data for m in mvs] == sorted([m.data for m in mvs], reverse=True)


# =============================================================  DataJud

def test_alias_do_tribunal_pelo_numero_cnj():
    casos = {
        "5005877-37.2026.8.21.0041": "api_publica_tjrs",
        "1234567-89.2023.8.26.0100": "api_publica_tjsp",
        "5020315-43.2014.4.04.7107": "api_publica_trf4",
        "0020309-49.2025.5.04.0351": "api_publica_trt4",
    }
    for numero, alias in casos.items():
        assert MV.alias_do_numero(numero) == alias, numero


def test_numero_invalido_nao_gera_alias():
    for ruim in ("", "12345", "abc", "5005877-37.2026.9.99.0041"):
        assert MV.alias_do_numero(ruim) is None


def test_chave_do_datajud_vem_do_ambiente(monkeypatch):
    monkeypatch.delenv(MV.ENV_CHAVE, raising=False)
    with pytest.raises(MV.DataJudError, match=MV.ENV_CHAVE):
        MV.chave()


def test_nenhuma_chave_embutida_no_codigo():
    fonte = (APP / "movimentacoes.py").read_text(encoding="utf-8")
    assert "APIKey " not in fonte.replace('f"APIKey {chave()}"', "")


def test_resposta_vazia_do_datajud_nao_inventa_movimento():
    assert MV.movimentos_da_resposta({}) == []
    assert MV.movimentos_da_resposta({"hits": {"hits": []}}) == []
    assert MV.capa_da_resposta({}) == {}


def test_normaliza_movimentos_do_formato_elasticsearch():
    dados = {"hits": {"hits": [{"_source": {
        "numeroProcesso": "50058773720268210041",
        "classe": {"nome": "Procedimento Comum"},
        "movimentos": [
            {"codigo": 12223, "nome": "Sentença", "dataHora": "2026-03-15T14:32:00",
             "complementosTabelados": [{"codigo": 3, "nome": "Procedência"}]},
            {"codigo": 22, "nome": "Distribuição", "dataHora": "2024-01-10T09:15:00"},
        ]}}]}}
    mvs = MV.movimentos_da_resposta(dados)
    assert len(mvs) == 2
    assert mvs[0].data == date(2026, 3, 15)
    assert "Procedência" in mvs[0].descricao
    assert MV.capa_da_resposta(dados)["classe"] == "Procedimento Comum"


# ============================================================  documentos

def test_todos_os_modelos_geram_arquivo():
    from app import document_generator as G
    cliente = {"name": "Maria Silva", "document": "123.456.789-00", "rg": "1234567",
               "nationality": "brasileira", "marital_status": "solteira",
               "profession": "professora", "address": "Rua X, 10", "city": "Canela",
               "state": "RS", "zip_code": "95680-000", "email": "m@x.br",
               "phone": "5499", "person_type": "PF"}
    org = {"name": "CHAGAS – ADVOGADOS", "address": "Rua Jacob Adami, 55",
           "city": "Canela", "website": "", "primary_color": "#9f2948"}
    contrato = {"title": "Ação revisional", "contract_value": 5000,
                "entry_amount": 1500, "installment_count": 5, "success_percent": 20}
    d = Path(tempfile.mkdtemp())
    for cod, (nome, fn) in G.MODELOS.items():
        alvo = d / f"{cod}.docx"
        if cod == "contrato_honorarios":
            fn(cliente, org, alvo, contract=contrato)
        elif cod == "substabelecimento":
            fn(cliente, org, alvo, substabelecido="Dr. Fulano, OAB/RS 1234")
        elif cod == "recibo_honorarios":
            fn(cliente, org, alvo, valor=1500, referente="entrada")
        else:
            fn(cliente, org, alvo)
        assert alvo.is_file() and alvo.stat().st_size > 10000, cod


def test_catalogo_de_modelos_cobre_o_que_faltava():
    from app import document_generator as G
    for cod in ("contrato_honorarios", "substabelecimento",
                "recibo_honorarios", "hipossuficiencia"):
        assert cod in G.MODELOS, cod


def test_contrato_traz_as_clausulas_que_protegem_o_escritorio():
    from app import document_generator as G
    fonte = (APP / "document_generator.py").read_text(encoding="utf-8")
    i = fonte.index("def create_fee_contract")
    corpo = fonte[i:fonte.index("\ndef ", i + 10)]
    assert "art. 23 da Lei 8.906/94" in corpo, "sucumbência não ressalvada"
    assert "784, III" in corpo, "sem testemunhas não é título executivo"
    assert "13.709" in corpo, "sem cláusula de tratamento de dados"


# ====================================================  fiação no sistema

def test_rota_de_prazos_registrada():
    src = (APP / "main.py").read_text(encoding="utf-8")
    assert "prazo_router" in src
    assert "/prazos" in (APP / "templates" / "base.html").read_text(encoding="utf-8")


def test_upload_importa_a_linha_do_tempo():
    src = (APP / "main.py").read_text(encoding="utf-8")
    assert "_importar_movimentos" in src
    i = src.index("index_pdf(conn, org_id=org_id")
    j = src.index("novos, intimacoes = _importar_movimentos(")
    assert i < j, "extrair eventos antes de indexar não encontra texto"
