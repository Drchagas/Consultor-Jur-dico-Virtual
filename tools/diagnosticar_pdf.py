#!/usr/bin/env python3
"""Diagnostica por que um PDF nao entra no JARBAS.

Um arquivo pode parar em quatro lugares diferentes, e a mensagem que o usuario
ve ("apenas PDF e aceito") nao distingue entre eles. Esta ferramenta roda as
mesmas checagens do upload, na mesma ordem, e diz onde parou.

Uso:
    python tools/diagnosticar_pdf.py "C:\\caminho\\do\\arquivo.pdf"
    python tools/diagnosticar_pdf.py "C:\\pasta\\*.PDF*"
"""

from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("JARBAS_ROOT", Path(__file__).resolve().parent.parent)).resolve()
sys.path.insert(0, str(ROOT))

OK = "  [OK]   "
FALHA = "  [FALHA]"
AVISO = "  [aviso]"


def versao_instalada() -> str:
    for c in (ROOT / "VERSION.txt", ROOT / "payload" / "VERSION.txt"):
        if c.is_file():
            return c.read_text(encoding="utf-8").strip()
    return "desconhecida"


def diagnosticar(caminho: Path) -> bool:
    print(f"\n{'='*70}\nARQUIVO: {caminho.name[:64]}")
    if len(caminho.name) > 64:
        print(f"         ...{caminho.name[64:128]}")
    print(f"{'='*70}")
    print(f"  nome: {len(caminho.name)} caracteres")
    print(f"  tamanho: {caminho.stat().st_size/1024/1024:.2f} MB")

    falhou_em = None

    # 1 -----------------------------------------------------------------
    print("\n[1/4] Aceitacao pelo nome")
    try:
        from app import nome_documento as ND
        aceito = ND.parece_pdf(caminho.name, "application/pdf")
        print(f"{OK if aceito else FALHA} parece_pdf() -> {aceito}")
        if not aceito:
            falhou_em = falhou_em or "nome"
    except ImportError:
        print(f"{FALHA} modulo app/nome_documento.py NAO EXISTE nesta instalacao.")
        print("         Esta versao ainda rejeita PDF cujo nome nao termine em .pdf.")
        print("         Downloads do eproc trazem a extensao NO MEIO do nome e sao")
        print("         recusados antes de qualquer leitura. Atualize para 8.8.2+.")
        falhou_em = "versao antiga"

    # 2 -----------------------------------------------------------------
    print("\n[2/4] Cabecalho PDF (a prova que vale)")
    cabecalho = caminho.open("rb").read(1024)
    tem = b"%PDF-" in cabecalho
    print(f"{OK if tem else FALHA} %PDF- presente -> {tem}")
    if tem:
        i = cabecalho.index(b"%PDF-")
        print(f"         versao: {cabecalho[i:i+8].decode('ascii','ignore')} (offset {i})")
    else:
        print(f"         primeiros bytes: {cabecalho[:16]!r}")
        falhou_em = falhou_em or "conteudo"
        return _fim(falhou_em)

    # 3 -----------------------------------------------------------------
    print("\n[3/4] Limite de tamanho")
    limite = max(5, int(os.getenv("JARBAS_MAX_UPLOAD_MB", "200")))
    mb = caminho.stat().st_size / 1024 / 1024
    cabe = mb <= limite
    print(f"{OK if cabe else FALHA} {mb:.2f} MB de {limite} MB permitidos")
    if not cabe:
        print("         Aumente JARBAS_MAX_UPLOAD_MB no .env.local.")
        falhou_em = falhou_em or "tamanho"

    # 4 -----------------------------------------------------------------
    print("\n[4/4] Extracao de texto")
    try:
        from app import pdf_pipeline as PP
        r = PP.extract_pdf(caminho)
        cobertura = getattr(r, "coverage", 0) or 0
        chars = getattr(r, "text_chars", 0)
        paginas = getattr(r, "page_count", 0)
        status = getattr(r, "status", "?")
        bom = status == "indexed" and cobertura > 0
        print(f"{OK if bom else FALHA} status={status} paginas={paginas} "
              f"chars={chars} cobertura={cobertura:.0%}")
        print(f"         {getattr(r, 'note', '')[:150]}")
        if getattr(r, "encrypted", False):
            print(f"{AVISO} PDF protegido por senha.")
        try:
            from app import pdf_pipeline as PP2
            ocr = PP2.ocr_capability()
            if not ocr.get("local_ocr"):
                print(f"{AVISO} OCR local NAO instalado. PDF digitalizado sem camada")
                print("         de texto entra vazio e o Copiloto nao acha nada.")
                print("         Instale o Tesseract com portugues:")
                print("         https://github.com/UB-Mannheim/tesseract/wiki")
                print("         (marque 'Portuguese' na lista de idiomas)")
        except Exception:
            pass
        if cobertura < 0.5:
            print(f"{AVISO} Cobertura baixa: provavelmente digitalizado sem camada")
            print("         de texto. Instale o Tesseract para OCR local, ou deixe")
            print("         JARBAS_AI_PDF_ALWAYS=1 para enviar o PDF direto a IA.")
        if not bom:
            falhou_em = falhou_em or "extracao"
    except Exception as exc:
        print(f"{FALHA} {type(exc).__name__}: {str(exc)[:160]}")
        falhou_em = falhou_em or "extracao"

    # nome sugerido
    try:
        from app import nome_documento as ND
        from app import pdf_pipeline as PP
        r = PP.extract_pdf(caminho)
        texto = "\n".join((getattr(p, "text", "") or "")
                          for p in (getattr(r, "pages", []) or [])[:3])
        print(f"\n  nome sugerido: {ND.nome_amigavel(caminho.name, texto)}")
    except Exception:
        pass

    return _fim(falhou_em)


def _fim(falhou_em) -> bool:
    print()
    if falhou_em:
        print(f"  >>> BLOQUEIO EM: {falhou_em}")
        return False
    print("  >>> Este arquivo entra normalmente no JARBAS.")
    return True


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    print(f"JARBAS {versao_instalada()} — diagnostico de PDF")
    alvos: list[Path] = []
    for padrao in sys.argv[1:]:
        achados = glob.glob(padrao)
        alvos += [Path(a) for a in achados] if achados else [Path(padrao)]

    ausentes = [a for a in alvos if not a.is_file()]
    for a in ausentes:
        print(f"\n  arquivo nao encontrado: {a}")
    alvos = [a for a in alvos if a.is_file()]
    if not alvos:
        return 1

    ok = sum(1 for a in alvos if diagnosticar(a))
    print(f"\n{'='*70}\n{ok} de {len(alvos)} arquivos entram no JARBAS.")
    return 0 if ok == len(alvos) else 1


if __name__ == "__main__":
    sys.exit(main())
