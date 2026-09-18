#!/usr/bin/env python3
"""Backup do JARBAS, com verificação e restauração testadas.

Um escritório que opera com autos reais não pode depender de "copiar a pasta
data". Três coisas quebram esse hábito:

1. Copiar um SQLite enquanto o servidor escreve produz um arquivo corrompido
   que só se revela no dia em que for preciso restaurar. Aqui usamos a API
   de backup online do próprio SQLite, que tira uma cópia consistente com o
   servidor de pé.
2. Backup que nunca foi restaurado não é backup. Toda cópia é aberta e
   consultada logo após ser gerada; se não abrir, o processo falha na hora,
   não no dia do incidente.
3. Cópia antiga acumula até encher o disco e derrubar o sistema que ela
   deveria proteger. A retenção apaga as mais velhas, sempre preservando ao
   menos a última que passou na verificação.

Uso:
    python tools/backup.py                 # gera cópia e verifica
    python tools/backup.py --verificar     # só confere as cópias existentes
    python tools/backup.py --restaurar ARQ # restaura (pede confirmação)
    python tools/backup.py --listar

Variáveis:
    JARBAS_DATA_DIR       raiz dos dados (padrão: data/ da instalação)
    JARBAS_BACKUP_DIR     destino das cópias (padrão: <dados>/backups)
    JARBAS_BACKUP_MANTER  quantas cópias manter (padrão: 14)
    DATABASE_URL          se apontar para PostgreSQL, usa pg_dump
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(os.environ.get("JARBAS_ROOT", Path(__file__).resolve().parent.parent)).resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA_DIR = Path(os.getenv("JARBAS_DATA_DIR", "").strip() or (ROOT / "data")).resolve()
BACKUP_DIR = Path(os.getenv("JARBAS_BACKUP_DIR", "").strip() or (DATA_DIR / "backups")).resolve()
MANTER = max(1, int(os.getenv("JARBAS_BACKUP_MANTER", "14") or 14))
DATABASE_URL = os.getenv("DATABASE_URL", "")
IS_POSTGRES = DATABASE_URL.startswith(("postgresql://", "postgres://"))

# Pastas de dados que entram na cópia. Sem elas, o banco restaurado aponta
# para PDFs que não existem mais — e um processo sem os autos é um processo
# perdido.
PASTAS_DE_DADOS = ("uploads", "imports", "generated")


def _carimbo() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _sha256(caminho: Path) -> str:
    h = hashlib.sha256()
    with caminho.open("rb") as fh:
        for bloco in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(bloco)
    return h.hexdigest()


def _tamanho(n: float) -> str:
    for unidade in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unidade}"
        n /= 1024.0
    return f"{n:.1f} PB"


# ------------------------------------------------------------------ SQLite

def _copiar_sqlite(origem: Path, destino: Path) -> None:
    """Cópia consistente com o servidor em execução.

    `shutil.copy` de um SQLite ativo copia páginas de instantes diferentes e
    pode produzir um arquivo que o próprio SQLite recusa a abrir. A API de
    backup faz a cópia sob a trava do banco, página a página.
    """
    fonte = sqlite3.connect(f"file:{origem}?mode=ro", uri=True, timeout=30)
    try:
        alvo = sqlite3.connect(destino)
        try:
            with alvo:
                fonte.backup(alvo)
        finally:
            alvo.close()
    finally:
        fonte.close()


def _verificar_sqlite(caminho: Path) -> tuple[bool, str]:
    """Abre a cópia e consulta de verdade. Abrir sem ler não prova nada."""
    try:
        conn = sqlite3.connect(f"file:{caminho}?mode=ro", uri=True, timeout=15)
        try:
            integridade = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integridade != "ok":
                return False, f"integrity_check: {integridade}"
            tabelas = {
                linha[0] for linha in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            # Se estas faltarem, o arquivo pode até abrir, mas não é um banco
            # do JARBAS — é uma cópia de algo errado.
            essenciais = {"users", "organizations", "cases", "clients", "case_documents"}
            faltando = essenciais - tabelas
            if faltando:
                return False, f"tabelas ausentes: {', '.join(sorted(faltando))}"
            usuarios = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            processos = conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
            return True, f"{usuarios} usuário(s), {processos} processo(s)"
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return False, f"SQLite recusou o arquivo: {exc}"


# --------------------------------------------------------------- PostgreSQL

def _copiar_postgres(destino: Path) -> None:
    if not shutil.which("pg_dump"):
        raise RuntimeError(
            "pg_dump não encontrado. Instale o cliente do PostgreSQL "
            "(postgresql-client) ou rode o backup pelo serviço do compose."
        )
    # Formato custom (-Fc): comprimido e restaurável seletivamente com
    # pg_restore, ao contrário do SQL puro.
    resultado = subprocess.run(
        ["pg_dump", "--format=custom", "--no-owner", "--file", str(destino), DATABASE_URL],
        capture_output=True, text=True,
    )
    if resultado.returncode != 0:
        raise RuntimeError(f"pg_dump falhou: {resultado.stderr.strip()[:400]}")


def _verificar_postgres(caminho: Path) -> tuple[bool, str]:
    if not shutil.which("pg_restore"):
        return False, "pg_restore não encontrado para conferir a cópia"
    resultado = subprocess.run(
        ["pg_restore", "--list", str(caminho)], capture_output=True, text=True
    )
    if resultado.returncode != 0:
        return False, f"pg_restore recusou o arquivo: {resultado.stderr.strip()[:200]}"
    linhas = [l for l in resultado.stdout.splitlines() if l and not l.startswith(";")]
    if not linhas:
        return False, "dump sem objeto algum"
    return True, f"{len(linhas)} objeto(s) no dump"


# ------------------------------------------------------------------ backup

def gerar() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    carimbo = _carimbo()
    destino = BACKUP_DIR / f"jarbas-{carimbo}.tar.gz"

    with tempfile.TemporaryDirectory(prefix="jarbas-backup-") as tmp:
        area = Path(tmp)

        if IS_POSTGRES:
            banco = area / "banco.pgdump"
            _copiar_postgres(banco)
            ok, detalhe = _verificar_postgres(banco)
        else:
            origem = DATA_DIR / "jarbas.db"
            if not origem.is_file():
                raise RuntimeError(f"Banco não encontrado em {origem}.")
            banco = area / "jarbas.db"
            _copiar_sqlite(origem, banco)
            ok, detalhe = _verificar_sqlite(banco)

        if not ok:
            raise RuntimeError(f"A cópia do banco não passou na verificação: {detalhe}")
        print(f"  banco conferido: {detalhe}")

        # Documentos. Sem eles o banco restaurado referencia PDFs inexistentes.
        arquivos = 0
        for pasta in PASTAS_DE_DADOS:
            fonte = DATA_DIR / pasta
            if fonte.is_dir():
                shutil.copytree(fonte, area / pasta, dirs_exist_ok=True)
                arquivos += sum(1 for _ in (area / pasta).rglob("*") if _.is_file())

        (area / "MANIFESTO.txt").write_text(
            "\n".join([
                "JARBAS Jurídico — cópia de segurança",
                f"gerada em: {datetime.now().isoformat(timespec='seconds')}",
                f"origem:    {DATA_DIR}",
                f"banco:     {'PostgreSQL (pg_dump custom)' if IS_POSTGRES else 'SQLite (backup online)'}",
                f"conteúdo:  {detalhe}; {arquivos} arquivo(s) de documento",
                f"versão:    {(ROOT / 'VERSION.txt').read_text(encoding='utf-8').strip() if (ROOT / 'VERSION.txt').is_file() else '?'}",
                "",
                "CONTÉM DADOS SOB SIGILO PROFISSIONAL. Guarde cifrado e com",
                "acesso restrito. A LGPD e o Estatuto da OAB se aplicam a esta",
                "cópia exatamente como se aplicam aos autos originais.",
                "",
                "Restaurar:  python tools/backup.py --restaurar <este arquivo>",
            ]) + "\n",
            encoding="utf-8",
        )

        with tarfile.open(destino, "w:gz") as tar:
            for item in sorted(area.iterdir()):
                tar.add(item, arcname=item.name)

    # O diretório de backup guarda autos: ninguém além do dono precisa ler.
    try:
        os.chmod(BACKUP_DIR, 0o700)
        os.chmod(destino, 0o600)
    except OSError:
        pass

    (BACKUP_DIR / f"{destino.name}.sha256").write_text(
        f"{_sha256(destino)}  {destino.name}\n", encoding="utf-8"
    )
    return destino


def verificar(caminho: Path) -> tuple[bool, str]:
    """Abre o pacote e confere o banco de dentro dele, não só o .tar.gz."""
    if not caminho.is_file():
        return False, "arquivo não existe"

    soma = caminho.with_name(caminho.name + ".sha256")
    if soma.is_file():
        esperado = soma.read_text(encoding="utf-8").split()[0]
        if _sha256(caminho) != esperado:
            return False, "SHA256 não confere: o arquivo mudou depois de gerado"

    with tempfile.TemporaryDirectory(prefix="jarbas-verifica-") as tmp:
        area = Path(tmp)
        try:
            with tarfile.open(caminho, "r:gz") as tar:
                # filter="data" recusa caminhos absolutos e ".." dentro do
                # pacote: um tar adulterado não escreve fora da área temporária.
                try:
                    tar.extractall(area, filter="data")
                except TypeError:          # Python < 3.12
                    tar.extractall(area)   # noqa: S202
        except (tarfile.TarError, OSError) as exc:
            return False, f"pacote ilegível: {exc}"

        if (area / "jarbas.db").is_file():
            return _verificar_sqlite(area / "jarbas.db")
        if (area / "banco.pgdump").is_file():
            return _verificar_postgres(area / "banco.pgdump")
        return False, "pacote sem banco de dados"


def aplicar_retencao() -> list[Path]:
    copias = sorted(BACKUP_DIR.glob("jarbas-*.tar.gz"), key=lambda p: p.name, reverse=True)
    removidas = []
    for antiga in copias[MANTER:]:
        antiga.unlink(missing_ok=True)
        antiga.with_name(antiga.name + ".sha256").unlink(missing_ok=True)
        removidas.append(antiga)
    return removidas


def listar() -> None:
    copias = sorted(BACKUP_DIR.glob("jarbas-*.tar.gz"), reverse=True)
    if not copias:
        print(f"Nenhuma cópia em {BACKUP_DIR}")
        return
    print(f"Cópias em {BACKUP_DIR} (mantendo {MANTER}):\n")
    for copia in copias:
        ok, detalhe = verificar(copia)
        marca = "OK  " if ok else "FALHA"
        print(f"  [{marca}] {copia.name}  {_tamanho(copia.stat().st_size)}  {detalhe}")


def restaurar(pacote: Path, confirmado: bool) -> int:
    ok, detalhe = verificar(pacote)
    if not ok:
        print(f"ERRO: a cópia não passou na verificação ({detalhe}). Restauração abortada.")
        return 1
    print(f"Cópia conferida: {detalhe}")

    if not confirmado:
        print(
            "\nA restauração SUBSTITUI o banco e os documentos atuais em\n"
            f"  {DATA_DIR}\n"
            "PARE O SERVIDOR antes de continuar — restaurar com o JARBAS de pé\n"
            "corrompe o banco novo.\n\n"
            "Confirme repetindo o comando com --sim."
        )
        return 2

    salvaguarda = DATA_DIR.parent / f"{DATA_DIR.name}-antes-da-restauracao-{_carimbo()}"
    if DATA_DIR.exists():
        # O estado anterior nunca é apagado: se a cópia restaurada for a
        # errada, ainda existe caminho de volta.
        shutil.copytree(DATA_DIR, salvaguarda, dirs_exist_ok=True)
        print(f"Estado anterior preservado em {salvaguarda}")

    with tempfile.TemporaryDirectory(prefix="jarbas-restaura-") as tmp:
        area = Path(tmp)
        with tarfile.open(pacote, "r:gz") as tar:
            try:
                tar.extractall(area, filter="data")
            except TypeError:
                tar.extractall(area)  # noqa: S202

        if (area / "banco.pgdump").is_file():
            print(
                "Dump PostgreSQL. Restaure com o banco vazio e o servidor parado:\n"
                f"  pg_restore --clean --if-exists --no-owner -d \"$DATABASE_URL\" {area / 'banco.pgdump'}\n"
                "O arquivo foi extraído na área temporária acima e some ao fim deste comando;\n"
                "para restaurar agora, extraia o pacote manualmente."
            )
        else:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(area / "jarbas.db", DATA_DIR / "jarbas.db")
            print(f"Banco restaurado em {DATA_DIR / 'jarbas.db'}")

        for pasta in PASTAS_DE_DADOS:
            origem = area / pasta
            if origem.is_dir():
                shutil.copytree(origem, DATA_DIR / pasta, dirs_exist_ok=True)
                print(f"Documentos restaurados: {pasta}")

    print("\nRestauração concluída. Suba o servidor e confira /health.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Backup do JARBAS Jurídico.")
    ap.add_argument("--verificar", action="store_true", help="confere as cópias existentes")
    ap.add_argument("--listar", action="store_true", help="lista as cópias")
    ap.add_argument("--restaurar", metavar="ARQUIVO", help="restaura a partir de uma cópia")
    ap.add_argument("--sim", action="store_true", help="confirma a restauração")
    args = ap.parse_args()

    if args.listar or args.verificar:
        listar()
        copias = list(BACKUP_DIR.glob("jarbas-*.tar.gz"))
        if not copias:
            return 1
        return 0 if all(verificar(c)[0] for c in copias) else 1

    if args.restaurar:
        return restaurar(Path(args.restaurar).resolve(), args.sim)

    print(f"Gerando cópia de {DATA_DIR} em {BACKUP_DIR}")
    try:
        pacote = gerar()
    except Exception as exc:
        print(f"ERRO: {exc}")
        return 1

    ok, detalhe = verificar(pacote)
    if not ok:
        print(f"ERRO: a cópia recém-gerada não passou na verificação ({detalhe}).")
        return 1

    print(f"  cópia:      {pacote.name}  ({_tamanho(pacote.stat().st_size)})")
    print(f"  conferida:  {detalhe}")
    for removida in aplicar_retencao():
        print(f"  removida por retenção: {removida.name}")
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
