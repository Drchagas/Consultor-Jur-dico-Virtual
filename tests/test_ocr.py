"""OCR para autos digitalizados.

Um PDF gerado pelo eproc tem camada de texto e é lido sem OCR. Um auto
escaneado — petição assinada à mão, documento antigo, ofício de outro órgão —
é só imagem: sem OCR o JARBAS não tem uma letra para ler, e a única saída é
mandar cada página para a IA, o que custa por página e depende de internet.

O detalhe que decide o resultado é o idioma. O instalador do Tesseract marca
apenas o inglês por padrão; com ele, o OCR de um auto brasileiro RODA e
devolve letra embaralhada — um sintoma que não aponta para a causa. Por isso o
português viaja dentro do pacote de instalação.
"""

import shutil
import sys
from pathlib import Path

import pytest

_RAIZ = Path(__file__).resolve().parent.parent
RAIZ = _RAIZ / "payload" if (_RAIZ / "payload" / "app").is_dir() else _RAIZ
sys.path.insert(0, str(RAIZ))

from app import pdf_pipeline as P  # noqa: E402

# A suíte roda em dois lugares: na árvore de fontes e DENTRO do pacote
# montado, onde o idioma fica em ocr/ na raiz. Um caminho fixo deixava quatro
# testes vermelhos no pacote — e teste que acusa falha onde não há ensina a
# equipe a ignorar a suíte inteira.
POR = next(
    (c for c in (_RAIZ / "installer" / "ocr" / "por.traineddata",
                 _RAIZ / "ocr" / "por.traineddata",
                 RAIZ / "ocr" / "por.traineddata")
     if c.is_file()),
    _RAIZ / "installer" / "ocr" / "por.traineddata",
)


def test_o_idioma_portugues_viaja_no_projeto():
    """Baixar na hora não serve: rede de escritório bloqueia o GitHub."""
    assert POR.is_file(), "installer/ocr/por.traineddata não está no projeto"
    assert POR.stat().st_size > 500_000, "arquivo de idioma truncado"


def test_o_idioma_tem_a_procedencia_registrada():
    """Arquivo binário sem origem declarada é dívida de auditoria."""
    leia = POR.parent / "LEIA-ME.txt"
    assert leia.is_file(), "falta a procedência do arquivo de idioma"
    texto = leia.read_text(encoding="utf-8")
    assert "tessdata_fast" in texto and "Apache" in texto
    assert "SHA-256" in texto


def test_o_portugues_e_instalado_quando_falta_no_tessdata(tmp_path, monkeypatch):
    """Vale mesmo quando o advogado instalou o Tesseract por conta própria."""
    tessdata = tmp_path / "tessdata"
    tessdata.mkdir()
    (tessdata / "eng.traineddata").write_bytes(b"ingles")

    monkeypatch.setattr(P, "_POR_EMBUTIDO", POR)
    assert P._garantir_portugues(tessdata) is True
    assert (tessdata / "por.traineddata").is_file()
    assert (tessdata / "por.traineddata").stat().st_size == POR.stat().st_size


def test_nao_sobrescreve_um_portugues_ja_instalado(tmp_path, monkeypatch):
    """O advogado pode ter posto a variante 'best', de melhor acerto."""
    tessdata = tmp_path / "tessdata"
    tessdata.mkdir()
    (tessdata / "por.traineddata").write_bytes(b"variante escolhida pelo usuario")

    monkeypatch.setattr(P, "_POR_EMBUTIDO", POR)
    assert P._garantir_portugues(tessdata) is False
    assert (tessdata / "por.traineddata").read_bytes() == b"variante escolhida pelo usuario"


def test_sem_permissao_de_escrita_nao_derruba_a_leitura(tmp_path, monkeypatch):
    """Tesseract em Program Files sem administrador: seguimos com o que existe."""
    tessdata = tmp_path / "tessdata"
    tessdata.mkdir()

    def recusa(*_a, **_k):
        raise PermissionError("acesso negado")

    monkeypatch.setattr(P, "_POR_EMBUTIDO", POR)
    monkeypatch.setattr(P.shutil, "copy2", recusa)
    assert P._garantir_portugues(tessdata) is False  # não levanta


def test_caminho_explicito_vence_a_busca(tmp_path, monkeypatch):
    """JARBAS_TESSERACT existe porque o PATH do Windows só é relido no reboot.

    Depois de instalar o Tesseract, a sessão em curso não enxerga o novo PATH;
    sem a variável, o OCR só passaria a funcionar no dia seguinte.
    """
    falso = tmp_path / ("tesseract.exe" if P.os.name == "nt" else "tesseract")
    falso.write_text("#!/bin/sh\n")
    monkeypatch.setenv("JARBAS_TESSERACT", str(falso))

    ok, caminho = P._detect_tesseract()
    assert ok is True
    assert caminho == falso.resolve()


def test_caminho_explicito_aceita_a_pasta(tmp_path, monkeypatch):
    """Quem configura à mão costuma apontar a pasta, não o executável."""
    binario = tmp_path / ("tesseract.exe" if P.os.name == "nt" else "tesseract")
    binario.write_text("#!/bin/sh\n")
    monkeypatch.setenv("JARBAS_TESSERACT", str(tmp_path))

    ok, caminho = P._detect_tesseract()
    assert ok is True and caminho == binario.resolve()


def test_caminho_inexistente_nao_engana_o_sistema(tmp_path, monkeypatch):
    """Variável apontando para o nada deve cair na busca normal, não mentir."""
    monkeypatch.setenv("JARBAS_TESSERACT", str(tmp_path / "nao-existe"))
    monkeypatch.setattr(P.shutil, "which", lambda _n: None)
    ok, caminho = P._detect_tesseract()
    assert ok is False and caminho is None


# ================================================= scripts de instalação

SCRIPTS = _RAIZ / "scripts"


def _ps1(nome):
    arq = SCRIPTS / nome
    if not arq.is_file():
        pytest.skip(f"{nome} ausente nesta árvore")
    return arq.read_text(encoding="utf-8-sig")


def test_o_instalador_de_ocr_dispensa_administrador():
    """Falta de privilégio é o que mais trava instalação na máquina do escritório."""
    fonte = _ps1("INSTALAR_OCR.ps1")
    assert "LOCALAPPDATA" in fonte and "Programs\\Tesseract-OCR" in fonte
    assert "/S" in fonte, "a instalação precisa ser silenciosa"


def test_o_instalador_de_ocr_garante_o_portugues():
    fonte = _ps1("INSTALAR_OCR.ps1")
    assert "por.traineddata" in fonte
    assert "--list-langs" in fonte, "precisa CONFERIR se o português ficou disponível"


def test_o_instalador_de_ocr_explica_o_que_fazer_quando_falha():
    """Download bloqueado pelo antivírus é o caso mais provável."""
    fonte = _ps1("INSTALAR_OCR.ps1")
    assert "UB-Mannheim" in fonte, "precisa dar o caminho manual"
    assert "continua funcionando sem OCR" in fonte, (
        "o operador precisa saber que o sistema não ficou quebrado"
    )


def test_o_idioma_e_encontrado_em_qualquer_layout(tmp_path, monkeypatch):
    """Caminho fixo funcionava em dois layouts de três e falhava no terceiro.

    Falha silenciosa aqui significa OCR rodando em inglês num auto brasileiro:
    roda, não dá erro, e devolve letra embaralhada.
    """
    conteudo = POR.read_bytes()[:1000] if POR.is_file() else b"modelo"
    for relativo in ("installer/ocr", "ocr"):
        raiz = tmp_path / relativo.replace("/", "_")
        alvo = raiz / relativo
        alvo.mkdir(parents=True)
        (alvo / "por.traineddata").write_bytes(conteudo)
        monkeypatch.setattr(P, "BASE_DIR", raiz)
        achado = P._achar_portugues()
        assert achado is not None and achado.is_file(), f"não achou em {relativo}"


def test_ausencia_do_idioma_nao_derruba_a_leitura(tmp_path, monkeypatch):
    """Sem o arquivo, seguimos com o que o Tesseract tiver instalado."""
    monkeypatch.setattr(P, "BASE_DIR", tmp_path)
    assert P._achar_portugues() is None
    monkeypatch.setattr(P, "_POR_EMBUTIDO", None)
    tessdata = tmp_path / "tessdata"
    tessdata.mkdir()
    assert P._garantir_portugues(tessdata) is False  # não levanta
