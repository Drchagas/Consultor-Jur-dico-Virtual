"""Configuração da chave da IA depois que o sistema já está instalado.

Por que esta tela existe separada de /settings:

Até a 9.1 a única forma de informar a chave era (a) no meio da instalação,
quando o advogado ainda não tinha a chave em mãos, ou (b) num campo no pé do
segundo painel da tela de Configurações, visível apenas para Super Admin e
apenas quando JARBAS_ALLOW_SECRET_CONFIG=1 — condição que ninguém descobre
sozinha. Quem pulava o passo na instalação ficava sem IA sem saber o caminho
de volta, e o sistema respondia "IA ainda não configurada" sem dizer onde.

Aqui a configuração é uma tela própria, com endereço curto (/configurar-ia),
ligada em todo lugar que depende de IA, e que explica três coisas que a tela
antiga não explicava:

1. O que funciona sem chave nenhuma (quase tudo) e o que só funciona com ela.
2. Que a chave é testada ANTES de substituir a que estiver valendo.
3. Que uma variável de ambiente do Windows vence o .env.local — a causa da
   situação em que o operador corrige o arquivo, reinicia, e nada muda.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import ai_gateway as G

router = APIRouter()


def _main():
    from . import main as m
    return m


def pode_configurar(user) -> bool:
    """Mesma regra de /settings: Super Admin e instalação que permite segredo.

    Em SaaS a variável fica em 0 e a chave é do provedor; na instalação do
    escritório o instalador grava 1, e o Super Admin é o próprio advogado.
    """
    return bool(user and user["is_superadmin"]
                and os.getenv("JARBAS_ALLOW_SECRET_CONFIG", "0") == "1")


@router.get("/configurar-ia", response_class=HTMLResponse)
def configurar_ia(request: Request):
    c = _main()
    user, org = c.require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    # O contexto é montado aqui, e não numa função auxiliar, porque
    # tools/check_templates.py lê a CHAMADA para descobrir o que o template
    # recebe. Escondido atrás de um helper, ele não enxerga — e variável de
    # template faltando é HTTP 500 numa tela, que foi como três rotas caíram
    # na 9.0.
    return c.safe_template_response("configurar_ia.html", c.common_context(
        request, user, org,
        ai_status=G.connection_status(),
        ai_origem=G.origem_da_chave(),
        ai_saneada=G.chave_saneada(),
        ai_modelos_ignorados=G.modelos_ignorados(),
        pode_configurar=pode_configurar(user),
        flash=request.session.pop("ia_flash", ""),
        flash_ok=bool(request.session.pop("ia_flash_ok", False)),
    ))


def _falhou(request: Request, mensagem: str):
    request.session["ia_flash"] = mensagem[:600]
    request.session["ia_flash_ok"] = False
    return RedirectResponse("/configurar-ia", status_code=303)


@router.post("/configurar-ia/chave")
def salvar_chave(request: Request, api_key: str = Form(""),
                 csrf: str = Form("", alias="_csrf")):
    c = _main()
    if not c.valid_csrf(request, csrf):
        return c.csrf_error()
    user, org = c.require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    if not pode_configurar(user):
        return _falhou(request, "Somente o administrador desta instalação pode "
                                "alterar a chave da IA.")
    try:
        limpa = G.validar_chave(api_key)
    except G.ChaveInvalida as exc:
        return _falhou(request, str(exc))

    # Testa ANTES de gravar. Substituir uma chave que funciona por uma que não
    # funciona deixaria o escritório sem IA sem nenhum aviso — e a chave antiga
    # não teria como ser recuperada, porque o console da Anthropic só mostra
    # cada chave uma vez.
    try:
        resultado = G.test_credentials(limpa, G.routine_model_name())
    except Exception as exc:
        return _falhou(request, "A chave NÃO foi salva porque o teste falhou: "
                                + G.friendly_error(exc))
    try:
        G.salvar_chave(limpa)
    except Exception as exc:
        return _falhou(request, f"O teste passou, mas a gravação falhou: {exc}")

    aviso = ""
    if G.origem_da_chave() == "conflito":
        aviso = (" ATENÇÃO: existe uma variável ANTHROPIC_API_KEY no ambiente do "
                 "Windows com valor DIFERENTE. Ela vence o arquivo quando o "
                 "JARBAS reinicia. Remova-a para a chave nova valer sempre — o "
                 "DIAGNOSTICO_JARBAS.cmd mostra em qual escopo ela está.")
    request.session["ia_flash"] = (
        f"Chave validada e salva. O teste respondeu com {resultado.model}." + aviso)
    request.session["ia_flash_ok"] = not aviso
    c.log_action(request, "Chave da Anthropic salva pela tela Configurar IA")
    return RedirectResponse("/configurar-ia", status_code=303)


@router.post("/configurar-ia/testar")
def testar(request: Request, csrf: str = Form("", alias="_csrf")):
    c = _main()
    if not c.valid_csrf(request, csrf):
        return c.csrf_error()
    user, org = c.require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    try:
        resultado = G.test_connection()
        request.session["ia_flash"] = f"Conexão com a Claude confirmada ({resultado.model})."
        request.session["ia_flash_ok"] = True
    except Exception as exc:
        request.session["ia_flash"] = "O teste falhou: " + G.friendly_error(exc)
        request.session["ia_flash_ok"] = False
    return RedirectResponse("/configurar-ia", status_code=303)


@router.post("/configurar-ia/modelos")
def salvar_modelos(request: Request, legal_model: str = Form(""),
                   intake_model: str = Form(""), routine_model: str = Form(""),
                   csrf: str = Form("", alias="_csrf")):
    c = _main()
    if not c.valid_csrf(request, csrf):
        return c.csrf_error()
    user, org = c.require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    if not pode_configurar(user):
        return _falhou(request, "Somente o administrador desta instalação pode "
                                "alterar os modelos.")
    escolhidos = {
        "JARBAS_AI_MODEL_LEGAL": (legal_model, G.PADRAO_LEGAL),
        "JARBAS_AI_MODEL_INTAKE": (intake_model, G.PADRAO_INTAKE),
        "JARBAS_AI_MODEL_ROUTINE": (routine_model, G.PADRAO_ROTINA),
    }
    for nome, (valor, padrao) in escolhidos.items():
        if not G.modelo_suportado(valor):
            escolhidos[nome] = (padrao, padrao)
    try:
        G.save_local_config(
            api_key="",
            legal_model=escolhidos["JARBAS_AI_MODEL_LEGAL"][0],
            intake_model=escolhidos["JARBAS_AI_MODEL_INTAKE"][0],
            routine_model=escolhidos["JARBAS_AI_MODEL_ROUTINE"][0],
        )
    except Exception as exc:
        return _falhou(request, f"Não consegui gravar os modelos: {exc}")
    request.session["ia_flash"] = "Modelos atualizados nesta instalação."
    request.session["ia_flash_ok"] = True
    c.log_action(request, "Modelos de IA alterados pela tela Configurar IA")
    return RedirectResponse("/configurar-ia", status_code=303)


@router.post("/configurar-ia/remover")
def remover(request: Request, csrf: str = Form("", alias="_csrf")):
    c = _main()
    if not c.valid_csrf(request, csrf):
        return c.csrf_error()
    user, org = c.require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    if not pode_configurar(user):
        return _falhou(request, "Somente o administrador desta instalação pode "
                                "remover a chave da IA.")
    G.clear_local_api_key()
    resto = "ambiente" if G.origem_da_chave() == "ambiente" else ""
    request.session["ia_flash"] = (
        "Chave removida do .env.local desta instalação."
        + (" Ainda existe uma ANTHROPIC_API_KEY no ambiente do Windows: "
           "enquanto ela existir, a IA continua ligada. Remova-a pelo painel "
           "de variáveis de ambiente do Windows." if resto else ""))
    request.session["ia_flash_ok"] = not resto
    c.log_action(request, "Chave da Anthropic removida pela tela Configurar IA")
    return RedirectResponse("/configurar-ia", status_code=303)
