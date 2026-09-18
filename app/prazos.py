"""Contagem de prazos processuais.

Antes disto o JARBAS guardava a data de vencimento que o advogado digitava e
nada mais. Contar o prazo era conta de cabeça — num sistema que existe
justamente para isso não depender de memória.

As regras implementadas:

- **Art. 219 do CPC**: prazo processual conta-se só em DIAS ÚTEIS. Vale para
  prazo processual; prazo material (decadencial, prescricional) corre em dias
  corridos, e o módulo trata os dois separadamente.
- **Art. 224**: exclui o dia do começo, inclui o dia do vencimento; se o termo
  inicial cair em dia sem expediente, começa no primeiro dia útil seguinte.
- **Art. 220**: suspende-se o curso do prazo entre 20 de dezembro e 20 de
  janeiro. Não é férias do advogado — é suspensão de prazo.
- **Art. 216 e Lei 662/1949 + 6.802/1980**: feriados nacionais, incluindo os
  móveis derivados da Páscoa.
- **Lei estadual RS 5.351/1966**: 20 de setembro, Revolução Farroupilha.

O que este módulo NÃO faz, e é importante saber: não conhece feriado
municipal, ponto facultativo de tribunal, suspensão por portaria nem
suspensão específica do processo. Toda data calculada é **sugestão sujeita a
conferência** no calendário do tribunal. A responsabilidade pelo prazo é do
advogado, não do sistema.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta

# --------------------------------------------------------------------------
# Feriados
# --------------------------------------------------------------------------

FIXOS_NACIONAIS = {
    (1, 1): "Confraternização Universal",
    (4, 21): "Tiradentes",
    (5, 1): "Dia do Trabalho",
    (9, 7): "Independência",
    (10, 12): "Nossa Senhora Aparecida",
    (11, 2): "Finados",
    (11, 15): "Proclamação da República",
    (11, 20): "Consciência Negra",
    (12, 25): "Natal",
}

FIXOS_ESTADUAIS = {
    "RS": {(9, 20): "Revolução Farroupilha"},
    "SP": {(7, 9): "Revolução Constitucionalista"},
    "RJ": {(4, 23): "São Jorge"},
}

# Datas de expediente forense suspenso que não são feriado civil.
FORENSES = {
    (1, 31): "Dia da Justiça Federal (varia por tribunal)",
    (8, 11): "Dia do Advogado",
    (12, 8): "Dia da Justiça",
}


def pascoa(ano: int) -> date:
    """Meeus/Jones/Butcher. Conferido contra 2023–2028."""
    a, b, c = ano % 19, ano // 100, ano % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    return date(ano, (h + l - 7 * m + 114) // 31, ((h + l - 7 * m + 114) % 31) + 1)


def moveis(ano: int) -> dict[date, str]:
    p = pascoa(ano)
    return {
        p - timedelta(days=48): "Carnaval (segunda)",
        p - timedelta(days=47): "Carnaval (terça)",
        p - timedelta(days=46): "Quarta-feira de Cinzas (até 14h)",
        p - timedelta(days=2): "Sexta-feira Santa",
        p + timedelta(days=60): "Corpus Christi",
    }


def feriados(ano: int, uf: str = "RS", incluir_forenses: bool = True) -> dict[date, str]:
    """Todos os dias sem expediente forense do ano, exceto o recesso."""
    saida: dict[date, str] = {}
    for (m, d), nome in FIXOS_NACIONAIS.items():
        saida[date(ano, m, d)] = nome
    for (m, d), nome in FIXOS_ESTADUAIS.get((uf or "").upper(), {}).items():
        saida[date(ano, m, d)] = nome
    if incluir_forenses:
        for (m, d), nome in FORENSES.items():
            saida.setdefault(date(ano, m, d), nome)
    saida.update(moveis(ano))
    return saida


def em_recesso(dia: date) -> bool:
    """Art. 220 do CPC: 20/12 a 20/01, inclusive."""
    return (dia.month == 12 and dia.day >= 20) or (dia.month == 1 and dia.day <= 20)


def uf_padrao() -> str:
    return (os.getenv("JARBAS_UF", "RS") or "RS").upper()


def e_dia_util(dia: date, uf: str | None = None,
               considerar_recesso: bool = True) -> bool:
    """Dia útil forense: não é fim de semana, feriado nem recesso."""
    if dia.weekday() >= 5:
        return False
    if considerar_recesso and em_recesso(dia):
        return False
    return dia not in feriados(dia.year, uf or uf_padrao())


def motivo_nao_util(dia: date, uf: str | None = None) -> str:
    if dia.weekday() == 5:
        return "sábado"
    if dia.weekday() == 6:
        return "domingo"
    if em_recesso(dia):
        return "recesso forense (art. 220 do CPC)"
    return feriados(dia.year, uf or uf_padrao()).get(dia, "")


def proximo_dia_util(dia: date, uf: str | None = None,
                     considerar_recesso: bool = True) -> date:
    d = dia
    for _ in range(400):
        if e_dia_util(d, uf, considerar_recesso):
            return d
        d += timedelta(days=1)
    raise ValueError("não encontrei dia útil em 400 dias")


def dias_uteis_entre(inicio: date, fim: date, uf: str | None = None) -> int:
    """Quantos dias úteis há de inicio (exclusive) até fim (inclusive)."""
    if fim <= inicio:
        return 0
    n, d = 0, inicio + timedelta(days=1)
    while d <= fim:
        if e_dia_util(d, uf):
            n += 1
        d += timedelta(days=1)
    return n


# --------------------------------------------------------------------------
# Cálculo do prazo
# --------------------------------------------------------------------------

@dataclass
class Prazo:
    termo_inicial: date
    inicio_contagem: date
    vencimento: date
    dias: int
    uteis: bool
    fundamento: str = ""
    suspensoes: list[str] = field(default_factory=list)

    @property
    def dias_restantes(self) -> int:
        return (self.vencimento - date.today()).days

    @property
    def uteis_restantes(self) -> int:
        return dias_uteis_entre(date.today(), self.vencimento)

    @property
    def vencido(self) -> bool:
        return self.vencimento < date.today()

    def resumo(self) -> str:
        tipo = "dias úteis" if self.uteis else "dias corridos"
        base = (f"{self.dias} {tipo} a partir de "
                f"{self.inicio_contagem.strftime('%d/%m/%Y')} → "
                f"vence {self.vencimento.strftime('%d/%m/%Y')}")
        return f"{base} ({self.fundamento})" if self.fundamento else base


def calcular(termo_inicial: date, dias: int, *, uteis: bool = True,
             uf: str | None = None, fundamento: str = "",
             dobro: bool = False) -> Prazo:
    """Calcula o vencimento a partir do termo inicial.

    Art. 224: exclui o dia do começo, inclui o do vencimento. Se o termo
    inicial cair em dia sem expediente, a contagem começa no primeiro dia
    útil seguinte.

    dobro=True aplica o art. 186 (Defensoria) / 183 (Fazenda) / 229
    (litisconsortes com procuradores distintos em autos físicos).
    """
    if dias <= 0:
        raise ValueError("o prazo precisa ter ao menos 1 dia")
    uf = uf or uf_padrao()
    total = dias * 2 if dobro else dias

    notas: list[str] = []
    inicio = termo_inicial
    if not e_dia_util(inicio, uf):
        motivo = motivo_nao_util(inicio, uf)
        inicio = proximo_dia_util(inicio, uf)
        notas.append(f"termo inicial em {motivo}: contagem iniciada em "
                     f"{inicio.strftime('%d/%m/%Y')}")

    if uteis:
        d, contados = inicio, 0
        while contados < total:
            d += timedelta(days=1)
            if e_dia_util(d, uf):
                contados += 1
        vencimento = d
        if any(em_recesso(inicio + timedelta(days=i))
               for i in range((vencimento - inicio).days + 1)):
            notas.append("prazo atravessa o recesso forense (art. 220 do CPC)")
    else:
        vencimento = inicio + timedelta(days=total)
        # Prazo material não conta em dias úteis, mas vencimento em dia sem
        # expediente prorroga para o próximo útil (art. 224, §1º).
        if not e_dia_util(vencimento, uf):
            motivo = motivo_nao_util(vencimento, uf)
            vencimento = proximo_dia_util(vencimento, uf)
            notas.append(f"vencimento caía em {motivo}: prorrogado para "
                         f"{vencimento.strftime('%d/%m/%Y')}")

    if dobro:
        notas.append(f"prazo em dobro: {dias} → {total} dias")

    return Prazo(termo_inicial, inicio, vencimento, total, uteis, fundamento, notas)


# --------------------------------------------------------------------------
# Catálogo de prazos comuns
# --------------------------------------------------------------------------

@dataclass
class TipoPrazo:
    codigo: str
    nome: str
    dias: int
    uteis: bool
    fundamento: str


CATALOGO: list[TipoPrazo] = [
    TipoPrazo("contestacao", "Contestação", 15, True, "art. 335 do CPC"),
    TipoPrazo("contestacao_jec", "Contestação (Juizado)", 15, True, "art. 30 da Lei 9.099/95"),
    TipoPrazo("apelacao", "Apelação", 15, True, "art. 1.003, §5º do CPC"),
    TipoPrazo("contrarrazoes_apelacao", "Contrarrazões de apelação", 15, True, "art. 1.010, §1º do CPC"),
    TipoPrazo("embargos_declaracao", "Embargos de declaração", 5, True, "art. 1.023 do CPC"),
    TipoPrazo("agravo_instrumento", "Agravo de instrumento", 15, True, "art. 1.003, §5º do CPC"),
    TipoPrazo("agravo_interno", "Agravo interno", 15, True, "art. 1.021, §2º do CPC"),
    TipoPrazo("recurso_especial", "Recurso especial / extraordinário", 15, True, "art. 1.003, §5º do CPC"),
    TipoPrazo("recurso_inominado", "Recurso inominado (Juizado)", 10, True, "art. 42 da Lei 9.099/95"),
    TipoPrazo("embargos_execucao", "Embargos à execução", 15, True, "art. 915 do CPC"),
    TipoPrazo("impugnacao_cumprimento", "Impugnação ao cumprimento de sentença", 15, True, "art. 525 do CPC"),
    TipoPrazo("cumprimento_pagamento", "Pagamento voluntário (cumprimento)", 15, True, "art. 523 do CPC"),
    TipoPrazo("replica", "Réplica", 15, True, "art. 350 do CPC"),
    TipoPrazo("manifestacao_geral", "Manifestação (prazo geral)", 5, True, "art. 218, §3º do CPC"),
    TipoPrazo("especificar_provas", "Especificar provas", 15, True, "art. 348 do CPC"),
    TipoPrazo("alegacoes_finais", "Alegações finais (memoriais)", 15, True, "art. 364, §2º do CPC"),
    TipoPrazo("laudo_pericial", "Manifestação sobre laudo pericial", 15, True, "art. 477, §1º do CPC"),
    TipoPrazo("custas_preparo", "Recolhimento de custas/preparo", 5, True, "art. 1.007, §4º do CPC"),
    TipoPrazo("emenda_inicial", "Emenda à inicial", 15, True, "art. 321 do CPC"),
    TipoPrazo("resposta_trabalhista", "Defesa trabalhista", 5, True, "art. 847 da CLT"),
    # Materiais: dias corridos
    TipoPrazo("rescisoria", "Ação rescisória", 730, False, "art. 975 do CPC (2 anos)"),
    TipoPrazo("mandado_seguranca", "Mandado de segurança", 120, False, "art. 23 da Lei 12.016/09"),
]

POR_CODIGO = {t.codigo: t for t in CATALOGO}


def calcular_do_catalogo(codigo: str, termo_inicial: date, *,
                         uf: str | None = None, dobro: bool = False) -> Prazo:
    t = POR_CODIGO.get(codigo)
    if not t:
        raise ValueError(f"tipo de prazo desconhecido: {codigo}")
    return calcular(termo_inicial, t.dias, uteis=t.uteis, uf=uf,
                    fundamento=t.fundamento, dobro=dobro)


# --------------------------------------------------------------------------
# Semáforo para o painel
# --------------------------------------------------------------------------

def criticidade(vencimento: date, hoje: date | None = None,
                uf: str | None = None) -> tuple[str, str]:
    """(nível, texto). Conta em dias ÚTEIS: é o que resta de trabalho real."""
    hoje = hoje or date.today()
    if vencimento < hoje:
        dias = (hoje - vencimento).days
        return "vencido", f"VENCIDO há {dias} dia{'s' if dias != 1 else ''}"
    if vencimento == hoje:
        return "hoje", "VENCE HOJE"
    uteis = dias_uteis_entre(hoje, vencimento, uf)
    corridos = (vencimento - hoje).days
    if uteis <= 2:
        return "critico", f"{uteis} dia(s) útil(eis) — {corridos} corridos"
    if uteis <= 5:
        return "atencao", f"{uteis} dias úteis"
    return "normal", f"{uteis} dias úteis"
