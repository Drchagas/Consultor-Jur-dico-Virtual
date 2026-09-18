#!/usr/bin/env python3
"""Diagnostica por que o modulo de IA nao esta funcionando.

O erro 401 'invalid x-api-key' nao distingue entre nao ter chave, ter colado
a chave errada e ter uma chave revogada. Esta ferramenta separa os casos.

Uso:  python tools/diagnosticar_ia.py  [--testar]
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_BASE = Path(os.environ.get("JARBAS_ROOT", Path(__file__).resolve().parent.parent)).resolve()
# A árvore de fontes e a instalação têm app/ na raiz; o pacote montado tem
# payload/app/. Os demais utilitários já tratam os dois — este não tratava, e
# rodá-lo de dentro do pacote falhava com "No module named 'app'", uma
# mensagem que sugere instalação quebrada quando o layout é que era outro.
ROOT = _BASE / "payload" if (_BASE / "payload" / "app").is_dir() else _BASE
sys.path.insert(0, str(ROOT))

OK, FALHA, AVISO = "  [OK]   ", "  [FALHA]", "  [aviso]"


def _origem(root: Path) -> str:
    """De onde veio a chave: do .env.local ou do ambiente do processo.

    Importa porque uma variavel exportada na sessao do Windows vence o
    .env.local — o operador corrige o arquivo, nada muda, e nao ha como
    adivinhar por que.
    """
    arquivo = root / ".env.local"
    if not arquivo.is_file():
        return "ambiente do processo (.env.local nao existe)"
    try:
        for linha in arquivo.read_text(encoding="utf-8", errors="replace").splitlines():
            if linha.strip().startswith("ANTHROPIC_API_KEY"):
                return f"{arquivo}"
    except OSError:
        pass
    return "ambiente do processo (.env.local nao tem a variavel)"


def main() -> int:
    testar = "--testar" in sys.argv
    try:
        from app import ai_gateway as G
    except Exception as exc:
        print(f"{FALHA} nao consegui importar o gateway: {exc}")
        return 1

    versao = "?"
    for c in (ROOT / "VERSION.txt", ROOT / "payload" / "VERSION.txt"):
        if c.is_file():
            versao = c.read_text(encoding="utf-8").strip()
    print(f"JARBAS {versao} — diagnostico do modulo de IA\n")

    # 1 -------------------------------------------------- chave
    print("[1/4] Chave de API")
    tipo, diagnostico = G.formato_chave()
    bruta = os.getenv(G.ENV_CHAVE, "")
    limpa = G._key()
    sujeira = G.chave_saneada()

    print(f"        variavel: {G.ENV_CHAVE}")
    print(f"        origem..: {_origem(ROOT)}")
    if not limpa:
        print("        valor...: (vazio)")
    else:
        # O prefixo 'sk-ant-' e publico e e justamente o que distingue uma
        # chave boa de uma chave da OpenAI, de uma linha inteira colada ou de
        # um valor entre aspas. Sem mostra-lo, o diagnostico esconde a unica
        # informacao que resolve o caso. O miolo continua oculto.
        print(f"        comeca..: {limpa[:7]!r}")
        print(f"        termina.: ...{limpa[-4:]}")
        print(f"        tamanho.: {len(limpa)} caracteres"
              + (f" (eram {len(bruta)} antes da limpeza)" if len(bruta) != len(limpa) else ""))

    if sujeira:
        print(f"{AVISO} o valor guardado esta com: {', '.join(sujeira)}.")
        print("        O JARBAS removeu isso para conseguir usar a chave, mas o")
        print("        arquivo continua errado e a proxima instalacao vai repetir")
        print("        o problema. Regrave com CONFIGURAR_IA.cmd, colando APENAS")
        print("        a chave — sem aspas e sem o nome da variavel.")

    print(f"{OK if tipo == 'anthropic' else FALHA} {diagnostico}")
    if tipo != "anthropic":
        print("\n        Como resolver:")
        print("        1. Acesse console.anthropic.com > API Keys")
        print("        2. Crie uma chave (comeca com 'sk-ant-')")
        print("        3. Rode CONFIGURAR_IA.cmd e cole SO a chave")
        print("        4. Rode este diagnostico de novo")
        return 1

    # 2 -------------------------------------------------- SDK
    print("\n[2/4] SDK")
    tem = G._sdk_available()
    print(f"{OK if tem else FALHA} pacote 'anthropic' {'instalado v' + G._sdk_version() if tem else 'AUSENTE'}")
    if not tem:
        print("        pip install -r requirements-ia.txt")
        return 1

    # 3 -------------------------------------------------- modelos
    print("\n[3/4] Modelos configurados")
    for rotulo, fn in (("juridico", G.legal_model_name),
                       ("intake", G.intake_model_name),
                       ("rotina", G.routine_model_name)):
        print(f"{OK} {rotulo:9} {fn()}")
    descartados = G.modelos_ignorados()
    if descartados:
        print(f"{AVISO} configuracao antiga descartada:")
        for k, v in descartados.items():
            print(f"        {k}={v}  (modelo de provedor removido na 9.0)")
        print("        Salve as Configuracoes para limpar o .env.local.")

    # 4 -------------------------------------------------- chamada real
    print("\n[4/4] Chamada real a API")
    if not testar:
        print(f"{AVISO} pulada. Rode com --testar para gastar 1 chamada e conferir.")
        return 0
    try:
        r = G.test_connection()
        print(f"{OK} resposta recebida de {r.model} "
              f"({r.input_tokens}+{r.output_tokens} tokens)")
        print("\n  >>> O modulo de IA esta funcional.")
        return 0
    except Exception as exc:
        print(f"{FALHA} {G.friendly_error(exc)}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
