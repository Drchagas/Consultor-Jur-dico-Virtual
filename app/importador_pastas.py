"""Importação de uma pasta de clientes: leitura, proposta e revisão.

O escritório já tem os autos organizados em pastas no computador há anos —
uma pasta por cliente, os PDFs dentro. Recadastrar isso à mão, cliente por
cliente, processo por processo, é o que faz um sistema novo nunca sair do
papel.

Este módulo lê essa estrutura e PROPÕE cadastros. Nada aqui grava coisa
alguma: a gravação acontece em pasta_routes.py, depois que o advogado
confere e confirma na tela. A distinção é deliberada e não é estilo —
cadastro automático de cliente errado, com CPF de outra pessoa colhido de um
PDF, é dado pessoal errado em dossiê sob sigilo profissional.

Duas origens, um caminho só:

- ENVIO DE PASTA pelo navegador: funciona em qualquer instalação, inclusive
  em servidor, e não dá ao sistema acesso nenhum ao disco do usuário.
- MAPEAMENTO DE PASTA LOCAL: só na instalação do escritório, e só quando
  JARBAS_PERMITE_MAPEAR_PASTA=1. Ler um caminho arbitrário do disco a pedido
  de quem está logado é um poder que não pode existir num servidor
  multi-escritório.

As duas produzem a mesma lista de Proposta, e daí para a frente o código é o
mesmo.

Sobre a IA: ela entra lendo UM PDF por processo, não todos. Rodar a IA em
cada arquivo de um acervo de 300 clientes custaria caro e demoraria horas
sem melhorar a proposta — o que identifica um processo está na petição
inicial ou na capa dos autos, não no 40º anexo. E por padrão a leitura é
local: a IA é opção marcada na tela, com teto por execução.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from . import smart_intake

# Só PDF é indexado e lido. Outros formatos são guardados como anexo do
# processo — recusá-los faria o advogado ter de separar a pasta à mão, que é
# exatamente o trabalho que este módulo existe para evitar.
EXT_PDF = ".pdf"
EXT_ANEXO = {".docx", ".doc", ".odt", ".rtf", ".txt", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}

# Lixo que o Windows e o Office deixam em toda pasta. Aparecer na proposta
# não ajuda ninguém e ainda esconde os arquivos que importam.
NOMES_IGNORADOS = {"thumbs.db", "desktop.ini", ".ds_store"}
PREFIXOS_IGNORADOS = ("~$", "._")

MAX_PROFUNDIDADE = 4
MAX_PASTAS = 300
MAX_ARQUIVOS = 4000
MAX_COMPONENTES = 8


def teto_de_ia() -> int:
    """Quantos PDFs, no máximo, vão para a IA numa execução."""
    try:
        return max(0, int(os.getenv("JARBAS_IMPORT_MAX_IA", "25") or 0))
    except ValueError:
        return 25


def mapeamento_liberado() -> bool:
    """Ler pasta do disco é privilégio da instalação local, não do usuário.

    Numa instalação em servidor com mais de um escritório, isto seria leitura
    de arquivo arbitrário concedida a quem tem login. O instalador do Windows
    grava 1; o exemplo de servidor mantém 0.
    """
    return os.getenv("JARBAS_PERMITE_MAPEAR_PASTA", "0").strip() == "1"


# ----------------------------------------------------------- caminhos seguros

def _sem_acento(valor: str) -> str:
    return unicodedata.normalize("NFKD", valor or "").encode("ascii", "ignore").decode("ascii")


def caminho_relativo_seguro(bruto: str) -> str:
    """Normaliza o caminho que o NAVEGADOR enviou. Devolve '' se não servir.

    O nome de arquivo de um upload é texto controlado por quem envia. Com
    webkitdirectory ele vem com o caminho relativo dentro da pasta escolhida,
    que é justamente o que queremos preservar — e também o que permitiria
    escrever fora da área do JARBAS se fosse usado como veio.
    """
    valor = (bruto or "").strip().replace("\\", "/")
    valor = re.sub(r"^[A-Za-z]:", "", valor)          # C:/... do Windows
    valor = valor.lstrip("/")
    partes: list[str] = []
    for parte in valor.split("/"):
        parte = parte.strip()
        if not parte or parte == ".":
            continue
        if parte == "..":
            return ""                                  # nunca sobe um nível
        if "\x00" in parte:
            return ""
        partes.append(parte[:120])
    if not partes or len(partes) > MAX_COMPONENTES:
        return ""
    return "/".join(partes)


def arquivo_ignorado(nome: str) -> bool:
    baixo = Path(nome).name.lower()
    if baixo in NOMES_IGNORADOS or baixo.startswith("."):
        return True
    return any(baixo.startswith(p) for p in PREFIXOS_IGNORADOS)


def classificar(nome: str) -> str:
    """'pdf', 'anexo' ou '' (descartado)."""
    ext = Path(nome).suffix.lower()
    if ext == EXT_PDF:
        return "pdf"
    if ext in EXT_ANEXO:
        return "anexo"
    return ""


# -------------------------------------------------- leitura do nome da pasta

_ORDINAL = re.compile(r"^\s*\d{1,4}\s*[-–—._)\]]\s*")
_SEPARADORES = re.compile(r"[_\-–—]+")


def dados_do_nome_de_pasta(nome: str) -> dict[str, str]:
    """Tira do nome da pasta o que ele já entrega de graça.

    Pastas de escritório costumam trazer tudo no próprio nome:
    '012 - JOAO DA SILVA - 5001234-56.2026.8.21.0041'. Ler isso primeiro
    significa que a proposta já vem preenchida mesmo quando o PDF é
    digitalizado e não há uma letra para extrair.
    """
    bruto = (nome or "").strip()
    numero = smart_intake._find_cnj(bruto)
    documento = smart_intake._extract_document(bruto)

    resto = bruto
    if numero:
        resto = resto.replace(numero, " ")
        # a forma "sem pontuação" do mesmo número também costuma aparecer
        resto = re.sub(r"(?<!\d)\d{7}[-–—.\s]?\d{2}[.\s]?\d{4}[.\s]?\d[.\s]?\d{2}[.\s]?\d{4}(?!\d)", " ", resto)
    if documento:
        resto = re.sub(r"(?<!\d)\d{2,3}[.\s]?\d{3}[.\s]?\d{3}[/\s]?(?:\d{4})?[-\s]?\d{2}(?!\d)", " ", resto)
    resto = _ORDINAL.sub("", resto)
    resto = _SEPARADORES.sub(" ", resto)
    resto = re.sub(r"\s+", " ", resto).strip(" .,-–—")

    nome_cliente = resto if smart_intake._looks_like_name(resto) else ""
    return {"nome": nome_cliente, "documento": documento, "numero": numero}


# ------------------------------------------------------------ estrutura lida

@dataclass
class ArquivoLido:
    relativo: str           # caminho como o operador o reconhece
    origem: Optional[Path]  # onde está agora (staging do upload ou disco)
    tamanho: int = 0
    tipo: str = "pdf"       # 'pdf' | 'anexo'


@dataclass
class ProcessoProposto:
    subpasta: str
    numero: str = ""
    titulo: str = ""
    area: str = ""
    tribunal: str = ""
    valor_causa: str = ""
    confianca: str = "baixa"
    avisos: list[str] = field(default_factory=list)
    arquivos: list[ArquivoLido] = field(default_factory=list)
    ia_usada: bool = False
    partes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def pdfs(self) -> list[ArquivoLido]:
        return [a for a in self.arquivos if a.tipo == "pdf"]


@dataclass
class Proposta:
    pasta: str
    cliente_nome: str = ""
    documento: str = ""
    processos: list[ProcessoProposto] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)

    @property
    def total_arquivos(self) -> int:
        return sum(len(p.arquivos) for p in self.processos)


# -------------------------------------------------------------- agrupamento

def agrupar(arquivos: Iterable[ArquivoLido], raiz_rotulo: str = "") -> dict[str, dict[str, list[ArquivoLido]]]:
    """{pasta do cliente: {subpasta do processo: [arquivos]}}.

    O primeiro nível é o cliente; o segundo, quando existe, é o processo.
    Arquivo solto na raiz vira um grupo com o nome da própria raiz — perder
    arquivo em silêncio seria pior do que propor um cadastro a mais, que o
    advogado desmarca em um clique.
    """
    grupos: dict[str, dict[str, list[ArquivoLido]]] = {}
    for arq in arquivos:
        partes = arq.relativo.split("/")
        if len(partes) == 1:
            cliente, sub = (raiz_rotulo or "Arquivos soltos"), ""
        else:
            cliente = partes[0]
            sub = partes[1] if len(partes) > 2 else ""
        grupos.setdefault(cliente, {}).setdefault(sub, []).append(arq)
    return grupos


def _melhor_pdf(arquivos: list[ArquivoLido]) -> Optional[ArquivoLido]:
    """O PDF com mais chance de identificar o processo.

    Preferência por nome (inicial, petição, capa) e, na falta de pista, o
    primeiro em ordem alfabética — determinístico de propósito: duas leituras
    da mesma pasta têm de propor a mesma coisa.
    """
    pdfs = [a for a in arquivos if a.tipo == "pdf" and a.origem]
    if not pdfs:
        return None
    pistas = ("inicial", "peticao", "petição", "capa", "autos", "processo", "contrato", "procuracao", "procuração")

    def chave(a: ArquivoLido):
        baixo = _sem_acento(a.relativo).lower()
        posicao = next((i for i, p in enumerate(pistas) if _sem_acento(p).lower() in baixo), len(pistas))
        return (posicao, a.relativo.lower())

    return sorted(pdfs, key=chave)[0]


def analisar(grupos: dict[str, dict[str, list[ArquivoLido]]], *,
             usar_ia: bool = False, limite_ia: Optional[int] = None) -> list[Proposta]:
    """Monta as propostas. Não grava nada e não levanta por PDF defeituoso."""
    limite = teto_de_ia() if limite_ia is None else max(0, limite_ia)
    gastos_ia = 0
    propostas: list[Proposta] = []

    for pasta in sorted(grupos, key=lambda x: x.lower()):
        do_nome = dados_do_nome_de_pasta(pasta)
        proposta = Proposta(pasta=pasta, cliente_nome=do_nome["nome"] or pasta.strip(),
                            documento=do_nome["documento"])
        for sub in sorted(grupos[pasta], key=lambda x: x.lower()):
            arquivos = sorted(grupos[pasta][sub], key=lambda a: a.relativo.lower())
            do_sub = dados_do_nome_de_pasta(sub) if sub else {"nome": "", "documento": "", "numero": ""}
            processo = ProcessoProposto(
                subpasta=sub,
                numero=do_sub["numero"] or (do_nome["numero"] if not sub else ""),
                titulo=(sub or proposta.cliente_nome).strip(),
                arquivos=arquivos,
            )
            escolhido = _melhor_pdf(arquivos)
            if escolhido is None:
                processo.avisos.append(
                    "Nenhum PDF legível nesta pasta: os campos vieram só do nome da pasta.")
            else:
                com_ia = usar_ia and gastos_ia < limite
                lido = _ler_pdf(escolhido, com_ia=com_ia)
                if lido.get("ai_used"):
                    gastos_ia += 1
                _aplicar(processo, proposta, lido, escolhido)
            _avaliar_confianca(processo, proposta)
            proposta.processos.append(processo)
        if usar_ia and gastos_ia >= limite:
            proposta.avisos.append(
                f"O teto de {limite} leituras por IA desta execução foi atingido; "
                "as pastas seguintes foram lidas apenas localmente.")
        propostas.append(proposta)
    return propostas


def _ler_pdf(arquivo: ArquivoLido, *, com_ia: bool) -> dict[str, Any]:
    """Nunca levanta: um PDF corrompido no meio do acervo não pode derrubar
    a importação inteira e obrigar a começar de novo."""
    try:
        return smart_intake.detect_case_metadata(Path(arquivo.origem), prefer_ai=com_ia)
    except Exception as exc:  # noqa: BLE001 — falha de arquivo isolada
        return {"warnings": [f"Não consegui ler {arquivo.relativo}: "
                             f"{type(exc).__name__}: {str(exc)[:160]}"],
                "parties": [], "ai_used": False}


def _chave(nome: str) -> str:
    return re.sub(r"\W+", "", _sem_acento(nome or "").lower())


def _aplicar(processo: ProcessoProposto, proposta: Proposta,
             lido: dict[str, Any], origem: ArquivoLido) -> None:
    processo.numero = processo.numero or str(lido.get("number") or "")
    processo.tribunal = str(lido.get("court") or "")
    processo.area = str(lido.get("area") or "")
    processo.valor_causa = str(lido.get("claim_value") or "")
    processo.ia_usada = bool(lido.get("ai_used"))
    processo.partes = [p for p in (lido.get("parties") or []) if isinstance(p, dict)]
    titulo_lido = str(lido.get("title") or "").strip()
    if titulo_lido and not processo.subpasta:
        processo.titulo = titulo_lido
    elif titulo_lido and titulo_lido not in ("", processo.titulo):
        processo.titulo = titulo_lido
    processo.avisos.extend(str(a) for a in (lido.get("warnings") or []))
    if lido.get("ai_error"):
        processo.avisos.append(str(lido["ai_error"]))
    processo.avisos.append(f"Campos lidos de: {origem.relativo}")

    # CPF/CNPJ do cliente: só o da parte cujo NOME confere com o da pasta.
    # Pegar o documento da primeira parte que aparecer é como colar o CPF do
    # réu na ficha do cliente — erro que ninguém percebe até a procuração sair
    # com o número errado.
    if not proposta.documento:
        alvo = _chave(proposta.cliente_nome)
        for parte in processo.partes:
            if alvo and _chave(str(parte.get("name") or "")) == alvo and parte.get("document"):
                proposta.documento = str(parte["document"])
                break


def _avaliar_confianca(processo: ProcessoProposto, proposta: Proposta) -> None:
    pontos = 0
    if processo.numero:
        pontos += 2
    if proposta.cliente_nome and smart_intake._looks_like_name(proposta.cliente_nome):
        pontos += 1
    if proposta.documento:
        pontos += 1
    if processo.partes:
        pontos += 1
    processo.confianca = "alta" if pontos >= 4 else "media" if pontos >= 2 else "baixa"
    if processo.confianca != "alta":
        processo.avisos.append(
            "Confira nome, CPF/CNPJ e número do processo antes de confirmar: "
            "a leitura automática não substitui a conferência.")


# --------------------------------------------------- varredura do disco local

class PastaRecusada(ValueError):
    """Caminho que não pode ser varrido, com o motivo em português."""


SISTEMA_PROIBIDO = ("/", "/etc", "/proc", "/sys", "/dev", "/boot", "/var", "/usr",
                    "C:\\", "C:\\Windows", "C:\\Program Files", "C:\\Program Files (x86)")


def _pasta_de_dados() -> Optional[Path]:
    from .database import DATA_DIR
    try:
        return Path(DATA_DIR).resolve()
    except OSError:
        return None


def validar_raiz(caminho: str) -> Path:
    """Devolve a pasta a varrer ou explica por que ela não serve."""
    bruto = (caminho or "").strip().strip('"').strip("'")
    if not bruto:
        raise PastaRecusada("Informe o caminho da pasta, por exemplo D:\\Clientes.")
    raiz = Path(bruto)
    if not raiz.is_absolute():
        raise PastaRecusada(
            "Informe o caminho completo da pasta, começando pela unidade "
            "(exemplo: D:\\Clientes). Caminho relativo depende de onde o "
            "serviço foi iniciado e apontaria para outro lugar.")
    try:
        raiz = raiz.resolve(strict=True)
    except OSError:
        raise PastaRecusada(f"A pasta não foi encontrada: {bruto}") from None
    if not raiz.is_dir():
        raise PastaRecusada(f"Isto não é uma pasta: {bruto}")
    for bruto_proibido in SISTEMA_PROIBIDO:
        if str(raiz).rstrip("\\/").lower() == bruto_proibido.rstrip("\\/").lower():
            raise PastaRecusada(
                f"Esta pasta não pode ser varrida: {raiz}. Escolha a pasta onde "
                "ficam os clientes — a raiz do disco e as pastas do sistema "
                "levariam horas e não trariam nenhum cliente.")
    dados = _pasta_de_dados()
    if dados and (raiz == dados or dados in raiz.parents or raiz in dados.parents):
        # Varrer a própria área do JARBAS reimportaria os PDFs que já estão
        # cadastrados, duplicando processo e documento de cliente real.
        raise PastaRecusada(
            f"Esta pasta contém (ou está dentro de) a área de dados do próprio "
            f"JARBAS: {dados}. Escolha a pasta de clientes do escritório.")
    limite = (os.getenv("JARBAS_PASTA_CLIENTES_RAIZ", "") or "").strip()
    if limite:
        try:
            permitida = Path(limite).resolve()
        except OSError:
            raise PastaRecusada(
                f"JARBAS_PASTA_CLIENTES_RAIZ aponta para um caminho inexistente: {limite}") from None
        if raiz != permitida and permitida not in raiz.parents:
            raise PastaRecusada(
                f"Esta instalação só permite mapear pastas dentro de {permitida}.")
    return raiz


def varrer(raiz: Path) -> tuple[list[ArquivoLido], list[str]]:
    """Lista os arquivos aproveitáveis abaixo de `raiz`, com os limites em pé.

    Sem teto, apontar para a raiz do disco por engano travaria o servidor
    varrendo o sistema operacional inteiro — e o operador veria a tela
    pendurada, sem nenhuma explicação.
    """
    achados: list[ArquivoLido] = []
    avisos: list[str] = []
    raiz = Path(raiz)
    pastas = 0
    for atual, subpastas, arquivos in os.walk(raiz, followlinks=False):
        pasta = Path(atual)
        try:
            profundidade = len(pasta.relative_to(raiz).parts)
        except ValueError:
            continue
        if profundidade >= MAX_PROFUNDIDADE:
            subpastas[:] = []
        subpastas[:] = [d for d in sorted(subpastas)
                        if not d.startswith(".") and d.lower() not in ("__pycache__", "$recycle.bin")]
        pastas += 1
        if pastas > MAX_PASTAS:
            avisos.append(f"A varredura parou em {MAX_PASTAS} pastas. "
                          "Aponte para uma pasta mais específica ou importe por partes.")
            break
        for nome in sorted(arquivos):
            if arquivo_ignorado(nome):
                continue
            tipo = classificar(nome)
            if not tipo:
                continue
            caminho = pasta / nome
            try:
                if caminho.is_symlink() or not caminho.is_file():
                    continue
                tamanho = caminho.stat().st_size
            except OSError:
                continue
            relativo = caminho_relativo_seguro(str(caminho.relative_to(raiz)))
            if not relativo:
                continue
            achados.append(ArquivoLido(relativo=relativo, origem=caminho,
                                       tamanho=tamanho, tipo=tipo))
            if len(achados) >= MAX_ARQUIVOS:
                avisos.append(f"A varredura parou em {MAX_ARQUIVOS} arquivos. "
                              "Importe a pasta por partes para não deixar nada de fora.")
                return achados, avisos
    return achados, avisos


# ------------------------------------------------- serialização para a sessão

def para_json(propostas: list[Proposta]) -> list[dict[str, Any]]:
    return [
        {
            "pasta": p.pasta,
            "cliente_nome": p.cliente_nome,
            "documento": p.documento,
            "avisos": p.avisos,
            "processos": [
                {
                    "subpasta": c.subpasta,
                    "numero": c.numero,
                    "titulo": c.titulo,
                    "area": c.area,
                    "tribunal": c.tribunal,
                    "valor_causa": c.valor_causa,
                    "confianca": c.confianca,
                    "avisos": c.avisos,
                    "ia_usada": c.ia_usada,
                    "partes": c.partes,
                    "arquivos": [
                        {"relativo": a.relativo, "origem": str(a.origem) if a.origem else "",
                         "tamanho": a.tamanho, "tipo": a.tipo}
                        for a in c.arquivos
                    ],
                }
                for c in p.processos
            ],
        }
        for p in propostas
    ]
