from __future__ import annotations

import contextlib
import io
import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


@dataclass
class PDFPage:
    page: int
    text: str
    engine: str
    chars: int
    quality: float


@dataclass
class PDFExtraction:
    pages: list[PDFPage]
    page_count: int
    text_chars: int
    text_pages: int
    empty_pages: int
    coverage: float
    status: str
    engine_summary: str
    note: str
    encrypted: bool = False
    damaged: bool = False


def _clean(text: str) -> str:
    text = (text or "").replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[\t\f\v ]+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln).strip()


def _quality(text: str) -> float:
    text = _clean(text)
    if not text:
        return 0.0
    alnum = sum(ch.isalnum() for ch in text)
    printable = sum(ch.isprintable() for ch in text)
    weird = text.count("�") + text.count("\ufffd")
    words = len(re.findall(r"[A-Za-zÀ-ÿ0-9]{2,}", text))
    score = min(len(text) / 1200.0, 1.0) * 0.35
    score += min(words / 120.0, 1.0) * 0.35
    score += (alnum / max(len(text), 1)) * 0.25
    score += (printable / max(len(text), 1)) * 0.05
    score -= min(weird / 20.0, 0.25)
    return max(0.0, min(score, 1.0))


def _good_enough(text: str) -> bool:
    cleaned = _clean(text)
    return len(cleaned) >= 90 and _quality(cleaned) >= 0.22


def _choose_best(candidates: Iterable[tuple[str, str]]) -> tuple[str, str]:
    best_engine = "none"
    best_text = ""
    best_score = -1.0
    for engine, raw in candidates:
        text = _clean(raw)
        score = _quality(text)
        weighted = score + min(len(text) / 20000.0, 0.10)
        if weighted > best_score:
            best_engine, best_text, best_score = engine, text, weighted
    return best_engine, best_text


def _page_indexes(page_count: int, max_pages: Optional[int], tail_pages: int) -> list[int]:
    if page_count <= 0:
        return []
    if not max_pages or page_count <= max_pages:
        return list(range(page_count))
    head = max(1, max_pages - max(0, tail_pages))
    indexes = list(range(min(head, page_count)))
    start_tail = max(head, page_count - max(0, tail_pages))
    indexes.extend(range(start_tail, page_count))
    return sorted(set(indexes))


def _pymupdf_doc(path: Path):
    try:
        import pymupdf  # type: ignore
        return pymupdf.open(str(path))
    except Exception:
        try:
            import fitz  # type: ignore
            return fitz.open(str(path))
        except Exception:
            return None


def _extract_pymupdf_page(doc, idx: int) -> str:
    if doc is None:
        return ""
    try:
        page = doc.load_page(idx)
        text = page.get_text("text", sort=True) or ""
        if len(_clean(text)) < 60:
            blocks = page.get_text("blocks", sort=True) or []
            block_text = "\n".join(str(b[4]) for b in blocks if len(b) >= 5 and b[4])
            if _quality(block_text) > _quality(text):
                text = block_text
        return _clean(text)
    except Exception:
        return ""


def _make_pypdf_reader(path: Path):
    try:
        from pypdf import PdfReader
        # Alguns PDFs imperfeitos escrevem avisos no stderr. Isso não pode derrubar
        # o servidor/instalador; o resultado é validado explicitamente abaixo.
        with contextlib.redirect_stderr(io.StringIO()):
            reader = PdfReader(str(path), strict=False)
        encrypted = bool(getattr(reader, "is_encrypted", False))
        if encrypted:
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    ok = reader.decrypt("")
                if not ok:
                    return reader, len(reader.pages), True
                encrypted = False
            except Exception:
                return reader, len(reader.pages), True
        return reader, len(reader.pages), encrypted
    except Exception:
        return None, 0, False


def _extract_pypdf_page(reader, idx: int) -> str:
    if reader is None:
        return ""
    try:
        if idx >= len(reader.pages):
            return ""
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                text = reader.pages[idx].extract_text(extraction_mode="layout") or ""
            except TypeError:
                text = reader.pages[idx].extract_text() or ""
        return _clean(text)
    except Exception:
        return ""


def _extract_pdfplumber_page(path: Path, idx: int) -> str:
    """Fallback de último recurso.

    pdfplumber/pdfminer é útil para PDFs peculiares, mas alguns arquivos possuem
    descritores de fonte defeituosos (ex.: FontBBox=None). Esses avisos não são
    fatais e não podem ser interpretados como erro de upload/instalação.
    """
    logger = logging.getLogger("pdfminer")
    old_level = logger.level
    try:
        logger.setLevel(logging.ERROR)
        import pdfplumber  # type: ignore
        with contextlib.redirect_stderr(io.StringIO()):
            with pdfplumber.open(str(path)) as pdf:
                if idx >= len(pdf.pages):
                    return ""
                try:
                    return _clean(pdf.pages[idx].extract_text(x_tolerance=2, y_tolerance=3, layout=False) or "")
                except Exception:
                    return ""
    except Exception:
        return ""
    finally:
        logger.setLevel(old_level)


def _detect_tesseract() -> tuple[bool, Optional[Path]]:
    found = shutil.which("tesseract")
    candidate_path: Optional[Path] = Path(found).resolve() if found else None
    if candidate_path is None and os.name == "nt":
        candidates = [
            Path(os.getenv("ProgramFiles", r"C:\Program Files")) / "Tesseract-OCR" / "tesseract.exe",
            Path(os.getenv("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Tesseract-OCR" / "tesseract.exe",
            Path(os.getenv("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR" / "tesseract.exe",
        ]
        for candidate in candidates:
            if candidate.is_file():
                candidate_path = candidate.resolve()
                break
    if candidate_path is None:
        return False, None
    parent = str(candidate_path.parent)
    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    if parent and parent not in path_parts:
        os.environ["PATH"] = parent + os.pathsep + os.environ.get("PATH", "")
    tessdata = candidate_path.parent / "tessdata"
    if tessdata.is_dir() and not os.getenv("TESSDATA_PREFIX"):
        os.environ["TESSDATA_PREFIX"] = str(tessdata)
    return True, candidate_path


def _tesseract_available() -> bool:
    return _detect_tesseract()[0]


def _ocr_languages() -> list[str]:
    requested = (os.getenv("JARBAS_OCR_LANG", "por+eng") or "por+eng").strip()
    candidates = [requested, "por+eng", "por", "eng"]
    out: list[str] = []
    for lang in candidates:
        if lang and lang not in out:
            out.append(lang)
    return out


def _try_pymupdf_ocr(doc, idx: int) -> str:
    if doc is None or not _tesseract_available():
        return ""
    try:
        page = doc.load_page(idx)
    except Exception:
        return ""
    best = ""
    for lang in _ocr_languages():
        try:
            tp = page.get_textpage_ocr(language=lang, dpi=200, full=True)
            candidate = page.get_text("text", textpage=tp, sort=True) or ""
            if _quality(candidate) > _quality(best):
                best = candidate
            if len(_clean(best)) >= 80:
                break
        except Exception:
            continue
    return _clean(best)


def ocr_capability() -> dict[str, Any]:
    available, executable = _detect_tesseract()
    return {
        "local_ocr": available,
        "engine": "Tesseract + PyMuPDF" if available else "não instalado",
        "executable": str(executable) if executable else "",
        "languages_requested": ", ".join(_ocr_languages()),
    }


def extract_pdf(
    path: Path,
    *,
    max_pages: Optional[int] = None,
    tail_pages: int = 8,
    max_chars: Optional[int] = None,
    allow_local_ocr: bool = True,
) -> PDFExtraction:
    """Extração resiliente e página-a-página.

    Ordem: PyMuPDF (principal) -> pypdf (somente páginas fracas) -> pdfplumber
    (somente se ainda fraca) -> Tesseract OCR (somente página sem texto).

    A grande diferença para a 8.3.1 é que uma biblioteca problemática nunca
    invalida o PDF inteiro. Cada página conserva o melhor texto obtido.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    with path.open("rb") as fh:
        head = fh.read(1024)
        if b"%PDF-" not in head:
            raise ValueError("O arquivo não possui cabeçalho PDF válido.")

    pymu = _pymupdf_doc(path)
    pymu_count = 0
    pymu_encrypted = False
    if pymu is not None:
        try:
            pymu_count = int(getattr(pymu, "page_count", 0) or 0)
            if bool(getattr(pymu, "needs_pass", False)):
                pymu_encrypted = True
                try:
                    if pymu.authenticate(""):
                        pymu_encrypted = False
                except Exception:
                    pass
        except Exception:
            pass

    pypdf_reader = None
    pypdf_count = 0
    pypdf_encrypted = False
    # Só precisamos abrir pypdf para descobrir contagem quando PyMuPDF falhou,
    # ou como fallback de páginas fracas. Abrir uma vez evita custo por página.
    if pymu_count <= 0:
        pypdf_reader, pypdf_count, pypdf_encrypted = _make_pypdf_reader(path)
    page_count = max(pymu_count, pypdf_count)
    if page_count <= 0:
        if pymu is not None:
            try: pymu.close()
            except Exception: pass
        return PDFExtraction([], 0, 0, 0, 0, 0.0, "error", "none", "Nenhum mecanismo conseguiu identificar páginas no PDF.", damaged=True)

    encrypted = pymu_encrypted and (pypdf_encrypted or pypdf_reader is None)
    if encrypted:
        if pymu is not None:
            try: pymu.close()
            except Exception: pass
        return PDFExtraction([], page_count, 0, 0, page_count, 0.0, "encrypted", "none", "PDF protegido por senha; não foi possível extrair conteúdo.", encrypted=True)

    indexes = _page_indexes(page_count, max_pages, tail_pages)
    pages: list[PDFPage] = []
    engines_used: dict[str, int] = {}
    total_chars = 0
    text_pages = 0
    char_budget = max_chars if max_chars and max_chars > 0 else None
    fallback_pages = 0
    ocr_pages = 0

    for idx in indexes:
        candidates: list[tuple[str, str]] = []
        py_text = _extract_pymupdf_page(pymu, idx)
        candidates.append(("pymupdf", py_text))
        engine, text = _choose_best(candidates)

        if not _good_enough(text):
            fallback_pages += 1
            if pypdf_reader is None:
                pypdf_reader, pypdf_count, pypdf_encrypted = _make_pypdf_reader(path)
            pypdf_text = _extract_pypdf_page(pypdf_reader, idx)
            candidates.append(("pypdf", pypdf_text))
            engine, text = _choose_best(candidates)

        if not _good_enough(text):
            plumber_text = _extract_pdfplumber_page(path, idx)
            candidates.append(("pdfplumber", plumber_text))
            engine, text = _choose_best(candidates)

        if allow_local_ocr and len(_clean(text)) < 35:
            ocr_text = _try_pymupdf_ocr(pymu, idx)
            if _quality(ocr_text) > _quality(text):
                engine, text = "tesseract_ocr", _clean(ocr_text)
                ocr_pages += 1

        if char_budget is not None and total_chars + len(text) > char_budget:
            remaining = max(0, char_budget - total_chars)
            text = text[:remaining]

        chars = len(text)
        q = _quality(text)
        if chars >= 30:
            text_pages += 1
        total_chars += chars
        engines_used[engine] = engines_used.get(engine, 0) + 1
        pages.append(PDFPage(page=idx + 1, text=text, engine=engine, chars=chars, quality=q))
        if char_budget is not None and total_chars >= char_budget:
            break

    if pymu is not None:
        try: pymu.close()
        except Exception: pass

    sampled = len(pages)
    empty_pages = sum(1 for p in pages if p.chars < 30)
    coverage = (text_pages / sampled) if sampled else 0.0
    if not pages or total_chars == 0:
        status = "needs_ocr"
    elif coverage < 0.55:
        status = "partial_ocr"
    else:
        status = "indexed"

    engine_summary = ", ".join(f"{k}:{v}" for k, v in sorted(engines_used.items(), key=lambda kv: (-kv[1], kv[0]))) or "none"
    scope_label = "amostra" if max_pages and page_count > sampled else "documento"
    note_parts = [
        f"Leitura resiliente ({engine_summary}).",
        f"Cobertura textual do {scope_label}: {coverage*100:.0f}% ({text_pages}/{sampled} páginas processadas).",
    ]
    if fallback_pages:
        note_parts.append(f"Fallback necessário em {fallback_pages} página(s).")
    if ocr_pages:
        note_parts.append(f"OCR local usado em {ocr_pages} página(s).")
    if status == "needs_ocr":
        note_parts.append("Nenhum texto utilizável foi extraído; requer OCR local ou leitura direta pela OpenAI.")
    elif status == "partial_ocr":
        note_parts.append("Há páginas sem texto suficiente; o Copiloto deve complementar com o PDF original pela OpenAI.")
    if max_pages and page_count > sampled:
        note_parts.append(f"Prévia amostral: {sampled} de {page_count} páginas.")

    return PDFExtraction(
        pages=pages,
        page_count=page_count,
        text_chars=total_chars,
        text_pages=text_pages,
        empty_pages=empty_pages,
        coverage=coverage,
        status=status,
        engine_summary=engine_summary,
        note=" ".join(note_parts),
        encrypted=False,
        damaged=False,
    )


def preview_dict(path: Path, *, max_pages: int = 60, tail_pages: int = 8, max_chars: int = 180_000) -> dict[str, Any]:
    result = extract_pdf(path, max_pages=max_pages, tail_pages=tail_pages, max_chars=max_chars, allow_local_ocr=True)
    page_dicts = [{"page": p.page, "text": p.text, "engine": p.engine, "quality": p.quality} for p in result.pages]
    joined = "\n".join(f"--- PAGINA {p['page']} ---\n{p['text']}" for p in page_dicts)
    return {
        "text": joined[:max_chars],
        "pages": page_dicts,
        "page_count": result.page_count,
        "sampled_pages": len(page_dicts),
        "empty_sampled_pages": result.empty_pages,
        "text_chars": min(len(joined), max_chars),
        "coverage": result.coverage,
        "status": result.status,
        "engine_summary": result.engine_summary,
        "note": result.note,
    }
