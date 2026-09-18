"""Gateway de IA — Anthropic (Claude), provedor único do JARBAS.

A partir da 9.0 o sistema opera com um provedor só. A interface pública deste
módulo foi preservada integralmente — mesmos nomes, mesmas assinaturas — para
que main.py, v7.py e copilot.py continuassem funcionando sem alteração.

O QUE SE PERDE COM UM PROVEDOR SÓ, registrado aqui de propósito:

A arquitetura anterior tinha uma regra — quem redige não critica — e a crítica
caía obrigatoriamente em modelo de OUTRA empresa. O motivo não era preferência
técnica: um modelo tende a aprovar o próprio texto, porque reconhece o próprio
raciocínio. Um modelo treinado por outra equipe, com outros dados, encontra a
citação inventada que o autor não vê.

Com Claude sozinho a crítica continua existindo e continua sendo feita por um
modelo DIFERENTE (Opus redige, Sonnet critica), mas os dois compartilham
linhagem de treino. Isso reduz, e não elimina, o ponto cego comum. É melhor
que autorrevisão pelo mesmo modelo e pior que revisão entre fornecedores.

Consequência prática: a conferência humana de fundamento legal, que já era
obrigatória, passa a ser ainda mais necessária. Cada [CONFERIR FUNDAMENTO]
precisa ser checado um a um.

A abstração de provedores continua íntegra em ai_council.py. Basta configurar
uma segunda chave para a revisão entre fornecedores voltar.
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env.local", override=False)

ENV_CHAVE = "ANTHROPIC_API_KEY"

PADRAO_LEGAL = "claude-opus-5"
PADRAO_INTAKE = "claude-sonnet-5"
PADRAO_ROTINA = "claude-haiku-4-5-20251001"

MAX_TOKENS_SAIDA = 16000


@dataclass
class AIResult:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    response_id: str = ""


@dataclass
class AIConnectionStatus:
    configured: bool
    sdk_available: bool
    sdk_version: str
    key_hint: str
    legal_model: str
    intake_model: str
    routine_model: str
    message: str


# Caracteres que o navegador, o Word e o PowerShell inserem sem aparecer na
# tela: BOM, espaços de largura zero e espaço não separável. Colar uma chave
# de uma página web costuma trazer pelo menos um deles junto.
_INVISIVEIS = "\ufeff\u200b\u200c\u200d\u2060\u00a0"

# O que foi removido da chave guardada, para o diagnóstico poder dizer ao
# operador que o .env.local precisa ser corrigido — em vez de o sistema
# funcionar por acidente e voltar a quebrar na próxima instalação.
_CHAVE_SANEADA: list[str] = []


def _limpar_chave(bruta: str) -> str:
    """Tira da chave o que nunca faz parte dela.

    Existe por causa de um caso real: o erro na tela dizia ao mesmo tempo
    "Chave recusada (401)" e "não reconheci o formato desta chave". As duas
    coisas juntas só acontecem quando o que está guardado NÃO é a chave pura
    — tem aspas, espaço invisível, ou veio a linha inteira do arquivo colada
    no campo. O servidor recusa porque recebe lixo junto; a checagem local
    não reconhece o prefixo pelo mesmo motivo.

    Três origens, todas comuns:

    - `ANTHROPIC_API_KEY="sk-ant-..."` no .env.local. O python-dotenv tira as
      aspas, mas o INICIAR_JARBAS.ps1 lê o arquivo por conta própria, faz
      Split('=') e exporta o valor COM as aspas. Como o load_dotenv roda com
      override=False, ele não corrige o que o PowerShell já exportou.
    - A linha inteira colada no campo da chave, virando
      `ANTHROPIC_API_KEY=ANTHROPIC_API_KEY=sk-ant-...`.
    - Caractere invisível vindo do copiar-e-colar da página do console.

    Sanear aqui conserta os três de uma vez, em qualquer caminho de entrada.
    """
    k = bruta or ""
    for c in _INVISIVEIS:
        if c in k:
            k = k.replace(c, "")
            _registrar_saneamento("caractere invisível")
    if k != k.strip():
        _registrar_saneamento("espaço em volta")
        k = k.strip()

    # Linha inteira colada no campo: fica ANTHROPIC_API_KEY=<chave>.
    if "=" in k:
        nome, _, valor = k.partition("=")
        if nome.strip().strip("\"'").upper() == ENV_CHAVE:
            _registrar_saneamento("nome da variável colado junto")
            k = valor.strip()

    # Aspas em volta, do .env.local ou do copiar-e-colar.
    if len(k) >= 2 and k[0] == k[-1] and k[0] in "\"'":
        _registrar_saneamento("aspas em volta")
        k = k[1:-1].strip()
    return k


def _registrar_saneamento(motivo: str) -> None:
    if motivo not in _CHAVE_SANEADA:
        _CHAVE_SANEADA.append(motivo)


def chave_saneada() -> list[str]:
    """O que foi removido da chave na última leitura. Vazio = chave limpa."""
    _key()
    return list(_CHAVE_SANEADA)


def _key() -> str:
    _CHAVE_SANEADA.clear()
    return _limpar_chave(os.getenv(ENV_CHAVE, ""))


def configured() -> bool:
    return bool(_key())


def formato_chave(chave: str | None = None) -> tuple[str, str]:
    """Identifica de qual fornecedor a chave parece ser, pelo prefixo.

    Existe porque o erro 401 'invalid x-api-key' não distingue "não tenho
    chave" de "colei a chave errada" — e colar a chave da OpenAI no campo do
    Claude é o engano mais provável de quem migrou da versão 8.x.

    Diagnóstico, não bloqueio: se a Anthropic mudar o formato, uma chave
    legítima não pode deixar de funcionar por causa desta função.
    """
    k = (chave if chave is not None else _key()).strip()
    if not k:
        return "ausente", (
            f"{ENV_CHAVE} não está configurada. Gere uma chave em "
            "console.anthropic.com > API Keys e informe em Configurações "
            "(ou rode CONFIGURAR_IA.cmd).")
    if k.startswith("sk-ant-"):
        return "anthropic", "Formato compatível com uma chave da Anthropic."
    if k.startswith(("sk-proj-", "sk-svcacct-")) or (
            k.startswith("sk-") and not k.startswith("sk-ant-")):
        return "openai", (
            "Esta chave tem formato da OpenAI (começa com 'sk-' sem 'ant'). "
            "A chave da OpenAI NÃO funciona na Anthropic: são empresas "
            "distintas. Gere uma em console.anthropic.com — ela começa com "
            "'sk-ant-'.")
    if k.startswith("AIza"):
        return "google", (
            "Esta chave tem formato do Google (Gemini). O JARBAS 9.x usa "
            "apenas Claude. Gere uma chave em console.anthropic.com.")
    return "desconhecido", (
        "Não reconheci o formato desta chave. Uma chave da Anthropic começa "
        "com 'sk-ant-'.")


def modelo_suportado(nome: str) -> bool:
    """Todo modelo do provedor atual começa com 'claude-'.

    Prefixo em vez de lista fixa: um modelo Claude novo passa a funcionar sem
    precisar editar código, e um modelo de outro fornecedor nunca passa.
    """
    return (nome or "").strip().lower().startswith("claude-")


def _model_env(name: str, default: str) -> str:
    """Lê o modelo do ambiente, DESCARTANDO valor de provedor que não existe mais.

    Instalação vinda da 8.x carrega JARBAS_AI_MODEL_LEGAL=gpt-5.6-sol no
    .env.local. Sem esta validação o gateway enviava esse nome à Anthropic e o
    erro que voltava — 401 invalid x-api-key ou 404 de modelo — não dizia nada
    sobre a verdadeira causa, que era configuração velha.
    """
    valor = (os.getenv(name, "") or "").strip()
    if valor and not modelo_suportado(valor):
        _MODELOS_IGNORADOS[name] = valor
        return default
    return valor or default


# Modelos de configuração antiga que foram descartados, para a tela de
# Configurações poder explicar o que aconteceu em vez de falhar em silêncio.
_MODELOS_IGNORADOS: dict[str, str] = {}


def modelos_ignorados() -> dict[str, str]:
    # força a releitura para popular o registro
    legal_model_name(); intake_model_name(); routine_model_name()
    return dict(_MODELOS_IGNORADOS)


def legal_model_name() -> str:
    return _model_env("JARBAS_AI_MODEL_LEGAL", PADRAO_LEGAL)


def intake_model_name() -> str:
    return _model_env("JARBAS_AI_MODEL_INTAKE", PADRAO_INTAKE)


def routine_model_name() -> str:
    return _model_env("JARBAS_AI_MODEL_ROUTINE", PADRAO_ROTINA)


def model_name(profile: str = "legal") -> str:
    return {"legal": legal_model_name, "intake": intake_model_name,
            "routine": routine_model_name}.get(profile, legal_model_name)()


def _sdk_available() -> bool:
    try:
        import anthropic  # noqa: F401
        return True
    except Exception:
        return False


def _sdk_version() -> str:
    try:
        import anthropic
        return getattr(anthropic, "__version__", "?")
    except Exception:
        return ""


def connection_status() -> AIConnectionStatus:
    k = _key()
    tem_sdk = _sdk_available()
    tipo, diagnostico = formato_chave(k)
    if not k:
        msg = (f"{ENV_CHAVE} nao configurada. Gere a chave em "
               "console.anthropic.com e informe nas Configuracoes.")
    elif tipo != "anthropic":
        msg = diagnostico
    elif not tem_sdk:
        msg = ("Pacote 'anthropic' nao instalado. Rode: "
               "pip install -r requirements-ia.txt")
    else:
        msg = "Claude configurado no ambiente do JARBAS."
    antigos = modelos_ignorados()
    if antigos:
        lista = ", ".join(f"{k}={v}" for k, v in antigos.items())
        msg += (f" Configuração antiga ignorada ({lista}): esses modelos são de "
                "provedor removido na versão 9.0. Estão sendo usados os modelos "
                "Claude padrão. Salve as Configurações para limpar o .env.local.")
    return AIConnectionStatus(
        configured=bool(k), sdk_available=tem_sdk, sdk_version=_sdk_version(),
        key_hint=(f"****{k[-4:]}" if len(k) >= 4 else ""),
        legal_model=legal_model_name(), intake_model=intake_model_name(),
        routine_model=routine_model_name(), message=msg)


def _client(api_key: str | None = None):
    try:
        import anthropic
    except Exception as exc:
        raise RuntimeError("Pacote 'anthropic' nao instalado no runtime.") from exc
    chave = api_key or _key()
    if not chave:
        raise RuntimeError(f"{ENV_CHAVE} nao configurada.")
    timeout = float(os.getenv("JARBAS_AI_TIMEOUT",
                              os.getenv("JARBAS_OPENAI_TIMEOUT", "240")) or 240)
    return anthropic.Anthropic(api_key=chave, timeout=timeout, max_retries=2)


def friendly_error(exc: Exception) -> str:
    nome = type(exc).__name__
    msg = str(exc).strip()
    status = getattr(exc, "status_code", None)
    if status == 401 or "Authentication" in nome:
        tipo, diagnostico = formato_chave()
        sujeira = chave_saneada()
        if sujeira:
            # A chave guardada tinha lixo que o JARBAS removeu na leitura, e
            # ainda assim o servidor recusou. O arquivo continua errado, e a
            # próxima instalação vai repetir o problema: mande consertar a
            # origem, não só tentar de novo.
            return ("Chave recusada (401). O valor guardado esta com "
                    + ", ".join(sujeira) + ". Regrave a chave com "
                    "CONFIGURAR_IA.cmd (ou em Configuracoes), colando APENAS "
                    "a chave — sem aspas e sem o nome da variavel.")
        if tipo == "anthropic":
            return ("Chave da Anthropic recusada (401). O formato esta certo, entao "
                    "ela foi revogada, expirou, pertence a outra organizacao ou a "
                    "conta esta sem credito. Confira em console.anthropic.com > "
                    "API Keys e em Billing.")
        # Formato não reconhecido E recusa do servidor dizem a MESMA coisa: o
        # que está guardado não é uma chave da Anthropic. Antes a mensagem
        # emendava os dois diagnósticos ("Chave recusada (401). Nao reconheci
        # o formato...") e parecia contradição — o operador lia "o servidor
        # recusou" e "nem cheguei a mandar" na mesma frase, sem saber o que
        # fazer.
        return (diagnostico + " Rode DIAGNOSTICAR_IA.cmd para ver o que esta "
                "guardado sem expor a chave.")
    if status == 429 or "RateLimit" in nome:
        return "Limite de uso atingido. Verifique os creditos da conta Anthropic."
    if status == 403 or "PermissionDenied" in nome:
        return "A chave nao tem permissao para este modelo."
    if status == 404 or ("model" in msg.lower() and "not" in msg.lower()):
        return "Modelo indisponivel para esta chave. Ajuste nas Configuracoes."
    if "Connection" in nome or "Timeout" in nome:
        return "Falha de conexao. Verifique internet, firewall e proxy."
    if isinstance(status, int) and status >= 500:
        return "Indisponibilidade temporaria da Anthropic. Tente em instantes."
    return f"{nome}: {msg[:300]}" if msg else nome


def _extract_text(response: Any) -> str:
    partes = []
    for b in getattr(response, "content", []) or []:
        if getattr(b, "type", "") == "text":
            partes.append(getattr(b, "text", "") or "")
    return "\n".join(partes).strip()


def _usage(response: Any) -> tuple[int, int]:
    u = getattr(response, "usage", None)
    if not u:
        return 0, 0
    return (int(getattr(u, "input_tokens", 0) or 0),
            int(getattr(u, "output_tokens", 0) or 0))


def _chamar(client, *, model: str, instructions: str, blocos: list[dict],
            max_tokens: int = MAX_TOKENS_SAIDA) -> AIResult:
    # anthropic 1.x rejeita temperature/top_p/top_k em Messages: nao passar.
    r = client.messages.create(
        model=model, max_tokens=max_tokens, system=instructions,
        messages=[{"role": "user", "content": blocos}])
    texto = _extract_text(r)
    if not texto:
        raise RuntimeError("Claude respondeu sem conteudo textual utilizavel.")
    ti, to = _usage(r)
    return AIResult(texto, model, ti, to, getattr(r, "id", "") or "")


def ask(instructions: str, user_input: str, *, model: str | None = None,
        profile: str = "legal", reasoning_effort: str | None = None) -> AIResult:
    """reasoning_effort existia na API da OpenAI. Mantido na assinatura para
    nao quebrar os chamadores; ignorado aqui."""
    return _chamar(_client(), model=model or model_name(profile),
                   instructions=instructions,
                   blocos=[{"type": "text", "text": user_input}])


def ask_with_pdf_files(instructions: str, user_input: str, paths: Iterable[Path],
                       *, model: str | None = None, profile: str = "legal",
                       reasoning_effort: str | None = None) -> AIResult:
    """Envia os PDFs direto ao Claude, que le o documento nativamente."""
    limite_arquivos = int(os.getenv("JARBAS_AI_MAX_PDF_FILES",
                                    os.getenv("JARBAS_OPENAI_MAX_PDF_FILES", "10")) or 10)
    limite_mb = int(os.getenv("JARBAS_AI_MAX_PDF_MB",
                              os.getenv("JARBAS_OPENAI_MAX_PDF_MB", "200")) or 200)
    limite_bytes = limite_mb * 1024 * 1024

    blocos: list[dict] = []
    enviados = usados = 0
    for p in paths or []:
        if enviados >= limite_arquivos:
            break
        p = Path(p)
        if not p.is_file() or p.suffix.lower() != ".pdf":
            continue
        tamanho = p.stat().st_size
        if usados + tamanho > limite_bytes:
            continue
        blocos.append({
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf",
                       "data": base64.standard_b64encode(p.read_bytes()).decode("ascii")},
        })
        enviados += 1
        usados += tamanho

    blocos.append({"type": "text", "text": user_input})
    return _chamar(_client(), model=model or model_name(profile),
                   instructions=instructions, blocos=blocos)


def _json_from_text(text: str) -> dict[str, Any]:
    bruto = (text or "").strip()
    bruto = re.sub(r"^```(?:json)?|```$", "", bruto, flags=re.M).strip()
    try:
        dados = json.loads(bruto)
        return dados if isinstance(dados, dict) else {}
    except Exception:
        pass
    i, j = bruto.find("{"), bruto.rfind("}")
    if i >= 0 and j > i:
        try:
            dados = json.loads(bruto[i:j + 1])
            return dados if isinstance(dados, dict) else {}
        except Exception:
            return {}
    return {}


INSTRUCAO_INTAKE = (
    "Voce extrai metadados de autos processuais brasileiros para um escritorio "
    "de advocacia. Responda APENAS com JSON valido, sem cercas de codigo e sem "
    "texto antes ou depois.\n\n"
    "Campos: number (n CNJ), court, case_class, subject, area, claim_value, "
    "city, state, parties (lista de objetos com name, role, person_type, "
    "document, address, city, state, zip_code, email, phone, nationality, "
    "marital_status, profession).\n\n"
    "Use string vazia para o que nao constar dos autos. NAO invente dado "
    "nenhum: um CPF ou endereco inventado vira peticao errada."
)


def extract_case_metadata_from_pdf(path: Path) -> tuple[dict[str, Any], AIResult]:
    r = ask_with_pdf_files(
        INSTRUCAO_INTAKE,
        "Extraia os metadados deste processo e devolva apenas o JSON.",
        [path], profile="intake")
    return _json_from_text(r.text), r


def test_connection() -> AIResult:
    return ask("Responda exatamente: OK", "teste de conexao", profile="routine")


def test_credentials(api_key: str, model: str) -> AIResult:
    return _chamar(_client(api_key), model=model or legal_model_name(),
                   instructions="Responda exatamente: OK",
                   blocos=[{"type": "text", "text": "teste"}], max_tokens=16)


ENV_FILE = BASE_DIR / ".env.local"


def _read_env_lines() -> list[str]:
    if not ENV_FILE.is_file():
        return []
    return ENV_FILE.read_text(encoding="utf-8").splitlines()


def _set_env_line(lines: list[str], key: str, value: str) -> list[str]:
    saida, achou = [], False
    for l in lines:
        if l.strip().startswith(f"{key}="):
            if value:
                saida.append(f"{key}={value}")
            achou = True
        else:
            saida.append(l)
    if value and not achou:
        saida.append(f"{key}={value}")
    return saida


def save_local_config(*, api_key: str, legal_model: str, intake_model: str,
                      routine_model: str) -> None:
    if os.getenv("JARBAS_ALLOW_SECRET_CONFIG", "0") != "1":
        raise RuntimeError("Configuracao de segredo pela interface esta desativada.")
    linhas = _read_env_lines()
    if api_key.strip():
        linhas = _set_env_line(linhas, ENV_CHAVE, api_key.strip())
        os.environ[ENV_CHAVE] = api_key.strip()
    for chave_env, valor in (("JARBAS_AI_MODEL_LEGAL", legal_model),
                             ("JARBAS_AI_MODEL_INTAKE", intake_model),
                             ("JARBAS_AI_MODEL_ROUTINE", routine_model)):
        if valor.strip():
            linhas = _set_env_line(linhas, chave_env, valor.strip())
            os.environ[chave_env] = valor.strip()
    ENV_FILE.write_text("\n".join(linhas) + "\n", encoding="utf-8")


def clear_local_api_key() -> None:
    linhas = _set_env_line(_read_env_lines(), ENV_CHAVE, "")
    ENV_FILE.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    os.environ.pop(ENV_CHAVE, None)


OFFICE_RULES = """
Você é o JARBAS, copiloto operacional e jurídico de um escritório de advocacia brasileiro.
- Nunca invente fatos, números, pagamentos, prazos, documentos, jurisprudência ou dados cadastrais.
- Use apenas o contexto fornecido pelo sistema para afirmar fatos internos do escritório.
- Quando faltar dado interno, diga claramente que não foi localizado.
- Em matéria jurídica, diferencie fato, hipótese, risco, estratégia e ponto a confirmar.
- Jurisprudência só pode ser citada quando houver fonte oficial validada no contexto; caso contrário, sinalize pesquisa oficial pendente.
- Nunca autorize protocolo automático. Toda peça, prazo e orientação jurídica exige revisão humana do advogado.
- Ao tratar de financeiro, reproduza exatamente valores e status do contexto; não estime recebimentos inexistentes.
- Seja objetivo, técnico, organizado e útil para tomada de decisão.
""".strip()
