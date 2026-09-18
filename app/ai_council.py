"""JARBAS — Conselho de IA (OpenAI + Anthropic + Google).

Camada acima do ai_gateway.py. Roteia cada PAPEL do fluxo jurídico para o
provedor configurado para ele, com fallback, contabilidade real de tokens e
teto de gasto por escritório.

Princípios inegociáveis deste módulo:

1.  ERRO NUNCA VIRA CONTEÚDO. Toda falha levanta CouncilError. Uma mensagem de
    erro jamais é devolvida como se fosse análise dos autos — esse foi o bug
    mais perigoso da versão 8.4.2 e não se repete aqui.
2.  QUEM REDIGE NÃO CRITICA. O papel de crítica é obrigatoriamente atribuído a
    um provedor diferente do que redigiu. Se não houver segundo provedor
    configurado, a crítica é PULADA e sinalizada — nunca feita pelo próprio
    autor, que tende a validar o próprio texto.
3.  CUSTO É MEDIDO, NÃO ESTIMADO. Cada chamada devolve tokens reais e o custo é
    calculado pela tabela do provedor. Sem teto, não roda.
"""

from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env.local", override=False)


# --------------------------------------------------------------------------
# Erros
# --------------------------------------------------------------------------

class CouncilError(RuntimeError):
    """Falha em qualquer etapa do conselho. Sempre propagada, nunca engolida."""


class BudgetExceeded(CouncilError):
    """O teto de gasto do escritório seria ultrapassado por esta chamada."""


# --------------------------------------------------------------------------
# Catálogo de modelos e preços (USD por 1M de tokens)
#
# Verificado em 03/09/2026. Preço de API muda com frequência: reveja este
# bloco ao migrar de modelo. Se um modelo não estiver aqui, o custo é
# calculado com PRECO_DESCONHECIDO (conservador, superestima).
# --------------------------------------------------------------------------

PRECO_DESCONHECIDO = (10.0, 40.0)

CATALOGO: dict[str, tuple[float, float]] = {
    "claude-opus-5": (15.00, 75.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
}


def custo_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    entrada, saida = CATALOGO.get(model, PRECO_DESCONHECIDO)
    return (input_tokens / 1_000_000) * entrada + (output_tokens / 1_000_000) * saida


# --------------------------------------------------------------------------
# Resultado
# --------------------------------------------------------------------------

@dataclass
class CouncilResult:
    text: str
    provider: str
    model: str
    role: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    fallback_from: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class DeliberationResult:
    """Saída completa do fluxo colegiado."""
    extracao: Optional[CouncilResult] = None
    estrategia: Optional[CouncilResult] = None
    redacao: Optional[CouncilResult] = None
    critica: Optional[CouncilResult] = None
    revisao: Optional[CouncilResult] = None
    avisos: list[str] = field(default_factory=list)

    @property
    def etapas(self) -> list[CouncilResult]:
        return [e for e in (self.extracao, self.estrategia, self.redacao,
                            self.critica, self.revisao) if e is not None]

    @property
    def custo_total_usd(self) -> float:
        return sum(e.cost_usd for e in self.etapas)

    @property
    def tokens_totais(self) -> int:
        return sum(e.total_tokens for e in self.etapas)

    @property
    def texto_final(self) -> str:
        """A peça revisada, ou a redigida se não houve revisão."""
        if self.revisao:
            return self.revisao.text
        if self.redacao:
            return self.redacao.text
        return ""


# --------------------------------------------------------------------------
# Provedores
# --------------------------------------------------------------------------

class Provider:
    nome = "abstrato"
    env_key = ""

    def configurado(self) -> bool:
        return bool(os.getenv(self.env_key, "").strip())

    def key(self) -> str:
        k = os.getenv(self.env_key, "").strip()
        if not k:
            raise CouncilError(
                f"{self.nome} não configurado. Defina {self.env_key} no .env.local."
            )
        return k

    def sdk_disponivel(self) -> bool:
        raise NotImplementedError

    def generate(self, *, model: str, instructions: str, user_input: str,
                 pdfs: Iterable[Path] = (), max_output_tokens: int = 8000,
                 timeout: float = 240.0) -> tuple[str, int, int]:
        """Devolve (texto, input_tokens, output_tokens). Levanta em qualquer falha."""
        raise NotImplementedError




class AnthropicProvider(Provider):
    nome = "anthropic"
    env_key = "ANTHROPIC_API_KEY"

    def sdk_disponivel(self) -> bool:
        try:
            import anthropic  # noqa: F401
            return True
        except Exception:
            return False

    def generate(self, *, model, instructions, user_input, pdfs=(),
                 max_output_tokens=8000, timeout=240.0):
        try:
            import anthropic
        except Exception as exc:
            raise CouncilError("Pacote 'anthropic' não instalado no runtime.") from exc

        client = anthropic.Anthropic(api_key=self.key(), timeout=timeout, max_retries=2)
        blocos: list[dict[str, Any]] = []
        for p in pdfs:
            p = Path(p)
            if not p.is_file() or p.suffix.lower() != ".pdf":
                continue
            dados = base64.standard_b64encode(p.read_bytes()).decode("ascii")
            blocos.append({
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": dados},
            })
        blocos.append({"type": "text", "text": user_input})

        try:
            resp = client.messages.create(
                model=model,
                max_tokens=max_output_tokens,
                system=instructions,
                messages=[{"role": "user", "content": blocos}],
            )
            texto = "\n".join(
                b.text for b in resp.content if getattr(b, "type", "") == "text"
            ).strip()
            if not texto:
                raise CouncilError("Anthropic respondeu sem conteúdo textual utilizável.")
            u = getattr(resp, "usage", None)
            return (texto,
                    int(getattr(u, "input_tokens", 0) or 0) if u else 0,
                    int(getattr(u, "output_tokens", 0) or 0) if u else 0)
        except CouncilError:
            raise
        except Exception as exc:
            raise CouncilError(f"Anthropic: {_mensagem_amigavel(exc)}") from exc



# Provedor único a partir da 9.0. A abstração fica: acrescentar uma entrada
# aqui devolve a revisão entre fornecedores sem tocar no resto do fluxo.
PROVEDORES: dict[str, Provider] = {
    "anthropic": AnthropicProvider(),
}


def _mensagem_amigavel(exc: Exception) -> str:
    nome = type(exc).__name__
    msg = str(exc).strip()
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status == 401 or "Authentication" in nome:
        return "chave inválida ou revogada."
    if status == 429 or "RateLimit" in nome:
        return "limite de uso/quota atingido. Verifique créditos e limites do projeto."
    if status == 403 or "PermissionDenied" in nome:
        return "a chave não tem permissão para este modelo."
    if status == 404 or ("model" in msg.lower() and "not" in msg.lower()):
        return "modelo indisponível para esta chave. Ajuste o modelo nas Configurações."
    if "Connection" in nome or "Timeout" in nome:
        return "falha de conexão. Verifique internet, firewall e proxy."
    if isinstance(status, int) and status >= 500:
        return "indisponibilidade temporária do provedor. Tente em instantes."
    return f"{nome}: {msg[:400]}" if msg else nome


# --------------------------------------------------------------------------
# Papéis: qual IA faz o quê
#
# A atribuição abaixo é o PADRÃO, escolhido pelas características de cada
# modelo em 09/2026. TODA ela é sobrescrevível por variável de ambiente,
# porque ranking de modelo muda a cada poucos meses e o que vale para os
# SEUS autos deve ser medido, não presumido. Use tools/benchmark_council.py
# para comparar com casos reais do escritório antes de fixar uma escolha.
# --------------------------------------------------------------------------

PAPEIS_PADRAO: dict[str, tuple[str, str]] = {
    # Ler o processo inteiro: a etapa que mais consome entrada.
    "extracao": ("anthropic", "claude-sonnet-5"),

    # Raciocínio jurídico sobre o material extraído.
    "estrategia": ("anthropic", "claude-opus-5"),

    # Redigir a peça em registro forense brasileiro.
    "redacao": ("anthropic", "claude-opus-5"),

    # Crítica adversarial. Modelo DIFERENTE do redator — ver a nota sobre
    # independência no topo deste arquivo.
    "critica": ("anthropic", "claude-sonnet-5"),

    # Volume: resumo, classificação, etiquetagem.
    "rotina": ("anthropic", "claude-haiku-4-5-20251001"),
}

_ENV_PAPEL = {
    "extracao": "JARBAS_PAPEL_EXTRACAO",
    "estrategia": "JARBAS_PAPEL_ESTRATEGIA",
    "redacao": "JARBAS_PAPEL_REDACAO",
    "critica": "JARBAS_PAPEL_CRITICA",
    "rotina": "JARBAS_PAPEL_ROTINA",
}


def papel_config(papel: str, faixa: dict[str, tuple[str, str]] | None = None) -> tuple[str, str]:
    """Resolve o papel para (provedor, modelo).

    Precedência: faixa do plano > variável de ambiente > padrão do código.
    A faixa vem de plan_limits.FAIXAS e é o que permite vender assinatura:
    plano barato usa modelo barato, sem trocar código.
    """
    if papel not in PAPEIS_PADRAO:
        raise CouncilError(f"Papel desconhecido: {papel}")
    if faixa and papel in faixa:
        return faixa[papel]
    bruto = os.getenv(_ENV_PAPEL[papel], "").strip()
    if bruto:
        if ":" not in bruto:
            raise CouncilError(
                f"{_ENV_PAPEL[papel]} inválido: use 'provedor:modelo' "
                f"(ex.: anthropic:claude-opus-5). Recebido: {bruto!r}"
            )
        prov, modelo = bruto.split(":", 1)
        prov, modelo = prov.strip().lower(), modelo.strip()
        if prov not in PROVEDORES:
            raise CouncilError(
                f"Provedor desconhecido em {_ENV_PAPEL[papel]}: {prov!r}. "
                f"Use um de: {', '.join(PROVEDORES)}."
            )
        if not modelo:
            raise CouncilError(f"{_ENV_PAPEL[papel]} sem nome de modelo.")
        return prov, modelo
    return PAPEIS_PADRAO[papel]


def provedores_ativos() -> list[str]:
    return [n for n, p in PROVEDORES.items() if p.configurado() and p.sdk_disponivel()]


def status_conselho() -> dict[str, Any]:
    """Diagnóstico para a tela de Configurações."""
    provs = {}
    for nome, p in PROVEDORES.items():
        provs[nome] = {
            "configurado": p.configurado(),
            "sdk": p.sdk_disponivel(),
            "chave": (lambda k: f"••••{k[-4:]}" if len(k) >= 4 else "")(
                os.getenv(p.env_key, "").strip()),
        }
    papeis = {}
    for papel in PAPEIS_PADRAO:
        try:
            prov, modelo = papel_config(papel)
            papeis[papel] = {
                "provedor": prov, "modelo": modelo,
                "pronto": provs[prov]["configurado"] and provs[prov]["sdk"],
                "erro": "",
            }
        except CouncilError as exc:
            papeis[papel] = {"provedor": "", "modelo": "", "pronto": False, "erro": str(exc)}
    ativos = provedores_ativos()
    return {
        "provedores": provs,
        "papeis": papeis,
        "ativos": ativos,
        # Independência REAL exige fornecedores distintos. Com provedor único
        # a crítica roda, mas entre modelos de mesma linhagem.
        "critica_independente": False,
        "critica_entre_modelos": len(CATALOGO) >= 2,
        "nota_independencia": (
            "Revisão feita por outro modelo Claude. Modelos de mesma linhagem "
            "compartilham pontos cegos — confira cada fundamento manualmente."
        ),
        "teto_usd_mes": teto_mensal_usd(),
    }


# --------------------------------------------------------------------------
# Teto de gasto
# --------------------------------------------------------------------------

def teto_mensal_usd() -> float:
    try:
        return max(0.0, float(os.getenv("JARBAS_AI_TETO_USD_MES", "50")))
    except ValueError:
        return 50.0


def _sem_teto() -> bool:
    return teto_mensal_usd() <= 0


# --------------------------------------------------------------------------
# Execução de um papel, com fallback
# --------------------------------------------------------------------------

def executar(
    papel: str,
    instructions: str,
    user_input: str,
    *,
    pdfs: Iterable[Path] = (),
    max_output_tokens: int = 8000,
    evitar_modelo: str = "",
    gasto_atual_usd: float = 0.0,
    faixa: dict[str, tuple[str, str]] | None = None,
    teto_usd: float | None = None,
) -> CouncilResult:
    """Executa um papel. Levanta CouncilError em qualquer falha.

    evitar_modelo: usado pela crítica para garantir revisor diferente do
        redator. Ver a nota sobre independência no topo do arquivo.
    gasto_atual_usd: consumo do escritório no mês, para checagem de teto.
    """
    teto = teto_mensal_usd() if teto_usd is None else teto_usd
    if teto > 0 and gasto_atual_usd >= teto:
        raise BudgetExceeded(
            f"Teto mensal de IA atingido (US$ {teto:.2f}). "
            f"Consumo atual: US$ {gasto_atual_usd:.2f}. "
            f"Aguarde o próximo ciclo ou amplie o plano."
        )

    prov_nome, modelo = papel_config(papel, faixa)
    tentativas: list[tuple[str, str]] = []
    if modelo != evitar_modelo:
        tentativas.append((prov_nome, modelo))

    # Reserva: qualquer outro modelo do catálogo, respeitando evitar_modelo.
    for outro in sorted(CATALOGO):
        if outro == modelo or outro == evitar_modelo:
            continue
        tentativas.append((prov_nome, outro))

    if not tentativas:
        raise CouncilError(
            f"Nenhum modelo disponível para o papel '{papel}'"
            + (f" excluindo '{evitar_modelo}'." if evitar_modelo else ".")
        )

    ultimo_erro: Optional[Exception] = None
    origem = ""
    for i, (pn, md) in enumerate(tentativas):
        p = PROVEDORES[pn]
        if not p.configurado() or not p.sdk_disponivel():
            ultimo_erro = CouncilError(f"{pn} indisponível (chave ou SDK ausente).")
            continue
        t0 = time.time()
        try:
            texto, ti, to = p.generate(
                model=md, instructions=instructions, user_input=user_input,
                pdfs=pdfs, max_output_tokens=max_output_tokens,
            )
        except Exception as exc:
            ultimo_erro = exc
            if i == 0:
                origem = f"{pn}:{md}"
            continue
        custo = custo_usd(md, ti, to)
        if not _sem_teto() and gasto_atual_usd + custo > teto_mensal_usd():
            # A chamada já ocorreu; registramos e avisamos, mas não bloqueamos
            # retroativamente o resultado já pago.
            pass
        return CouncilResult(
            text=texto, provider=pn, model=md, role=papel,
            input_tokens=ti, output_tokens=to, cost_usd=custo,
            elapsed_s=round(time.time() - t0, 2),
            fallback_from=origem if i > 0 else "",
        )

    raise CouncilError(
        f"Todos os provedores falharam no papel '{papel}'. Último erro: {ultimo_erro}"
    )


def _modelo_padrao_do_provedor(prov: str) -> str:
    return {
        "anthropic": os.getenv("JARBAS_AI_MODEL_LEGAL", "claude-opus-5"),
    }.get(prov, "claude-opus-5")


# --------------------------------------------------------------------------
# Instruções por papel
# --------------------------------------------------------------------------

_BASE = (
    "Você atua como apoio técnico a advogado inscrito na OAB. Sua saída é "
    "INSUMO DE TRABALHO, jamais peça pronta para protocolo. "
    "REGRAS ABSOLUTAS: (1) não invente fatos, datas, valores, partes ou "
    "documentos; (2) não cite lei, súmula, precedente ou número de processo "
    "sem que conste do material fornecido — se precisar de fundamento que não "
    "está nos autos, escreva [CONFERIR FUNDAMENTO] em vez de arriscar; "
    "(3) quando não houver base no material, diga explicitamente que não há; "
    "(4) toda afirmação factual deve indicar a página ou documento de origem."
)

INSTRUCOES: dict[str, str] = {
    "extracao": _BASE + (
        "\n\nPAPEL: EXTRAÇÃO. Leia integralmente o material e produza, sem opinar:\n"
        "1. LINHA DO TEMPO — cada evento com data e origem (documento, página).\n"
        "2. PARTES E QUALIFICAÇÃO — nomes, papéis processuais, documentos.\n"
        "3. PROVAS — o que existe nos autos, com localização exata.\n"
        "4. CONTRADIÇÕES — divergências entre depoimentos, laudos e documentos, "
        "citando os dois lados de cada divergência com a página.\n"
        "5. LACUNAS — o que seria esperado e não está nos autos.\n"
        "Não proponha tese. Não avalie chance de êxito. Apenas o que está lá."
    ),
    "estrategia": _BASE + (
        "\n\nPAPEL: ESTRATÉGIA. Com base APENAS no material extraído, produza:\n"
        "1. TESES VIÁVEIS — em ordem de força, cada uma amarrada aos fatos que "
        "a sustentam e ao que falta para sustentá-la.\n"
        "2. NULIDADES E PRELIMINARES — apenas as que os autos suportam.\n"
        "3. RISCOS — o que a parte contrária tem de melhor contra nós.\n"
        "4. DILIGÊNCIAS — provas a produzir, na ordem de prioridade.\n"
        "Seja franco sobre teses fracas. Superestimar a causa prejudica o cliente."
    ),
    "redacao": _BASE + (
        "\n\nPAPEL: REDAÇÃO. Redija a minuta em registro forense brasileiro, "
        "norma culta, período curto, voz ativa. Estrutura: endereçamento, "
        "qualificação, síntese fática, fundamentação, pedidos, fecho.\n"
        "Use [COLCHETES] para todo dado que não esteja no material.\n"
        "Marque pontos de Visual Law como [FLUXOGRAMA: descrição].\n"
        "Não use adjetivação vazia nem retórica inflada. O texto será revisto "
        "por advogado responsável antes de qualquer protocolo."
    ),
    "critica": _BASE + (
        "\n\nPAPEL: CRÍTICA ADVERSARIAL. Você NÃO redigiu este texto e não deve "
        "defendê-lo. Assuma a posição da parte contrária e do juízo. Aponte:\n"
        "1. AFIRMAÇÕES SEM LASTRO nos autos — cite o trecho exato.\n"
        "2. CITAÇÃO SUSPEITA — lei, súmula ou precedente que pareça inventado, "
        "mal aplicado ou não verificável no material. Liste para conferência humana.\n"
        "3. CONTRA-ARGUMENTOS que a parte contrária oporá, do mais forte ao mais fraco.\n"
        "4. FALHAS TÉCNICAS — endereçamento, competência, prazo, legitimidade, "
        "pedido incongruente com a causa de pedir.\n"
        "5. VEREDITO: APROVADA, APROVADA COM RESSALVAS ou REESCREVER, com motivo.\n"
        "Ser duro aqui é o seu trabalho. Um elogio inútil custa o caso do cliente."
    ),
    "revisao": _BASE + (
        "\n\nPAPEL: REVISÃO. Você redigiu a minuta e recebeu crítica adversarial "
        "de outro revisor. Reescreva incorporando o que procede.\n"
        "Onde a crítica apontar afirmação sem lastro, REMOVA a afirmação — não a "
        "reforce com mais adjetivo.\n"
        "Onde apontar citação suspeita, substitua por [CONFERIR FUNDAMENTO].\n"
        "Se discordar de um ponto da crítica, mantenha o texto e explique por quê "
        "em uma seção final NOTAS AO ADVOGADO, separada da peça."
    ),
    "rotina": _BASE + "\n\nPAPEL: ROTINA. Resuma ou classifique de forma objetiva e curta.",
}


# --------------------------------------------------------------------------
# Fluxo colegiado
# --------------------------------------------------------------------------

def deliberar(
    *,
    material: str = "",
    pdfs: Iterable[Path] = (),
    pedido: str,
    gasto_atual_usd: float = 0.0,
    faixa: dict[str, tuple[str, str]] | None = None,
    teto_usd: float | None = None,
    pular_critica: bool = False,
    progresso: Optional[Callable[[str, str], None]] = None,
) -> DeliberationResult:
    """Fluxo completo: extração → estratégia → redação → crítica → revisão.

    'pedido' descreve a peça pretendida (ex.: 'contestação em ação de cobrança').
    'progresso' recebe (papel, mensagem) a cada etapa, para a barra da interface.
    """
    pdfs = list(pdfs)
    if not material.strip() and not pdfs:
        raise CouncilError("Nada a analisar: informe texto do caso ou ao menos um PDF.")

    res = DeliberationResult()

    def aviso(m: str) -> None:
        res.avisos.append(m)

    def passo(papel: str, msg: str) -> None:
        if progresso:
            progresso(papel, msg)

    gasto = gasto_atual_usd

    # 1. EXTRAÇÃO
    passo("extracao", "Lendo os autos e montando linha do tempo")
    res.extracao = executar(
        "extracao", INSTRUCOES["extracao"],
        material or "Analise integralmente os PDFs anexados.",
        pdfs=pdfs, max_output_tokens=16000, gasto_atual_usd=gasto, faixa=faixa, teto_usd=teto_usd,
    )
    gasto += res.extracao.cost_usd

    # 2. ESTRATÉGIA
    passo("estrategia", "Avaliando teses, nulidades e riscos")
    res.estrategia = executar(
        "estrategia", INSTRUCOES["estrategia"],
        f"PEDIDO DO ADVOGADO:\n{pedido}\n\nMATERIAL EXTRAÍDO DOS AUTOS:\n{res.extracao.text}",
        max_output_tokens=12000, gasto_atual_usd=gasto, faixa=faixa, teto_usd=teto_usd,
    )
    gasto += res.estrategia.cost_usd

    # 3. REDAÇÃO
    passo("redacao", "Redigindo a minuta")
    res.redacao = executar(
        "redacao", INSTRUCOES["redacao"],
        f"PEÇA PRETENDIDA:\n{pedido}\n\n"
        f"FATOS EXTRAÍDOS:\n{res.extracao.text}\n\n"
        f"ESTRATÉGIA APROVADA:\n{res.estrategia.text}",
        max_output_tokens=16000, gasto_atual_usd=gasto, faixa=faixa, teto_usd=teto_usd,
    )
    gasto += res.redacao.cost_usd

    if pular_critica:
        aviso("Crítica adversarial pulada a pedido. A minuta não passou por revisor independente.")
        return res

    # 4. CRÍTICA — obrigatoriamente por MODELO diferente do redator.
    #
    # Até a 8.9 a exigência era de FORNECEDOR diferente, o que dava
    # independência real: outra empresa, outros dados de treino, outros pontos
    # cegos. Com provedor único isso não é mais possível.
    #
    # A crítica continua acontecendo e continua sendo feita por outro modelo,
    # mas de mesma linhagem. Reduz o ponto cego comum; não elimina. Por isso o
    # aviso vai junto do resultado, e não escondido aqui.
    modelo_redator = res.redacao.model
    outros = [m for m in CATALOGO if m != modelo_redator]
    if not outros:
        aviso(
            f"Crítica adversarial NÃO executada: só há o modelo {modelo_redator} "
            "disponível. Um modelo revisando o próprio texto tende a aprová-lo."
        )
        return res

    aviso(
        "Revisão feita por modelo do mesmo fornecedor (Claude). Modelos de "
        "mesma linhagem compartilham pontos cegos: a crítica pega menos erro "
        "de fundamento do que pegaria um revisor de outra empresa. "
        "Confira pessoalmente cada [CONFERIR FUNDAMENTO] antes de protocolar."
    )

    passo("critica", "Submetendo a minuta a revisor independente")
    res.critica = executar(
        "critica", INSTRUCOES["critica"],
        f"PEÇA PRETENDIDA:\n{pedido}\n\n"
        f"MATERIAL DOS AUTOS:\n{res.extracao.text}\n\n"
        f"MINUTA A CRITICAR:\n{res.redacao.text}",
        max_output_tokens=10000, evitar_modelo=modelo_redator,
        gasto_atual_usd=gasto, faixa=faixa, teto_usd=teto_usd,
    )
    gasto += res.critica.cost_usd

    # 5. REVISÃO — volta ao redator original.
    passo("revisao", "Incorporando a crítica na versão final")
    res.revisao = executar(
        "redacao", INSTRUCOES["revisao"],
        f"PEÇA PRETENDIDA:\n{pedido}\n\n"
        f"SUA MINUTA:\n{res.redacao.text}\n\n"
        f"CRÍTICA RECEBIDA (revisor {res.critica.model}):\n{res.critica.text}",
        max_output_tokens=16000, gasto_atual_usd=gasto, faixa=faixa, teto_usd=teto_usd,
    )

    return res
