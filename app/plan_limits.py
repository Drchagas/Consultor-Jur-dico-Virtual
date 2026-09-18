"""Limites de plano — o que separa "tem coluna no banco" de "é vendável".

Antes deste módulo a tabela `plans` prometia storage_gb e o sistema nunca
conferia. Um assinante de 20 GB podia subir 500 GB. Em 2.000 assinantes isso
é a diferença entre ~70 TB previstos e algo sem teto.

Também resolve o custo de IA: o teto vivia em variável de ambiente, global e
igual para todos. Aqui cada plano tem sua faixa de modelos (ai_tier) e seu
orçamento mensal em dólar, cobrados por token real.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .database import db

# --------------------------------------------------------------------------
# Faixas de modelo por plano.
#
# Os números vêm de um processo real de 328 páginas (a média da base de
# testes). Custo por análise completa de 5 etapas:
#
# A partir da 9.0 todas as faixas usam Claude. A regra "quem redige não
# critica" continua valendo, mas agora entre MODELOS e não entre fornecedores:
# em toda faixa o crítico é um modelo diferente do redator. Independência
# menor que a anterior — ver a nota no topo de ai_council.py.
# --------------------------------------------------------------------------

FAIXAS: dict[str, dict[str, tuple[str, str]]] = {
    "premium": {
        "extracao":   ("anthropic", "claude-sonnet-5"),
        "estrategia": ("anthropic", "claude-opus-5"),
        "redacao":    ("anthropic", "claude-opus-5"),
        "critica":    ("anthropic", "claude-sonnet-5"),
        "rotina":     ("anthropic", "claude-haiku-4-5-20251001"),
    },
    "padrao": {
        "extracao":   ("anthropic", "claude-haiku-4-5-20251001"),
        "estrategia": ("anthropic", "claude-opus-5"),
        "redacao":    ("anthropic", "claude-sonnet-5"),
        "critica":    ("anthropic", "claude-opus-5"),
        "rotina":     ("anthropic", "claude-haiku-4-5-20251001"),
    },
    "economico": {
        "extracao":   ("anthropic", "claude-haiku-4-5-20251001"),
        "estrategia": ("anthropic", "claude-sonnet-5"),
        "redacao":    ("anthropic", "claude-sonnet-5"),
        "critica":    ("anthropic", "claude-haiku-4-5-20251001"),
        "rotina":     ("anthropic", "claude-haiku-4-5-20251001"),
    },
    "basico": {
        "extracao":   ("anthropic", "claude-haiku-4-5-20251001"),
        "estrategia": ("anthropic", "claude-haiku-4-5-20251001"),
        "redacao":    ("anthropic", "claude-haiku-4-5-20251001"),
        "critica":    ("anthropic", "claude-sonnet-5"),
        "rotina":     ("anthropic", "claude-haiku-4-5-20251001"),
    },
}

FAIXA_PADRAO = "economico"


@dataclass
class LimitesDoPlano:
    plan_code: str
    plan_name: str
    user_limit: int
    case_limit: int
    storage_bytes: int
    ai_tier: str
    ai_usd_mes: float

    @property
    def storage_gb(self) -> float:
        return self.storage_bytes / 1024 ** 3

    def papeis(self) -> dict[str, tuple[str, str]]:
        return FAIXAS.get(self.ai_tier, FAIXAS[FAIXA_PADRAO])


class LimiteExcedido(RuntimeError):
    """Base para estouros de plano. Vira HTTP 402 na interface."""


class CotaDeArmazenamentoExcedida(LimiteExcedido):
    pass


class OrcamentoDeIAExcedido(LimiteExcedido):
    pass


# --------------------------------------------------------------------------

def limites(org_id: int) -> LimitesDoPlano:
    """Limites vigentes do escritório. Sem assinatura ativa, cai no mínimo."""
    with db() as conn:
        row = conn.execute(
            """SELECT p.code,p.name,p.user_limit,p.case_limit,p.storage_gb,
                      p.monthly_credits,
                      COALESCE(p.ai_tier,'') ai_tier,
                      COALESCE(p.monthly_ai_usd,0) monthly_ai_usd
               FROM subscriptions s JOIN plans p ON p.id=s.plan_id
               WHERE s.organization_id=? AND s.status IN ('active','trialing')
               ORDER BY s.id DESC LIMIT 1""",
            (org_id,),
        ).fetchone()

    if not row:
        # Cobre "nunca assinou", "pendente de autorização" e "past_due"
        # (cobrança falhou). Manter limites cheios em past_due seria
        # entregar serviço de graça a quem não pagou.
        return LimitesDoPlano("sem-plano", "Sem assinatura ativa", 1, 3,
                              1 * 1024 ** 3, "basico", 1.0)

    return LimitesDoPlano(
        plan_code=row["code"],
        plan_name=row["name"],
        user_limit=int(row["user_limit"] or 1),
        case_limit=int(row["case_limit"] or 0),
        storage_bytes=int(row["storage_gb"] or 0) * 1024 ** 3,
        ai_tier=(row["ai_tier"] or FAIXA_PADRAO),
        ai_usd_mes=float(row["monthly_ai_usd"] or 0.0),
    )


# ------------------------------------------------------------ armazenamento

def armazenamento_usado(org_id: int) -> int:
    """Bytes ocupados pelos PDFs do escritório."""
    with db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(size_bytes),0) t FROM case_documents WHERE organization_id=?",
            (org_id,),
        ).fetchone()
    return int(row["t"] or 0)


def checar_cota_armazenamento(org_id: int, bytes_novos: int) -> None:
    """Chamar ANTES de gravar o arquivo. Levanta se estourar."""
    lim = limites(org_id)
    if lim.storage_bytes <= 0:
        return
    usado = armazenamento_usado(org_id)
    if usado + bytes_novos > lim.storage_bytes:
        livre = max(0, lim.storage_bytes - usado)
        raise CotaDeArmazenamentoExcedida(
            f"Cota de armazenamento do plano {lim.plan_name} atingida: "
            f"{lim.storage_gb:.0f} GB contratados, {usado / 1024 ** 3:.1f} GB em uso, "
            f"{livre / 1024 ** 2:.0f} MB livres — o arquivo tem "
            f"{bytes_novos / 1024 ** 2:.0f} MB. Libere espaço ou mude de plano."
        )


# --------------------------------------------------------------------- IA

def gasto_ia_mes(org_id: int) -> float:
    primeiro = date.today().replace(day=1).isoformat()
    with db() as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(cost_usd),0) t FROM ai_council_runs
               WHERE organization_id=? AND created_at>=?""",
            (org_id, primeiro),
        ).fetchone()
    return float(row["t"] or 0.0)


def checar_orcamento_ia(org_id: int) -> float:
    """Devolve o gasto do mês. Levanta se o orçamento do plano já acabou."""
    lim = limites(org_id)
    gasto = gasto_ia_mes(org_id)
    if lim.ai_usd_mes > 0 and gasto >= lim.ai_usd_mes:
        raise OrcamentoDeIAExcedido(
            f"Orçamento de IA do plano {lim.plan_name} esgotado neste mês: "
            f"US$ {gasto:.2f} de US$ {lim.ai_usd_mes:.2f}. "
            f"Renova no dia 1º, ou mude de plano para ampliar."
        )
    return gasto


def resumo_consumo(org_id: int) -> dict:
    """Para a tela de assinatura: quanto de cada limite já foi usado."""
    lim = limites(org_id)
    usado = armazenamento_usado(org_id)
    gasto = gasto_ia_mes(org_id)
    with db() as conn:
        casos = conn.execute(
            "SELECT COUNT(*) n FROM cases WHERE organization_id=?", (org_id,)
        ).fetchone()["n"]
        usuarios = conn.execute(
            "SELECT COUNT(*) n FROM memberships WHERE organization_id=? AND is_active=1",
            (org_id,),
        ).fetchone()["n"]

    def pct(u, t):
        return round(min(100.0, u / t * 100), 1) if t else 0.0

    return {
        "plano": lim.plan_name,
        "faixa_ia": lim.ai_tier,
        "armazenamento": {"usado_gb": round(usado / 1024 ** 3, 2),
                          "cota_gb": round(lim.storage_gb, 0),
                          "pct": pct(usado, lim.storage_bytes)},
        "ia": {"usado_usd": round(gasto, 4),
               "cota_usd": lim.ai_usd_mes,
               "pct": pct(gasto, lim.ai_usd_mes)},
        "casos": {"usado": casos, "cota": lim.case_limit,
                  "pct": pct(casos, lim.case_limit)},
        "usuarios": {"usado": usuarios, "cota": lim.user_limit,
                     "pct": pct(usuarios, lim.user_limit)},
    }
