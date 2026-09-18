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

ROOT = Path(os.environ.get("JARBAS_ROOT", Path(__file__).resolve().parent.parent)).resolve()
sys.path.insert(0, str(ROOT))

OK, FALHA, AVISO = "  [OK]   ", "  [FALHA]", "  [aviso]"


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
    chave = os.getenv(G.ENV_CHAVE, "").strip()
    print(f"        variavel: {G.ENV_CHAVE}")
    print(f"        valor...: {('*'*4 + chave[-4:]) if len(chave) >= 4 else '(vazio)'}")
    print(f"        tamanho.: {len(chave)} caracteres")
    print(f"{OK if tipo == 'anthropic' else FALHA} {diagnostico}")
    if tipo != "anthropic":
        print("\n        Como resolver:")
        print("        1. Acesse console.anthropic.com > API Keys")
        print("        2. Crie uma chave (comeca com 'sk-ant-')")
        print("        3. Rode CONFIGURAR_IA.cmd, ou informe em /settings")
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
