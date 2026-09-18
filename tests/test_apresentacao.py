"""Como os números e as datas chegam aos olhos de quem opera.

O sistema exibia "R$ 25000.00" e "2026-09-18" — ponto decimal e data ISO, que
é como o Python serializa, não como o Brasil escreve.

Não é questão de estética. Num sistema que mostra valor de causa, honorário e
prazo lado a lado, "R$ 25000.00" e "R$ 25.000,00" são lidos com esforço
diferente, e quem confere uma planilha às pressas erra a casa decimal. Data em
ISO exige tradução mental a cada leitura, e prazo lido errado é prazo perdido.
"""

import re
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

_RAIZ = Path(__file__).resolve().parent.parent
RAIZ = _RAIZ / "payload" if (_RAIZ / "payload" / "app").is_dir() else _RAIZ
sys.path.insert(0, str(RAIZ))

from app.main import formatar_data, formatar_datahora, formatar_moeda, templates  # noqa: E402

TDIR = RAIZ / "app" / "templates"


# ============================================================ dinheiro

def test_moeda_no_padrao_brasileiro():
    assert formatar_moeda(0) == "R$ 0,00"
    assert formatar_moeda(25000) == "R$ 25.000,00"
    assert formatar_moeda(1234.5) == "R$ 1.234,50"
    assert formatar_moeda(1234567.891) == "R$ 1.234.567,89"


def test_moeda_negativa_mantem_o_sinal_visivel():
    """Despesa e estorno aparecem no financeiro. Sinal sumido inverte a leitura."""
    assert formatar_moeda(-450.7) == "-R$ 450,70"


def test_moeda_aceita_texto_vindo_do_banco():
    """SQLite devolve REAL, mas coluna migrada pode chegar como texto."""
    assert formatar_moeda("1500.00") == "R$ 1.500,00"


def test_moeda_vazia_nao_vira_zero():
    """"Sem valor" e "zero reais" são coisas diferentes num contrato."""
    for vazio in (None, ""):
        assert formatar_moeda(vazio) == "—"
    assert formatar_moeda(0) == "R$ 0,00"


def test_moeda_sem_simbolo_para_moeda_estrangeira():
    """O teto de gasto de IA é em dólar: 'US$' vem do template."""
    assert formatar_moeda(50, simbolo=False) == "50,00"


# ============================================================== datas

def test_data_no_padrao_brasileiro():
    assert formatar_data("2026-09-18") == "18/09/2026"
    assert formatar_data(date(2026, 9, 18)) == "18/09/2026"
    assert formatar_data("2026-09-18T14:30:05") == "18/09/2026"


def test_datahora_mostra_hora_sem_segundos():
    assert formatar_datahora("2026-09-18T14:30:05") == "18/09/2026 14:30"
    assert formatar_datahora(datetime(2026, 9, 18, 14, 30)) == "18/09/2026 14:30"


def test_data_vazia_nao_inventa_dia():
    assert formatar_data(None) == "—"
    assert formatar_data("") == "—"


def test_data_irreconhecivel_e_devolvida_como_esta():
    """Sumir com o dado é pior do que exibi-lo feio.

    Um campo de data preenchido à mão com "a combinar" precisa continuar
    legível — virar travessão apagaria a informação da tela.
    """
    assert formatar_data("a combinar") == "a combinar"


# ================================================== aplicação nos templates

def test_nenhum_valor_monetario_ficou_no_formato_americano():
    """`"%.2f"|format(x)` produz 1234.56 — ponto decimal, sem milhar."""
    problemas = []
    for arq in sorted(TDIR.glob("*.html")):
        for n, linha in enumerate(arq.read_text(encoding="utf-8").splitlines(), 1):
            for m in re.finditer(r'R\$\s*\{\{\s*[\'"]%\.2f[\'"]\|format', linha):
                problemas.append(f"{arq.name}:{n}")
    assert not problemas, "valor em formato americano em:\n" + "\n".join(problemas)


def test_o_campo_de_valor_do_formulario_continua_com_ponto_decimal():
    """<input type="number"> exige ponto. Formatar ali quebraria a baixa.

    Este teste existe para impedir que uma varredura futura "corrija" o
    último %.2f do projeto e derrube o registro de pagamento.
    """
    finance = (TDIR / "finance.html").read_text(encoding="utf-8")
    m = re.search(r'name="amount"[^>]*value="\{\{([^}]+)\}\}"', finance)
    assert m, "o campo de baixa de pagamento sumiu ou mudou de forma"
    assert "moeda" not in m.group(1), (
        "o valor do input virou texto formatado; o navegador recusa vírgula "
        "em input type=number e a baixa deixa de funcionar"
    )


def test_os_filtros_estao_registrados_na_aplicacao():
    for nome in ("moeda", "data", "datahora"):
        assert nome in templates.env.filters, f"filtro '{nome}' não registrado"


def test_o_verificador_de_templates_conhece_os_filtros_do_jarbas():
    """Sem isso o check_templates quebra com TemplateAssertionError.

    O Jinja recusa compilar template com filtro desconhecido. O verificador
    então falhava por um motivo que não tem relação com o que ele verifica, e
    o traceback sugeria que os templates estavam quebrados.
    """
    ferramenta = RAIZ / "tools" / "check_templates.py"
    if not ferramenta.is_file():
        pytest.skip("sem tools/ neste layout")
    fonte = ferramenta.read_text(encoding="utf-8")
    assert "app_templates.env.filters" in fonte, (
        "o verificador precisa herdar os filtros da aplicação"
    )


# ========================================= estados vazios com próximo ato

TELAS_COM_SAIDA = {
    "dashboard.html": ("Nenhum caso cadastrado", "/intake"),
    "cases.html": ("Sem casos cadastrados", "/intake"),
    "clients.html": ("Sem clientes cadastrados", "/intake"),
}


def test_estado_vazio_das_telas_principais_indica_o_proximo_ato():
    """"Sem casos cadastrados." é um beco: informa e não oferece saída."""
    for arquivo, (trecho, destino) in TELAS_COM_SAIDA.items():
        texto = (TDIR / arquivo).read_text(encoding="utf-8")
        assert trecho in texto, f"{arquivo}: mensagem de estado vazio mudou"
        bloco = texto[texto.index(trecho): texto.index(trecho) + 400]
        assert destino in bloco, (
            f"{arquivo}: o estado vazio não oferece caminho para {destino}"
        )


def test_o_guia_de_primeiros_passos_some_quando_ha_dados():
    """Ajuda que não sai de cena vira ruído permanente."""
    dash = (TDIR / "dashboard.html").read_text(encoding="utf-8")
    assert "Primeiros passos" in dash
    assert re.search(r"\{%\s*if\s+stats\.clients\s*==\s*0\s+and\s+stats\.cases\s*==\s*0\s*%\}", dash), (
        "o guia precisa ser condicionado ao workspace vazio"
    )


# ===================================== instruções que não levam a lugar nenhum

def test_nenhuma_tela_manda_configurar_a_openai():
    """A 9.0 só aceita chave da Anthropic. Mandar configurar OpenAI é beco."""
    problemas = []
    for arq in sorted(TDIR.glob("*.html")):
        for n, linha in enumerate(arq.read_text(encoding="utf-8").splitlines(), 1):
            if "OpenAI" in linha:
                problemas.append(f"{arq.name}:{n}")
    assert not problemas, "instrução impossível na interface:\n" + "\n".join(problemas)


def test_nenhuma_mensagem_do_codigo_manda_conectar_a_openai():
    app = RAIZ / "app"
    problemas = []
    for arq in sorted(app.glob("*.py")):
        if arq.name == "ai_gateway.py":
            continue  # explica a diferença entre as duas chaves de propósito
        fonte = arq.read_text(encoding="utf-8")
        # Só strings, não comentários nem docstrings de módulo.
        for m in re.finditer(r'"([^"\n]*OpenAI[^"\n]*)"|\'([^\'\n]*OpenAI[^\'\n]*)\'', fonte):
            trecho = m.group(1) or m.group(2)
            linha = fonte[:m.start()].count("\n") + 1
            problemas.append(f"{arq.name}:{linha}: {trecho[:70]}")
    assert not problemas, (
        "mensagem ao usuário citando um provedor que a 9.0 não aceita:\n"
        + "\n".join(problemas)
    )
