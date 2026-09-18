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

Uso:  python tools/build_installer.py [destino]
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

    # 6b. LEIA_PRIMEIRO na RAIZ do pacote, não só dentro de payload/.
    #     Quem extrai o ZIP vê uma pasta com .cmd e uma pasta payload/; sem
    #     um arquivo de orientação ao lado dos executáveis, a primeira ação
    #     de quem instala é adivinhar qual .cmd clicar.
    leia = RAIZ / "docs" / "LEIA_PRIMEIRO.txt"
    if leia.is_file():
        shutil.copy2(leia, destino / "LEIA_PRIMEIRO.txt")

    # 7. Manifesto — caminhos relativos com barra invertida (Join-Path do Windows)
    linhas = []
    for p in sorted(destino.rglob("*")):
        if not p.is_file() or p.name == "PACOTE_MANIFEST_SHA256.txt":
            continue
        rel = p.relative_to(destino).as_posix().replace("/", "\\")
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        linhas.append(f"{h}  {rel}")
    (destino / "PACOTE_MANIFEST_SHA256.txt").write_text(
        "\r\n".join(linhas) + "\r\n", encoding="utf-8")

    return destino


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
    alvo = Path(sys.argv[1]) if len(sys.argv) > 1 else RAIZ.parent / f"JARBAS_Instalador_{VERSAO.replace('.', '_')}"
    print(f"Montando pacote de instalação {VERSAO} em {alvo}")
    montar(alvo)
    print("Conferindo integridade (mesma checagem do passo [1/19]):")
    sys.exit(1 if conferir(alvo) else 0)
