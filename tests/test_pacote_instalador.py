"""O pacote de instalação bate com o que o .ps1 espera?

Regressão da falha em [1/19] "Manifesto SHA-256 do pacote nao encontrado":
o instalador foi executado a partir da árvore de código-fonte, que tem um
layout diferente do pacote de distribuição. Estes testes montam o pacote de
verdade e reproduzem as checagens do instalador.
"""

import re
import sys
import tempfile
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
# A árvore de fontes tem app/ na raiz; o pacote de instalação tem payload/app/.
sys.path.insert(0, str(_RAIZ / "payload" if (_RAIZ / "payload" / "app").is_dir() else _RAIZ))

import pytest

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "tools"))
import build_installer as B  # noqa: E402

# Estes testes montam o pacote a partir da ÁRVORE DE FONTES. Dentro de um
# pacote já montado não há installer/, então a suíte é ignorada.
_PS1 = sorted((RAIZ / "installer").glob("INSTALAR_JARBAS_*.ps1")) \
    if (RAIZ / "installer").is_dir() else []
if not _PS1:
    print("   (pulado: sem installer/ — rode da árvore de fontes)")
    raise SystemExit(0)
PS1_FONTE = _PS1[0]
PS1_TEXTO = PS1_FONTE.read_text(encoding="utf-8-sig")


@pytest.fixture(autouse=True)
def pacote():
    global PKG, _tmp
    _tmp = tempfile.TemporaryDirectory()
    PKG = B.montar(Path(_tmp.name) / "pkg")
    yield
    _tmp.cleanup()


# ------------------------------------------------- pré-condições do .ps1

def test_manifesto_existe_com_o_nome_exato_que_o_ps1_procura():
    nome = re.search(r"\$PackageManifest=Join-Path \$SourceDir '([^']+)'",
                     PS1_TEXTO).group(1)
    assert (PKG / nome).is_file(), (
        f"o instalador procura {nome}; sem ele falha em [1/19]")


def test_pastas_que_o_ps1_exige_existem():
    for var in ("Payload", "ToolsSource", "ScriptsSource"):
        nome = re.search(rf"\${var}=Join-Path \$SourceDir '([^']+)'",
                         PS1_TEXTO).group(1)
        assert (PKG / nome).is_dir(), f"pasta '{nome}' ausente no pacote"


def test_payload_tem_a_aplicacao():
    assert (PKG / "payload" / "app" / "main.py").is_file()
    assert (PKG / "payload" / "app" / "ai_council.py").is_file()
    assert (PKG / "payload" / "requirements.txt").is_file()


def test_cmd_aponta_para_o_ps1_que_existe():
    cmd = (PKG / "INSTALAR_AGORA.cmd").read_text(encoding="utf-8", errors="replace")
    referenciado = re.search(r"(INSTALAR_JARBAS_[\w.]+\.ps1)", cmd).group(1)
    assert (PKG / referenciado).is_file(), (
        f"INSTALAR_AGORA.cmd chama {referenciado}, que não está no pacote")


# ------------------------------------------------------------- manifesto

def test_manifesto_confere_arquivo_por_arquivo():
    assert B.conferir(PKG) == 0


def test_manifesto_usa_barra_invertida_para_o_join_path_do_windows():
    linhas = (PKG / "PACOTE_MANIFEST_SHA256.txt").read_text(
        encoding="utf-8").splitlines()
    aninhados = [l for l in linhas if "payload" in l]
    assert aninhados, "manifesto sem entradas do payload"
    assert all("\\" in l.split("  ", 1)[1] for l in aninhados)


def test_manifesto_nao_lista_a_si_mesmo():
    texto = (PKG / "PACOTE_MANIFEST_SHA256.txt").read_text(encoding="utf-8")
    assert "PACOTE_MANIFEST_SHA256.txt" not in texto, (
        "manifesto listando a si mesmo nunca confere: o hash muda ao ser escrito")


def test_manifesto_cobre_todo_arquivo_do_pacote():
    listados = {l.split("  ", 1)[1].replace("\\", "/")
                for l in (PKG / "PACOTE_MANIFEST_SHA256.txt")
                .read_text(encoding="utf-8").splitlines() if l.strip()}
    reais = {p.relative_to(PKG).as_posix() for p in PKG.rglob("*")
             if p.is_file() and p.name != "PACOTE_MANIFEST_SHA256.txt"}
    assert reais - listados == set(), f"fora do manifesto: {sorted(reais - listados)}"


# ------------------------------------------------------------ consistência

def test_docs_que_o_ps1_copia_estao_no_payload():
    """O .ps1 tem lista fixa de docs; um nome fora de sincronia some na instalação."""
    bloco = re.search(r"foreach\(\$name in @\(([^)]*)\)\)\{\$src=Join-Path \$Payload",
                      PS1_TEXTO).group(1)
    for nome in re.findall(r"'([^']+)'", bloco):
        assert (PKG / "payload" / nome).is_file(), (
            f"o instalador copia '{nome}' de payload/, mas ele não está lá")


def test_versao_do_pacote_bate_com_o_nome_do_ps1():
    versao = (PKG / "payload" / "VERSION.txt").read_text(encoding="utf-8").strip()
    esperado = f"INSTALAR_JARBAS_{versao.replace('.', '_')}.ps1"
    assert (PKG / esperado).is_file(), f"esperava {esperado} para a versão {versao}"


def test_nenhum_segredo_vazou_para_o_pacote():
    proibidos = {".env", ".env.local"}
    achados = [p.relative_to(PKG).as_posix() for p in PKG.rglob("*")
               if p.name in proibidos or p.suffix in {".db", ".sqlite3"}]
    assert not achados, f"arquivo sensível dentro do pacote: {achados}"


def test_nenhum_pycache_no_pacote():
    assert not list(PKG.rglob("__pycache__")), "__pycache__ quebra o manifesto"
