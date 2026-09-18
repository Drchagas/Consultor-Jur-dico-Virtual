"""Movimentações processuais: do PDF dos autos e da API do CNJ.

Duas fontes, o mesmo destino (`case_movements`):

**1. Listagem de eventos do eproc.** O PDF baixado dos autos traz, logo nas
primeiras páginas, uma seção "Listagem dos Eventos do Processo" com evento,
data/hora, descrição e usuário. O intake extraía trinta campos e ignorava
justamente essa tabela — que é a linha do tempo pronta.

**2. API Pública do DataJud (CNJ).** Gratuita, cobre os tribunais do país e
entrega capa e movimentações segundo a Tabela Processual Unificada. Evita o
ciclo manual de entrar no tribunal, baixar PDF e subir no JARBAS.

Limite honesto do DataJud: ele traz METADADOS e movimentos, não o inteiro
teor. O PDF continua necessário para o Copiloto ler as peças. O DataJud serve
para *saber que algo mudou* e disparar o prazo.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime

API_BASE = "https://api-publica.datajud.cnj.jus.br"
TIMEOUT = 30.0

# O CNJ divulga a chave pública na wiki e pode trocá-la a qualquer momento;
# por isso ela fica em configuração, não no código.
ENV_CHAVE = "DATAJUD_API_KEY"


class DataJudError(RuntimeError):
    """Falha de consulta. Sempre propagada — nunca vira movimento falso."""


# --------------------------------------------------------------------------
# 1. Listagem de eventos do PDF do eproc
# --------------------------------------------------------------------------

# "22/07/2026 14:32" ou "22/07/2026 14:32:10"
RE_DATA_HORA = re.compile(r"(\d{2}/\d{2}/\d{4})(?:\s+(\d{2}:\d{2}(?::\d{2})?))?")

# Linha típica: "12  22/07/2026 14:32  Juntada de petição  FULANO"
RE_EVENTO = re.compile(
    r"^\s*(\d{1,4})\s+(\d{2}/\d{2}/\d{4})(?:\s+(\d{2}:\d{2}(?::\d{2})?))?\s+(.{4,})$")

# Movimentos que iniciam prazo. Deliberadamente conservador: marcar demais
# gera prazo fantasma, e prazo fantasma treina o advogado a ignorar o alerta.
PADROES_INTIMACAO = [
    r"\bintima[çc][ãa]o\b",
    r"\bintimad[oa]\b",
    r"\bpublica[çc][ãa]o\b.*\bdi[áa]rio\b",
    r"\bcita[çc][ãa]o\b",
    r"\bcitad[oa]\b",
    r"\bdecurso de prazo\b",
    r"\bvista\b.*\bpartes?\b",
    r"\bcarga\b.*\bautos\b",
]
RE_INTIMACAO = re.compile("|".join(PADROES_INTIMACAO), re.I)


@dataclass
class Movimento:
    evento: str
    data: date | None
    hora: str
    descricao: str
    autor: str = ""
    codigo: str = ""
    origem: str = "eproc"

    @property
    def intimacao(self) -> bool:
        return bool(RE_INTIMACAO.search(self.descricao or ""))


def _para_data(texto: str) -> date | None:
    try:
        d, m, a = texto.split("/")
        return date(int(a), int(m), int(d))
    except (ValueError, AttributeError):
        return None


def extrair_eventos_eproc(texto: str, limite: int = 400) -> list[Movimento]:
    """Lê a "Listagem dos Eventos do Processo" do texto do PDF.

    Tolerante ao layout: o PDF do eproc às vezes quebra a linha entre a data e
    a descrição, então quando a linha traz só número e data, a descrição é
    buscada na linha seguinte.
    """
    linhas = (texto or "").splitlines()
    saida: list[Movimento] = []
    i = 0
    while i < len(linhas) and len(saida) < limite:
        linha = linhas[i].strip()
        m = RE_EVENTO.match(linha)
        if m:
            evento, data_txt, hora, resto = m.groups()
            descricao = (resto or "").strip()
        else:
            # número + data isolados, descrição na linha de baixo
            m2 = re.match(r"^\s*(\d{1,4})\s+(\d{2}/\d{2}/\d{4})"
                          r"(?:\s+(\d{2}:\d{2}(?::\d{2})?))?\s*$", linha)
            if not m2:
                i += 1
                continue
            evento, data_txt, hora = m2.groups()
            descricao = linhas[i + 1].strip() if i + 1 < len(linhas) else ""
            i += 1

        if len(descricao) < 4:
            i += 1
            continue

        # o último token costuma ser o usuário do sistema, em caixa alta
        autor = ""
        partes = descricao.rsplit("  ", 1)
        if len(partes) == 2 and partes[1].strip().isupper() and len(partes[1].strip()) > 2:
            descricao, autor = partes[0].strip(), partes[1].strip()

        saida.append(Movimento(evento, _para_data(data_txt), hora or "",
                               descricao[:400], autor, origem="eproc"))
        i += 1

    # dedup por (evento, data, descrição)
    vistos, unicos = set(), []
    for mv in saida:
        chave = (mv.evento, mv.data, mv.descricao[:80])
        if chave not in vistos:
            vistos.add(chave)
            unicos.append(mv)
    unicos.sort(key=lambda x: (x.data or date.min, x.evento), reverse=True)
    return unicos


# --------------------------------------------------------------------------
# 2. API Pública do DataJud
# --------------------------------------------------------------------------

def chave() -> str:
    k = os.getenv(ENV_CHAVE, "").strip()
    if not k:
        raise DataJudError(
            f"{ENV_CHAVE} não configurada. A chave pública é divulgada pelo CNJ "
            "em datajud-wiki.cnj.jus.br/api-publica/acesso/ e pode ser trocada "
            "por eles a qualquer momento."
        )
    return k


def configurado() -> bool:
    return bool(os.getenv(ENV_CHAVE, "").strip())


def alias_do_numero(numero: str) -> str | None:
    """Deduz o alias do tribunal a partir do número CNJ.

    NNNNNNN-DD.AAAA.J.TR.OOOO — J é o segmento e TR o tribunal.
      J=8 justiça estadual, TR = código do estado (21 = RS)
      J=4 justiça federal,  TR = região do TRF
      J=5 justiça do trabalho, TR = região do TRT
    """
    m = re.fullmatch(r"(\d{7})-(\d{2})\.(\d{4})\.(\d)\.(\d{2})\.(\d{4})",
                     (numero or "").strip())
    if not m:
        return None
    j, tr = m.group(4), m.group(5)
    estaduais = {
        "01": "tjac", "02": "tjal", "03": "tjap", "04": "tjam", "05": "tjba",
        "06": "tjce", "07": "tjdft", "08": "tjes", "09": "tjgo", "10": "tjma",
        "11": "tjmt", "12": "tjms", "13": "tjmg", "14": "tjpa", "15": "tjpb",
        "16": "tjpr", "17": "tjpe", "18": "tjpi", "19": "tjrj", "20": "tjrn",
        "21": "tjrs", "22": "tjro", "23": "tjrr", "24": "tjsc", "25": "tjse",
        "26": "tjsp", "27": "tjto",
    }
    if j == "8":
        return f"api_publica_{estaduais[tr]}" if tr in estaduais else None
    if j == "4":
        return f"api_publica_trf{int(tr)}"
    if j == "5":
        return f"api_publica_trt{int(tr)}"
    if j == "3":
        return "api_publica_stj" if tr == "00" else None
    return None


def consultar(numero: str, alias: str | None = None) -> dict:
    """Consulta o processo pelo número. Levanta em qualquer falha."""
    try:
        import httpx
    except Exception as exc:
        raise DataJudError("pacote 'httpx' indisponível no runtime.") from exc

    alias = alias or alias_do_numero(numero)
    if not alias:
        raise DataJudError(
            f"não consegui deduzir o tribunal a partir de {numero}. "
            "Informe o alias manualmente (ex.: api_publica_tjrs)."
        )
    so_digitos = re.sub(r"\D", "", numero or "")
    try:
        r = httpx.post(
            f"{API_BASE}/{alias}/_search",
            headers={"Authorization": f"APIKey {chave()}",
                     "Content-Type": "application/json"},
            json={"query": {"match": {"numeroProcesso": so_digitos}}},
            timeout=TIMEOUT,
        )
    except Exception as exc:
        raise DataJudError(f"falha de rede ao consultar o DataJud: {exc}") from exc

    if r.status_code == 401:
        raise DataJudError("chave do DataJud recusada. Confira a chave vigente na wiki do CNJ.")
    if r.status_code >= 400:
        raise DataJudError(f"DataJud HTTP {r.status_code}: {r.text[:200]}")
    try:
        return r.json()
    except Exception as exc:
        raise DataJudError("resposta do DataJud não é JSON.") from exc


def movimentos_da_resposta(dados: dict) -> list[Movimento]:
    """Normaliza a resposta Elasticsearch do DataJud em Movimento."""
    hits = ((dados or {}).get("hits") or {}).get("hits") or []
    saida: list[Movimento] = []
    for h in hits:
        fonte = h.get("_source") or {}
        for mv in fonte.get("movimentos") or []:
            quando = str(mv.get("dataHora") or "")[:10]
            try:
                d = date.fromisoformat(quando) if quando else None
            except ValueError:
                d = None
            complementos = ", ".join(
                str(c.get("nome", "")) for c in (mv.get("complementosTabelados") or [])
                if c.get("nome"))
            nome = str(mv.get("nome") or "")
            saida.append(Movimento(
                evento="", data=d, hora=str(mv.get("dataHora") or "")[11:16],
                descricao=(f"{nome} — {complementos}" if complementos else nome)[:400],
                codigo=str(mv.get("codigo") or ""), origem="datajud"))
    saida.sort(key=lambda x: x.data or date.min, reverse=True)
    return saida


def capa_da_resposta(dados: dict) -> dict:
    hits = ((dados or {}).get("hits") or {}).get("hits") or []
    if not hits:
        return {}
    f = hits[0].get("_source") or {}
    def nome(chave_):
        v = f.get(chave_)
        return (v or {}).get("nome") if isinstance(v, dict) else v
    return {
        "numero": f.get("numeroProcesso"),
        "classe": nome("classe"),
        "orgao": nome("orgaoJulgador"),
        "tribunal": f.get("tribunal"),
        "grau": f.get("grau"),
        "ajuizamento": str(f.get("dataAjuizamento") or "")[:10],
        "assuntos": [a.get("nome") for a in (f.get("assuntos") or []) if a.get("nome")],
        "ultima_atualizacao": str(f.get("dataHoraUltimaAtualizacao") or "")[:10],
    }


# --------------------------------------------------------------------------
# Persistência
# --------------------------------------------------------------------------

def salvar(conn, org_id: int, case_id: int, movimentos: list[Movimento]) -> int:
    """Grava ignorando repetidos. O índice único decide, não uma consulta
    prévia — que teria corrida entre importações simultâneas."""
    agora = datetime.now().isoformat(timespec="seconds")
    novos = 0
    for mv in movimentos:
        try:
            conn.execute(
                """INSERT INTO case_movements
                   (organization_id,case_id,event_number,occurred_at,code,
                    description,actor,source,is_intimacao,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (org_id, case_id, mv.evento or None,
                 mv.data.isoformat() if mv.data else None, mv.codigo or None,
                 mv.descricao, mv.autor or None, mv.origem,
                 1 if mv.intimacao else 0, agora))
            novos += 1
        except Exception:
            continue   # violação do índice único = já existia
    return novos


def intimacoes_sem_prazo(conn, org_id: int, case_id: int | None = None) -> list[dict]:
    """Movimentos de intimação que ainda não geraram prazo. É a lista que o
    advogado precisa olhar: cada uma delas pode estar correndo agora."""
    onde = "organization_id=? AND is_intimacao=1 AND deadline_id IS NULL"
    args: list = [org_id]
    if case_id:
        onde += " AND case_id=?"
        args.append(case_id)
    return [dict(r) for r in conn.execute(
        f"""SELECT * FROM case_movements WHERE {onde}
            ORDER BY occurred_at DESC LIMIT 100""", tuple(args)).fetchall()]
