"""Atendimento JARBAS — chatbot do CHAGAS – ADVOGADOS.

O que este módulo NÃO é: mais um chat com a IA. Isso já existe em
`/ai` (Central IA), voltado ao advogado, com os dados do escritório
dentro do contexto.

Este é o outro lado do balcão: a primeira conversa com quem procura o
escritório. Triagem, classificação de risco, coleta do mínimo
necessário, proposta de horário dentro da agenda real e encaminhamento
ao Dr. Sandro. As diretrizes JARBAS V3.5 que governam essa conversa
estão nas seções 14 (atendimento), 15 (agenda), 13 (honorários),
3 (risco) e 17 (LGPD) — e cada regra abaixo aponta a sua.

Três decisões de projeto que explicam o formato do código:

1. ZERO INVENÇÃO vale mais aqui do que em qualquer outra tela. Uma peça
   inventada é revista pelo advogado antes do protocolo; uma resposta
   inventada ao cliente JÁ FOI ENVIADA. Por isso o prompt proíbe
   prognóstico, valor de indenização e citação de precedente, e
   `revisar_saida` relê o texto produzido atrás dessas três coisas antes
   de ele chegar à tela.

2. SEM CHAVE DE IA O CHATBOT CONTINUA ATENDENDO. `roteiro` é um
   atendimento escrito, determinístico, sem IA nenhuma — triagem por
   área, perguntas certas, horário livre e honorário da consulta. Não é
   degradação: é o piso. A IA melhora o texto, não sustenta o serviço.

3. ERRO DE IA NUNCA VIRA CONTEÚDO. Falhou a chamada, levanta
   `ChatbotError`. Quem chama decide o que dizer ao operador. O que não
   acontece é um pedido de desculpas do modelo ser gravado como se fosse
   resposta do escritório ao cliente.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Iterable, Sequence

from . import prazos
from .ai_gateway import ask as gateway_ask, configured as gateway_configured


class ChatbotError(RuntimeError):
    """Falha ao produzir a resposta. Nunca é gravada como atendimento."""


# --------------------------------------------------------------------------
# Identidade institucional — diretriz JARBAS §11.
# Fonte única: o rodapé, o endereço e a OAB saem daqui para o prompt, para a
# tela e para o texto sugerido ao cliente. Escrito à mão em três lugares,
# um deles fica desatualizado.
# --------------------------------------------------------------------------

ESCRITORIO = {
    "nome": "CHAGAS – ADVOGADOS",
    "advogado": "Dr. Sandro D. Chagas",
    "oab": "OAB/RS 105.040",
    "endereco": "Rua Jacob Adami, nº 55, Bairro Suíça, Canela/RS, CEP 95684-196",
    "telefone": "(54) 99110-1959",
    "email": "schagasadvocacia@gmail.com",
}

RODAPE_INSTITUCIONAL = (
    f"{ESCRITORIO['nome']}\n"
    f"{ESCRITORIO['endereco']}\n"
    f"Telefone/Whats: {ESCRITORIO['telefone']}\n"
    f"E-mail: {ESCRITORIO['email']}"
)

# Diretriz §13. Valor operacional recorrente, abatível dos honorários em caso
# de contratação. Não é "a partir de": é o valor.
VALOR_CONSULTA = 250.0

# Diretriz §15. Consulta só em dia útil, 08h–17h, 60 minutos. Como a consulta
# dura uma hora e não pode passar das 17h, o último início possível é 16h.
JANELA_INICIO = 8
JANELA_FIM = 17
DURACAO_CONSULTA_MIN = 60
HORARIOS_PADRAO = (time(9, 0), time(10, 0), time(14, 0), time(15, 0), time(16, 0))

MODALIDADES = (
    "presencial no escritório, em Canela/RS",
    "presencial na residência do cliente",
    "virtual por Google Meet",
)

AVISO_REVISAO_HUMANA = (
    "Minuta de atendimento. Revise antes de enviar ao cliente: nenhuma "
    "resposta deste chatbot vale como orientação jurídica definitiva."
)

AVISO_LGPD = (
    "Colete apenas o que a triagem exige. Documento, renda e dados de saúde "
    "só quando indispensáveis ao caso (diretriz §17)."
)


# --------------------------------------------------------------------------
# Áreas de atuação.
#
# `termos` decide o encaminhamento; `perguntas` é o que falta saber para o
# advogado abrir o caso sem uma segunda rodada de mensagens; `documentos` é
# o que o cliente já pode separar antes da consulta. Cada área corresponde a
# uma skill JARBAS existente — o nome em `skill` é o que o advogado deve
# acionar na sequência, e mantém os dois lados alinhados.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Area:
    codigo: str
    nome: str
    skill: str
    termos: tuple[str, ...]
    perguntas: tuple[str, ...]
    documentos: tuple[str, ...]
    risco_minimo: str = "MÉDIO"


AREAS: tuple[Area, ...] = (
    Area(
        "criminal", "Criminal e execução penal", "jarbas-criminal",
        ("preso", "prisao", "prisao em flagrante", "flagrante", "delegacia", "boletim de ocorrencia",
         "inquerito", "policia", "audiencia de custodia", "habeas corpus", "denuncia",
         "crime", "furto", "roubo", "trafico", "homicidio", "lesao corporal", "ameaca",
         "maria da penha", "medida protetiva", "regime semiaberto", "progressao de regime",
         "execucao penal", "mandado de prisao", "fianca", "reu", "acusado", "audiencia de instrucao"),
        ("A pessoa está presa neste momento? Onde e desde quando?",
         "Já houve audiência de custódia ou alguma audiência marcada?",
         "Existe advogado constituído ou é a Defensoria que acompanha?",
         "Há número de inquérito, boletim de ocorrência ou processo?"),
        ("Boletim de ocorrência ou auto de prisão em flagrante",
         "Documento de identidade da pessoa investigada ou acusada",
         "Qualquer intimação ou mandado recebido"),
        risco_minimo="ALTO",
    ),
    Area(
        "familia", "Família e sucessões", "jarbas-familia-sucessoes",
        ("divorcio", "separacao", "guarda", "pensao", "alimentos", "visita", "convivencia",
         "uniao estavel", "partilha", "inventario", "heranca", "espolio", "arrolamento",
         "testamento", "paternidade", "reconhecimento de filho", "alienacao parental",
         "curatela", "tutela de menor", "adocao"),
        ("Qual é a relação entre as partes e há quanto tempo a situação se arrasta?",
         "Há filhos menores ou pessoa dependente envolvida?",
         "Existem bens a partilhar e qual a ordem de grandeza deles?",
         "Já existe processo em andamento ou acordo assinado?"),
        ("Certidão de casamento ou de nascimento dos filhos",
         "Comprovantes de renda das partes, quando o pedido envolver alimentos",
         "Documentos dos bens (matrícula, CRLV, extratos)"),
    ),
    Area(
        "trabalhista", "Trabalhista", "jarbas-trabalhista",
        ("demitido", "demissao", "rescisao", "verbas rescisorias", "carteira assinada", "ctps",
         "hora extra", "horas extras", "adicional noturno", "insalubridade", "periculosidade",
         "justa causa", "assedio moral", "assedio", "vinculo de emprego", "fgts", "aviso previo",
         "reclamatoria", "empregado", "patrao", "empregador", "acidente de trabalho"),
        ("Qual foi o período trabalhado e qual a função exercida?",
         "Havia registro em carteira? Qual o último salário?",
         "Como terminou o contrato: pedido de demissão, dispensa, justa causa ou abandono?",
         "Há testemunhas, mensagens, holerites ou controle de ponto guardados?"),
        ("CTPS ou contrato de trabalho",
         "Holerites e termo de rescisão",
         "Mensagens, e-mails e escalas que comprovem a jornada"),
    ),
    Area(
        "consumidor", "Consumidor", "jarbas-consumidor",
        ("consumidor", "procon", "produto com defeito", "vicio do produto", "garantia",
         "cobranca indevida", "negativado", "negativacao", "spc", "serasa", "nome sujo",
         "compra", "loja", "assinatura", "cancelamento", "plano de saude", "companhia aerea",
         "voo cancelado", "operadora", "internet", "telefonia", "propaganda enganosa"),
        ("O que foi contratado ou comprado, quando e por qual valor?",
         "Qual foi o problema e o que a empresa respondeu até agora?",
         "Já houve reclamação formal (SAC, PROCON, plataforma)? Tem o número de protocolo?",
         "Houve cobrança, negativação ou prejuízo já concretizado?"),
        ("Nota fiscal, contrato ou comprovante da compra",
         "Protocolos de atendimento e conversas com a empresa",
         "Extrato ou print da cobrança e da negativação"),
    ),
    Area(
        "bancario", "Bancário e superendividamento", "jarbas-bancario",
        ("banco", "financiamento", "emprestimo", "consignado", "juros", "revisional",
         "busca e apreensao", "veiculo apreendido", "alienacao fiduciaria", "cartao de credito",
         "desconto indevido", "superendividamento", "divida", "tarifa", "seguro embutido",
         "leilao do imovel", "alienacao do imovel"),
        ("Qual é o contrato: financiamento, consignado, cartão ou empréstimo pessoal?",
         "Qual o valor contratado, quantas parcelas e quantas já foram pagas?",
         "Existe ação judicial em curso, apreensão de bem ou leilão marcado?",
         "Os descontos foram autorizados ou apareceram sem contratação?"),
        ("Contrato e extrato completo das parcelas",
         "Extrato do benefício ou da conta com os descontos",
         "Qualquer citação, intimação ou notificação do banco"),
    ),
    Area(
        "previdenciario", "Previdenciário", "jarbas-civel",
        ("inss", "aposentadoria", "beneficio", "auxilio doenca", "auxilio-doenca", "bpc", "loas",
         "pericia medica", "pensao por morte", "salario maternidade", "revisao de beneficio",
         "tempo de contribuicao", "meu inss"),
        ("Qual benefício está em discussão e em que fase ele está?",
         "Houve indeferimento? Qual a data e o motivo informado pelo INSS?",
         "Há laudos médicos ou perícia já realizada?",
         "Qual o histórico de contribuições (CNIS) disponível?"),
        ("Carta de indeferimento ou comunicado do INSS",
         "Extrato CNIS e senha do Meu INSS, quando houver",
         "Laudos, exames e receituários"),
    ),
    Area(
        "tributario", "Tributário e fiscal", "jarbas-tributario-administrativo",
        ("imposto", "tributo", "execucao fiscal", "iptu", "itbi", "iss", "icms", "multa fiscal",
         "auto de infracao fiscal", "receita federal", "prefeitura cobranca", "certidao negativa",
         "parcelamento de debito", "refis"),
        ("Qual tributo e qual o ente que está cobrando (município, estado, União)?",
         "Há auto de infração, execução fiscal ou apenas cobrança administrativa?",
         "Qual o valor e a data da ciência da cobrança?",
         "Há bem penhorado ou bloqueio já efetivado?"),
        ("Auto de infração ou certidão de dívida ativa",
         "Guias, carnês e comprovantes de pagamento",
         "Citação ou intimação recebida"),
    ),
    Area(
        "empresarial", "Empresarial e recuperacional", "jarbas-empresarial-recuperacional",
        ("empresa", "socio", "sociedade", "contrato social", "dissolucao", "apuracao de haveres",
         "recuperacao judicial", "falencia", "habilitacao de credito", "cnpj", "franquia",
         "distrato societario", "marca"),
        ("Qual o tipo societário e qual a participação de cada sócio?",
         "O conflito é entre sócios, com terceiros ou com credores?",
         "A empresa está em atividade? Qual a situação das dívidas?",
         "Existe contrato social atualizado e acordo de sócios?"),
        ("Contrato social e alterações",
         "Balanços e demonstrativos recentes",
         "Contratos e notificações relevantes"),
    ),
    Area(
        "ambiental", "Ambiental e urbanístico", "jarbas-ambiental-urbanistico",
        ("ambiental", "ibama", "fepam", "licenciamento", "auto de infracao ambiental", "embargo",
         "demolicao", "area de preservacao", "app", "desmatamento", "alvara", "obra embargada",
         "termo de ajustamento de conduta", "tac", "ministerio publico ambiental", "acao civil publica"),
        ("Qual órgão lavrou o auto ou o embargo e em que data?",
         "A obra ou atividade tem licença/alvará? Em que situação?",
         "Há prazo de defesa em curso? Qual a data da ciência?",
         "Existe inquérito civil ou ação civil pública instaurada?"),
        ("Auto de infração, embargo ou notificação",
         "Licenças, alvarás e projetos aprovados",
         "Matrícula do imóvel e fotos da área"),
    ),
    Area(
        "administrativo", "Administrativo", "jarbas-administrativo",
        ("servidor publico", "concurso", "sindicancia", "processo administrativo disciplinar",
         "pad", "licitacao", "contrato administrativo", "improbidade", "prefeitura", "orgao publico",
         "exoneracao", "estagio probatorio"),
        ("Qual órgão e qual o ato administrativo em discussão?",
         "Houve processo administrativo com direito de defesa?",
         "Qual a data da publicação ou da ciência do ato?",
         "Há prazo administrativo ou judicial já em curso?"),
        ("Publicação do ato ou portaria",
         "Cópia do processo administrativo",
         "Documentos funcionais, quando servidor"),
    ),
    Area(
        "civel", "Cível", "jarbas-civel",
        ("indenizacao", "dano moral", "dano material", "acidente", "batida", "colisao",
         "contrato", "cobranca", "aluguel", "locacao", "despejo", "condominio", "vizinho",
         "usucapiao", "posse", "imovel", "obra", "reforma", "protesto", "responsabilidade civil"),
        ("O que aconteceu, quando e quem são as partes envolvidas?",
         "Qual o prejuízo concreto e como ele pode ser comprovado?",
         "Houve tentativa de acordo ou notificação anterior?",
         "Já existe processo, protesto ou negativação?"),
        ("Contrato, recibos e comprovantes de pagamento",
         "Fotos, laudos, orçamentos e boletim de ocorrência quando houver",
         "Mensagens trocadas com a outra parte"),
    ),
)

AREA_PADRAO = AREAS[-1]  # Cível: quando não há termo suficiente para decidir.
AREAS_POR_CODIGO = {a.codigo: a for a in AREAS}


# --------------------------------------------------------------------------
# Risco — diretriz §3. A classificação aqui não escolhe a tese: escolhe a
# velocidade. CRÍTICO significa "não deixe para amanhã".
# --------------------------------------------------------------------------

RISCOS = ("BAIXO", "MÉDIO", "ALTO", "CRÍTICO")

GATILHOS_CRITICOS = {
    "preso": "pessoa privada de liberdade",
    "prisao": "prisão em curso ou iminente",
    "flagrante": "prisão em flagrante",
    "audiencia de custodia": "audiência de custódia",
    "mandado de prisao": "mandado de prisão",
    "habeas corpus": "habeas corpus",
    "medida protetiva": "medida protetiva de urgência",
    "violencia domestica": "violência doméstica",
    "ameaca de morte": "ameaça à integridade física",
    "internacao": "internação compulsória ou urgência de saúde",
    "vence hoje": "prazo vencendo hoje",
    "vence amanha": "prazo vencendo amanhã",
    "ultimo dia": "último dia de prazo",
    "audiencia amanha": "audiência no dia seguinte",
    "audiencia hoje": "audiência no mesmo dia",
    "leilao": "leilão designado",
    "despejo": "ordem de desocupação",
    "reintegracao de posse": "ordem de reintegração de posse",
}

GATILHOS_ALTOS = {
    "prazo": "prazo processual em curso",
    "intimacao": "intimação recebida",
    "citacao": "citação recebida",
    "liminar": "pedido ou decisão liminar",
    "tutela de urgencia": "tutela de urgência",
    "bloqueio": "bloqueio de valores",
    "penhora": "penhora",
    "busca e apreensao": "busca e apreensão",
    "audiencia": "audiência designada",
    "recurso": "prazo recursal",
    "sentenca": "sentença publicada",
    "oficial de justica": "diligência de oficial de justiça",
    "urgente": "urgência declarada pelo cliente",
    "hoje": "referência a prazo no mesmo dia",
    "amanha": "referência a prazo no dia seguinte",
    "bloqueada": "constrição sobre conta",
    "execucao": "processo de execução",
}


# --------------------------------------------------------------------------
# Fluxos de atendimento — diretriz §14.
# --------------------------------------------------------------------------

FLUXOS = ("#INICIO", "#CLIENTE_EXISTENTE", "#NOVO_CLIENTE", "FOLLOW-UP")

TERMOS_CLIENTE_EXISTENTE = (
    "meu processo", "meu caso", "ja sou cliente", "sou cliente", "o doutor ja",
    "andamento", "como esta o processo", "novidade do processo", "audiencia do meu",
    "protocolou", "ja contratei", "processo numero", "numero do processo",
)

SAUDACOES = ("bom dia", "boa tarde", "boa noite", "ola", "oi", "tudo bem", "prezado", "prezada")


# --------------------------------------------------------------------------
# Dados sensíveis — diretriz §17 (minimização).
#
# Mascarar serve à trilha de auditoria e ao resumo: o log registra que houve
# CPF na conversa sem repetir o CPF. A transcrição do atendimento, essa sim,
# fica íntegra — o escritório precisa dela para abrir o caso.
# --------------------------------------------------------------------------

_CPF = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
_CNPJ = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")
_TELEFONE = re.compile(r"\b\(?\d{2}\)?\s?9?\d{4}[-\s]?\d{4}\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_CARTAO = re.compile(r"\b\d{4}[\s.-]?\d{4}[\s.-]?\d{4}[\s.-]?\d{4}\b")


def mascarar_sensiveis(texto: str) -> str:
    """Troca identificadores diretos por marcas. Ordem importa.

    Cartão antes de telefone e CNPJ antes de CPF: os padrões se
    sobrepõem, e o mais longo precisa casar primeiro, senão o telefone
    come metade do cartão e sobra lixo reconhecível na trilha.
    """
    t = texto or ""
    t = _CARTAO.sub("[cartão]", t)
    t = _CNPJ.sub("[CNPJ]", t)
    t = _CPF.sub("[CPF]", t)
    t = _EMAIL.sub("[e-mail]", t)
    t = _TELEFONE.sub("[telefone]", t)
    return t


def normalizar(texto: str) -> str:
    t = unicodedata.normalize("NFKD", texto or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t.lower()).strip()


# --------------------------------------------------------------------------
# Classificação
# --------------------------------------------------------------------------

@dataclass
class Classificacao:
    area: Area
    risco: str
    fluxo: str
    motivos: tuple[str, ...] = ()
    processo: str = ""
    urgente: bool = False

    @property
    def prioritario(self) -> bool:
        return self.risco in ("ALTO", "CRÍTICO")


NUMERO_CNJ = re.compile(r"\b\d{7}-?\d{2}\.?\d{4}\.?\d\.?\d{2}\.?\d{4}\b")


def _pontuar_area(texto_norm: str) -> list[tuple[int, Area]]:
    placar: list[tuple[int, Area]] = []
    for area in AREAS:
        pontos = 0
        for termo in area.termos:
            if re.search(rf"(?<![a-z0-9]){re.escape(termo)}(?![a-z0-9])", texto_norm):
                # Termo composto vale mais: "busca e apreensao" é mais
                # informativo do que "banco" e não pode perder para ele.
                pontos += 2 if " " in termo else 1
        if pontos:
            placar.append((pontos, area))
    placar.sort(key=lambda p: (-p[0], AREAS.index(p[1])))
    return placar


def area_com_confianca(texto: str) -> tuple[Area, bool]:
    """(área, foi_mesmo_identificada).

    O booleano existe porque "cível" é o destino tanto de um caso cível
    quanto de um relato que não casou com termo nenhum. Tratar os dois
    como iguais faz um "bom dia" herdar o risco mínimo da área cível e
    aparecer na triagem com um motivo que ninguém escreveu.
    """
    placar = _pontuar_area(normalizar(texto))
    return (placar[0][1], True) if placar else (AREA_PADRAO, False)


def detectar_area(texto: str) -> Area:
    return area_com_confianca(texto)[0]


def classificar_risco(texto: str, area: Area | None = None) -> tuple[str, tuple[str, ...]]:
    """Devolve (nível, motivos). Motivo é obrigatório: risco sem motivo
    declarado não é classificação, é palpite — e não dá para auditar."""
    t = normalizar(texto)
    motivos: list[str] = []
    nivel = "BAIXO"

    for termo, motivo in GATILHOS_CRITICOS.items():
        if termo in t:
            nivel = "CRÍTICO"
            motivos.append(motivo)
    if nivel != "CRÍTICO":
        for termo, motivo in GATILHOS_ALTOS.items():
            if re.search(rf"(?<![a-z0-9]){re.escape(termo)}(?![a-z0-9])", t):
                nivel = "ALTO"
                motivos.append(motivo)

    if area and nivel == "BAIXO" and RISCOS.index(area.risco_minimo) > 0:
        nivel = area.risco_minimo
        motivos.append(f"matéria de {area.nome.lower()} tratada como {area.risco_minimo.lower()} por padrão")

    if nivel == "BAIXO" and len(t) > 40:
        # Relato existe e não acendeu gatilho: ainda não é "baixo risco",
        # é "risco não identificado na triagem". Médio é o piso honesto.
        nivel = "MÉDIO"
        motivos.append("relato sem gatilho de urgência identificado na triagem")

    # Ordem estável e sem repetição, para o mesmo relato classificar igual
    # duas vezes seguidas.
    vistos: list[str] = []
    for m in motivos:
        if m not in vistos:
            vistos.append(m)
    return nivel, tuple(vistos)


def detectar_fluxo(texto: str, *, historico: Sequence[dict] | None = None,
                   cliente_conhecido: bool = False) -> str:
    t = normalizar(texto)
    if cliente_conhecido or NUMERO_CNJ.search(texto or ""):
        return "#CLIENTE_EXISTENTE"
    if any(termo in t for termo in TERMOS_CLIENTE_EXISTENTE):
        return "#CLIENTE_EXISTENTE"
    if historico:
        # Conversa já aberta e cliente calado: quem retoma é o escritório.
        if not (texto or "").strip():
            return "FOLLOW-UP"
        return "#NOVO_CLIENTE"
    if len(t) <= 25 and any(s in t for s in SAUDACOES):
        return "#INICIO"
    return "#NOVO_CLIENTE"


def classificar(texto: str, *, historico: Sequence[dict] | None = None,
                cliente_conhecido: bool = False) -> Classificacao:
    area, identificada = area_com_confianca(texto)
    risco, motivos = classificar_risco(texto, area if identificada else None)
    fluxo = detectar_fluxo(texto, historico=historico, cliente_conhecido=cliente_conhecido)
    numero = NUMERO_CNJ.search(texto or "")
    t = normalizar(texto)
    return Classificacao(
        area=area, risco=risco, fluxo=fluxo, motivos=motivos,
        processo=numero.group(0) if numero else "",
        urgente=risco in ("ALTO", "CRÍTICO") or "urgente" in t,
    )


# --------------------------------------------------------------------------
# Agenda — diretriz §15.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Horario:
    inicio: datetime

    @property
    def fim(self) -> datetime:
        return self.inicio + timedelta(minutes=DURACAO_CONSULTA_MIN)

    def __str__(self) -> str:
        dias = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
                "sexta-feira", "sábado", "domingo")
        return (f"{dias[self.inicio.weekday()]}, {self.inicio:%d/%m} às "
                f"{self.inicio:%Hh%M}".replace("h00", "h"))


def horario_valido(momento: datetime) -> bool:
    """Regra literal da diretriz §15: dia útil, 08h–17h, e a consulta de 60
    minutos tem de CABER antes das 17h — por isso o fim também é checado."""
    if not prazos.e_dia_util(momento.date()):
        return False
    if momento.hour < JANELA_INICIO:
        return False
    fim = momento + timedelta(minutes=DURACAO_CONSULTA_MIN)
    if fim.date() != momento.date():
        return False
    return (fim.hour, fim.minute) <= (JANELA_FIM, 0)


def horarios_disponiveis(a_partir: datetime | None = None, *, quantidade: int = 3,
                         ocupados: Iterable[datetime] = ()) -> list[Horario]:
    """Propõe horários concretos em vez de perguntar "qual prefere?".

    A diretriz §15 é explícita: quando a agenda pode oferecer alternativas,
    devolver alternativas. Perguntar joga o trabalho de volta ao cliente e
    custa mais uma rodada de mensagens.
    """
    agora = a_partir or datetime.now()
    tomados = {d.replace(second=0, microsecond=0) for d in ocupados}
    achados: list[Horario] = []
    dia = agora.date()
    for _ in range(60):
        if prazos.e_dia_util(dia):
            for h in HORARIOS_PADRAO:
                candidato = datetime.combine(dia, h)
                if candidato <= agora or candidato in tomados:
                    continue
                if not horario_valido(candidato):
                    continue
                achados.append(Horario(candidato))
                if len(achados) >= quantidade:
                    return achados
        dia += timedelta(days=1)
    return achados


def texto_dos_horarios(horarios: Sequence[Horario]) -> str:
    if not horarios:
        return ("Não localizei horário livre na janela padrão (dias úteis, 8h às 17h). "
                "Confirme a agenda com o Dr. Sandro antes de propor data.")
    linhas = [f"• {h}" for h in horarios]
    return "\n".join(linhas)


def texto_dos_honorarios() -> str:
    return (f"A consulta é de R$ {VALOR_CONSULTA:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            + " e dura cerca de 60 minutos. Em caso de contratação, esse valor "
              "pode ser abatido dos honorários. O orçamento do serviço em si só "
              "sai depois da consulta, quando o caso estiver delimitado.")


# --------------------------------------------------------------------------
# Roteiro determinístico — o atendimento sem IA.
# --------------------------------------------------------------------------

def _saudacao(agora: datetime | None = None) -> str:
    h = (agora or datetime.now()).hour
    if h < 12:
        return "Bom dia"
    if h < 18:
        return "Boa tarde"
    return "Boa noite"


def roteiro(classificacao: Classificacao, mensagem: str = "", *,
            nome: str = "", agora: datetime | None = None,
            horarios: Sequence[Horario] | None = None) -> str:
    """Atendimento escrito, sem IA. É o piso do serviço, não um erro.

    Tom da diretriz §21: cordial, direto, técnico sem juridiquês, e sem
    prometer resultado — que é exatamente o que um roteiro fixo consegue
    garantir melhor do que qualquer modelo.
    """
    agora = agora or datetime.now()
    tratamento = f"{_saudacao(agora)}, {nome.split()[0]}!" if nome.strip() else f"{_saudacao(agora)}!"
    livres = list(horarios if horarios is not None else horarios_disponiveis(agora))
    partes: list[str] = [f"{tratamento} Aqui é o atendimento do {ESCRITORIO['nome']}."]

    if classificacao.fluxo == "#INICIO":
        partes.append(
            "Pode me contar, com suas palavras, o que está acontecendo? "
            "Quanto mais concreto (o que houve, quando, com quem), mais rápido "
            "consigo direcionar ao Dr. Sandro.")
        partes.append(RODAPE_INSTITUCIONAL)
        return "\n\n".join(partes)

    if classificacao.fluxo == "#CLIENTE_EXISTENTE":
        if classificacao.processo:
            partes.append(
                f"Localizei a referência ao processo {classificacao.processo} na sua mensagem. "
                "Vou levar ao Dr. Sandro para ele conferir o andamento nos autos e te responder.")
        else:
            partes.append(
                "Para consultar o andamento com precisão, me envie o número do processo "
                "ou o CPF do titular. Assim eu confiro direto nos autos, sem achismo.")
        partes.append(
            "Não vou adiantar prazo de decisão nem resultado: isso depende do juízo e "
            "ninguém aqui tem como prometer. O que eu confirmo é o que está registrado nos autos.")
        partes.append(RODAPE_INSTITUCIONAL)
        return "\n\n".join(partes)

    if classificacao.fluxo == "FOLLOW-UP":
        partes.append(
            "Passando para saber se você ainda precisa de ajuda com o que conversamos. "
            "Se preferir, respondo por aqui mesmo ou já reservo um horário de consulta.")
        if livres:
            partes.append("Horários livres:\n" + texto_dos_horarios(livres))
        partes.append(RODAPE_INSTITUCIONAL)
        return "\n\n".join(partes)

    # #NOVO_CLIENTE — triagem.
    area = classificacao.area
    if classificacao.risco == "CRÍTICO":
        partes.append(
            "Pelo que você descreveu, esse caso é urgente e eu vou acionar o "
            f"{ESCRITORIO['advogado']} agora, fora da fila normal de atendimento. "
            f"Se preferir falar direto, o telefone é {ESCRITORIO['telefone']}.")
    elif classificacao.risco == "ALTO":
        partes.append(
            "Entendi. Esse caso tem elemento de prazo ou de urgência, então ele entra "
            "com prioridade na triagem do Dr. Sandro.")
    else:
        partes.append("Obrigado pelo relato. Já consigo direcionar.")

    partes.append(f"Pelo que você contou, o assunto é de {area.nome.lower()}.")
    partes.append("Para o Dr. Sandro analisar sem precisar voltar atrás, me responda:\n"
                  + "\n".join(f"{i}. {p}" for i, p in enumerate(area.perguntas, 1)))
    partes.append("Se tiver em mãos, separe também:\n"
                  + "\n".join(f"• {d}" for d in area.documentos))
    partes.append(texto_dos_honorarios())
    if livres:
        partes.append("Tenho estes horários livres:\n" + texto_dos_horarios(livres)
                      + "\n\nO atendimento pode ser " + "; ".join(MODALIDADES) + ".")
    partes.append(
        "Uma observação honesta: sem ler os documentos ninguém aqui vai dizer se o caso "
        "ganha, quanto rende ou quanto demora. Na consulta o Dr. Sandro te dá o cenário real, "
        "com os riscos inclusive.")
    partes.append(RODAPE_INSTITUCIONAL)
    return "\n\n".join(partes)


# --------------------------------------------------------------------------
# Prompt da IA
# --------------------------------------------------------------------------

DIRETRIZES_ATENDIMENTO = f"""Você é o JARBAS, atendimento do {ESCRITORIO['nome']},
escritório de {ESCRITORIO['advogado']} ({ESCRITORIO['oab']}), em Canela/RS.

Você fala com quem procura o escritório — cliente ou futuro cliente. Não fala
com o advogado. Não produz peça, parecer nem análise processual aqui.

REGRA ABSOLUTA — ZERO INVENÇÃO. É proibido, sem exceção:
- afirmar fato que o cliente não relatou;
- citar jurisprudência, súmula, tema, número de processo, ementa, relator ou
  data de julgamento;
- citar doutrina, autor, obra ou página;
- inventar andamento processual, prazo, evento ou decisão;
- estimar valor de indenização, chance de êxito ou tempo de tramitação;
- prometer resultado judicial ou prazo de decisão do Judiciário.
Quando não souber, diga que não sabe e que depende de analisar os documentos.
Uma resposta cautelosa e correta vale mais do que uma assertiva e inventada.

O QUE VOCÊ FAZ:
1. Acolhe, entende o problema e faz a triagem da área.
2. Pergunta só o que falta para o advogado abrir o caso — nada além (LGPD,
   minimização de dados). Não peça CPF, renda, dados de saúde ou documento
   sem necessidade concreta.
3. Explica os próximos passos em linguagem simples.
4. Oferece horário de consulta entre os que forem informados no contexto.
   Não invente horário nem diga que a agenda está livre se nada foi informado.
5. Informa a consulta de R$ 250,00, de cerca de 60 minutos, abatível dos
   honorários em caso de contratação. Não estipule honorários do serviço:
   isso sai depois da consulta, com base na Tabela OAB/RS.
6. Encaminha ao advogado o que for urgente.

TOM (diretriz §21): cordial, humano, direto, informal na medida, técnico sem
juridiquês. Frases curtas. Sem adjetivação vazia, sem "prezado cliente", sem
formalidade de petição. Você é o escritório falando, não um robô de URA.

FORMATO: texto corrido de WhatsApp, no máximo 5 parágrafos curtos, listas
quando houver mais de duas perguntas. Termine com o rodapé institucional:

{RODAPE_INSTITUCIONAL}

Você produz MINUTA DE ATENDIMENTO. Um advogado revisa antes do envio."""


def contexto_do_atendimento(classificacao: Classificacao, *,
                            horarios: Sequence[Horario] = (),
                            nome: str = "", historico: Sequence[dict] = (),
                            observacoes: str = "") -> str:
    """Contexto factual. Tudo que a IA pode afirmar sobre agenda, área e risco
    precisa estar AQUI — o que não estiver, ela não tem de onde tirar."""
    linhas = [
        f"FLUXO: {classificacao.fluxo}",
        f"ÁREA IDENTIFICADA NA TRIAGEM: {classificacao.area.nome}",
        f"RISCO INTERNO: {classificacao.risco}"
        + (f" ({'; '.join(classificacao.motivos)})" if classificacao.motivos else ""),
        f"NOME INFORMADO: {nome or 'não informado'}",
        f"PROCESSO CITADO: {classificacao.processo or 'nenhum'}",
        "PERGUNTAS DE TRIAGEM DESTA ÁREA:\n"
        + "\n".join(f"- {p}" for p in classificacao.area.perguntas),
        "DOCUMENTOS ÚTEIS DESTA ÁREA:\n"
        + "\n".join(f"- {d}" for d in classificacao.area.documentos),
        "HORÁRIOS REALMENTE LIVRES NA AGENDA (só estes podem ser oferecidos):\n"
        + (texto_dos_horarios(horarios) if horarios
           else "nenhum horário confirmado — não ofereça data, diga que vai confirmar a agenda"),
        f"MODALIDADES DE ATENDIMENTO: {'; '.join(MODALIDADES)}",
        f"CONSULTA: R$ {VALOR_CONSULTA:.2f}".replace(".", ","),
    ]
    if observacoes.strip():
        linhas.append(f"OBSERVAÇÕES DO ESCRITÓRIO: {observacoes.strip()}")
    if historico:
        conversa = "\n".join(
            f"{'CLIENTE' if m.get('role') == 'user' else 'JARBAS'}: {m.get('content', '')}"
            for m in historico[-12:])
        linhas.append("CONVERSA ATÉ AQUI:\n" + conversa)
    return "\n\n".join(linhas)


# --------------------------------------------------------------------------
# Revisão da saída — diretriz §18, aplicada ao que a IA acabou de escrever.
#
# Não reescreve o texto: sinaliza. Reescrever seria produzir conteúdo sobre
# conteúdo, e quem decide o que sai para o cliente é o advogado.
# --------------------------------------------------------------------------

PROMESSAS = (
    "voce vai ganhar", "vamos ganhar", "com certeza vai receber", "garanto",
    "garantimos", "certamente", "sem duvida voce", "com certeza voce",
    "e ganho certo", "ganho certo", "100% de chance", "vitoria garantida",
    "sera deferido", "o juiz vai deferir", "o juiz vai conceder",
)

PRAZOS_PROMETIDOS = (
    "a decisao sai em", "o juiz decide em", "sera julgado em", "sai em ate",
    "em ate 30 dias voce recebe", "resolve em ate",
)

CITACOES = re.compile(
    r"(?:\bresp\b|\bagrg\b|\baresp\b|\bhc\b\s*\d|\bsumula\b|\btema\s*\d|"
    r"\brepercussao geral\b|\bacordao\b|\bementa\b|\bstj\b|\bstf\b|\btjrs\b)",
    re.I)

VALORES = re.compile(r"r\$\s*\d", re.I)


def revisar_saida(texto: str) -> tuple[str, ...]:
    """Alertas sobre o texto produzido. Tupla vazia = nada encontrado."""
    t = normalizar(texto)
    alertas: list[str] = []
    if any(p in t for p in PROMESSAS):
        alertas.append("promessa de resultado detectada — a diretriz §16 proíbe. Reescreva antes de enviar.")
    if any(p in t for p in PRAZOS_PROMETIDOS):
        alertas.append("promessa de prazo de decisão do Judiciário detectada. Retire antes de enviar.")
    if CITACOES.search(texto or ""):
        alertas.append("citação de tribunal, súmula ou precedente no atendimento — "
                       "confira a existência e a fonte oficial, ou remova (diretriz §6).")
    valores = [v for v in VALORES.findall(texto or "")]
    if valores and f"{VALOR_CONSULTA:.2f}".replace(".", ",") not in (texto or "") \
            and "250" not in (texto or ""):
        alertas.append("valor em reais diferente da consulta — o chatbot não estipula "
                       "honorários nem estima indenização (diretriz §13).")
    return tuple(alertas)


# --------------------------------------------------------------------------
# Resposta
# --------------------------------------------------------------------------

@dataclass
class Resposta:
    texto: str
    classificacao: Classificacao
    modelo: str = "roteiro"
    input_tokens: int = 0
    output_tokens: int = 0
    horarios: tuple[Horario, ...] = ()
    alertas: tuple[str, ...] = ()
    aviso: str = AVISO_REVISAO_HUMANA

    @property
    def por_ia(self) -> bool:
        return self.modelo != "roteiro"


def ia_disponivel() -> bool:
    return bool(gateway_configured()) and os.getenv("JARBAS_CHATBOT_IA", "1").strip() != "0"


def responder(mensagem: str, *, historico: Sequence[dict] = (), nome: str = "",
              cliente_conhecido: bool = False, agora: datetime | None = None,
              ocupados: Iterable[datetime] = (), observacoes: str = "",
              forcar_roteiro: bool = False) -> Resposta:
    """Produz a resposta do atendimento.

    Sem chave, ou com `forcar_roteiro`, entrega o roteiro determinístico.
    Com chave, chama o Claude com o contexto factual e revisa o que voltou.
    Falha da IA levanta ChatbotError: não vira texto de atendimento.
    """
    agora = agora or datetime.now()
    classificacao = classificar(mensagem, historico=historico,
                                cliente_conhecido=cliente_conhecido)
    livres = tuple(horarios_disponiveis(agora, ocupados=ocupados))

    if forcar_roteiro or not ia_disponivel():
        texto = roteiro(classificacao, mensagem, nome=nome, agora=agora, horarios=livres)
        return Resposta(texto=texto, classificacao=classificacao, horarios=livres,
                        alertas=revisar_saida(texto))

    entrada = (
        contexto_do_atendimento(classificacao, horarios=livres, nome=nome,
                                historico=historico, observacoes=observacoes)
        + "\n\nMENSAGEM DO CLIENTE AGORA:\n" + (mensagem or "").strip()
        + "\n\nEscreva APENAS a resposta a ser enviada ao cliente."
    )
    # Atendimento é conversa, não redação de peça: o perfil de rotina responde
    # rápido e com o custo certo. Peça e parecer continuam no perfil legal.
    perfil = "intake" if classificacao.prioritario else "routine"
    try:
        resultado = gateway_ask(DIRETRIZES_ATENDIMENTO, entrada, profile=perfil)
    except Exception as exc:  # a origem varia: SDK ausente, 401, rede, cota
        raise ChatbotError(str(exc)) from exc

    texto = (resultado.text or "").strip()
    if not texto:
        raise ChatbotError("A IA respondeu sem texto utilizável.")
    if ESCRITORIO["telefone"] not in texto:
        texto = f"{texto}\n\n{RODAPE_INSTITUCIONAL}"
    return Resposta(texto=texto, classificacao=classificacao, modelo=resultado.model,
                    input_tokens=resultado.input_tokens, output_tokens=resultado.output_tokens,
                    horarios=livres, alertas=revisar_saida(texto))


def resumo_do_atendimento(mensagens: Sequence[dict], classificacao: Classificacao | None = None) -> str:
    """Resumo para o advogado, com dado direto mascarado (diretriz §17)."""
    ditas = [m.get("content", "") for m in mensagens if m.get("role") == "user"]
    if not ditas:
        return "Atendimento sem mensagem do cliente."
    corpo = mascarar_sensiveis(" ".join(ditas))[:600]
    if classificacao:
        cabeca = (f"{classificacao.area.nome} · risco {classificacao.risco}"
                  f" · {classificacao.fluxo}")
        if classificacao.motivos:
            cabeca += f" · {'; '.join(classificacao.motivos)}"
        return f"{cabeca}\n{corpo}"
    return corpo
