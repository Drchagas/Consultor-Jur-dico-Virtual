"""Importação de pasta de clientes: o que ela nunca pode fazer.

Este arquivo cobre três garantias, nesta ordem de importância:

1. NADA é cadastrado sem confirmação. A leitura produz proposta; a gravação
   depende do que o advogado marcou na tela. Um cadastro automático errado,
   em acervo sob sigilo profissional, não é um bug de interface: é ficha de
   cliente com o CPF de outra pessoa.
2. A pasta do escritório não é alterada. O JARBAS copia; nunca move, nunca
   apaga, nunca renomeia o original.
3. O caminho enviado pelo navegador é texto de terceiro. Ele nunca pode
   escrever fora da área do JARBAS.
"""

import os
import sys
from pathlib import Path

import pytest

_RAIZ = Path(__file__).resolve().parent.parent
RAIZ = _RAIZ / "payload" if (_RAIZ / "payload" / "app").is_dir() else _RAIZ
sys.path.insert(0, str(RAIZ))

from app import importador_pastas as IP  # noqa: E402


def _pdf(destino: Path, texto: str) -> Path:
    """PDF de verdade: PDF falso não exercita a leitura, só o tratamento de erro."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    destino.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(destino), pagesize=A4)
    y = 800
    for linha in texto.splitlines():
        c.drawString(60, y, linha[:110])
        y -= 16
    c.showPage()
    c.save()
    return destino


AUTOS = """PODER JUDICIARIO DO ESTADO DO RIO GRANDE DO SUL
2a Vara Judicial da Comarca de Canela
Processo: 5001234-56.2026.8.21.0041
Classe: Procedimento Comum Civel
Autor: JOAO DA SILVA
CPF: 123.456.789-09
Reu: EMPRESA TESTE LTDA
CNPJ: 12.345.678/0001-95
Valor da causa: R$ 25.000,00
"""


# ================================================ caminho vindo do navegador

def test_caminho_relativo_nunca_sobe_de_pasta():
    """'..' no nome do arquivo é a forma clássica de escrever fora da área."""
    for perigoso in ("../../etc/passwd", "cliente/../../fora.pdf", "..\\..\\x.pdf"):
        assert IP.caminho_relativo_seguro(perigoso) == "", perigoso


def test_caminho_relativo_perde_unidade_e_barra_inicial():
    assert IP.caminho_relativo_seguro(r"C:\Clientes\Joao\autos.pdf") == "Clientes/Joao/autos.pdf"
    assert IP.caminho_relativo_seguro("/etc/passwd") == "etc/passwd"


def test_caminho_relativo_preserva_a_estrutura_de_pastas():
    """Sem isso, o envio de pasta inteira viraria um cliente só.

    É o webkitRelativePath que distingue 'Joao/autos.pdf' de 'Maria/autos.pdf'.
    """
    assert IP.caminho_relativo_seguro("Joao da Silva/Processo 1/inicial.pdf") == \
        "Joao da Silva/Processo 1/inicial.pdf"


def test_caminho_relativo_recusa_aninhamento_absurdo():
    assert IP.caminho_relativo_seguro("/".join(["a"] * 30)) == ""


def test_caminho_relativo_recusa_byte_nulo():
    assert IP.caminho_relativo_seguro("ok/arq\x00.pdf") == ""


# ============================================ o que o nome da pasta entrega

def test_nome_da_pasta_entrega_numero_cpf_e_nome():
    dados = IP.dados_do_nome_de_pasta("012 - JOAO DA SILVA - 5001234-56.2026.8.21.0041")
    assert dados["numero"] == "5001234-56.2026.8.21.0041"
    assert "JOAO DA SILVA" in dados["nome"]

    dados = IP.dados_do_nome_de_pasta("Maria de Souza 123.456.789-09")
    assert dados["documento"] == "123.456.789-09"
    assert dados["nome"].startswith("Maria de Souza")


def test_nome_da_pasta_sem_pista_nao_inventa():
    """Pasta chamada '2026' não vira cliente chamado '2026' com CPF nenhum."""
    dados = IP.dados_do_nome_de_pasta("2026")
    assert dados["documento"] == "" and dados["numero"] == ""


# ============================================================= agrupamento

def _arq(rel):
    return IP.ArquivoLido(relativo=rel, origem=None, tamanho=1, tipo="pdf")


def test_primeiro_nivel_e_cliente_e_o_segundo_e_processo():
    grupos = IP.agrupar([
        _arq("Joao da Silva/Processo A/inicial.pdf"),
        _arq("Joao da Silva/Processo B/inicial.pdf"),
        _arq("Maria de Souza/autos.pdf"),
    ])
    assert set(grupos) == {"Joao da Silva", "Maria de Souza"}
    assert set(grupos["Joao da Silva"]) == {"Processo A", "Processo B"}
    # Cliente sem subpasta: um processo só, com chave vazia.
    assert set(grupos["Maria de Souza"]) == {""}


def test_arquivo_solto_na_raiz_nao_some():
    """Sumir com arquivo em silêncio é pior do que propor um cadastro a mais."""
    grupos = IP.agrupar([_arq("avulso.pdf")], raiz_rotulo="Clientes")
    assert grupos["Clientes"][""][0].relativo == "avulso.pdf"


def test_a_escolha_do_pdf_e_estavel_e_prefere_a_inicial():
    arquivos = [_arq("z-anexo.pdf"), _arq("peticao inicial.pdf"), _arq("a-procuracao.pdf")]
    for _ in range(3):
        assert IP._melhor_pdf(arquivos) is None  # sem origem em disco, não serve

    with_origem = [IP.ArquivoLido(relativo=a.relativo, origem=Path("/tmp") / a.relativo,
                                  tamanho=1, tipo="pdf") for a in arquivos]
    assert IP._melhor_pdf(with_origem).relativo == "peticao inicial.pdf"


# ========================================================= varredura local

def test_varredura_ignora_lixo_do_windows_e_o_que_nao_e_documento(tmp_path):
    (tmp_path / "Joao").mkdir()
    _pdf(tmp_path / "Joao" / "autos.pdf", AUTOS)
    (tmp_path / "Joao" / "Thumbs.db").write_bytes(b"x")
    (tmp_path / "Joao" / "~$rascunho.docx").write_bytes(b"x")
    (tmp_path / "Joao" / "planilha.xlsx").write_bytes(b"x")
    (tmp_path / ".oculta").mkdir()
    _pdf(tmp_path / ".oculta" / "x.pdf", "nada")

    achados, _ = IP.varrer(tmp_path)
    nomes = {a.relativo for a in achados}
    assert nomes == {"Joao/autos.pdf"}


def test_varredura_nao_segue_atalho_para_fora(tmp_path):
    """Symlink apontando para fora transformaria a varredura em leitura livre."""
    fora = tmp_path.parent / "fora_da_raiz"
    fora.mkdir(exist_ok=True)
    _pdf(fora / "segredo.pdf", "outro escritorio")
    raiz = tmp_path / "acervo"
    (raiz / "Joao").mkdir(parents=True)
    _pdf(raiz / "Joao" / "autos.pdf", AUTOS)
    try:
        (raiz / "atalho").symlink_to(fora, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("sistema de arquivos sem symlink")
    achados, _ = IP.varrer(raiz)
    assert {a.relativo for a in achados} == {"Joao/autos.pdf"}


def test_raiz_precisa_ser_caminho_completo():
    with pytest.raises(IP.PastaRecusada):
        IP.validar_raiz("Clientes")


def test_raiz_inexistente_e_recusada_com_o_caminho_na_mensagem():
    with pytest.raises(IP.PastaRecusada) as exc:
        IP.validar_raiz("/nao/existe/pasta_de_clientes")
    assert "pasta_de_clientes" in str(exc.value)


def test_a_area_de_dados_do_jarbas_nunca_e_varrida(monkeypatch, tmp_path):
    """Varrer a própria pasta do JARBAS reimportaria os autos já cadastrados.

    O resultado seria processo duplicado e documento em dobro no dossiê — sem
    nenhum erro na tela.
    """
    from app import database
    dados = tmp_path / "data"
    dados.mkdir()
    monkeypatch.setattr(database, "DATA_DIR", dados)
    with pytest.raises(IP.PastaRecusada):
        IP.validar_raiz(str(dados))
    with pytest.raises(IP.PastaRecusada):
        IP.validar_raiz(str(tmp_path))          # é pai da pasta de dados


def test_raiz_do_disco_e_recusada():
    with pytest.raises(IP.PastaRecusada):
        IP.validar_raiz("/")


def test_limite_de_raiz_configurado_e_respeitado(monkeypatch, tmp_path):
    permitida = tmp_path / "permitida"
    (permitida / "Joao").mkdir(parents=True)
    outra = tmp_path / "outra"
    outra.mkdir()
    monkeypatch.setenv("JARBAS_PASTA_CLIENTES_RAIZ", str(permitida))
    assert IP.validar_raiz(str(permitida / "Joao")) == (permitida / "Joao").resolve()
    with pytest.raises(IP.PastaRecusada):
        IP.validar_raiz(str(outra))


def test_mapeamento_vem_desligado_por_padrao(monkeypatch):
    """Em servidor com mais de um escritório isto seria leitura livre do disco."""
    monkeypatch.delenv("JARBAS_PERMITE_MAPEAR_PASTA", raising=False)
    assert IP.mapeamento_liberado() is False
    monkeypatch.setenv("JARBAS_PERMITE_MAPEAR_PASTA", "1")
    assert IP.mapeamento_liberado() is True


# ============================================================== a proposta

def test_a_proposta_sai_preenchida_a_partir_do_pdf(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / "JOAO DA SILVA").mkdir()
    _pdf(tmp_path / "JOAO DA SILVA" / "peticao inicial.pdf", AUTOS)
    achados, _ = IP.varrer(tmp_path)
    propostas = IP.analisar(IP.agrupar(achados), usar_ia=False)

    assert len(propostas) == 1
    p = propostas[0]
    assert p.cliente_nome == "JOAO DA SILVA"
    assert len(p.processos) == 1
    assert p.processos[0].numero == "5001234-56.2026.8.21.0041"
    assert p.processos[0].ia_usada is False


def test_o_cpf_so_e_atribuido_a_parte_de_mesmo_nome():
    """O erro que este teste impede: colar na ficha do cliente o CPF do réu.

    A extração devolve TODAS as partes. Pegar o documento da primeira que
    aparecer produziria uma procuração com o número do adversário — e ninguém
    conferiria, porque o campo estaria preenchido.
    """
    proposta = IP.Proposta(pasta="Joao da Silva", cliente_nome="Joao da Silva")
    processo = IP.ProcessoProposto(subpasta="")
    lido = {"parties": [
        {"name": "Empresa Teste Ltda", "document": "12.345.678/0001-95"},
        {"name": "Joao da Silva", "document": "123.456.789-09"},
    ]}
    IP._aplicar(processo, proposta,
                lido, IP.ArquivoLido(relativo="autos.pdf", origem=None))
    assert proposta.documento == "123.456.789-09"


def test_sem_parte_de_mesmo_nome_o_documento_fica_em_branco():
    proposta = IP.Proposta(pasta="Pasta 42", cliente_nome="Pasta 42")
    processo = IP.ProcessoProposto(subpasta="")
    lido = {"parties": [{"name": "Empresa Teste Ltda", "document": "12.345.678/0001-95"}]}
    IP._aplicar(processo, proposta, lido,
                IP.ArquivoLido(relativo="autos.pdf", origem=None))
    assert proposta.documento == "", "documento do adversário foi parar no cliente"


def test_pdf_corrompido_nao_derruba_a_importacao_inteira(tmp_path):
    """Um arquivo ruim no meio de 300 pastas não pode obrigar a recomeçar."""
    (tmp_path / "Joao").mkdir()
    (tmp_path / "Joao" / "quebrado.pdf").write_bytes(b"%PDF-1.4 isto nao e um pdf")
    achados, _ = IP.varrer(tmp_path)
    propostas = IP.analisar(IP.agrupar(achados), usar_ia=False)
    assert len(propostas) == 1
    assert propostas[0].processos[0].confianca in ("baixa", "media")


def test_a_ia_respeita_o_teto_da_execucao(tmp_path, monkeypatch):
    """Sem teto, mandar 300 pastas para a IA viraria uma fatura inesperada."""
    chamadas = []

    def falsa(path, *, prefer_ai=True):
        chamadas.append(prefer_ai)
        return {"parties": [], "warnings": [], "ai_used": bool(prefer_ai),
                "number": "", "title": "", "area": "", "court": ""}

    # Remenda no MESMO objeto de módulo que IP._ler_pdf de fato usa
    # (IP.smart_intake, vinculado quando importador_pastas.py foi
    # importado), não num `from app import smart_intake` novo em folha.
    # Outro arquivo de teste que recarregue app.* (há mais de um nesta
    # suíte) deixa sys.modules['app.smart_intake'] apontando para um objeto
    # DIFERENTE do que IP já carrega internamente — remendar o errado faz
    # o teste “passar” sem nunca ter tocado o código de verdade.
    monkeypatch.setattr(IP.smart_intake, "detect_case_metadata", falsa)

    for nome in ("A", "B", "C", "D"):
        (tmp_path / nome).mkdir()
        _pdf(tmp_path / nome / "autos.pdf", AUTOS)
    achados, _ = IP.varrer(tmp_path)
    IP.analisar(IP.agrupar(achados), usar_ia=True, limite_ia=2)

    assert chamadas.count(True) == 2, "o teto de leituras por IA não foi respeitado"
    assert chamadas.count(False) == 2, "as pastas restantes deveriam ser lidas localmente"


def test_a_serializacao_preserva_o_que_a_tela_precisa(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / "Joao").mkdir()
    _pdf(tmp_path / "Joao" / "autos.pdf", AUTOS)
    achados, _ = IP.varrer(tmp_path)
    dados = IP.para_json(IP.analisar(IP.agrupar(achados), usar_ia=False))
    assert dados[0]["cliente_nome"] == "Joao"
    proc = dados[0]["processos"][0]
    for campo in ("numero", "titulo", "confianca", "avisos", "arquivos", "ia_usada"):
        assert campo in proc
    assert proc["arquivos"][0]["origem"].endswith("autos.pdf")


# =================================================== o caminho inteiro na tela

EMAIL = "pastas@chagas.local"
SENHA = "SenhaDeTesteMuitoLonga1!"


@pytest.fixture()
def sistema(tmp_path, monkeypatch):
    monkeypatch.setenv("JARBAS_ENV", "development")
    monkeypatch.setenv("JARBAS_ALLOWED_HOSTS", "localhost,127.0.0.1")
    monkeypatch.setenv("JARBAS_ADMIN_EMAIL", EMAIL)
    monkeypatch.setenv("JARBAS_ADMIN_PASSWORD", SENHA)
    monkeypatch.setenv("JARBAS_2FA_OBRIGATORIO", "0")
    monkeypatch.setenv("JARBAS_DATA_DIR", str(tmp_path / "dados"))
    monkeypatch.setenv("JARBAS_PERMITE_MAPEAR_PASTA", "1")
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
    return main


@pytest.fixture()
def navegador(sistema):
    import asyncio
    import re

    import httpx

    laco = asyncio.new_event_loop()
    cliente = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=sistema.app),
        base_url="http://localhost", follow_redirects=True, timeout=120)

    class Nav:
        def get(self, caminho):
            return laco.run_until_complete(cliente.get(caminho))

        def post(self, caminho, dados=None, arquivos=None):
            corpo = dict(dados or {})
            corpo["_csrf"] = self.token()
            return laco.run_until_complete(
                cliente.post(caminho, data=corpo, files=arquivos))

        def token(self):
            corpo = laco.run_until_complete(cliente.get("/importar-pastas")).text
            return re.search(r'name="_csrf" value="([^"]+)"', corpo).group(1)

        def entrar(self):
            corpo = laco.run_until_complete(cliente.get("/login")).text
            token = re.search(r'name="_csrf" value="([^"]+)"', corpo).group(1)
            return laco.run_until_complete(cliente.post(
                "/login", data={"email": EMAIL, "password": SENHA, "_csrf": token}))

    nav = Nav()
    nav.entrar()
    yield nav, sistema
    laco.run_until_complete(cliente.aclose())
    laco.close()


def _acervo(tmp_path: Path) -> Path:
    raiz = tmp_path / "Clientes"
    _pdf(raiz / "JOAO DA SILVA" / "peticao inicial.pdf", AUTOS)
    _pdf(raiz / "Maria de Souza 987.654.321-00" / "autos.pdf",
         "Processo: 5009999-11.2026.8.21.0041\nAutora: MARIA DE SOUZA\n")
    return raiz


def test_a_leitura_da_pasta_nao_cadastra_nada(navegador, tmp_path):
    """A garantia central: ler é propor. Só o confirmar grava."""
    nav, main = navegador
    raiz = _acervo(tmp_path)
    resposta = nav.post("/importar-pastas/mapear", {"caminho": str(raiz)})
    assert resposta.status_code == 200

    with main.db() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM clients").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM cases").fetchone()["c"] == 0
        pendente = conn.execute(
            "SELECT * FROM folder_imports WHERE status='pendente'").fetchone()
    assert pendente is not None
    assert "JOAO DA SILVA" in resposta.text


def test_confirmar_cadastra_somente_o_que_foi_marcado(navegador, tmp_path):
    nav, main = navegador
    raiz = _acervo(tmp_path)
    nav.post("/importar-pastas/mapear", {"caminho": str(raiz)})
    with main.db() as conn:
        imp = conn.execute("SELECT * FROM folder_imports ORDER BY id DESC LIMIT 1").fetchone()

    import json as _json
    propostas = _json.loads(imp["propostas_json"])
    indices = {p["cliente_nome"]: i for i, p in enumerate(propostas)}
    escolhido = next(i for nome, i in indices.items() if "JOAO" in nome.upper())

    # Só a primeira pasta entra; a outra fica de fora de propósito.
    nav.post(f"/importar-pastas/{imp['id']}/confirmar", {
        "incluir": str(escolhido),
        "incluir_proc": f"{escolhido}:0",
        f"nome_{escolhido}": "João da Silva",
        f"doc_{escolhido}": "123.456.789-09",
        f"titulo_{escolhido}_0": "João da Silva x Empresa Teste Ltda",
        f"numero_{escolhido}_0": "5001234-56.2026.8.21.0041",
    })

    with main.db() as conn:
        clientes = conn.execute("SELECT * FROM clients").fetchall()
        casos = conn.execute("SELECT * FROM cases").fetchall()
        docs = conn.execute("SELECT * FROM case_documents").fetchall()
    assert [c["name"] for c in clientes] == ["João da Silva"], \
        "a pasta desmarcada não podia ter sido cadastrada"
    assert len(casos) == 1 and casos[0]["number"] == "5001234-56.2026.8.21.0041"
    assert len(docs) == 1 and docs[0]["page_count"] >= 1, "o PDF não foi indexado"


def test_a_pasta_do_escritorio_nao_e_tocada(navegador, tmp_path):
    """Mover ou apagar o original seria destruir o acervo de um escritório."""
    nav, main = navegador
    raiz = _acervo(tmp_path)
    antes = sorted(p.relative_to(raiz).as_posix() for p in raiz.rglob("*") if p.is_file())
    assinaturas = {p: p.stat().st_size for p in raiz.rglob("*.pdf")}

    nav.post("/importar-pastas/mapear", {"caminho": str(raiz)})
    with main.db() as conn:
        imp = conn.execute("SELECT * FROM folder_imports ORDER BY id DESC LIMIT 1").fetchone()
    nav.post(f"/importar-pastas/{imp['id']}/confirmar", {
        "incluir": ["0", "1"], "incluir_proc": ["0:0", "1:0"],
        "nome_0": "Um", "nome_1": "Dois",
    })

    depois = sorted(p.relative_to(raiz).as_posix() for p in raiz.rglob("*") if p.is_file())
    assert antes == depois, "arquivo sumiu da pasta de origem"
    for caminho, tamanho in assinaturas.items():
        assert caminho.is_file() and caminho.stat().st_size == tamanho


def test_o_envio_pelo_navegador_separa_os_clientes_pela_estrutura(navegador, tmp_path):
    """É o caminho relativo que distingue um cliente do outro no upload."""
    nav, main = navegador
    raiz = _acervo(tmp_path)
    arquivos = []
    for caminho in sorted(raiz.rglob("*.pdf")):
        relativo = caminho.relative_to(raiz.parent).as_posix()
        arquivos.append(("arquivos", (relativo, caminho.read_bytes(), "application/pdf")))

    resposta = nav.post("/importar-pastas/enviar", {}, arquivos)
    assert resposta.status_code == 200
    with main.db() as conn:
        imp = conn.execute("SELECT * FROM folder_imports ORDER BY id DESC LIMIT 1").fetchone()
    import json as _json
    propostas = _json.loads(imp["propostas_json"])
    # Clientes/<cliente>/<arquivo> -> o primeiro nível é 'Clientes'; o envio de
    # UMA pasta de clientes agrupa por ela.
    assert imp["origem"] == "upload"
    assert propostas, "o envio não produziu proposta nenhuma"


def test_descartar_apaga_o_que_foi_enviado_e_nada_mais(navegador, tmp_path):
    nav, main = navegador
    raiz = _acervo(tmp_path)
    arquivos = [("arquivos", ("Joao/autos.pdf",
                              (raiz / "JOAO DA SILVA" / "peticao inicial.pdf").read_bytes(),
                              "application/pdf"))]
    nav.post("/importar-pastas/enviar", {}, arquivos)
    with main.db() as conn:
        imp = conn.execute("SELECT * FROM folder_imports ORDER BY id DESC LIMIT 1").fetchone()
    staging = Path(imp["caminho"])
    assert staging.is_dir()

    nav.post(f"/importar-pastas/{imp['id']}/descartar")
    assert not staging.exists(), "o staging do envio descartado continuou ocupando disco"
    with main.db() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM clients").fetchone()["c"] == 0


def test_mapeamento_desligado_recusa_o_caminho(navegador, tmp_path, monkeypatch):
    nav, main = navegador
    monkeypatch.setenv("JARBAS_PERMITE_MAPEAR_PASTA", "0")
    raiz = _acervo(tmp_path)
    resposta = nav.post("/importar-pastas/mapear", {"caminho": str(raiz)})
    assert "não permite mapear" in resposta.text
    with main.db() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM folder_imports").fetchone()["c"] == 0
