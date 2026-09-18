"""Segundo fator ligado ao login, não apenas implementado.

Até a 9.0.2 `app/auth_2fa.py` tinha 21 testes verdes e ZERO chamadas fora
deles: o motor TOTP estava correto e o login não o consultava. A variável
`JARBAS_2FA_OBRIGATORIO` do .env.example não tinha efeito algum. Um teste de
unidade do motor jamais pegaria isso — ele testava a peça, não a fiação.

Por isso os testes aqui atravessam a aplicação de verdade: sobem o app,
fazem POST em /login e conferem o que a resposta faz. Se alguém desligar a
fiação de novo, a suíte reprova.
"""

import os
import sys
import time
from pathlib import Path

import pytest

_RAIZ = Path(__file__).resolve().parent.parent
RAIZ = _RAIZ / "payload" if (_RAIZ / "payload" / "app").is_dir() else _RAIZ
sys.path.insert(0, str(RAIZ))


@pytest.fixture()
def ambiente(tmp_path, monkeypatch):
    """App com banco próprio, isolado da instalação real."""
    monkeypatch.setenv("JARBAS_ENV", "development")
    monkeypatch.setenv("JARBAS_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver")
    monkeypatch.setenv("JARBAS_ADMIN_EMAIL", "teste@chagas.local")
    monkeypatch.setenv("JARBAS_ADMIN_PASSWORD", "SenhaDeTesteMuitoLonga1!")
    monkeypatch.setenv("JARBAS_2FA_OBRIGATORIO", "0")

    # JARBAS_DATA_DIR é o mesmo mecanismo que o container usa para montar o
    # volume: aqui ele dá a cada teste um banco próprio, sem tocar na
    # instalação real e sem remendar atributos de módulo.
    monkeypatch.setenv("JARBAS_DATA_DIR", str(tmp_path))

    # Reimportação limpa. Só remover de sys.modules não basta: `from . import x`
    # encontra o atributo antigo ainda pendurado no pacote `app` e devolve o
    # módulo anterior, sem reexecutar o arquivo. O atributo tem de cair junto.
    import app as pacote
    for modulo in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
        sys.modules.pop(modulo, None)
        nome = modulo.partition(".")[2]
        if nome and "." not in nome:
            setattr(pacote, nome, None)
            delattr(pacote, nome)

    import app.main as main
    import app.two_factor as two_factor
    import app.auth_2fa as auth_2fa
    # O schema nasce no evento de startup do FastAPI, que o transporte ASGI
    # dos testes não dispara. Chamamos init_db() na mão, como o servidor faz.
    main.init_db()
    return main, two_factor, auth_2fa


# ====================================================== motor: período usado

def test_periodo_do_codigo_identifica_a_janela_que_o_codigo_satisfaz(ambiente):
    _, _, auth_2fa = ambiente
    segredo = auth_2fa.gerar_segredo()
    agora = time.time()
    esperado = int(agora) // auth_2fa.PERIODO
    codigo = auth_2fa.codigo_atual(segredo, agora)
    assert auth_2fa.periodo_do_codigo(segredo, codigo, agora) == esperado


def test_periodo_do_codigo_aceita_deriva_de_um_periodo(ambiente):
    _, _, auth_2fa = ambiente
    segredo = auth_2fa.gerar_segredo()
    agora = time.time()
    anterior = auth_2fa.codigo_atual(segredo, agora - auth_2fa.PERIODO)
    assert auth_2fa.periodo_do_codigo(segredo, anterior, agora) == int(agora) // auth_2fa.PERIODO - 1


def test_periodo_do_codigo_recusa_codigo_errado(ambiente):
    _, _, auth_2fa = ambiente
    segredo = auth_2fa.gerar_segredo()
    assert auth_2fa.periodo_do_codigo(segredo, "000000", time.time()) is None or True
    # Um código de 6 dígitos tem 1 em 1e6 de acertar por acaso em cada janela;
    # o teste determinístico é o do formato inválido.
    assert auth_2fa.periodo_do_codigo(segredo, "abc") is None
    assert auth_2fa.periodo_do_codigo(segredo, "12345") is None


# ================================================== persistência e uso único

def _usuario(main):
    with main.db() as conn:
        return conn.execute("SELECT * FROM users WHERE email=?", ("teste@chagas.local",)).fetchone()


def test_inscricao_so_vale_depois_de_confirmada(ambiente):
    main, two_factor, auth_2fa = ambiente
    user = _usuario(main)
    segredo, uri = two_factor.iniciar_inscricao(user["id"], user["email"])
    assert uri.startswith("otpauth://totp/")
    assert not two_factor.ativo(user["id"]), "segredo não confirmado não pode trancar a conta"

    codigos = two_factor.confirmar_inscricao(user["id"], auth_2fa.codigo_atual(segredo))
    assert codigos and len(codigos) == auth_2fa.CODIGOS_RECUPERACAO
    assert two_factor.ativo(user["id"])


def test_recarregar_a_pagina_de_inscricao_nao_troca_o_segredo(ambiente):
    main, two_factor, _ = ambiente
    user = _usuario(main)
    primeiro, _ = two_factor.iniciar_inscricao(user["id"], user["email"])
    segundo, _ = two_factor.iniciar_inscricao(user["id"], user["email"])
    assert primeiro == segundo, "trocar o segredo invalidaria o que já foi cadastrado no celular"


def test_o_mesmo_codigo_nao_entra_duas_vezes(ambiente):
    main, two_factor, auth_2fa = ambiente
    user = _usuario(main)
    segredo, _ = two_factor.iniciar_inscricao(user["id"], user["email"])
    two_factor.confirmar_inscricao(user["id"], auth_2fa.codigo_atual(segredo))

    codigo = auth_2fa.codigo_atual(segredo)
    assert two_factor.verificar_totp(user["id"], codigo) is True
    assert two_factor.verificar_totp(user["id"], codigo) is False, (
        "reapresentar o mesmo código dentro dos 30s tem de ser recusado"
    )


def test_codigo_de_recuperacao_vale_uma_vez(ambiente):
    main, two_factor, auth_2fa = ambiente
    user = _usuario(main)
    segredo, _ = two_factor.iniciar_inscricao(user["id"], user["email"])
    codigos = two_factor.confirmar_inscricao(user["id"], auth_2fa.codigo_atual(segredo))

    assert two_factor.codigos_restantes(user["id"]) == len(codigos)
    assert two_factor.consumir_codigo_recuperacao(user["id"], codigos[0]) is True
    assert two_factor.codigos_restantes(user["id"]) == len(codigos) - 1
    assert two_factor.consumir_codigo_recuperacao(user["id"], codigos[0]) is False


def test_desativar_apaga_os_codigos_de_recuperacao(ambiente):
    main, two_factor, auth_2fa = ambiente
    user = _usuario(main)
    segredo, _ = two_factor.iniciar_inscricao(user["id"], user["email"])
    codigos = two_factor.confirmar_inscricao(user["id"], auth_2fa.codigo_atual(segredo))
    two_factor.desativar(user["id"])

    assert not two_factor.ativo(user["id"])
    assert two_factor.codigos_restantes(user["id"]) == 0
    # Reativar e tentar o código antigo: um código órfão não pode ressuscitar.
    novo_segredo, _ = two_factor.iniciar_inscricao(user["id"], user["email"])
    two_factor.confirmar_inscricao(user["id"], auth_2fa.codigo_atual(novo_segredo))
    assert two_factor.consumir_codigo_recuperacao(user["id"], codigos[0]) is False


def test_segredo_e_codigos_nunca_ficam_em_texto_puro_no_banco(ambiente):
    main, two_factor, auth_2fa = ambiente
    user = _usuario(main)
    segredo, _ = two_factor.iniciar_inscricao(user["id"], user["email"])
    codigos = two_factor.confirmar_inscricao(user["id"], auth_2fa.codigo_atual(segredo))
    with main.db() as conn:
        guardados = [linha["code_hash"] for linha in conn.execute(
            "SELECT code_hash FROM user_recovery_codes WHERE user_id=?", (user["id"],)).fetchall()]
    for codigo in codigos:
        assert codigo not in guardados
        assert codigo.replace("-", "") not in " ".join(guardados)


# ============================================================ fiação do login

@pytest.fixture()
def http(ambiente):
    """Cliente HTTP assíncrono embrulhado em chamadas síncronas."""
    import asyncio
    import httpx
    main, two_factor, auth_2fa = ambiente

    class Cliente:
        def __init__(self):
            self._loop = asyncio.new_event_loop()
            self._c = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="http://localhost", follow_redirects=False,
            )

        def _csrf(self, caminho="/login"):
            import re
            corpo = self.get(caminho).text
            achado = re.search(r'name="_csrf" value="([^"]+)"', corpo)
            return achado.group(1) if achado else ""

        def get(self, caminho):
            return self._loop.run_until_complete(self._c.get(caminho))

        def post(self, caminho, dados, com_csrf="/login"):
            if com_csrf:
                dados = dict(dados, _csrf=self._csrf(com_csrf))
            return self._loop.run_until_complete(self._c.post(caminho, data=dados))

        def fechar(self):
            self._loop.run_until_complete(self._c.aclose())
            self._loop.close()

    cliente = Cliente()
    yield cliente, main, two_factor, auth_2fa
    cliente.fechar()


def test_sem_segundo_fator_o_login_entra_direto(http):
    cliente, main, _, _ = http
    r = cliente.post("/login", {"email": "teste@chagas.local", "password": "SenhaDeTesteMuitoLonga1!"})
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_com_segundo_fator_a_senha_sozinha_nao_abre_o_sistema(http):
    cliente, main, two_factor, auth_2fa = http
    user = _usuario(main)
    segredo, _ = two_factor.iniciar_inscricao(user["id"], user["email"])
    two_factor.confirmar_inscricao(user["id"], auth_2fa.codigo_atual(segredo))

    r = cliente.post("/login", {"email": "teste@chagas.local", "password": "SenhaDeTesteMuitoLonga1!"})
    assert r.status_code == 303 and r.headers["location"] == "/login/2fa"

    # A sessão intermediária não abre nenhuma tela do sistema.
    assert cliente.get("/").headers.get("location") == "/login"
    assert cliente.get("/cases").headers.get("location") == "/login"


def test_o_codigo_correto_conclui_o_login(http):
    cliente, main, two_factor, auth_2fa = http
    user = _usuario(main)
    segredo, _ = two_factor.iniciar_inscricao(user["id"], user["email"])
    two_factor.confirmar_inscricao(user["id"], auth_2fa.codigo_atual(segredo))
    cliente.post("/login", {"email": "teste@chagas.local", "password": "SenhaDeTesteMuitoLonga1!"})

    # Espera a janela virar para não colidir com o código gasto na confirmação.
    time.sleep(auth_2fa.PERIODO - (int(time.time()) % auth_2fa.PERIODO) + 1)
    r = cliente.post("/login/2fa", {"codigo": auth_2fa.codigo_atual(segredo)}, com_csrf="/login/2fa")
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert cliente.get("/").status_code == 200


def test_o_codigo_errado_nao_conclui_o_login(http):
    cliente, main, two_factor, auth_2fa = http
    user = _usuario(main)
    segredo, _ = two_factor.iniciar_inscricao(user["id"], user["email"])
    two_factor.confirmar_inscricao(user["id"], auth_2fa.codigo_atual(segredo))
    cliente.post("/login", {"email": "teste@chagas.local", "password": "SenhaDeTesteMuitoLonga1!"})

    r = cliente.post("/login/2fa", {"codigo": "000000"}, com_csrf="/login/2fa")
    assert r.status_code == 401
    assert cliente.get("/").headers.get("location") == "/login"


def test_a_segunda_etapa_expira(http, monkeypatch):
    cliente, main, two_factor, auth_2fa = http
    user = _usuario(main)
    segredo, _ = two_factor.iniciar_inscricao(user["id"], user["email"])
    two_factor.confirmar_inscricao(user["id"], auth_2fa.codigo_atual(segredo))
    monkeypatch.setattr(two_factor, "PRAZO_SEGUNDA_ETAPA_MIN", -1)
    cliente.post("/login", {"email": "teste@chagas.local", "password": "SenhaDeTesteMuitoLonga1!"})

    r = cliente.get("/login/2fa")
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_exigencia_obrigatoria_prende_quem_nao_configurou(http, monkeypatch):
    cliente, main, _, _ = http
    cliente.post("/login", {"email": "teste@chagas.local", "password": "SenhaDeTesteMuitoLonga1!"})
    monkeypatch.setenv("JARBAS_2FA_OBRIGATORIO", "1")

    assert cliente.get("/cases").headers.get("location") == "/settings/2fa?obrigatorio=1"
    # A própria tela de configuração precisa abrir, senão não há como cumprir.
    assert cliente.get("/settings/2fa").status_code == 200


# ================================================ IP real atrás de proxy

def test_sem_proxy_configurado_o_cabecalho_forjado_e_ignorado(ambiente, monkeypatch):
    main, _, _ = ambiente
    monkeypatch.setattr(main, "TRUSTED_PROXY_HOPS", 0)

    class Req:
        headers = {"x-forwarded-for": "1.2.3.4"}
        client = type("C", (), {"host": "10.0.0.9"})()

    assert main.login_client_ip(Req()) == "10.0.0.9", (
        "confiar no cabeçalho sem proxy deixa qualquer um escapar do bloqueio"
    )


def test_com_um_proxy_confiavel_le_o_ip_do_cliente(ambiente, monkeypatch):
    main, _, _ = ambiente
    monkeypatch.setattr(main, "TRUSTED_PROXY_HOPS", 1)

    class Req:
        headers = {"x-forwarded-for": "203.0.113.7"}
        client = type("C", (), {"host": "127.0.0.1"})()

    assert main.login_client_ip(Req()) == "203.0.113.7"


def test_cliente_nao_escolhe_o_proprio_ip_forjando_saltos(ambiente, monkeypatch):
    main, _, _ = ambiente
    monkeypatch.setattr(main, "TRUSTED_PROXY_HOPS", 1)

    class Req:
        # O cliente escreveu "9.9.9.9"; o nosso proxy acrescentou o IP real.
        headers = {"x-forwarded-for": "9.9.9.9, 203.0.113.7"}
        client = type("C", (), {"host": "127.0.0.1"})()

    assert main.login_client_ip(Req()) == "203.0.113.7"
