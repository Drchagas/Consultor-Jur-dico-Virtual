"""Backup que não foi restaurado não é backup.

Estes testes existem porque a falha de um backup é silenciosa por natureza:
o script roda todo dia, o arquivo aparece no disco, o operador relaxa — e a
corrupção só se revela no dia do incidente, quando não há mais o que fazer.

Por isso aqui não basta "o arquivo foi criado". Cada teste ou restaura de
verdade e confere o conteúdo, ou adultera a cópia e exige que o script
RECUSE.
"""

import importlib
import os
import sqlite3
import sys
import tarfile
from pathlib import Path

import pytest

_RAIZ = Path(__file__).resolve().parent.parent
RAIZ = _RAIZ / "payload" if (_RAIZ / "payload" / "app").is_dir() else _RAIZ
sys.path.insert(0, str(RAIZ))

TABELAS = """
CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT);
CREATE TABLE organizations (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE cases (id INTEGER PRIMARY KEY, title TEXT);
CREATE TABLE clients (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE case_documents (id INTEGER PRIMARY KEY, original_name TEXT);
"""


@pytest.fixture()
def backup(tmp_path, monkeypatch):
    """Módulo de backup apontado para dados descartáveis."""
    dados = tmp_path / "dados"
    copias = tmp_path / "copias"
    dados.mkdir()

    banco = dados / "jarbas.db"
    conn = sqlite3.connect(banco)
    conn.executescript(TABELAS)
    conn.execute("INSERT INTO users (id,email) VALUES (1,'dr@chagas.local')")
    conn.execute("INSERT INTO organizations (id,name) VALUES (1,'CHAGAS – ADVOGADOS')")
    conn.execute("INSERT INTO cases (id,title) VALUES (1,'Processo sigiloso')")
    conn.execute("INSERT INTO clients (id,name) VALUES (1,'Cliente sigiloso')")
    conn.commit()
    conn.close()

    autos = dados / "uploads" / "1" / "1"
    autos.mkdir(parents=True)
    (autos / "autos.pdf").write_bytes(b"%PDF-1.4 conteudo dos autos")

    monkeypatch.setenv("JARBAS_DATA_DIR", str(dados))
    monkeypatch.setenv("JARBAS_BACKUP_DIR", str(copias))
    monkeypatch.setenv("JARBAS_BACKUP_MANTER", "3")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    sys.path.insert(0, str(RAIZ / "tools"))
    modulo = importlib.import_module("backup")
    modulo = importlib.reload(modulo)
    return modulo, dados, copias


def test_a_copia_contem_banco_e_documentos(backup):
    modulo, dados, copias = backup
    pacote = modulo.gerar()

    with tarfile.open(pacote, "r:gz") as tar:
        nomes = tar.getnames()
    assert "jarbas.db" in nomes
    assert "MANIFESTO.txt" in nomes
    assert any(n.startswith("uploads") for n in nomes), (
        "sem os PDFs, o banco restaurado aponta para autos que não existem mais"
    )


def test_a_copia_recem_gerada_e_conferida(backup):
    modulo, _, _ = backup
    pacote = modulo.gerar()
    ok, detalhe = modulo.verificar(pacote)
    assert ok, detalhe
    assert "1 usuário(s)" in detalhe and "1 processo(s)" in detalhe


def test_pacote_adulterado_e_recusado(backup):
    modulo, _, _ = backup
    pacote = modulo.gerar()
    # Um byte trocado no meio do arquivo: o .sha256 gravado ao lado não bate.
    conteudo = bytearray(pacote.read_bytes())
    conteudo[len(conteudo) // 2] ^= 0xFF
    pacote.write_bytes(bytes(conteudo))

    ok, detalhe = modulo.verificar(pacote)
    assert not ok
    assert "SHA256" in detalhe or "ilegível" in detalhe


def test_banco_corrompido_dentro_do_pacote_e_recusado(backup, tmp_path):
    modulo, _, copias = backup
    falso = tmp_path / "jarbas.db"
    falso.write_bytes(b"isto nao e um banco SQLite")
    pacote = copias / "jarbas-99999999-999999.tar.gz"
    copias.mkdir(parents=True, exist_ok=True)
    with tarfile.open(pacote, "w:gz") as tar:
        tar.add(falso, arcname="jarbas.db")

    ok, detalhe = modulo.verificar(pacote)
    assert not ok, "um arquivo que não abre como banco não pode passar por backup válido"
    assert "SQLite" in detalhe or "tabelas" in detalhe


def test_banco_sem_as_tabelas_do_jarbas_e_recusado(backup, tmp_path):
    modulo, _, copias = backup
    outro = tmp_path / "jarbas.db"
    conn = sqlite3.connect(outro)
    conn.execute("CREATE TABLE qualquer_coisa (x INTEGER)")
    conn.commit()
    conn.close()

    pacote = copias / "jarbas-88888888-888888.tar.gz"
    copias.mkdir(parents=True, exist_ok=True)
    with tarfile.open(pacote, "w:gz") as tar:
        tar.add(outro, arcname="jarbas.db")

    ok, detalhe = modulo.verificar(pacote)
    assert not ok
    assert "tabelas ausentes" in detalhe


def test_restauracao_devolve_banco_e_documentos(backup):
    modulo, dados, _ = backup
    pacote = modulo.gerar()

    # Desastre: o diretório de dados inteiro desaparece.
    import shutil
    shutil.rmtree(dados)
    assert not dados.exists()

    assert modulo.restaurar(pacote, confirmado=True) == 0

    conn = sqlite3.connect(dados / "jarbas.db")
    try:
        assert conn.execute("SELECT name FROM clients").fetchone()[0] == "Cliente sigiloso"
    finally:
        conn.close()
    assert (dados / "uploads" / "1" / "1" / "autos.pdf").read_bytes().startswith(b"%PDF")


def test_restauracao_sem_confirmacao_nao_toca_nos_dados(backup):
    modulo, dados, _ = backup
    pacote = modulo.gerar()
    conn = sqlite3.connect(dados / "jarbas.db")
    conn.execute("UPDATE clients SET name='Nome mais recente' WHERE id=1")
    conn.commit()
    conn.close()

    assert modulo.restaurar(pacote, confirmado=False) == 2

    conn = sqlite3.connect(dados / "jarbas.db")
    try:
        assert conn.execute("SELECT name FROM clients").fetchone()[0] == "Nome mais recente"
    finally:
        conn.close()


def test_restauracao_preserva_o_estado_anterior(backup):
    modulo, dados, _ = backup
    pacote = modulo.gerar()
    (dados / "arquivo_que_so_existe_agora.txt").write_text("antes da restauração")

    modulo.restaurar(pacote, confirmado=True)

    salvaguardas = list(dados.parent.glob(f"{dados.name}-antes-da-restauracao-*"))
    assert salvaguardas, "restaurar a cópia errada não pode ser irreversível"
    assert (salvaguardas[0] / "arquivo_que_so_existe_agora.txt").is_file()


def test_retencao_apaga_as_mais_antigas_e_mantem_o_limite(backup):
    modulo, _, copias = backup
    copias.mkdir(parents=True, exist_ok=True)
    for i in range(6):
        alvo = copias / f"jarbas-2026010{i}-120000.tar.gz"
        alvo.write_bytes(b"copia antiga")
        alvo.with_name(alvo.name + ".sha256").write_text("x  y\n")

    removidas = modulo.aplicar_retencao()
    restantes = sorted(p.name for p in copias.glob("jarbas-*.tar.gz"))
    assert len(restantes) == 3, restantes
    assert len(removidas) == 3
    # As que sobram são as mais NOVAS.
    assert restantes == ["jarbas-20260103-120000.tar.gz",
                         "jarbas-20260104-120000.tar.gz",
                         "jarbas-20260105-120000.tar.gz"]


def test_o_manifesto_avisa_sobre_sigilo(backup):
    modulo, _, _ = backup
    pacote = modulo.gerar()
    with tarfile.open(pacote, "r:gz") as tar:
        texto = tar.extractfile("MANIFESTO.txt").read().decode("utf-8")
    assert "SIGILO PROFISSIONAL" in texto
    assert "LGPD" in texto


def test_a_copia_do_banco_nao_fica_legivel_para_outros_usuarios(backup):
    modulo, _, copias = backup
    pacote = modulo.gerar()
    if os.name == "nt":  # pragma: no cover
        pytest.skip("permissão POSIX não se aplica ao Windows")
    modo = pacote.stat().st_mode & 0o777
    assert modo == 0o600, (
        f"a cópia contém autos e está com permissão {modo:o}: "
        "qualquer usuário da máquina conseguiria lê-la"
    )


# ================================================== corrida de inicialização

def test_ensure_column_aguenta_dois_processos_subindo_juntos(tmp_path):
    """Dois workers inicializando o mesmo banco não podem se matar.

    Descoberto ao subir o container com `uvicorn --workers 2`: os dois
    processos chamam init_db() ao mesmo tempo, ambos veem a coluna faltando,
    ambos emitem o ALTER e o segundo morre com "duplicate column name" antes
    de atender à primeira requisição. Com dois workers sobrava um; com um
    banco novo e mais workers, a inicialização fica pela metade.
    """
    import multiprocessing
    import sqlite3 as sq

    banco = tmp_path / "corrida.db"
    conn = sq.connect(banco)
    conn.execute("CREATE TABLE deadlines (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    def trabalhador(caminho, fila):
        import sys
        sys.path.insert(0, str(RAIZ))
        import importlib
        import os as _os
        _os.environ["JARBAS_DATA_DIR"] = str(Path(caminho).parent)
        database = importlib.import_module("app.database")
        database = importlib.reload(database)
        database.DB_PATH = Path(caminho)
        try:
            for _ in range(40):
                with database.db() as c:
                    database.ensure_column(c, "deadlines", "count_start TEXT")
                    database.ensure_column(c, "deadlines", "protocolo TEXT")
            fila.put("ok")
        except Exception as exc:  # pragma: no cover - é o que o teste caça
            fila.put(f"{type(exc).__name__}: {exc}")

    fila = multiprocessing.Queue()
    processos = [
        multiprocessing.Process(target=trabalhador, args=(str(banco), fila))
        for _ in range(4)
    ]
    for p in processos:
        p.start()
    for p in processos:
        p.join(timeout=60)

    resultados = [fila.get() for _ in processos]
    assert all(r == "ok" for r in resultados), resultados

    conn = sq.connect(banco)
    try:
        colunas = {linha[1] for linha in conn.execute("PRAGMA table_info(deadlines)")}
    finally:
        conn.close()
    assert {"count_start", "protocolo"} <= colunas
