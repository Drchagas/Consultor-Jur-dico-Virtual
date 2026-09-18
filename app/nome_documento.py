"""Identificação e nomeação de documentos processuais.

Dois problemas que os downloads do eproc criavam.

**1. PDF válido recusado pelo nome.** O upload rejeitava qualquer arquivo cujo
nome não terminasse em `.pdf`. Baixar dos autos pelo eproc produz nomes assim:

    https___eproc1g-download_tjrs_jus_br_..._downloa__1_.PDF_numIdSessao_0117...
                                                      ^^^^ extensão no MEIO

O arquivo é PDF legítimo — 43 páginas, cabeçalho `%PDF-1.7`. Só o nome é que
não colabora. Identificação passa a ser por CONTEÚDO: `store_uploaded_pdf` já
confere os bytes mágicos `%PDF-`, que é a única prova que vale.

**2. Nome inutilizável.** Salvo assim, o documento aparece na interface e nas
citações do Copiloto como 200 caracteres de URL. Aqui extraímos o número CNJ
de dentro do PDF e nomeamos `Processo 5005877-37.2026.8.21.0041.pdf`.

O número extraído é validado pelo dígito verificador da Resolução CNJ 65/2008,
então uma sequência parecida que apareça no corpo dos autos não é confundida
com o número do processo.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# NNNNNNN-DD.AAAA.J.TR.OOOO — tolera espaços vindos da extração de texto.
RE_CNJ = re.compile(
    r"(\d{7})\s*-\s*(\d{2})\s*\.\s*(\d{4})\s*\.\s*(\d)\s*\.\s*(\d{2})\s*\.\s*(\d{4})")

# Ruído típico de download de tribunal, em qualquer posição do nome.
# Ruído de download de tribunal. "acao" NÃO entra aqui: é palavra jurídica
# legítima ("Ação Revisional"), e a limpeza só roda em nome que já se provou
# ser URL — ver precisa_renomear().
RUIDO = re.compile(
    r"(https?|www|eproc\w*|download\w*|downloa|controlador|php|completo|"
    r"numIdSessao|numIdProcessosBatch|numIdDocumento|hash|jus|br|gov|"
    r"tjrs|tjsp|tjmg|trf\d|tst|trt\d|stj|stf|pje|projudi|esaj|seeu)",
    re.I)

# Marcadores que provam que o nome veio de uma URL, não de uma pessoa.
MARCA_URL = re.compile(
    r"(https?[_:]|www\.|\.php|numIdSessao|numIdProcessosBatch|hash[=_]|"
    r"controlador|eproc\w*[-_.]|acao[=_]download)", re.I)

# Extensões que definitivamente não são documento processual.
NAO_PDF = {".exe", ".msi", ".bat", ".cmd", ".sh", ".zip", ".rar", ".7z",
           ".mp3", ".mp4", ".avi", ".mkv", ".mov", ".iso", ".dll", ".js"}


# --------------------------------------------------------------- identificar

def parece_pdf(nome: str, content_type: str = "") -> bool:
    """Pré-filtro barato. A palavra final é dos bytes mágicos, não daqui.

    Serve só para evitar gravar um arquivo claramente errado em disco antes
    da checagem de conteúdo. Na dúvida, aceita: recusar PDF legítimo é pior
    que gravar e apagar um arquivo inválido.
    """
    nome = (nome or "").strip()
    if not nome:
        return False

    sufixo = Path(nome).suffix.lower()
    if sufixo in NAO_PDF:
        return False

    if ".pdf" in nome.lower():        # no fim OU no meio (caso eproc)
        return True

    tipo = (content_type or "").lower()
    if "pdf" in tipo or tipo in ("application/octet-stream", ""):
        return True

    # Nome sem extensão alguma: o eproc às vezes entrega assim.
    return not sufixo


def tem_cabecalho_pdf(primeiros_bytes: bytes) -> bool:
    """`%PDF-` pode não estar no byte 0: há arquivos com lixo antes."""
    return b"%PDF-" in (primeiros_bytes or b"")[:1024]


# ------------------------------------------------------------- número do CNJ

def _dv_cnj(n7: str, aaaa: str, j: str, tr: str, oooo: str) -> int:
    """Dígito verificador da Resolução CNJ 65/2008: 98 - (base*100 mod 97)."""
    base = int(f"{n7}{aaaa}{j}{tr}{oooo}")
    return 98 - ((base * 100) % 97)


def cnj_valido(numero: str) -> bool:
    m = RE_CNJ.fullmatch((numero or "").strip())
    if not m:
        return False
    n7, dd, aaaa, j, tr, oooo = m.groups()
    return _dv_cnj(n7, aaaa, j, tr, oooo) == int(dd)


def formatar_cnj(n7, dd, aaaa, j, tr, oooo) -> str:
    return f"{n7}-{dd}.{aaaa}.{j}.{tr}.{oooo}"


def extrair_numero_processo(texto: str, validar: bool = True) -> str | None:
    """Primeiro número CNJ do texto. Com validar=True, só aceita DV correto.

    A validação importa: os autos citam números de outros processos (apensos,
    precedentes, recursos). Sem o dígito verificador qualquer sequência de
    formato parecido viraria o nome do arquivo.
    """
    for m in RE_CNJ.finditer(texto or ""):
        n7, dd, aaaa, j, tr, oooo = m.groups()
        if not validar or _dv_cnj(n7, aaaa, j, tr, oooo) == int(dd):
            return formatar_cnj(n7, dd, aaaa, j, tr, oooo)
    return None


# ------------------------------------------------------------------- nomear

def parece_url(nome: str) -> bool:
    return bool(MARCA_URL.search(nome or ""))


def limpar_nome(nome: str, tamanho_max: int = 70) -> str:
    """Nome legível. Só remove ruído quando o nome é comprovadamente URL.

    Aplicar a limpeza agressiva em tudo destruiria nomes legítimos: "Ação
    Revisional" perderia "Ação", "Petição" perderia a acentuação útil.
    """
    base = Path((nome or "").strip().replace("\\", "/")).name
    if not parece_url(base):
        # Nome de gente: preserva, só tira caracteres proibidos em arquivo.
        limpo = re.sub(r'[<>:"/\\|?*]+', " ", Path(base).stem)
        return re.sub(r"\s+", " ", limpo).strip()[:tamanho_max]

    base = re.sub(r"\.pdf\b.*$", "", base, flags=re.I)     # corta após a extensão
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode()
    base = re.sub(r"[^A-Za-z0-9]+", " ", base)
    partes = [p for p in base.split()
              if not RUIDO.fullmatch(p)
              and not re.fullmatch(r"\d{6,}", p)               # ids longos
              and not re.fullmatch(r"[0-9a-f]{16,}", p, re.I)   # hashes
              and not re.fullmatch(r"acao", p, re.I)            # só em URL
              and len(p) > 1]
    return " ".join(partes)[:tamanho_max].strip()


def nome_amigavel(nome_original: str, texto_pdf: str = "",
                  numero_caso: str = "") -> str:
    """Melhor nome disponível, nesta ordem de preferência.

    1. número CNJ encontrado dentro do PDF (validado)
    2. número do processo já cadastrado no caso
    3. nome original limpo do ruído de URL
    4. 'Documento.pdf'
    """
    numero = extrair_numero_processo(texto_pdf) if texto_pdf else None
    if not numero and numero_caso and cnj_valido(numero_caso):
        numero = numero_caso.strip()
    if numero:
        return f"Processo {numero}.pdf"

    limpo = limpar_nome(nome_original)
    if limpo:
        return f"{limpo}.pdf"

    original = Path((nome_original or "").strip()).name
    if original and len(original) <= 80 and ".pdf" in original.lower():
        return original
    return "Documento.pdf"


def precisa_renomear(nome: str) -> bool:
    """Nome é ruído de download de tribunal?"""
    nome = nome or ""
    return len(nome) > 120 or parece_url(nome)
