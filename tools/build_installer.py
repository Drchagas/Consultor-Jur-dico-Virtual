#!/usr/bin/env python3
"""Monta o PACOTE DE INSTALAÇÃO a partir da árvore de código-fonte.

O instalador PowerShell espera um layout de distribuição diferente da árvore
de fontes. Rodar o .ps1 direto do código-fonte falha em [1/19] com
"Manifesto SHA-256 do pacote nao encontrado" — que é exatamente o que
acontecia antes deste script existir.

Layout de fonte              ->  Layout de distribuição
  app/                            payload/app/
  requirements.txt                payload/requirements.txt
  docs/*.md                       payload/*.md      (achatado)
  installer/*.ps1|*.cmd           raiz
  tools/  scripts/                tools/  scripts/  (iguais)
  SHA256SUMS.txt                  PACOTE_MANIFEST_SHA256.txt

Uso:  python tools/build_installer.py [destino] [--com-dependencias]

`--com-dependencias` baixa as rodas (.whl) de Windows/Python 3.12 para dentro
do pacote, em vendor/wheels. O instalador passa a instalar sem internet.

As rodas NÃO ficam versionadas no repositório: são artefatos de terceiros, de
dezenas de MB, que envelheceriam junto com o histórico. Baixá-las no momento
da montagem mantém o repositório limpo e garante que o pacote leve exatamente
as versões fixadas em requirements.txt.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
# Na árvore de fontes VERSION.txt fica na raiz; no pacote montado, em payload/.
_VERSAO_ARQ = next((c for c in (RAIZ / "VERSION.txt", RAIZ / "payload" / "VERSION.txt")
                    if c.is_file()), None)
VERSAO = _VERSAO_ARQ.read_text(encoding="utf-8").strip() if _VERSAO_ARQ else "0.0.0"
PS1 = f"INSTALAR_JARBAS_{VERSAO.replace('.', '_')}.ps1"

# O instalador copia estes nomes de payload/ para a pasta de instalação
# (linha "foreach($name in @(...)" do .ps1). Manter em sincronia.
DOCS_NA_RAIZ_DO_PAYLOAD = [
    "README.md", "AUDITORIA_8_3.md", "AUDITORIA_8_3_1.md",
    "SEGURANCA_LGPD_IA.md", "MATRIZ_SISTEMA_PRINCIPAL.md",
    "MATRIZ_FUNCIONAL_7_0.md", "ARQUITETURA_SAAS_7_0.md",
    "ROADMAP_PRODUCAO.md", "CONSELHO_IA.md",
    "CAPACIDADE_2000_ASSINANTES.md",
    "NOTAS_DA_VERSAO_9_0_2.txt", "README_INSTALACAO.md", "LEIA_PRIMEIRO.txt",
]

IGNORAR = shutil.ignore_patterns("__pycache__", "*.pyc", ".env", ".env.local",
                                 "*.db", "*.sqlite3", ".DS_Store")


def montar(destino: Path) -> Path:
    if destino.exists():
        shutil.rmtree(destino)
    destino.mkdir(parents=True)

    payload = destino / "payload"
    payload.mkdir()

    # 1. Código da aplicação
    shutil.copytree(RAIZ / "app", payload / "app", ignore=IGNORAR)

    # 2. Arquivos soltos que o instalador procura em payload/
    for nome in ("requirements.txt", "requirements-ia.txt", "requirements-dev.txt",
                 "VERSION.txt", "LICENSE_PROPRIETARY.txt", ".env.example"):
        origem = RAIZ / nome
        if origem.is_file():
            shutil.copy2(origem, payload / nome)

    # 3. Docs achatados na raiz do payload
    for nome in DOCS_NA_RAIZ_DO_PAYLOAD:
        for origem in (RAIZ / "docs" / nome, RAIZ / nome):
            if origem.is_file():
                shutil.copy2(origem, payload / nome)
                break

    # 4. tools/ e scripts/ ficam na raiz do pacote, como o .ps1 espera
    shutil.copytree(RAIZ / "tools", destino / "tools", ignore=IGNORAR)
    shutil.copytree(RAIZ / "scripts", destino / "scripts", ignore=IGNORAR)

    # 5. Testes viajam junto: permitem revalidar a instalação na máquina do escritório
    shutil.copytree(RAIZ / "tests", destino / "tests", ignore=IGNORAR)

    # 6. Scripts do instalador na raiz
    for arq in sorted((RAIZ / "installer").iterdir()):
        if arq.is_file():
            shutil.copy2(arq, destino / arq.name)

    # 6a. Dados de idioma do OCR. Viajam no pacote porque a rede do escritório
    #     costuma bloquear o GitHub, e sem o português o OCR de um auto
    #     brasileiro devolve texto inutilizável.
    ocr = RAIZ / "installer" / "ocr"
    if ocr.is_dir():
        shutil.copytree(ocr, destino / "ocr", ignore=IGNORAR)

    # 6b. LEIA_PRIMEIRO na RAIZ do pacote, não só dentro de payload/.
    #     Quem extrai o ZIP vê uma pasta com .cmd e uma pasta payload/; sem
    #     um arquivo de orientação ao lado dos executáveis, a primeira ação
    #     de quem instala é adivinhar qual .cmd clicar.
    leia = RAIZ / "docs" / "LEIA_PRIMEIRO.txt"
    if leia.is_file():
        shutil.copy2(leia, destino / "LEIA_PRIMEIRO.txt")

    # 7. Carimbo do build ANTES do manifesto, para entrar nele.
    _gravar_carimbo(destino)

    # 8. Manifesto — caminhos relativos com barra invertida (Join-Path do Windows)
    _gravar_manifesto(destino)

    return destino


def _gravar_carimbo(destino: Path) -> str:
    """Identidade única deste build, dentro do pacote e da instalação.

    Dois pacotes diferentes diziam "9.0.2" e não havia como distingui-los.
    Num ciclo de correção — enviar o pacote, o operador extrair, instalar,
    mandar o diagnóstico — isso custou uma rodada inteira: a instalação
    falhou com o MESMO erro já corrigido, e nem o operador nem eu tínhamos
    como saber, olhando o diagnóstico, que o pacote aplicado era o antigo.

    O carimbo responde "qual build está instalado?" sem depender da memória
    de ninguém.
    """
    import subprocess
    from datetime import datetime, timezone

    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                cwd=RAIZ, capture_output=True, text=True).stdout.strip()
    except Exception:
        commit = ""
    sujo = ""
    try:
        if subprocess.run(["git", "status", "--porcelain"], cwd=RAIZ,
                          capture_output=True, text=True).stdout.strip():
            sujo = " (com alterações não commitadas)"
    except Exception:
        pass

    carimbo = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    linhas = [
        f"JARBAS Juridico Enterprise {VERSAO}",
        f"build.....: {carimbo} UTC",
        f"commit....: {commit or 'desconhecido'}{sujo}",
        "",
        "Este arquivo identifica ESTE pacote. Dois pacotes podem ter a mesma",
        "versao e conteudo diferente; o build nao repete.",
        "",
        "Confira o que esta instalado com DIAGNOSTICO_JARBAS.cmd ou em /health.",
        "Se o build da instalacao nao for o do pacote que voce extraiu, a",
        "extracao nao substituiu os arquivos: extraia de novo, por cima, e",
        "confirme quando o Windows perguntar se quer substituir.",
    ]
    texto = "\r\n".join(linhas) + "\r\n"
    (destino / "BUILD.txt").write_text(texto, encoding="utf-8")
    (destino / "payload" / "BUILD.txt").write_text(texto, encoding="utf-8")
    return carimbo


def _gravar_manifesto(destino: Path) -> None:
    linhas = []
    for p in sorted(destino.rglob("*")):
        if not p.is_file() or p.name == "PACOTE_MANIFEST_SHA256.txt":
            continue
        rel = p.relative_to(destino).as_posix().replace("/", "\\")
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        linhas.append(f"{h}  {rel}")
    (destino / "PACOTE_MANIFEST_SHA256.txt").write_text(
        "\r\n".join(linhas) + "\r\n", encoding="utf-8")


def baixar_dependencias(destino: Path) -> int:
    """Baixa as rodas de Windows/Python 3.12 para vendor/wheels.

    --only-binary=:all: é deliberado: uma roda pré-compilada instala sem
    compilador. Permitir sdist aqui produziria um pacote que, na máquina do
    escritório, tentaria compilar C e falharia — exatamente o que este modo
    existe para evitar.
    """
    import subprocess

    alvo = destino / "vendor" / "wheels"
    alvo.mkdir(parents=True, exist_ok=True)
    total = 0
    for req in ("requirements.txt", "requirements-ia.txt"):
        arquivo = RAIZ / req
        if not arquivo.is_file():
            continue
        opcional = req.endswith("-ia.txt")
        print(f"  baixando {req}...")
        r = subprocess.run(
            [sys.executable, "-m", "pip", "download", "--quiet",
             "--dest", str(alvo),
             "--platform", "win_amd64", "--python-version", "3.12",
             "--only-binary=:all:", "-r", str(arquivo)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            if opcional:
                # Mesmo critério do instalador: sem SDK de IA o sistema opera.
                print(f"  AVISO: {req} não resolveu; o pacote sai sem as funções de IA.")
                print(f"         {r.stderr.strip()[:300]}")
                continue
            print(f"  FALHA ao baixar {req}:\n{r.stderr.strip()[:600]}")
            return 1
    rodas = sorted(alvo.glob("*.whl"))
    total = sum(p.stat().st_size for p in rodas)
    print(f"  {len(rodas)} rodas, {total/1024/1024:.1f} MB")
    return 0 if rodas else 1


def conferir(destino: Path) -> int:
    """Repete a verificação que o .ps1 faz em [1/19]."""
    manifesto = destino / "PACOTE_MANIFEST_SHA256.txt"
    if not manifesto.is_file():
        print("FALHA: manifesto ausente")
        return 1
    erros = 0
    total = 0
    for linha in manifesto.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha:
            continue
        esperado, _, rel = linha.partition("  ")
        alvo = destino / rel.replace("\\", "/")
        total += 1
        if not alvo.is_file():
            print(f"  AUSENTE: {rel}")
            erros += 1
        elif hashlib.sha256(alvo.read_bytes()).hexdigest() != esperado:
            print(f"  CORROMPIDO: {rel}")
            erros += 1

    # Pré-condições que o .ps1 exige explicitamente
    for exigido in ("payload", "payload/app", "tools", "scripts", PS1,
                    "INSTALAR_AGORA.cmd"):
        if not (destino / exigido).exists():
            print(f"  FALTA: {exigido}")
            erros += 1

    print(f"  {total - erros} de {total} arquivos conferem")
    return erros


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    com_deps = "--com-dependencias" in sys.argv
    alvo = Path(args[0]) if args else RAIZ.parent / f"JARBAS_Instalador_{VERSAO.replace('.', '_')}"

    print(f"Montando pacote de instalação {VERSAO} em {alvo}")
    montar(alvo)

    if com_deps:
        print("Baixando dependências para dentro do pacote:")
        if baixar_dependencias(alvo) != 0:
            print("FALHA: não foi possível montar o pacote com dependências.")
            sys.exit(1)
        # O manifesto é refeito DEPOIS das rodas, senão o passo [1/19] do
        # instalador acusaria arquivo fora do manifesto e abortaria.
        print("Refazendo o manifesto com as dependências incluídas...")
        _gravar_manifesto(alvo)

    print("Conferindo integridade (mesma checagem do passo [1/19]):")
    sys.exit(1 if conferir(alvo) else 0)
