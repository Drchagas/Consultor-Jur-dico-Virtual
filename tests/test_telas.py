"""Toda tela do sistema abre, com dados dentro.

Este arquivo existe por causa de um defeito que eu mesmo introduzi e que
chegou à máquina do escritório: ao trocar a formatação de moeda em 65 pontos,
`{{ "%.2f"|format(d['size_bytes']/1048576) }}` virou
`{{ d['size_bytes']/1048576|moeda }}`.

No Jinja o filtro liga mais forte que a aritmética. A expressão passou a ser
`d['size_bytes'] / (1048576|moeda)` — divisão de inteiro por texto. A tela
/documents devolvia 500, e o self-test da instalação, corretamente, recusou
declarar o sistema instalado.

Nada disso aparecia nos testes existentes:

- `tools/check_templates.py` procura VARIÁVEL ausente, não erro de renderização.
- Os testes de rota conferiam a fiação, não o HTML produzido.
- A suíte rodava com o banco vazio, e `{% for %}` sobre lista vazia nunca
  executa o corpo — que é justamente onde o erro morava.

Por isso aqui o banco é POVOADO antes de abrir as telas. Um `{% for %}` que
não itera não prova nada.
"""

import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

_RAIZ = Path(__file__).resolve().parent.parent
RAIZ = _RAIZ / "payload" if (_RAIZ / "payload" / "app").is_dir() else _RAIZ
sys.path.insert(0, str(RAIZ))

EMAIL = "telas@chagas.local"
SENHA = "SenhaDeTesteMuitoLonga1!"


@pytest.fixture()
def sistema(tmp_path, monkeypatch):
    """App com banco próprio, povoado como um escritório em operação."""
    monkeypatch.setenv("JARBAS_ENV", "development")
    monkeypatch.setenv("JARBAS_ALLOWED_HOSTS", "localhost,127.0.0.1")
    monkeypatch.setenv("JARBAS_ADMIN_EMAIL", EMAIL)
    monkeypatch.setenv("JARBAS_ADMIN_PASSWORD", SENHA)
    monkeypatch.setenv("JARBAS_2FA_OBRIGATORIO", "0")
    monkeypatch.setenv("JARBAS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    import app as pacote
    for modulo in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
        sys.modules.pop(modulo, None)
        nome = modulo.partition(".")[2]
        if nome and "." not in nome:
            setattr(pacote, nome, None)
            delattr(pacote, nome)

    import app.main as main
    main.init_db()
    _povoar(main, tmp_path)
    return main


def _povoar(main, tmp_path):
    """Uma linha em cada tabela que alimenta uma tela.

    Sem isto os `{% for %}` não iteram e o teste passa sem olhar o HTML que
    interessa — foi assim que o erro de precedência escapou.
    """
    hoje = date.today()
    agora = datetime.now().isoformat(timespec="seconds")
    with main.db() as c:
        org = c.execute("SELECT id FROM organizations LIMIT 1").fetchone()["id"]
        user = c.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]

        c.execute("""INSERT INTO clients (organization_id,name,document,email,phone,created_at)
                     VALUES (?,?,?,?,?,?)""",
                  (org, "João da Silva", "123.456.789-09", "joao@exemplo.br",
                   "(54) 99999-0000", agora))
        cli = c.execute("SELECT id FROM clients ORDER BY id DESC LIMIT 1").fetchone()["id"]

        c.execute("""INSERT INTO cases (organization_id,client_id,title,number,area,court,
                     status,risk,claim_value,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                  (org, cli, "João da Silva x Empresa Teste Ltda",
                   "5001234-56.2026.8.21.0041", "Cível",
                   "2ª Vara Judicial da Comarca de Canela", "Ativo", "alto", 25000.0, agora))
        caso = c.execute("SELECT id FROM cases ORDER BY id DESC LIMIT 1").fetchone()["id"]

        # Documento: é o registro que quebrou /documents.
        pdf = tmp_path / "uploads" / str(org) / str(caso)
        pdf.mkdir(parents=True, exist_ok=True)
        arquivo = pdf / "autos.pdf"
        arquivo.write_bytes(b"%PDF-1.4 conteudo")
        c.execute("""INSERT INTO case_documents (organization_id,case_id,original_name,
                     stored_name,stored_path,sha256,mime_type,size_bytes,page_count,
                     text_chars,status,created_at)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (org, caso, "autos.pdf", "autos.pdf", str(arquivo), "hash",
                   "application/pdf", 1572864, 159, 154820, "indexed", agora))

        c.execute("""INSERT INTO deadlines (organization_id,case_id,title,due_date,status,created_at)
                     VALUES (?,?,?,?,?,?)""",
                  (org, caso, "Contestação", (hoje + timedelta(days=3)).isoformat(),
                   "Pendente", agora))

        main.ensure_financial_setup(c, org)
        categoria = c.execute("SELECT id FROM financial_categories WHERE organization_id=? LIMIT 1",
                              (org,)).fetchone()
        c.execute("""INSERT INTO financial_transactions (organization_id,client_id,case_id,
                     category_id,direction,description,original_amount,
                     due_date,status,created_at)
                     VALUES (?,?,?,?,?,?,?,?,?,?)""",
                  (org, cli, caso, categoria["id"] if categoria else None, "receivable",
                   "Honorários contratuais", 7500.0,
                   (hoje + timedelta(days=10)).isoformat(), "Parcial", agora))

        c.execute("""INSERT INTO fee_contracts (organization_id,client_id,case_id,title,
                     contract_value,success_percent,status,created_at)
                     VALUES (?,?,?,?,?,?,?,?)""",
                  (org, cli, caso, "Contrato de honorários", 15000.0, 20, "Ativo", agora))

        c.execute("""INSERT INTO leads (organization_id,name,phone,email,source,status,
                     estimated_fee,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                  (org, "Maria de Souza", "(54) 98888-0000", "maria@exemplo.br",
                   "WhatsApp", "Novo", 4200.0, agora, agora))

        c.execute("""INSERT INTO activities (organization_id,case_id,client_id,title,
                     activity_type,status,due_at,created_at) VALUES (?,?,?,?,?,?,?,?)""",
                  (org, caso, cli, "Audiência de conciliação", "Audiência", "Pendente",
                   (hoje + timedelta(days=7)).isoformat(), agora))

        c.execute("""INSERT INTO time_entries (organization_id,user_id,case_id,client_id,
                     description,minutes,hourly_rate,billable,work_date,created_at)
                     VALUES (?,?,?,?,?,?,?,?,?,?)""",
                  (org, user, caso, cli, "Análise dos autos", 150, 320.0, 1,
                   hoje.isoformat(), agora))


# Todas as telas de leitura do sistema. Rota nova entra aqui.
TELAS = [
    "/", "/clients", "/cases", "/documents", "/prazos", "/prazos/simular",
    "/agenda", "/crm", "/finance", "/finance/chart", "/timesheet", "/reports",
    "/audit", "/team", "/settings", "/settings/2fa", "/billing", "/platform",
    "/intake", "/conselho", "/ai", "/produto", "/health", "/robots.txt",
    "/search?q=silva",
]


@pytest.fixture()
def cliente(sistema):
    import asyncio
    import httpx

    class Navegador:
        def __init__(self, app):
            self._loop = asyncio.new_event_loop()
            self._c = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://localhost", follow_redirects=True, timeout=60,
            )

        def get(self, caminho):
            return self._loop.run_until_complete(self._c.get(caminho))

        def entrar(self):
            corpo = self.get("/login").text
            token = re.search(r'name="_csrf" value="([^"]+)"', corpo).group(1)
            return self._loop.run_until_complete(self._c.post(
                "/login", data={"email": EMAIL, "password": SENHA, "_csrf": token}))

        def fechar(self):
            self._loop.run_until_complete(self._c.aclose())
            self._loop.close()

    nav = Navegador(sistema.app)
    nav.entrar()
    yield nav, sistema
    nav.fechar()


def test_todas_as_telas_abrem_com_dados_dentro(cliente):
    """O erro que motivou este arquivo só aparecia com dados na tabela."""
    nav, _ = cliente
    falhas = []
    for caminho in TELAS:
        try:
            r = nav.get(caminho)
            if r.status_code >= 400:
                falhas.append(f"{caminho} -> HTTP {r.status_code}")
        except Exception as exc:
            falhas.append(f"{caminho} -> {type(exc).__name__}: {exc}")
    assert not falhas, "telas com erro:\n" + "\n".join(falhas)


def test_as_telas_de_detalhe_abrem(cliente):
    """Cliente, processo, documento e copiloto: onde o advogado passa o dia."""
    nav, main = cliente
    with main.db() as c:
        cli = c.execute("SELECT id FROM clients LIMIT 1").fetchone()["id"]
        caso = c.execute("SELECT id FROM cases LIMIT 1").fetchone()["id"]
        doc = c.execute("SELECT id FROM case_documents LIMIT 1").fetchone()["id"]

    falhas = []
    for caminho in (f"/clients/{cli}", f"/cases/{caso}", f"/cases/{caso}/copilot",
                    f"/cases/{caso}/documents/{doc}", f"/cases/{caso}/analysis"):
        r = nav.get(caminho)
        if r.status_code >= 400:
            falhas.append(f"{caminho} -> HTTP {r.status_code}")
    assert not falhas, "telas de detalhe com erro:\n" + "\n".join(falhas)


def test_os_valores_saem_no_formato_brasileiro(cliente):
    """Prova que o filtro chegou ao HTML, e não só que a tela não quebrou."""
    import re as _re
    nav, _ = cliente
    corpo = nav.get("/finance").text
    assert "R$ 7.500,00" in corpo, "valor não saiu no formato brasileiro"

    # O value= de <input type="number"> TEM de ficar com ponto decimal: o
    # navegador recusa vírgula e a baixa de pagamento deixaria de funcionar.
    # Só o texto visível é conferido.
    visivel = _re.sub(r'value="[^"]*"', "", corpo)
    assert "7500.00" not in visivel, "sobrou valor em formato americano na tela"


def test_o_tamanho_do_arquivo_nao_e_tratado_como_dinheiro(cliente):
    """size_bytes/1048576 é tamanho, não moeda — e foi o que quebrou /documents."""
    nav, _ = cliente
    corpo = nav.get("/documents").text
    assert "1,50 MB" in corpo, "o tamanho do documento não saiu formatado"
    assert "R$ 1,50" not in corpo, "tamanho de arquivo exibido como dinheiro"
