"""Chatbot público de atendimento do CHAGAS – ADVOGADOS.

Este é o único ponto do sistema que fala com alguém SEM LOGIN. Todo o resto
do JARBAS pressupõe usuário autenticado e workspace resolvido; aqui não há
nem um nem outro — quem está do outro lado é um visitante do site, e o
próprio desenho tem de assumir que qualquer um pode mandar qualquer coisa.

Três limites que não são detalhe de implementação, são a razão do módulo
existir separado do resto:

1. NUNCA consulta cliente, processo ou documento algum. As únicas tabelas
   que este módulo lê ou escreve são `chatbot_sessions`, `chatbot_messages`
   e, quando o visitante topa deixar contato, `leads` — a mesma tabela que
   o CRM já usa, sob revisão humana antes de virar cliente de verdade. Não
   existe caminho de código aqui que busque `clients`, `cases` ou
   `case_documents`: se um dia alguém for adicionar isso, é um projeto
   diferente, com autenticação, não um ajuste neste arquivo.
2. O erro da IA nunca chega cru ao visitante. `friendly_error()` foi feito
   para a equipe (cita modelo, hint da chave); mostrar isso a um estranho na
   internet é vazar detalhe de configuração. Toda falha vira uma frase
   genérica; o detalhe técnico só vai para o log do servidor.
3. Superfície pública custa dinheiro por mensagem. Rate limit por IP, teto
   de mensagens por sessão e um teto de gasto MENSAL PRÓPRIO — menor que o
   teto geral de IA do escritório — protegem contra quem descobrir o
   endpoint e mandar milhares de mensagens.

O que o chatbot faz: triagem cordial (novo caso ou cliente já atendido),
explica os próximos passos, incentiva o agendamento e — só com o aceite do
visitante, no formulário de contato — grava um lead no CRM. Nunca dá parecer
jurídico, nunca cita jurisprudência, nunca promete resultado.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import ai_council
from .ai_gateway import ask as ai_ask, configured as ai_configured, routine_model_name
from .database import db

router = APIRouter()

# Log próprio: o erro técnico da IA (modelo, tipo de exceção) precisa ficar
# registrado para quem for diagnosticar, mas nunca pode ir para a resposta
# JSON — essa é a fronteira exata que este arquivo protege.
_LOGGER = logging.getLogger("jarbas.chatbot")
if not _LOGGER.handlers:
    try:
        _log_dir = Path(__file__).resolve().parent.parent / "logs"
        _log_dir.mkdir(parents=True, exist_ok=True)
        _handler = logging.FileHandler(_log_dir / "chatbot.log", encoding="utf-8")
        _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        _LOGGER.addHandler(_handler)
        _LOGGER.setLevel(logging.INFO)
    except OSError:
        pass


def _main():
    from . import main as m
    return m


# ============================================================== configuração

# Instalação de um só escritório: o visitante do site não escolhe workspace,
# então o chatbot fala sempre pelo escritório configurado aqui. Em SaaS
# multiescritório de verdade este valor seria por página/domínio — fora do
# escopo desta versão; documentado no README.
ORG_SLUG_PADRAO = (os.getenv("JARBAS_CHATBOT_ORG_SLUG", "chagas-advogados").strip()
                  or "chagas-advogados")

# Teto PRÓPRIO do chatbot, deliberadamente menor que JARBAS_AI_TETO_USD_MES
# (o teto geral do escritório): é a única porta da casa sem fechadura de
# login, então tem o menor limite de todas.
TETO_USD_MES = max(0.0, float(os.getenv("JARBAS_CHATBOT_TETO_USD_MES", "15") or 15))

MAX_MSGS_POR_SESSAO = 40
MAX_CHARS_MENSAGEM = 1200
HISTORICO_MAX_MENSAGENS = 14

MAX_MSGS_POR_IP_JANELA = 20
JANELA_MSGS_SEGUNDOS = 600
MAX_SESSOES_POR_IP_HORA = 8
JANELA_SESSOES_SEGUNDOS = 3600

# Em memória, como LOGIN_FAILURES em main.py: reinicia com o processo, não é
# compartilhado entre workers. Aceitável pelo mesmo motivo que lá — é um
# freio de abuso, não um cofre; o teto de gasto mensal (guardado no banco) é
# quem segura o caso de múltiplos workers batendo o limite juntos.
_IP_MENSAGENS: dict[str, list[float]] = {}
_IP_SESSOES: dict[str, list[float]] = {}


def _limitado(bucket: dict[str, list[float]], chave: str, janela: float, maximo: int) -> bool:
    agora = time.time()
    tentativas = [t for t in bucket.get(chave, []) if agora - t < janela]
    bucket[chave] = tentativas
    return len(tentativas) >= maximo


def _registrar(bucket: dict[str, list[float]], chave: str) -> None:
    bucket.setdefault(chave, []).append(time.time())


def _ip_hash(request: Request) -> str:
    """Hash, não o IP em claro: existe só para limitar abuso.

    Guardar o IP puro no banco seria coletar mais dado pessoal do que a
    finalidade — limitar mensagens por origem — exige.
    """
    c = _main()
    ip = c.login_client_ip(request)
    sal = (c.SECRET_KEY or "jarbas")[:32]
    return hashlib.sha256(f"{sal}:{ip}".encode("utf-8")).hexdigest()[:24]


# =================================================================== persona

ATENDIMENTO_RULES = """
Você é o JARBAS, assistente virtual de atendimento do site do CHAGAS – ADVOGADOS,
falando DIRETAMENTE com um visitante do site — não é a equipe interna, é
o primeiro contato de alguém que pode nem ser cliente ainda.

REGRAS QUE NÃO PODEM SER QUEBRADAS:
- Você NÃO tem acesso a nenhum cadastro de cliente, processo ou documento.
  Nunca finja consultar um processo, nunca confirme nem negue informação
  sobre um caso específico. Se perguntarem sobre andamento, diga que só a
  equipe confirma isso, com o processo em mãos, e ofereça o WhatsApp.
- Nunca dê parecer jurídico, prognóstico de resultado, valor estimado de
  indenização ou prazo de decisão do Judiciário. Nunca cite lei, artigo,
  súmula ou jurisprudência — isso é trabalho do advogado, com os autos em
  mãos, não de uma conversa inicial no site.
- Nunca invente fato, nome, valor, precedente ou promessa. Na dúvida, diga
  que um advogado confirma depois.
- Sua função é: entender em poucas frases do que se trata, dizer que o
  escritório atua na área quando fizer sentido, explicar os próximos passos
  e incentivar o agendamento de uma consulta.
- Consulta inicial: R$ 250,00, valor que é abatido dos honorários se a
  pessoa contratar o escritório depois.
- Atendimento em dias úteis, das 8h às 17h.
- Se a pessoa disser que já é cliente, não peça CPF nem dado sensível: diga
  que vai conectar com a equipe e ofereça o WhatsApp para confirmar.
- Se alguém pedir para falar com um humano, ou insistir num assunto fora do
  que você pode responder, oriente o WhatsApp sem inventar desculpa.

ESTILO: cordial, direto, técnico sem juridiquês, frases curtas. Responda em
no máximo 100 palavras, texto simples, sem markdown, sem listas numeradas.
""".strip()


# ============================================================ dados do escritório

def _organizacao_padrao(conn):
    row = conn.execute(
        "SELECT * FROM organizations WHERE slug=? AND status='active'", (ORG_SLUG_PADRAO,)
    ).fetchone()
    if row:
        return row
    return conn.execute(
        "SELECT * FROM organizations WHERE status='active' ORDER BY id LIMIT 1"
    ).fetchone()


def link_whatsapp(telefone: str) -> str:
    """'(54) 99110-1959' -> 'https://wa.me/5554991101959'. Sem número, string vazia."""
    digitos = "".join(ch for ch in (telefone or "") if ch.isdigit())
    if not digitos:
        return ""
    if not digitos.startswith("55"):
        digitos = "55" + digitos
    if len(digitos) < 12:
        return ""
    return f"https://wa.me/{digitos}"


def _saudacao(org) -> str:
    nome = org["brand_name"] or org["name"] if org else "CHAGAS – ADVOGADOS"
    return (
        f"Oi! Eu sou o JARBAS, assistente virtual do {nome} 👋\n"
        "Posso te ajudar a entender melhor sua situação e já explicar os "
        "próximos passos. Você já é nosso cliente ou é a primeira vez que "
        "fala com a gente?"
    )


# ==================================================================== respostas

def _falha_tecnica_generica() -> str:
    # Frase fixa de propósito: nunca chama friendly_error() aqui. Aquela
    # função foi escrita para a EQUIPE (cita modelo, hint de chave); um
    # visitante anônimo não pode receber detalhe de configuração do sistema.
    return ("Tive um problema técnico agora e não consegui responder. Pode "
            "tentar de novo em instantes, ou já ir direto pelo WhatsApp — o "
            "botão está aqui embaixo.")


def _resposta_sem_ia() -> str:
    return ("No momento estou respondendo de forma mais simples por aqui. "
            "Me conta em poucas palavras sua situação e como prefere que a "
            "gente retorne (nome e telefone) — ou fale direto pelo WhatsApp "
            "que a equipe confirma tudo com você.")


def _custo_do_mes(conn, org_id: int) -> float:
    primeiro = date.today().replace(day=1).isoformat()
    row = conn.execute(
        """SELECT COALESCE(SUM(m.cost_usd),0) t FROM chatbot_messages m
           JOIN chatbot_sessions s ON s.id=m.session_id AND s.organization_id=m.organization_id
           WHERE m.organization_id=? AND m.created_at>=?""",
        (org_id, primeiro),
    ).fetchone()
    return float(row["t"] or 0.0)


def _responder(conn, *, org_id: int, sessao, mensagem_visitante: str) -> tuple[str, str, float]:
    """Devolve (texto_para_o_visitante, modelo, custo_usd). Nunca levanta."""
    if not ai_configured():
        return _resposta_sem_ia(), "local", 0.0
    if TETO_USD_MES > 0 and _custo_do_mes(conn, org_id) >= TETO_USD_MES:
        return _resposta_sem_ia(), "local", 0.0

    historico = conn.execute(
        """SELECT role,content FROM chatbot_messages
           WHERE organization_id=? AND session_id=? ORDER BY id DESC LIMIT ?""",
        (org_id, sessao["id"], HISTORICO_MAX_MENSAGENS),
    ).fetchall()
    linhas = []
    for linha in reversed(historico):
        papel = "Visitante" if linha["role"] == "visitor" else "JARBAS"
        linhas.append(f"{papel}: {linha['content']}")
    linhas.append(f"Visitante: {mensagem_visitante}")
    prompt = "\n".join(linhas) + "\n\nResponda agora como JARBAS, seguindo as regras."

    try:
        resultado = ai_ask(ATENDIMENTO_RULES, prompt, model=routine_model_name(), profile="routine")
    except Exception as exc:  # noqa: BLE001 — nunca propaga erro técnico ao visitante
        _LOGGER.warning("org=%s falha na IA do chatbot: %s: %s",
                        org_id, type(exc).__name__, str(exc)[:300])
        return _falha_tecnica_generica(), "erro", 0.0
    custo = ai_council.custo_usd(resultado.model, resultado.input_tokens, resultado.output_tokens)
    return resultado.text.strip()[:2000], resultado.model, custo


# ========================================================================= JSON

def _json_erro(mensagem: str, status: int = 400, *, whatsapp: str = "") -> JSONResponse:
    corpo = {"erro": mensagem}
    if whatsapp:
        corpo["whatsapp"] = whatsapp
    return JSONResponse(corpo, status_code=status)


# ============================================================================ rotas públicas

@router.get("/atendimento", response_class=HTMLResponse)
def atendimento_publico(request: Request):
    c = _main()
    with db() as conn:
        org = _organizacao_padrao(conn)
    if not org:
        return HTMLResponse("Atendimento indisponível no momento.", status_code=503)
    return c.safe_template_response("atendimento.html", {
        "request": request,
        "organization": org,
        "whatsapp": link_whatsapp(org["phone"] or ""),
        "chatbot_ativo": bool(org["chatbot_ativo"]) if "chatbot_ativo" in org.keys() else True,
    })


@router.post("/api/chatbot/iniciar")
def chatbot_iniciar(request: Request, csrf: str = Form("", alias="_csrf")):
    c = _main()
    if not c.valid_csrf(request, csrf):
        return _json_erro("Sessão expirada. Atualize a página e tente de novo.", 403)
    if _limitado(_IP_SESSOES, _ip_hash(request), JANELA_SESSOES_SEGUNDOS, MAX_SESSOES_POR_IP_HORA):
        return _json_erro("Muitas conversas iniciadas em pouco tempo. Tente novamente em instantes "
                          "ou fale direto pelo WhatsApp.", 429)
    with db() as conn:
        org = _organizacao_padrao(conn)
        if not org:
            return _json_erro("Atendimento indisponível no momento.", 503)
        if "chatbot_ativo" in org.keys() and not org["chatbot_ativo"]:
            return _json_erro("O atendimento pelo chat está desativado no momento. "
                              "Fale pelo WhatsApp.", 503,
                              whatsapp=link_whatsapp(org["phone"] or ""))
        _registrar(_IP_SESSOES, _ip_hash(request))
        token = secrets.token_urlsafe(24)
        agora = datetime.now().isoformat(timespec="seconds")
        sessao_id = conn.insert_id(
            """INSERT INTO chatbot_sessions
               (organization_id,token,ip_hash,status,message_count,started_at,last_message_at)
               VALUES (?,?,?,'ativo',0,?,?)""",
            (org["id"], token, _ip_hash(request), agora, agora),
        )
        saudacao = _saudacao(org)
        conn.execute(
            """INSERT INTO chatbot_messages (organization_id,session_id,role,content,model,cost_usd,created_at)
               VALUES (?,?,'assistant',?,'local',0,?)""",
            (org["id"], sessao_id, saudacao, agora),
        )
    return JSONResponse({
        "token": token,
        "saudacao": saudacao,
        "whatsapp": link_whatsapp(org["phone"] or ""),
        "ia_ativa": ai_configured(),
    })


def _sessao_por_token(conn, token: str):
    return conn.execute("SELECT * FROM chatbot_sessions WHERE token=?", (token or "",)).fetchone()


@router.post("/api/chatbot/mensagem")
def chatbot_mensagem(request: Request, token: str = Form(...), mensagem: str = Form(...),
                     csrf: str = Form("", alias="_csrf")):
    c = _main()
    if not c.valid_csrf(request, csrf):
        return _json_erro("Sessão expirada. Atualize a página e tente de novo.", 403)
    mensagem = (mensagem or "").strip()
    if not mensagem:
        return _json_erro("Digite uma mensagem antes de enviar.")
    if len(mensagem) > MAX_CHARS_MENSAGEM:
        return _json_erro(f"Mensagem muito longa (máximo {MAX_CHARS_MENSAGEM} caracteres). "
                          "Resuma e envie de novo.")
    if _limitado(_IP_MENSAGENS, _ip_hash(request), JANELA_MSGS_SEGUNDOS, MAX_MSGS_POR_IP_JANELA):
        return _json_erro("Muitas mensagens em pouco tempo. Aguarde um instante ou fale pelo "
                          "WhatsApp.", 429)

    with db() as conn:
        sessao = _sessao_por_token(conn, token)
        if not sessao:
            return _json_erro("Sessão não encontrada. Atualize a página para começar de novo.", 404)
        org_id = sessao["organization_id"]
        if sessao["status"] != "ativo":
            return _json_erro("Esta conversa já foi encerrada. Atualize a página para começar outra.")
        if sessao["message_count"] >= MAX_MSGS_POR_SESSAO:
            return _json_erro("Esta conversa chegou ao limite de mensagens. Continue pelo "
                              "WhatsApp — a equipe já tem o contexto.")
        _registrar(_IP_MENSAGENS, _ip_hash(request))
        agora = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """INSERT INTO chatbot_messages (organization_id,session_id,role,content,model,cost_usd,created_at)
               VALUES (?,?,'visitor',?,'',0,?)""",
            (org_id, sessao["id"], mensagem, agora),
        )
        texto, modelo, custo = _responder(conn, org_id=org_id, sessao=sessao, mensagem_visitante=mensagem)
        conn.execute(
            """INSERT INTO chatbot_messages (organization_id,session_id,role,content,model,cost_usd,created_at)
               VALUES (?,?,'assistant',?,?,?,?)""",
            (org_id, sessao["id"], texto, modelo, custo, agora),
        )
        conn.execute(
            "UPDATE chatbot_sessions SET message_count=message_count+2,last_message_at=? WHERE id=?",
            (agora, sessao["id"]),
        )
        if modelo not in ("local", "erro") and custo:
            creditos = max(1, int(custo * 100))
            conn.execute(
                """INSERT INTO usage_ledger (organization_id,user_id,kind,credits,description,created_at)
                   VALUES (?,NULL,'chatbot_site',?,?,?)""",
                (org_id, creditos, f"Chatbot do site — {modelo}", agora),
            )
    return JSONResponse({"resposta": texto})


@router.post("/api/chatbot/contato")
def chatbot_contato(request: Request, token: str = Form(...), nome: str = Form(...),
                    contato: str = Form(...), area: str = Form(""),
                    resumo: str = Form(""), csrf: str = Form("", alias="_csrf")):
    c = _main()
    if not c.valid_csrf(request, csrf):
        return _json_erro("Sessão expirada. Atualize a página e tente de novo.", 403)
    nome = nome.strip()[:180]
    contato = contato.strip()[:180]
    if not nome or not contato:
        return _json_erro("Informe nome e telefone ou e-mail para a equipe te retornar.")

    with db() as conn:
        sessao = _sessao_por_token(conn, token)
        if not sessao:
            return _json_erro("Sessão não encontrada. Atualize a página para começar de novo.", 404)
        org_id = sessao["organization_id"]
        agora = datetime.now().isoformat(timespec="seconds")

        # Nota curta de propósito: o card do lead no Kanban do CRM exibe
        # `notes` por inteiro. Despejar a transcrição inteira ali faria um
        # card virar uma parede de texto. A conversa completa continua a um
        # clique, em /crm/chatbot/<sessão> — aqui só o essencial para o
        # primeiro olhar.
        primeira_mensagem_visitante = conn.execute(
            """SELECT content FROM chatbot_messages
               WHERE organization_id=? AND session_id=? AND role='visitor'
               ORDER BY id LIMIT 1""",
            (org_id, sessao["id"]),
        ).fetchone()
        notas = "Lead recebido pelo chatbot do site (/atendimento)."
        if area.strip():
            notas += f"\nÁrea informada: {area.strip()[:200]}"
        if resumo.strip():
            notas += f"\nResumo: {resumo.strip()[:400]}"
        elif primeira_mensagem_visitante:
            notas += f"\nPrimeira mensagem: {primeira_mensagem_visitante['content'][:400]}"
        notas += f"\nConversa completa em CRM > Conversas do chatbot (sessão #{sessao['id']})."

        telefone = contato if any(ch.isdigit() for ch in contato) and "@" not in contato else ""
        email = contato if "@" in contato else ""
        lead_id = conn.insert_id(
            """INSERT INTO leads (organization_id,name,contact_name,phone,email,source,area,
               status,estimated_fee,next_action_date,notes,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,'Novo',0,NULL,?,?,?)""",
            (org_id, nome, nome, telefone, email, "Chatbot do site", area.strip()[:80],
             notas, agora, agora),
        )
        conn.execute(
            """UPDATE chatbot_sessions
               SET lead_id=?,visitor_name=?,visitor_contact=?,flow='novo_cliente',last_message_at=?
               WHERE id=?""",
            (lead_id, nome, contato, agora, sessao["id"]),
        )
        confirmacao = (f"Perfeito, {nome.split()[0] if nome.split() else nome}! Recebi seus "
                      "dados e a equipe entra em contato em breve, em horário comercial.")
        conn.execute(
            """INSERT INTO chatbot_messages (organization_id,session_id,role,content,model,cost_usd,created_at)
               VALUES (?,?,'assistant',?,'local',0,?)""",
            (org_id, sessao["id"], confirmacao, agora),
        )
    c.log_action(request, f"Chatbot do site: novo lead #{lead_id} ({nome})", org_id=org_id)
    return JSONResponse({"resposta": confirmacao, "lead_id": lead_id})


@router.post("/api/chatbot/encerrar")
def chatbot_encerrar(request: Request, token: str = Form(...), csrf: str = Form("", alias="_csrf")):
    c = _main()
    if not c.valid_csrf(request, csrf):
        return _json_erro("Sessão expirada.", 403)
    with db() as conn:
        conn.execute("UPDATE chatbot_sessions SET status='encerrada' WHERE token=?", (token,))
    return JSONResponse({"ok": True})


# ============================================================ painel da equipe

@router.get("/crm/chatbot", response_class=HTMLResponse)
def chatbot_admin(request: Request):
    c = _main()
    user, org = c.require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    with db() as conn:
        sessoes = conn.execute(
            """SELECT s.*, l.name lead_name FROM chatbot_sessions s
               LEFT JOIN leads l ON l.id=s.lead_id AND l.organization_id=s.organization_id
               WHERE s.organization_id=? ORDER BY s.id DESC LIMIT 100""",
            (org["id"],),
        ).fetchall()
        gasto_mes = _custo_do_mes(conn, org["id"])
    return c.safe_template_response("chatbot_admin.html", c.common_context(
        request, user, org, sessoes=sessoes, gasto_mes=gasto_mes, teto_mes=TETO_USD_MES,
    ))


@router.get("/crm/chatbot/{session_id}", response_class=HTMLResponse)
def chatbot_transcript(request: Request, session_id: int):
    c = _main()
    user, org = c.require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    with db() as conn:
        sessao = conn.execute(
            "SELECT * FROM chatbot_sessions WHERE id=? AND organization_id=?",
            (session_id, org["id"]),
        ).fetchone()
        if not sessao:
            return RedirectResponse("/crm/chatbot", status_code=303)
        mensagens = conn.execute(
            """SELECT * FROM chatbot_messages WHERE organization_id=? AND session_id=?
               ORDER BY id""",
            (org["id"], session_id),
        ).fetchall()
        lead = None
        if sessao["lead_id"]:
            lead = conn.execute(
                "SELECT * FROM leads WHERE id=? AND organization_id=?",
                (sessao["lead_id"], org["id"]),
            ).fetchone()
    return c.safe_template_response("chatbot_transcript.html", c.common_context(
        request, user, org, sessao=sessao, mensagens=mensagens, lead=lead,
    ))
