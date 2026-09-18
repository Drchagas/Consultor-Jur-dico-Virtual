"""Rotas do Atendimento JARBAS — o chatbot do CHAGAS – ADVOGADOS.

A tela vive em /atendimento. Quem opera é a recepção ou o próprio
advogado: cola o que o cliente escreveu (ou conversa direto pela tela) e
o JARBAS devolve a resposta pronta para enviar, já com a área triada, o
risco classificado e horários que existem de verdade na agenda.

Duas regras desta camada, e as duas vêm de erro conhecido:

- CRÉDITO SÓ É DEBITADO QUANDO A IA RESPONDE. O roteiro determinístico
  não custa nada porque não chama ninguém. Cobrar por ele faria o
  escritório pagar justamente quando o sistema está sem chave.

- FALHA DA IA NÃO VIRA MENSAGEM DO ESCRITÓRIO. A mensagem do cliente é
  gravada (não se perde o relato), o erro aparece como aviso na tela, e
  nenhuma resposta é registrada. Assim o operador reenvia em vez de
  descobrir depois que mandou ao cliente um pedido de desculpas do
  modelo.
"""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import chatbot
from .database import db

router = APIRouter()

CANAIS = ("whatsapp", "telefone", "e-mail", "presencial", "site")

# Custo fixo por resposta com IA. Metade da pergunta aos autos (5): o
# atendimento roda em perfil de rotina ou intake, não no perfil legal.
CUSTO_EM_CREDITOS = 2


def core():
    from . import main as m
    return m


def _csrf(request: Request, token: str):
    c = core()
    return None if c.valid_csrf(request, token) else c.csrf_error()


def _agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------

def _conversa(conn, org_id: int, conversa_id: int):
    return conn.execute(
        "SELECT * FROM chat_conversations WHERE id=? AND organization_id=?",
        (conversa_id, org_id)).fetchone()


def _mensagens(conn, org_id: int, conversa_id: int):
    return conn.execute(
        "SELECT * FROM chat_messages WHERE conversation_id=? AND organization_id=? ORDER BY id",
        (conversa_id, org_id)).fetchall()


def _historico(linhas) -> list[dict]:
    return [{"role": l["role"], "content": l["content"]} for l in linhas]


def _fila(conn, org_id: int, limite: int = 60):
    """Fila ordenada por risco, não por data.

    Um flagrante de ontem passa na frente de uma dúvida de contrato de hoje
    de manhã — é essa a razão de a classificação existir (diretriz §3).
    """
    ordem = {"CRÍTICO": 0, "ALTO": 1, "MÉDIO": 2, "BAIXO": 3}
    linhas = conn.execute(
        """SELECT * FROM chat_conversations WHERE organization_id=?
           ORDER BY updated_at DESC LIMIT ?""", (org_id, limite)).fetchall()
    abertas = [l for l in linhas if (l["status"] or "aberto") == "aberto"]
    fechadas = [l for l in linhas if (l["status"] or "aberto") != "aberto"]
    abertas.sort(key=lambda l: (ordem.get(l["risco"] or "MÉDIO", 2), l["updated_at"]))
    return abertas + fechadas


def _alertas(linha) -> list[str]:
    try:
        return list(json.loads(linha["alertas"] or "[]"))
    except (ValueError, TypeError):
        return []


def _triagem(mensagens) -> chatbot.Classificacao | None:
    """Triagem da ÚLTIMA fala do cliente, para o painel lateral."""
    ultima = next((m["content"] for m in reversed(mensagens) if m["role"] == "user"), "")
    if not ultima:
        return None
    return chatbot.classificar(ultima, historico=_historico(mensagens))


def _para_tela(mensagens) -> list[dict]:
    return [{"role": m["role"], "content": m["content"], "model": m["model"],
             "created_at": m["created_at"], "alertas": _alertas(m)} for m in mensagens]


# --------------------------------------------------------------------------

@router.get("/atendimento", response_class=HTMLResponse)
def atendimento(request: Request, conversa: int = 0):
    c = core()
    user, org = c.require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    atual = None
    mensagens = []
    with db() as conn:
        if conversa:
            atual = _conversa(conn, org["id"], conversa)
            if atual:
                mensagens = _mensagens(conn, org["id"], conversa)
        conversas = _fila(conn, org["id"])
    # O contexto é montado AQUI, literal, e não num helper: tools/check_templates.py
    # lê a chamada para saber o que o template recebe, e um helper esconderia
    # dele exatamente a informação que ele existe para conferir.
    return c.safe_template_response("chatbot.html", c.common_context(
        request, user, org,
        conversas=conversas,
        conversa=atual,
        mensagens=_para_tela(mensagens),
        classificacao=_triagem(mensagens),
        areas=chatbot.AREAS,
        riscos=chatbot.RISCOS,
        canais=CANAIS,
        horarios=[str(h) for h in chatbot.horarios_disponiveis()],
        modalidades=chatbot.MODALIDADES,
        valor_consulta=chatbot.VALOR_CONSULTA,
        rodape=chatbot.RODAPE_INSTITUCIONAL,
        escritorio=chatbot.ESCRITORIO,
        aviso_revisao=chatbot.AVISO_REVISAO_HUMANA,
        aviso_lgpd=chatbot.AVISO_LGPD,
        ia_ativa=chatbot.ia_disponivel(),
        erro=request.query_params.get("erro", ""),
        ok=request.query_params.get("ok", "")))


@router.post("/atendimento/nova")
def nova_conversa(request: Request, contato_nome: str = Form(""), contato_telefone: str = Form(""),
                  contato_email: str = Form(""), canal: str = Form("whatsapp"),
                  observacoes: str = Form(""), csrf: str = Form("", alias="_csrf")):
    c = core()
    bad = _csrf(request, csrf)
    if bad:
        return bad
    user, org = c.require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    agora = _agora()
    with db() as conn:
        novo = conn.insert_id(
            """INSERT INTO chat_conversations
               (organization_id,user_id,canal,contato_nome,contato_telefone,contato_email,
                status,observacoes,created_at,updated_at)
               VALUES (?,?,?,?,?,?,'aberto',?,?,?)""",
            (org["id"], user["id"], (canal or "whatsapp").strip()[:40],
             contato_nome.strip()[:160], contato_telefone.strip()[:40],
             contato_email.strip()[:160], observacoes.strip()[:2000], agora, agora))
    c.log_action(request, f"Atendimento JARBAS: conversa #{novo} aberta")
    return RedirectResponse(f"/atendimento?conversa={novo}", status_code=303)


@router.post("/atendimento/{conversa_id}/mensagem", response_class=HTMLResponse)
def enviar_mensagem(request: Request, conversa_id: int, mensagem: str = Form(""),
                    roteiro: str = Form(""), csrf: str = Form("", alias="_csrf")):
    c = core()
    bad = _csrf(request, csrf)
    if bad:
        return bad
    user, org = c.require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org

    texto = (mensagem or "").strip()
    agora = _agora()
    with db() as conn:
        atual = _conversa(conn, org["id"], conversa_id)
        if not atual:
            return RedirectResponse("/atendimento", status_code=303)
        if texto:
            conn.execute(
                """INSERT INTO chat_messages
                   (organization_id,conversation_id,role,content,model,alertas,created_at)
                   VALUES (?,?,'user',?,'','[]',?)""",
                (org["id"], conversa_id, texto, agora))
        anteriores = _mensagens(conn, org["id"], conversa_id)
        cliente_conhecido = bool(atual["client_id"])

    historico = _historico(anteriores)
    # A mensagem recém-gravada já está no histórico; o que a classificação lê
    # é a última fala do cliente, não necessariamente a que acabou de chegar
    # (num follow-up o operador manda a conversa adiante sem o cliente falar).
    base = texto or next((m["content"] for m in reversed(historico) if m["role"] == "user"), "")

    # Crédito é RESERVADO ANTES da chamada, como no Copiloto: debitar depois
    # significa pagar a API e só então descobrir que o teto do plano já tinha
    # estourado. Custo fixo porque o token só se conhece na volta.
    #
    # Negado, o atendimento NÃO para: cai no roteiro, que não chama ninguém e
    # não custa nada. Um cliente esperando resposta não é lugar de mensagem
    # sobre limite de plano.
    usar_ia = roteiro != "1" and chatbot.ia_disponivel()
    recado = ""
    if usar_ia and not c._consume_copilot_credits(
            org["id"], user["id"], "atendimento", CUSTO_EM_CREDITOS,
            f"Atendimento JARBAS — conversa #{conversa_id}"):
        usar_ia = False
        recado = ("Créditos do plano esgotados neste mês: a resposta saiu pelo roteiro, "
                  "sem IA. O conteúdo da triagem é o mesmo.")

    try:
        resposta = chatbot.responder(
            base, historico=historico[:-1] if texto else historico,
            nome=atual["contato_nome"] or "",
            cliente_conhecido=cliente_conhecido,
            observacoes=atual["observacoes"] or "",
            forcar_roteiro=not usar_ia)
    except chatbot.ChatbotError as exc:
        # Diretriz do projeto: erro de IA nunca vira conteúdo. A fala do
        # cliente já está salva; a resposta, não.
        c.log_action(request, f"Atendimento JARBAS: falha de IA na conversa #{conversa_id}")
        aviso = ("A IA não respondeu ({}). A mensagem do cliente foi registrada. "
                 "Use 'Responder pelo roteiro' para atender agora sem IA.").format(str(exc)[:180])
        return RedirectResponse(
            f"/atendimento?conversa={conversa_id}&erro={aviso}", status_code=303)

    classificacao = resposta.classificacao
    fim = _agora()
    with db() as conn:
        conn.execute(
            """INSERT INTO chat_messages
               (organization_id,conversation_id,role,content,model,alertas,
                input_tokens,output_tokens,created_at)
               VALUES (?,?,'assistant',?,?,?,?,?,?)""",
            (org["id"], conversa_id, resposta.texto, resposta.modelo,
             json.dumps(list(resposta.alertas), ensure_ascii=False),
             resposta.input_tokens, resposta.output_tokens, fim))
        linhas = _mensagens(conn, org["id"], conversa_id)
        conn.execute(
            """UPDATE chat_conversations SET fluxo=?,area=?,risco=?,resumo=?,updated_at=?
               WHERE id=? AND organization_id=?""",
            (classificacao.fluxo, classificacao.area.codigo, classificacao.risco,
             chatbot.resumo_do_atendimento(_historico(linhas), classificacao),
             fim, conversa_id, org["id"]))
    c.log_action(request, f"Atendimento JARBAS: resposta na conversa #{conversa_id} "
                          f"({resposta.modelo}, risco {classificacao.risco})")
    destino = f"/atendimento?conversa={conversa_id}"
    return RedirectResponse(destino + (f"&erro={recado}" if recado else ""), status_code=303)


@router.post("/atendimento/{conversa_id}/encerrar")
def encerrar(request: Request, conversa_id: int, status: str = Form("encerrado"),
             csrf: str = Form("", alias="_csrf")):
    c = core()
    bad = _csrf(request, csrf)
    if bad:
        return bad
    user, org = c.require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    destino = status if status in ("aberto", "encerrado", "encaminhado") else "encerrado"
    with db() as conn:
        conn.execute("UPDATE chat_conversations SET status=?,updated_at=? WHERE id=? AND organization_id=?",
                     (destino, _agora(), conversa_id, org["id"]))
    c.log_action(request, f"Atendimento JARBAS: conversa #{conversa_id} marcada como {destino}")
    return RedirectResponse(f"/atendimento?conversa={conversa_id}&ok=Situação atualizada.", status_code=303)


@router.post("/atendimento/{conversa_id}/lead")
def virar_lead(request: Request, conversa_id: int, csrf: str = Form("", alias="_csrf")):
    """Leva a triagem para o CRM sem redigitação.

    O lead nasce com o resumo MASCARADO (diretriz §17): o funil comercial
    não precisa do CPF do cliente, e a transcrição íntegra continua na
    conversa para quem tem motivo para abrir.
    """
    c = core()
    bad = _csrf(request, csrf)
    if bad:
        return bad
    user, org = c.require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    agora = _agora()
    with db() as conn:
        atual = _conversa(conn, org["id"], conversa_id)
        if not atual:
            return RedirectResponse("/atendimento", status_code=303)
        if atual["lead_id"]:
            return RedirectResponse(
                f"/atendimento?conversa={conversa_id}&ok=Esta conversa já virou lead.", status_code=303)
        area = chatbot.AREAS_POR_CODIGO.get(atual["area"] or "", chatbot.AREA_PADRAO)
        nome = (atual["contato_nome"] or "").strip() or f"Contato do atendimento #{conversa_id}"
        lead = conn.insert_id(
            """INSERT INTO leads (organization_id,name,contact_name,phone,email,source,area,
               status,estimated_fee,notes,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,'Novo',0,?,?,?)""",
            (org["id"], nome, nome, atual["contato_telefone"] or "", atual["contato_email"] or "",
             f"Atendimento JARBAS ({atual['canal'] or 'whatsapp'})", area.nome,
             chatbot.mascarar_sensiveis(atual["resumo"] or "")[:2000], agora, agora))
        conn.execute("UPDATE chat_conversations SET lead_id=?,status='encaminhado',updated_at=? "
                     "WHERE id=? AND organization_id=?", (lead, agora, conversa_id, org["id"]))
    c.log_action(request, f"Atendimento JARBAS: conversa #{conversa_id} encaminhada ao CRM (lead #{lead})")
    return RedirectResponse(f"/atendimento?conversa={conversa_id}&ok=Lead criado no CRM.", status_code=303)
