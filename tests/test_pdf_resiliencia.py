"""Um motor de PDF quebrado não pode derrubar a leitura inteira.

Descoberto ao testar um PDF digitalizado numa máquina com a biblioteca
`cryptography` meio quebrada: a extração morria por inteiro, e a tela dizia
apenas que o PDF não tinha sido lido.

A causa é sutil. As três bibliotecas de PDF carregam extensões nativas — o
pypdf importa `cryptography`, que é Rust via pyo3 — e uma extensão nativa
quebrada levanta PanicException, que herda de BaseException, NÃO de Exception.
Todo `except Exception` do pipeline passava ao largo.

O efeito era o pior possível: o pypdf só é chamado quando o PyMuPDF não
conseguiu texto suficiente, ou seja, exatamente nos PDFs digitalizados. Numa
instalação com dependência nativa meio quebrada — pip que falhou pela metade,
antivírus que bloqueou uma DLL — todo auto escaneado derrubava a leitura.
"""

import sys
from pathlib import Path

import pytest

_RAIZ = Path(__file__).resolve().parent.parent
RAIZ = _RAIZ / "payload" if (_RAIZ / "payload" / "app").is_dir() else _RAIZ
sys.path.insert(0, str(RAIZ))

from app import pdf_pipeline as P  # noqa: E402

FIXTURE = _RAIZ / "tests" / "fixtures" / "test_intake.pdf"


class PanicoNativo(BaseException):
    """Imita o PanicException do pyo3: herda de BaseException, não Exception."""


def test_o_isolador_converte_panico_nativo_em_falha_de_motor():
    def motor_quebrado():
        raise PanicoNativo("Python API call failed")

    with pytest.raises(P.FalhaDeMotor):
        P._isolar(motor_quebrado)


def test_o_isolador_deixa_passar_ordem_de_parar():
    """Ctrl+C e SystemExit são ordem de parar, não falha de motor."""
    for parada in (KeyboardInterrupt, SystemExit):
        def motor_interrompido(exc=parada):
            raise exc()
        with pytest.raises(parada):
            P._isolar(motor_interrompido)


def test_o_isolador_devolve_o_resultado_quando_da_certo():
    assert P._isolar(lambda a, b: a + b, 2, 3) == 5



def test_pdf_e_lido_mesmo_com_um_motor_em_panico(monkeypatch):
    """Com pypdf inutilizável, o PyMuPDF sozinho ainda entrega o texto."""
    if not FIXTURE.is_file():
        pytest.skip("fixture ausente")

    def explode(*_a, **_k):
        raise PanicoNativo("extensão nativa quebrada")

    monkeypatch.setattr(P, "_make_pypdf_reader", explode)
    monkeypatch.setattr(P, "_extract_pdfplumber_page", explode)

    r = P.extract_pdf(FIXTURE)
    assert r.text_chars > 300, "o texto do PDF deveria ter sido extraído mesmo assim"
    assert r.status == "indexed"


def test_a_nota_denuncia_o_motor_indisponivel(monkeypatch):
    """Dependência quebrada em silêncio é a pior forma de falha.

    Sem esta linha, a tela diz que o PDF não foi lido e ninguém descobre que
    faltava um motor — o operador procura defeito no PDF, não na instalação.
    """
    if not FIXTURE.is_file():
        pytest.skip("fixture ausente")

    paginas_ruins = {"n": 0}

    def texto_insuficiente(*_a, **_k):
        # Força o pipeline a buscar o fallback, que é onde o pypdf entra.
        paginas_ruins["n"] += 1
        return ""

    def explode(*_a, **_k):
        raise PanicoNativo("DLL bloqueada pelo antivirus")

    monkeypatch.setattr(P, "_extract_pymupdf_page", texto_insuficiente)
    monkeypatch.setattr(P, "_make_pypdf_reader", explode)
    monkeypatch.setattr(P, "_extract_pdfplumber_page", explode)

    r = P.extract_pdf(FIXTURE)
    assert paginas_ruins["n"] > 0, "o teste não exercitou o caminho do fallback"
    assert "indisponível" in (r.note or "").lower(), (
        "a nota precisa avisar que um motor está inutilizável:\n" + (r.note or "")
    )
    assert "DIAGNOSTICAR_PDF" in (r.note or ""), "a nota precisa indicar o próximo passo"
