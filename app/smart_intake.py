from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

from .ai_gateway import configured as ai_configured, extract_case_metadata_from_pdf, friendly_error
from .pdf_pipeline import preview_dict

CNJ_RE = re.compile(r"(?<!\d)(\d{7})\s*[-–—]\s*(\d{2})\s*[.]\s*(\d{4})\s*[.]\s*(\d)\s*[.]\s*(\d{2})\s*[.]\s*(\d{4})(?!\d)")
LEGACY_CASE_RE = re.compile(r"(?:processo|autos|agravo|apela[cç][aã]o|recurso)?\s*(?:n[º°o.]*)?\s*[:\-]?\s*(7\d{10})(?!\d)", re.I)
CPF_RE = re.compile(r"(?<!\d)(\d{3}[.]?\d{3}[.]?\d{3}[-]?\d{2})(?!\d)")
CNPJ_RE = re.compile(r"(?<!\d)(\d{2}[.]?\d{3}[.]?\d{3}[/]?\d{4}[-]?\d{2})(?!\d)")
MONEY_RE = re.compile(r"R\$\s*([\d.]+,\d{2})", re.I)

ROLE_ALIASES: list[tuple[str, tuple[str, ...]]] = [
    ("Autor", ("autor", "autora", "requerente", "exequente", "reclamante", "impetrante", "agravante", "apelante", "polo ativo", "parte autora")),
    ("Réu", ("réu", "reu", "ré", "requerido", "requerida", "executado", "executada", "reclamado", "reclamada", "impetrado", "agravado", "apelado", "polo passivo", "parte ré", "parte re")),
    ("Interessado", ("interessado", "interessada", "inventariante", "inventariado", "curatelado", "curatelada", "alimentante", "alimentando", "alimentanda")),
]

BAD_NAME_TERMS = (
    "processo", "classe", "assunto", "valor da causa", "advogado", "advogada", "oab", "juiz", "juíza", "juiza",
    "ministério público", "ministerio publico", "promotor", "promotora", "defensor", "procurador", "tribunal", "vara", "comarca",
    "documento", "evento", "petição", "peticao", "sentença", "sentenca", "decisão", "decisao", "intimação", "intimacao",
)

COURT_TERMS = ("tribunal", "vara", "comarca", "juizado", "foro", "fórum", "forum", "turma", "câmara", "camara")


def _ascii(value: str) -> str:
    return unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", _ascii(value).lower()).strip()


def _normalize_document(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11:
        return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"
    if len(digits) == 14:
        return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"
    return value.strip()


def _clean_name(value: str) -> str:
    value = re.split(r"\s{2,}|\t|\||\bCPF\b|\bCNPJ\b|\bADVOGAD[OA]?\b|\bOAB\b|\bEndere[cç]o\b", value or "", flags=re.I)[0]
    value = re.sub(r"^[\s:;,./\-–—]+|[\s:;,./\-–—]+$", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:160].strip()


def _looks_like_name(name: str) -> bool:
    if len(name) < 3 or len(name) > 160:
        return False
    low = _norm(name)
    if any(_norm(bad) in low for bad in BAD_NAME_TERMS):
        return False
    if re.fullmatch(r"[\d\W_]+", name):
        return False
    if "@" in name or "http" in low:
        return False
    words = [w for w in re.split(r"\s+", name) if w]
    if not 1 <= len(words) <= 18:
        return False
    alpha = sum(ch.isalpha() for ch in name)
    return alpha >= max(3, len(name) // 3)


def _extract_document(text: str) -> str:
    # Prioriza CPF/CNPJ quando o rótulo aparece, mas aceita número formatado isolado no bloco da parte.
    for regex in (CNPJ_RE, CPF_RE):
        m = regex.search(text or "")
        if m:
            return _normalize_document(m.group(1))
    return ""


def _find_cnj(text: str) -> str:
    m = CNJ_RE.search(text or "")
    if m:
        return f"{m.group(1)}-{m.group(2)}.{m.group(3)}.{m.group(4)}.{m.group(5)}.{m.group(6)}"
    # Compatibilidade com numeração legada ainda presente em acervos antigos (ex.: TJRS 700xxxxxxxx).
    legacy = LEGACY_CASE_RE.search(text or "")
    return legacy.group(1) if legacy else ""


def extract_preview(path: Path, max_pages: int = 60, max_chars: int = 180_000) -> dict[str, Any]:
    """Prévia robusta usando PyMuPDF + pypdf + pdfplumber e OCR local opcional."""
    return preview_dict(Path(path), max_pages=max_pages, tail_pages=8, max_chars=max_chars)


def _extract_labeled_value(lines: list[str], labels: tuple[str, ...], max_lookahead: int = 2) -> str:
    label_norms = tuple(_norm(x) for x in labels)
    for i, raw in enumerate(lines):
        n = _norm(raw)
        for label in label_norms:
            if not (n == label or n.startswith(label + ":") or n.startswith(label + " -") or n.startswith(label + " ")):
                continue
            # valor na mesma linha
            remainder = re.sub(r"^\s*" + re.escape(raw[: len(raw)]), "", raw)
            # mais seguro: remove prefixo pelo índice do ':' / '-' quando houver
            candidate = ""
            for sep in (":", " - ", " – ", " — "):
                if sep in raw:
                    candidate = raw.split(sep, 1)[1].strip()
                    break
            if not candidate:
                raw_norm = _norm(raw)
                if raw_norm.startswith(label) and len(raw_norm) > len(label):
                    # Usa tokens originais após a quantidade aproximada de palavras do rótulo.
                    words = raw.split()
                    lw = label.split()
                    candidate = " ".join(words[len(lw):]).lstrip(":-–— ").strip()
            if candidate:
                return candidate
            for j in range(1, max_lookahead + 1):
                if i + j < len(lines):
                    nxt = lines[i + j].strip()
                    if nxt and len(nxt) <= 220:
                        return nxt
    return ""


EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
PHONE_RE = re.compile(r"(?<!\d)(?:\+?55\s*)?(?:\(?\d{2}\)?\s*)?(?:9\s*)?\d{4}[\s.-]?\d{4}(?!\d)")
CEP_RE = re.compile(r"(?<!\d)(\d{5}[-.]?\d{3})(?!\d)")
RG_LABEL_RE = re.compile(r"\b(?:RG|R\.G\.|identidade)\s*[:º°n\.]*\s*([A-Z0-9.\-/]{5,30})", re.I)


def _extract_qualification(context: str) -> dict[str, str]:
    """Extrai apenas dados objetivamente visíveis no bloco textual próximo à parte."""
    raw = context or ""
    low = _norm(raw)
    email = (EMAIL_RE.search(raw).group(0) if EMAIL_RE.search(raw) else "")[:160]
    phone_m = PHONE_RE.search(raw)
    phone = re.sub(r"\s+", " ", phone_m.group(0)).strip() if phone_m else ""
    cep_m = CEP_RE.search(raw)
    zip_code = cep_m.group(1) if cep_m else ""
    rg_m = RG_LABEL_RE.search(raw)
    rg = rg_m.group(1).strip(" .,:;") if rg_m else ""
    person_type = "Pessoa Jurídica" if CNPJ_RE.search(raw) else ("Pessoa Física" if CPF_RE.search(raw) else "")
    # Endereço somente quando há rótulo claro. Evita transformar cabeçalho do foro em endereço do cliente.
    address = ""
    m = re.search(r"\bendere[cç]o\s*[:\-]\s*([^\n|]{8,260})", raw, re.I)
    if m:
        address = re.sub(r"\s+", " ", m.group(1)).strip(" .,:;")[:300]
    state = ""
    m = re.search(r"\b(?:UF|Estado)\s*[:\-]\s*([A-Z]{2})\b", raw, re.I)
    if m: state = m.group(1).upper()
    nationality = "Brasileiro(a)" if "brasileir" in low else ""
    # Os campos abaixo só são preenchidos quando há rótulo explícito no mesmo bloco.
    profession = ""
    m = re.search(r"\bprofiss[aã]o\s*[:\-]\s*([^,;|\n]{2,100})", raw, re.I)
    if m: profession = re.sub(r"\s+", " ", m.group(1)).strip()[:120]
    marital_status = ""
    m = re.search(r"\bestado\s+civil\s*[:\-]\s*([^,;|\n]{2,60})", raw, re.I)
    if m: marital_status = re.sub(r"\s+", " ", m.group(1)).strip()[:80]
    return {
        "person_type": person_type, "nationality": nationality, "marital_status": marital_status,
        "profession": profession, "rg": rg, "address": address, "city": "", "state": state,
        "zip_code": zip_code, "phone": phone, "email": email,
    }


def _is_role_label(line: str) -> bool:
    n = _norm(line)
    return any(n == _norm(a) or n.startswith(_norm(a) + ":") or n.startswith(_norm(a) + " -") or n.startswith(_norm(a) + " ") for _, aliases in ROLE_ALIASES for a in aliases)


def _party_context(lines: list[str], start: int, max_lines: int = 7) -> str:
    picked: list[str] = []
    for j in range(start, min(len(lines), start + max_lines)):
        if j > start and _is_role_label(lines[j]):
            break
        picked.append(lines[j])
    return "\n".join(picked)


def _party_candidates_from_pages(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parties: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in pages:
        raw_lines = [re.sub(r"\s+", " ", x).strip() for x in (page["text"] or "").splitlines() if x.strip()]
        for i, line in enumerate(raw_lines):
            nline = _norm(line)
            for role, aliases in ROLE_ALIASES:
                matched_alias = next((a for a in aliases if nline == _norm(a) or nline.startswith(_norm(a) + ":") or nline.startswith(_norm(a) + " -") or nline.startswith(_norm(a) + " ")), None)
                if not matched_alias:
                    continue
                candidate = ""
                # Tenta conteúdo após o rótulo preservando acentos e grafia do documento.
                # A versão anterior aplicava _ascii() à linha inteira e podia transformar
                # "JOÃO" em "JOAO" no cadastro automático.
                for sep in (":", " - ", " – ", " — "):
                    if sep in line:
                        left, right = line.split(sep, 1)
                        if _norm(left).strip() == _norm(matched_alias):
                            candidate = right.strip()
                            break
                if not candidate and nline.startswith(_norm(matched_alias) + " "):
                    words = line.split()
                    label_words = matched_alias.split()
                    candidate = " ".join(words[len(label_words):]).lstrip(":-–— ").strip()
                if not candidate:
                    # Cabeçalhos de PJe/eproc frequentemente colocam nome na linha seguinte.
                    for j in range(1, 3):
                        if i + j < len(raw_lines):
                            nxt = raw_lines[i + j].strip()
                            if _looks_like_name(_clean_name(nxt)):
                                candidate = nxt
                                break
                name = _clean_name(candidate)
                if not _looks_like_name(name):
                    continue
                key = re.sub(r"\W+", "", _norm(name))
                if not key or key in seen:
                    continue
                context = _party_context(raw_lines, i, 7)
                seen.add(key)
                qualification = _extract_qualification(context)
                parties.append({
                    "name": name,
                    "role": matched_alias.strip().title() if matched_alias else role,
                    "document": _extract_document(context),
                    **qualification,
                    "source_page": page["page"],
                    "source_excerpt": context[:360],
                    "verified_from_text": True,
                })
                if len(parties) >= 24:
                    return parties
    return parties


def _detect_area(text: str) -> str:
    low = _norm(text)
    rules = [
        ("Trabalhista", ("reclamante", "reclamado", "justica do trabalho", "trt", "vara do trabalho")),
        ("Criminal", ("acao penal", "denunciado", "acusado", "inquerito policial", "crime", "criminal")),
        ("Família", ("alimentos", "divorcio", "guarda", "uniao estavel", "vara de familia", "paternidade")),
        ("Sucessões", ("inventario", "arrolamento", "espolio", "sucessao", "herdeiro")),
        ("Previdenciário", ("inss", "beneficio previdenciario", "aposentadoria", "previdenciario")),
        ("Tributário", ("execucao fiscal", "tributario", "imposto", "icms", "iss", "iptu")),
        ("Empresarial", ("recuperacao judicial", "falencia", "societario", "sociedade empresaria", "empresarial")),
        ("Eleitoral", ("tribunal regional eleitoral", "tre-", "eleitoral", "candidato")),
        ("Cível", ("autor", "reu", "indenizacao", "obrigacao", "contrato", "execucao", "civil")),
    ]
    scores = [(sum(1 for term in terms if term in low), area) for area, terms in rules]
    scores.sort(reverse=True)
    return scores[0][1] if scores and scores[0][0] else "Outro"


def _detect_court(lines: list[str]) -> str:
    candidates: list[tuple[int, str]] = []
    for pos, line in enumerate(lines[:350]):
        low = _norm(line)
        if not any(_norm(t) in low for t in COURT_TERMS):
            continue
        if not 5 <= len(line) <= 220:
            continue
        score = 0
        if "vara" in low: score += 5
        if "comarca" in low: score += 4
        if "tribunal" in low: score += 3
        if "juizado" in low: score += 3
        if "turma" in low or "camara" in low: score += 2
        score += max(0, 4 - pos // 60)
        candidates.append((score, line))
    candidates.sort(reverse=True, key=lambda x: x[0])
    if candidates:
        return candidates[0][1]
    # Fallback conservador para siglas explícitas de tribunais quando não há Vara/Comarca.
    joined = " ".join(lines[:120])
    m = re.search(r"\b(STF|STJ|TST|TJ[A-Z]{2}|TRF\s*\d|TRT\s*\d|TRE[-/ ]?[A-Z]{2})\b", joined, re.I)
    return m.group(1).upper().replace(" ", "") if m else ""


def _detect_class_subject(lines: list[str]) -> tuple[str, str]:
    case_class = _extract_labeled_value(lines, ("Classe", "Classe processual", "Classe judicial", "Procedimento"))
    subject = _extract_labeled_value(lines, ("Assunto", "Assuntos", "Assunto principal"))
    if not case_class:
        # Muitos espelhos antigos exibem a classe como título em caixa alta, sem rótulo.
        patterns = (
            r"^(A[CÇ][AÃ]O\s+DE\s+.{3,150})$", r"^(EXECU[CÇ][AÃ]O\s+DE\s+.{3,150})$",
            r"^(CUMPRIMENTO\s+DE\s+SENTEN[CÇ]A.{0,120})$", r"^(MANDADO\s+DE\s+SEGURAN[CÇ]A.{0,120})$",
            r"^(HABEAS\s+CORPUS.{0,120})$", r"^(RECLAMA[CÇ][AÃ]O\s+TRABALHISTA.{0,120})$",
        )
        for raw in lines[:180]:
            candidate = re.sub(r"\s+", " ", raw).strip()
            if 5 <= len(candidate) <= 180 and any(re.match(p, candidate, re.I) for p in patterns):
                case_class = candidate
                break
    return _clean_name(case_class)[:180], _clean_name(subject)[:220]


def _detect_claim_value(text: str) -> str:
    patterns = (
        r"valor\s+da\s+causa\s*[:\-]?\s*R\$\s*([\d.]+,\d{2})",
        r"valor\s+atribu[ií]do\s+(?:à|a)\s+causa\s*[:\-]?\s*R\$\s*([\d.]+,\d{2})",
        r"(?:^|\n)\s*valor\s*[:\-]\s*R\$\s*([\d.]+,\d{2})",
    )
    for pattern in patterns:
        m = re.search(pattern, text or "", re.I | re.M)
        if m:
            return "R$ " + m.group(1)
    return ""


def _local_detect(path: Path) -> dict[str, Any]:
    preview = extract_preview(path)
    text = preview["text"]
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip() and not line.startswith("--- PAGINA")]
    number = _find_cnj(text)
    court = _detect_court(lines)
    case_class, subject = _detect_class_subject(lines)
    area = _detect_area(text)
    parties = _party_candidates_from_pages(preview["pages"])
    if len(parties) >= 2:
        title = f"{parties[0]['name']} x {parties[1]['name']}"
    elif parties:
        title = f"Processo de {parties[0]['name']}"
    elif number:
        title = f"Processo {number}"
    else:
        title = path.stem[:140]
    quality = "alta" if number and len(parties) >= 2 else "media" if number or parties else "baixa"
    warnings: list[str] = []
    if preview.get("status") == "needs_ocr":
        warnings.append("O PDF é digitalizado (imagem), sem texto para ler. Rode INSTALAR_OCR.cmd para habilitar a leitura local, ou configure a chave da Anthropic; sem um dos dois, preencha os campos à mão.")
    elif preview.get("status") == "partial_ocr":
        warnings.append("A leitura do texto saiu parcial. Confira os campos preenchidos: com a chave da Anthropic configurada, o JARBAS lê as páginas restantes direto do PDF.")
    if not parties:
        warnings.append("Partes não reconhecidas com segurança pela leitura textual local.")
    return {
        "number": number,
        "court": court,
        "area": area,
        "title": title,
        "case_class": case_class,
        "subject": subject,
        "claim_value": _detect_claim_value(text),
        "parties": parties,
        "preview_chars": preview["text_chars"],
        "page_count": preview["page_count"],
        "sampled_pages": preview["sampled_pages"],
        "text_available": bool(text.strip()),
        "confidence": quality,
        "warnings": warnings,
        "extraction_method": f"local_multi_engine:{preview.get('engine_summary','')}",
        "extraction_status": preview.get("status", "indexed"),
        "text_coverage": preview.get("coverage", 0.0),
        "extraction_note": preview.get("note", ""),
        "ai_used": False,
        "ai_model": "",
        "ai_error": "",
    }


def _sanitize_ai_party(p: dict[str, Any]) -> dict[str, Any] | None:
    name = _clean_name(str(p.get("name") or ""))
    if not _looks_like_name(name):
        return None
    role_raw = _clean_name(str(p.get("role") or "Parte")) or "Parte"
    document = _normalize_document(str(p.get("document") or ""))
    page = p.get("source_page")
    try:
        page = int(page) if page is not None else None
    except Exception:
        page = None
    return {
        "name": name,
        "role": role_raw[:60],
        "document": document,
        "person_type": str(p.get("person_type") or "")[:40],
        "nationality": str(p.get("nationality") or "")[:80],
        "marital_status": str(p.get("marital_status") or "")[:80],
        "profession": str(p.get("profession") or "")[:120],
        "rg": str(p.get("rg") or "")[:80],
        "address": str(p.get("address") or "")[:300],
        "city": str(p.get("city") or "")[:120],
        "state": str(p.get("state") or "")[:40],
        "zip_code": str(p.get("zip_code") or "")[:30],
        "phone": str(p.get("phone") or "")[:60],
        "email": str(p.get("email") or "")[:160],
        "source_page": page,
        "source_excerpt": str(p.get("source_excerpt") or "")[:360],
        "verified_from_text": False,
    }


def _merge(local: dict[str, Any], ai_data: dict[str, Any], ai_model: str, ai_input_tokens: int = 0, ai_output_tokens: int = 0) -> dict[str, Any]:
    merged = dict(local)
    for key in ("number", "court", "area", "title", "case_class", "subject", "claim_value"):
        ai_value = ai_data.get(key)
        if isinstance(ai_value, str) and ai_value.strip():
            # Para número CNJ, conserva o local se já tiver sido identificado pela regex objetiva.
            if key == "number" and local.get("number"):
                continue
            merged[key] = ai_value.strip()[:500]
    ai_parties = []
    for item in ai_data.get("parties") or []:
        if isinstance(item, dict):
            sanitized = _sanitize_ai_party(item)
            if sanitized:
                ai_parties.append(sanitized)
    if ai_parties:
        # Deduplica por nome; prioriza os dados estruturados da IA, complementando documento local quando possível.
        local_by_name = {re.sub(r"\W+", "", _norm(p["name"])): p for p in local.get("parties") or []}
        parties = []
        seen = set()
        for p in ai_parties + list(local.get("parties") or []):
            key = re.sub(r"\W+", "", _norm(p["name"]))
            if not key or key in seen:
                continue
            seen.add(key)
            if key in local_by_name:
                local_party = local_by_name[key]
                for field in ("document","person_type","nationality","marital_status","profession","rg","address","city","state","zip_code","phone","email"):
                    if not p.get(field) and local_party.get(field):
                        p[field] = local_party.get(field)
            parties.append(p)
        merged["parties"] = parties[:24]
    merged["confidence"] = str(ai_data.get("confidence") or local.get("confidence") or "media").lower()
    merged["warnings"] = list(dict.fromkeys((local.get("warnings") or []) + [str(x) for x in (ai_data.get("warnings") or []) if str(x).strip()]))
    merged["extraction_method"] = "openai_pdf+local_text" if local.get("text_available") else "openai_pdf"
    merged["ai_used"] = True
    merged["ai_model"] = ai_model
    merged["ai_error"] = ""
    merged["ai_input_tokens"] = int(ai_input_tokens or 0)
    merged["ai_output_tokens"] = int(ai_output_tokens or 0)
    if len(merged.get("parties") or []) >= 2 and (not merged.get("title") or merged.get("title", "").startswith("Processo ")):
        merged["title"] = f"{merged['parties'][0]['name']} x {merged['parties'][1]['name']}"
    return merged


def detect_case_metadata(path: Path, *, prefer_ai: bool = True) -> dict[str, Any]:
    """Intake híbrido: extração local + leitura do PDF pela OpenAI quando configurada.

    O retorno sempre existe mesmo se a OpenAI falhar. Nenhum dado é automaticamente
    cadastrado sem a tela de confirmação do advogado.
    """
    local = _local_detect(Path(path))
    if not prefer_ai or not ai_configured():
        if prefer_ai and not ai_configured():
            local["warnings"].append("IA não configurada: os campos abaixo vieram da leitura local do texto do PDF. Confira tudo antes de confirmar.")
        return local
    try:
        ai_data, result = extract_case_metadata_from_pdf(Path(path))
        return _merge(local, ai_data, result.model, result.input_tokens, result.output_tokens)
    except Exception as exc:
        local["ai_error"] = friendly_error(exc) if not isinstance(exc, RuntimeError) else str(exc)
        local["warnings"].append(f"A leitura IA do PDF falhou; dados abaixo vieram do extrator local. Motivo: {local['ai_error'][:220]}")
        return local
