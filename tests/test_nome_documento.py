"""PDF baixado de tribunal precisa ser aceito e ganhar nome utilizável.

Caso real: download do eproc/TJRS chega com a extensão NO MEIO do nome —

    ..._downloa__1_.PDF_numIdSessao_0117...&hash=08c6d57a05f5...

232 caracteres, sem `.pdf` no fim. O upload rejeitava com "apenas PDF é
aceito" antes de ler um byte, embora o arquivo seja um PDF legítimo de 43
páginas com cabeçalho %PDF-1.7.
"""

import re
import sys
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
RAIZ = _RAIZ / "payload" if (_RAIZ / "payload" / "app").is_dir() else _RAIZ
sys.path.insert(0, str(RAIZ))

import pytest

from app import nome_documento as N

APP = RAIZ / "app"

# Nome real do arquivo baixado do eproc, sem `.pdf` no fim.
EPROC = ("1789212877869_https___eproc1g-download_tjrs_jus_br_eproc_controlador_"
         "php_acao_download_completo_downloa__1_.PDF_numIdSessao_011789210211395"
         "64111_numIdProcessosBatch_11789212338913745002511760682_hash_08c6d57a0"
         "5f5d35b2a4e6868c52e085a")

# Nome do documento #10 já cadastrado, com formato de URL antigo.
EPROC_ANTIGO = ("https___eproc1g-download.tjrs.jus.br_eproc_controlador.php_acao="
                "download_completo_downloa (3).PDF&numIdSessao=011788352982716385"
                "27&numIdProcessosBatch=1178838950362306051623122482&hash=f90f12c3"
                "248707297a3fc28b64471d32.pdf")


# --------------------------------------------------------------- aceitação

def test_pdf_do_eproc_sem_extensao_no_fim_e_aceito():
    assert N.parece_pdf(EPROC), "o arquivo real do TJRS estava sendo recusado"


def test_formato_antigo_do_eproc_e_aceito():
    assert N.parece_pdf(EPROC_ANTIGO)


def test_nome_sem_extensao_alguma_e_aceito():
    """O eproc às vezes entrega o arquivo sem extensão nenhuma."""
    assert N.parece_pdf("documento_sem_extensao")


def test_content_type_pdf_basta():
    assert N.parece_pdf("arquivo", "application/pdf")
    assert N.parece_pdf("arquivo", "application/octet-stream")


def test_tipos_claramente_errados_sao_recusados():
    for n in ("virus.exe", "instalador.msi", "filme.mkv", "musica.mp3",
              "backup.zip", "imagem.iso", "script.sh", "lib.dll"):
        assert not N.parece_pdf(n), f"{n} deveria ser recusado"


def test_nome_vazio_e_recusado():
    assert not N.parece_pdf("")
    assert not N.parece_pdf("   ")


def test_cabecalho_pdf_e_o_que_decide():
    assert N.tem_cabecalho_pdf(b"%PDF-1.7\n...")
    assert N.tem_cabecalho_pdf(b"lixo antes\n%PDF-1.4")   # ocorre na prática
    assert not N.tem_cabecalho_pdf(b"PK\x03\x04 isto e um zip")
    assert not N.tem_cabecalho_pdf(b"")


# --------------------------------------------------- número do processo

def test_digito_verificador_cnj_confere_em_processos_reais():
    """Resolução CNJ 65/2008. Números reais do escritório."""
    for numero in ("5005877-37.2026.8.21.0041", "5020315-43.2014.4.04.7107",
                   "5004781-84.2026.8.21.0041", "5002483-48.2026.8.21.0097",
                   "5005841-29.2025.8.21.0041"):
        assert N.cnj_valido(numero), numero


def test_numero_com_digito_errado_e_rejeitado():
    assert not N.cnj_valido("5005877-99.2026.8.21.0041")
    assert not N.cnj_valido("1111111-11.2020.1.11.1111")


def test_formato_invalido_e_rejeitado():
    for ruim in ("", "12345", "5005877/2026", "processo 5005877-37"):
        assert not N.cnj_valido(ruim)


def test_extrai_numero_do_texto_dos_autos():
    texto = "CAPA PROCESSO abertura Capa: Parte 1 PROCESSO Nº 5005877-37.2026.8.21.0041"
    assert N.extrair_numero_processo(texto) == "5005877-37.2026.8.21.0041"


def test_numero_invalido_no_corpo_nao_vira_nome():
    """Os autos citam apensos, precedentes e recursos. Sem o dígito
    verificador, qualquer sequência parecida viraria o nome do arquivo."""
    assert N.extrair_numero_processo("ref. 5005877-99.2026.8.21.0041") is None


def test_escolhe_o_numero_valido_entre_varios():
    texto = "apenso 1111111-11.2020.1.11.1111 e autos 5005877-37.2026.8.21.0041"
    assert N.extrair_numero_processo(texto) == "5005877-37.2026.8.21.0041"


def test_tolera_espacos_da_extracao_de_texto():
    """pdfplumber e pymupdf às vezes inserem espaço entre os grupos."""
    assert N.extrair_numero_processo("5005877 - 37 . 2026 . 8 . 21 . 0041") \
        == "5005877-37.2026.8.21.0041"


# ------------------------------------------------------------- nomeação

def test_nome_vem_do_numero_do_processo_quando_ha_texto():
    texto = "PROCESSO Nº 5005877-37.2026.8.21.0041"
    assert N.nome_amigavel(EPROC, texto) == "Processo 5005877-37.2026.8.21.0041.pdf"


def test_cai_no_numero_do_caso_quando_o_pdf_nao_traz():
    assert N.nome_amigavel(EPROC, "", "5005841-29.2025.8.21.0041") \
        == "Processo 5005841-29.2025.8.21.0041.pdf"


def test_numero_de_caso_invalido_nao_e_usado():
    assert "9999999" not in N.nome_amigavel(EPROC, "", "9999999-99.2026.8.21.0041")


def test_nome_de_pessoa_e_preservado_intacto():
    """A limpeza agressiva só vale para URL. Aplicá-la a tudo destruiria
    nomes legítimos."""
    for n in ("Contestacao Cliente Joao.pdf", "Laudo Pericial Contabil.pdf"):
        assert N.nome_amigavel(n) == n


def test_acentuacao_e_palavra_juridica_sobrevivem():
    """Regressão: 'acao' estava na lista de ruído e comia 'Ação Revisional'."""
    n = "Petição Inicial - Ação Revisional.pdf"
    saida = N.nome_amigavel(n)
    assert "Ação" in saida and "Petição" in saida, saida


def test_nome_de_pessoa_nao_e_marcado_para_renomear():
    for n in ("Contestacao.pdf", "Petição Inicial - Ação Revisional.pdf"):
        assert not N.precisa_renomear(n)


def test_url_de_tribunal_e_marcada_para_renomear():
    assert N.precisa_renomear(EPROC)
    assert N.precisa_renomear(EPROC_ANTIGO)


def test_nome_longo_demais_e_marcado():
    assert N.precisa_renomear("x" * 130)


def test_saida_sempre_termina_em_pdf():
    for n in (EPROC, EPROC_ANTIGO, "qualquer", "", "arquivo.PDF"):
        assert N.nome_amigavel(n).lower().endswith(".pdf")


def test_saida_nao_contem_separador_de_caminho():
    """Nome que vira caminho é travessia de diretório."""
    for n in ("../../etc/passwd.pdf", r"C:\\Windows\\system32\\a.pdf", "a/b/c.pdf"):
        saida = N.nome_amigavel(n)
        assert "/" not in saida and "\\" not in saida, saida


def test_saida_tem_tamanho_utilizavel():
    assert len(N.nome_amigavel(EPROC, "PROCESSO Nº 5005877-37.2026.8.21.0041")) < 60


# ------------------------------------------------------- fiação no upload

def test_upload_nao_rejeita_mais_por_extensao_no_fim():
    src = (APP / "main.py").read_text(encoding="utf-8")
    assert 'original.lower().endswith(".pdf")' not in src, (
        "checagem por extensão no fim recusa PDF legítimo do eproc")
    assert "nome_documento.parece_pdf(" in src


def test_conteudo_continua_sendo_validado():
    """Afrouxar o nome não pode afrouxar a validação real."""
    cop = (APP / "copilot.py").read_text(encoding="utf-8")
    assert 'b"%PDF-" not in header' in cop


def test_renomeacao_roda_depois_da_indexacao():
    """Antes de indexar não há texto, então não há número a extrair."""
    src = (APP / "main.py").read_text(encoding="utf-8")
    i = src.index("index_pdf(conn, org_id=org_id")
    # a CHAMADA, não a definição (que fica antes no arquivo)
    j = src.index("amigavel = _renomear_documento(")
    assert i < j, "renomear antes de indexar não encontra o número"


def test_renomear_nao_move_o_arquivo_em_disco():
    """Mexer em stored_path quebraria os links de download já emitidos."""
    src = (APP / "main.py").read_text(encoding="utf-8")
    i = src.index("def _renomear_documento(")
    corpo = src[i:src.index("\ndef ", i + 10)]
    # ignora a docstring: ela menciona stored_path só para explicar a decisão
    codigo = re.sub(r'"""(?:.|\n)*?"""', "", corpo)
    assert "stored_path" not in codigo, "renomear não pode mexer no caminho em disco"
    assert "original_name=?" in codigo


def test_ferramenta_de_renomear_existe():
    for c in (RAIZ / "tools", _RAIZ / "tools"):
        if (c / "manutencao.py").is_file():
            assert "--renomear" in (c / "manutencao.py").read_text(encoding="utf-8")
            return
    pytest.skip("sem tools/ neste layout")


# ------------------------------------- leitura real do PDF de tribunal

def _pdf_eproc():
    import glob
    achados = sorted(glob.glob("/mnt/user-data/uploads/1789*eproc*"))
    if not achados:
        pytest.skip("PDF do eproc não disponível neste ambiente")
    return Path(achados[0])


def test_pipeline_le_o_pdf_do_eproc_integralmente():
    """O arquivo que o escritório relatou como ilegível: 43 páginas, 100%."""
    from app import pdf_pipeline as PP
    r = PP.extract_pdf(_pdf_eproc())
    assert getattr(r, "status", "") == "indexed"
    assert getattr(r, "page_count", 0) == 43
    assert getattr(r, "coverage", 0) >= 0.99
    assert getattr(r, "text_chars", 0) > 20000


def test_pipeline_tolera_lixo_antes_do_cabecalho():
    """store_uploaded_pdf aceita %PDF- em qualquer ponto dos 1024 bytes
    iniciais; os motores de leitura precisam aguentar o mesmo."""
    import tempfile
    from app import pdf_pipeline as PP
    orig = _pdf_eproc().read_bytes()
    d = Path(tempfile.mkdtemp())
    for rotulo, prefixo in (("crlf", b"\r\n\r\n"), ("bom", b"\xef\xbb\xbf")):
        p = d / f"{rotulo}.pdf"
        p.write_bytes(prefixo + orig)
        r = PP.extract_pdf(p)
        assert getattr(r, "page_count", 0) == 43, rotulo
        assert getattr(r, "text_chars", 0) > 20000, rotulo


def test_nome_extraido_do_pdf_real_bate_com_o_processo():
    from app import pdf_pipeline as PP
    arq = _pdf_eproc()
    r = PP.extract_pdf(arq)
    texto = "\n".join((getattr(p, "text", "") or "")
                      for p in (getattr(r, "pages", []) or [])[:3])
    assert N.extrair_numero_processo(texto) == "5005877-37.2026.8.21.0041"
    assert N.nome_amigavel(arq.name, texto) == "Processo 5005877-37.2026.8.21.0041.pdf"


# ------------------------------------------------ ferramenta de diagnóstico

def _ferramenta():
    for c in (RAIZ / "tools", _RAIZ / "tools"):
        if (c / "diagnosticar_pdf.py").is_file():
            return (c / "diagnosticar_pdf.py").read_text(encoding="utf-8")
    pytest.skip("sem tools/ neste layout")


def test_diagnostico_cobre_as_quatro_etapas():
    src = _ferramenta()
    for etapa in ("[1/4]", "[2/4]", "[3/4]", "[4/4]"):
        assert etapa in src


def test_diagnostico_avisa_quando_a_instalacao_e_antiga():
    """Sem esse aviso a ferramenta não distingue 'PDF ruim' de 'versão velha'."""
    src = _ferramenta()
    assert "except ImportError" in src
    assert "8.8.2" in src
