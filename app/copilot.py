from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from .petition_generator import identity_prompt, complete_local_draft
from .pdf_pipeline import extract_pdf as robust_extract_pdf
from .database import DATA_DIR

from .ai_gateway import (
    configured as gateway_configured,
    legal_model_name as gateway_legal_model,
    ask as gateway_ask,
    ask_with_pdf_files as gateway_ask_with_pdf_files,
)

STOPWORDS = {
    "a","ao","aos","aquela","aquele","as","com","como","da","das","de","dela","dele","do","dos",
    "e","em","entre","era","essa","esse","esta","este","foi","for","há","isso","mais","mas","na",
    "nas","no","nos","o","os","ou","para","pela","pelo","por","que","se","sem","ser","sua","suas",
    "seu","seus","um","uma","uns","umas","já","também","não","sim","quando","onde","qual","quais",
    "processo","autos","parte","partes","fls","pagina","página",
}

PROCEDURAL_LABELS = [
    ("Sentença", ("sentença", "julgo procedente", "julgo improcedente", "dispositivo")),
    ("Decisão", ("decisão", "defiro", "indefiro", "tutela de urgência", "despacho")),
    ("Intimação", ("intimação", "intime-se", "fica intimado", "vista às partes", "vista as partes")),
    ("Laudo pericial", ("laudo pericial", "perito", "perícia", "quesitos")),
    ("Contestação", ("contestação", "contesta", "réu")),
    ("Réplica/Impugnação", ("réplica", "impugnação à contestação", "impugnação a contestação")),
    ("Petição inicial", ("petição inicial", "dos fatos", "dos pedidos", "requerente", "autor")),
    ("Recurso", ("apelação", "agravo de instrumento", "recurso especial", "recurso extraordinário")),
    ("Audiência", ("audiência", "termo de audiência", "depoimento", "testemunha")),
    ("Prova documental", ("comprovante", "contrato", "nota fiscal", "extrato", "documento")),
]

ADVERSE_TERMS = {
    "prescrição": 5,
    "prescrito": 5,
    "intempestivo": 5,
    "intempestividade": 5,
    "improcedente": 4,
    "improcedência": 4,
    "indeferido": 3,
    "indeferimento": 3,
    "ausência de prova": 5,
    "falta de prova": 5,
    "não comprovou": 4,
    "não demonstrou": 4,
    "ilegitimidade": 4,
    "incompetência": 4,
    "preclusão": 4,
    "revelia": 4,
    "confissão": 3,
    "contradição": 2,
    "ônus da prova": 2,
    "sucumbência": 2,
}

DATE_RE = re.compile(r"\b(?:0?[1-9]|[12]\d|3[01])[./-](?:0?[1-9]|1[0-2])[./-](?:19|20)\d{2}\b")
EVENT_RE = re.compile(r"\b(?:evento|ev\.)\s*(\d{1,6})\b", re.I)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower()


def safe_filename(name: str) -> str:
    name = Path(name or "processo.pdf").name
    stem = unicodedata.normalize("NFKD", Path(name).stem).encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r"[^a-zA-Z0-9._-]+", "_", stem).strip("._")[:90] or "processo"
    return f"{stem}.pdf"


def resolve_uploaded_pdf_path(path_value: str, *, org_id: int | None = None, case_id: int | None = None, stored_name: str = "") -> Optional[Path]:
    """Resolve PDFs persistidos após upgrades/migrações sem abrir caminhos arbitrários.

    Prioriza a raiz canônica data/uploads/<org>/<case>. Se o banco ainda apontar
    para uma instalação antiga, tenta o stored_name/basename na raiz canônica.
    """
    upload_root = (DATA_DIR / "uploads").resolve(strict=False)
    expected_dir = upload_root
    if org_id is not None:
        expected_dir = expected_dir / str(org_id)
    if case_id is not None:
        expected_dir = expected_dir / str(case_id)
    expected_dir = expected_dir.resolve(strict=False)

    candidates: list[Path] = []
    raw = Path(path_value) if path_value else None
    if raw:
        candidates.append(raw)
    if stored_name:
        candidates.append(expected_dir / Path(stored_name).name)
    if raw and raw.name:
        candidates.append(expected_dir / raw.name)

    seen=set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=False)
            key=str(resolved).lower()
            if key in seen: continue
            seen.add(key)
            # Só aceita arquivos dentro da raiz canônica de uploads.
            resolved.relative_to(upload_root)
            if resolved.is_file():
                return resolved
        except Exception:
            continue

    # Último recurso controlado: procura o nome do arquivo apenas dentro do dossiê esperado.
    names=[]
    if stored_name: names.append(Path(stored_name).name)
    if raw and raw.name: names.append(raw.name)
    for name in names:
        try:
            for hit in expected_dir.glob(name):
                resolved=hit.resolve(strict=False); resolved.relative_to(upload_root)
                if resolved.is_file(): return resolved
        except Exception:
            continue
    return None


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def classify_page(text: str) -> str:
    low = normalize(text)
    best_label = "Documento"
    best_score = 0
    for label, terms in PROCEDURAL_LABELS:
        score = sum(1 for term in terms if normalize(term) in low)
        if score > best_score:
            best_score, best_label = score, label
    return best_label


def split_chunks(text: str, max_chars: int = 4200, overlap: int = 450) -> list[str]:
    text = re.sub(r"\r\n?", "\n", text or "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            cut = max(text.rfind("\n\n", start, end), text.rfind(". ", start, end))
            if cut > start + max_chars // 2:
                end = cut + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def extract_pdf(path: Path) -> dict:
    """Extração robusta multi-engine.

    Compara PyMuPDF, pypdf e pdfplumber página a página e, quando Tesseract já
    existe na máquina, tenta OCR local nas páginas sem camada textual.
    """
    result = robust_extract_pdf(Path(path), allow_local_ocr=True)
    pages = []
    for page in result.pages:
        text = page.text
        pages.append({
            "page": page.page,
            "text": text,
            "label": classify_page(text) if text else "Sem texto extraível",
            "engine": page.engine,
            "quality": page.quality,
        })
    return {
        "pages": pages,
        "page_count": result.page_count,
        "note": result.note,
        "status": result.status,
        "coverage": result.coverage,
        "engine_summary": result.engine_summary,
    }


def index_pdf(conn, *, org_id: int, case_id: int, document_id: int, path: Path) -> dict:
    """Indexação idempotente: reprocessar um PDF não duplica páginas/chunks e falhas não deixam índice parcial."""
    result = extract_pdf(path)
    now = datetime.now().isoformat(timespec="seconds")
    total_chars = 0
    chunk_count = 0
    conn.execute("DELETE FROM document_chunks WHERE organization_id=? AND case_id=? AND document_id=?",(org_id,case_id,document_id))
    conn.execute("DELETE FROM document_pages WHERE organization_id=? AND case_id=? AND document_id=?",(org_id,case_id,document_id))
    try:
        for item in result["pages"]:
            text = item["text"]
            total_chars += len(text)
            conn.execute(
                """INSERT INTO document_pages (organization_id,case_id,document_id,page_number,text,label,created_at) VALUES (?,?,?,?,?,?,?)""",
                (org_id, case_id, document_id, item["page"], text, item["label"], now),
            )
            for chunk_index, chunk in enumerate(split_chunks(text)):
                conn.execute(
                    """INSERT INTO document_chunks (organization_id,case_id,document_id,page_number,chunk_index,text,label,created_at) VALUES (?,?,?,?,?,?,?,?)""",
                    (org_id, case_id, document_id, item["page"], chunk_index, chunk, item["label"], now),
                )
                chunk_count += 1
    except Exception:
        conn.execute("DELETE FROM document_chunks WHERE organization_id=? AND case_id=? AND document_id=?",(org_id,case_id,document_id))
        conn.execute("DELETE FROM document_pages WHERE organization_id=? AND case_id=? AND document_id=?",(org_id,case_id,document_id))
        raise
    status = result.get("status") or ("indexed" if total_chars else "needs_ocr")
    conn.execute(
        """UPDATE case_documents SET page_count=?,text_chars=?,status=?,extraction_note=? WHERE id=? AND organization_id=?""",
        (result["page_count"], total_chars, status, result["note"], document_id, org_id),
    )
    return {"page_count": result["page_count"], "text_chars": total_chars, "chunks": chunk_count, "status": status, "note": result["note"]}


def tokenize(query: str) -> list[str]:
    tokens = re.findall(r"[\wÀ-ÿ]{3,}", normalize(query))
    return [t for t in tokens if t not in STOPWORDS]


def _score(text: str, query: str, tokens: list[str]) -> float:
    low = normalize(text)
    q = normalize(query).strip()
    score = 0.0
    if q and len(q) >= 6 and q in low:
        score += 18.0
    counts = Counter(re.findall(r"[a-z0-9À-ÿ]+", low))
    for token in tokens:
        c = counts.get(token, 0)
        if c:
            score += 2.5 + min(c, 5) * 0.7
    # bônus para termos próximos em consultas compostas
    if len(tokens) >= 2 and all(t in low for t in tokens[: min(4, len(tokens))]):
        score += 5
    return score


def search_case(conn, *, org_id: int, case_id: int, query: str, limit: int = 10) -> list[dict]:
    """Busca nos autos. Usa o índice quando existe; senão varre em memória."""
    from . import search_index
    indexado = search_index.buscar(conn, org_id=org_id, case_id=case_id,
                                   consulta=query, limite=limit)
    if indexado is not None:
        return indexado
    return _search_case_varredura(conn, org_id=org_id, case_id=case_id,
                                  query=query, limit=limit)


def _search_case_varredura(conn, *, org_id: int, case_id: int, query: str,
                           limit: int = 10) -> list[dict]:
    """Método antigo: carrega todos os chunks do caso. Só como reserva."""
    rows = conn.execute(
        """SELECT ch.id,ch.document_id,ch.page_number,ch.chunk_index,ch.text,ch.label,d.original_name
           FROM document_chunks ch JOIN case_documents d ON d.id=ch.document_id
           WHERE ch.organization_id=? AND ch.case_id=? AND d.organization_id=?""",
        (org_id, case_id, org_id),
    ).fetchall()
    tokens = tokenize(query)
    scored = []
    for r in rows:
        score = _score(r["text"], query, tokens)
        if score > 0:
            scored.append((score, r))
    scored.sort(key=lambda x: (-x[0], x[1]["page_number"], x[1]["chunk_index"]))
    out = []
    for score, r in scored[:limit]:
        text = r["text"].strip()
        snippet = text[:1500]
        out.append({
            "score": round(score, 2),
            "chunk_id": r["id"],
            "document_id": r["document_id"],
            "document": r["original_name"],
            "page": r["page_number"],
            "label": r["label"],
            "text": text,
            "snippet": snippet,
            "citation": f"[{r['original_name']} · p. {r['page_number']}]",
        })
    return out


def recent_context(conn, *, org_id: int, case_id: int, limit: int = 18) -> list[dict]:
    rows = conn.execute(
        """SELECT ch.id,ch.document_id,ch.page_number,ch.chunk_index,ch.text,ch.label,d.original_name
           FROM document_chunks ch JOIN case_documents d ON d.id=ch.document_id
           WHERE ch.organization_id=? AND ch.case_id=?
           ORDER BY d.id DESC,ch.page_number DESC,ch.chunk_index DESC LIMIT ?""",
        (org_id, case_id, limit),
    ).fetchall()
    return [
        {"chunk_id": r["id"], "document_id": r["document_id"], "document": r["original_name"], "page": r["page_number"],
         "label": r["label"], "text": r["text"], "snippet": r["text"][:1500], "citation": f"[{r['original_name']} · p. {r['page_number']}]"}
        for r in rows
    ]


def timeline(conn, *, org_id: int, case_id: int, limit: int = 40) -> list[dict]:
    rows = conn.execute(
        """SELECT p.page_number,p.text,p.label,d.original_name,d.id document_id
           FROM document_pages p JOIN case_documents d ON d.id=p.document_id
           WHERE p.organization_id=? AND p.case_id=? ORDER BY d.id,p.page_number""",
        (org_id, case_id),
    ).fetchall()
    items = []
    seen = set()
    for r in rows:
        text = r["text"] or ""
        events = EVENT_RE.findall(text[:3500])
        dates = DATE_RE.findall(text[:3500])
        if not events and not dates and r["label"] == "Documento":
            continue
        key = (r["document_id"], r["page_number"])
        if key in seen:
            continue
        seen.add(key)
        event = f"Evento {events[0]}" if events else ""
        date_text = dates[0] if dates else ""
        items.append({
            "event": event,
            "date": date_text,
            "label": r["label"],
            "document": r["original_name"],
            "page": r["page_number"],
            "snippet": re.sub(r"\s+", " ", text)[:360],
        })
        if len(items) >= limit:
            break
    return items


def case_stats(conn, *, org_id: int, case_id: int) -> dict:
    docs = conn.execute(
        """SELECT COUNT(*) c,COALESCE(SUM(page_count),0) pages,COALESCE(SUM(text_chars),0) chars,
                  COALESCE(SUM(size_bytes),0) bytes,
                  COALESCE(SUM(CASE WHEN status IN ('needs_ocr','partial_ocr','error','encrypted') THEN 1 ELSE 0 END),0) ocr
           FROM case_documents WHERE organization_id=? AND case_id=?""",
        (org_id, case_id),
    ).fetchone()
    chunks = conn.execute(
        "SELECT COUNT(*) c FROM document_chunks WHERE organization_id=? AND case_id=?", (org_id, case_id)
    ).fetchone()["c"]
    return {"documents": docs["c"], "pages": docs["pages"], "chars": docs["chars"], "bytes": docs["bytes"], "needs_ocr": docs["ocr"], "chunks": chunks}


def infer_measure(excerpts: list[dict]) -> dict:
    if not excerpts:
        return {
            "title": "Não determinada",
            "confidence": "Baixa",
            "reason": "Não há texto indexado suficiente nos autos para identificar a providência processual.",
            "cautions": ["Faça upload da intimação/decisão mais recente ou do processo integral."],
            "sources": [],
        }
    joined = "\n".join(e["text"] for e in excerpts[:12])
    low = normalize(joined)
    candidates: list[tuple[int, str, str]] = []
    rules = [
        ("manifestacao sobre laudo pericial", 8, ("laudo pericial", "manifestem-se sobre o laudo", "manifestação sobre o laudo", "manifestacao sobre o laudo"),
         "Há referência recente a laudo/perícia e possível abertura de vista."),
        ("replica / impugnacao a contestacao", 7, ("contestação", "apresente réplica", "apresente replica", "impugnar a contestação", "impugnar a contestacao"),
         "Há referência recente à contestação ou abertura de prazo para resposta da parte autora."),
        ("contrarrazoes", 7, ("contrarrazões", "contrarrazoes", "intime-se para contrarrazoar", "contrarrazoar"),
         "Há referência expressa a contrarrazões ou intimação para resposta a recurso."),
        ("embargos de declaracao - avaliar", 5, ("sentença", "acórdão", "acordao", "omissão", "omissao", "contradição", "contradicao", "erro material"),
         "Há decisão/sentença e termos que podem exigir exame de omissão, contradição, obscuridade ou erro material."),
        ("recurso de apelacao - avaliar", 4, ("sentença", "julgo improcedente", "julgo procedente"),
         "Há sentença nos trechos recentes; o cabimento, interesse e prazo recursal precisam ser conferidos."),
        ("agravo de instrumento - avaliar hipoteses legais", 4, ("decisão interlocutória", "decisao interlocutoria", "tutela de urgência", "tutela de urgencia"),
         "Há aparente decisão interlocutória. O agravo depende de enquadramento legal e análise específica do conteúdo."),
        ("manifestacao em cumprimento de intimacao", 3, ("intime-se", "fica intimado", "vista às partes", "vista as partes"),
         "Há comando de intimação/vista nos trechos mais recentes."),
    ]
    for title, weight, terms, reason in rules:
        hits = sum(1 for t in terms if normalize(t) in low)
        if hits:
            candidates.append((weight + hits, title, reason))
    candidates.sort(reverse=True)
    sources = [{"citation": e["citation"], "label": e["label"]} for e in excerpts[:5]]
    if not candidates:
        return {
            "title": "Manifestação específica não identificada",
            "confidence": "Baixa",
            "reason": "Os trechos recentes não contêm comando processual suficientemente claro para sugerir uma peça com segurança.",
            "cautions": ["Localize a última intimação/decisão e confirme o prazo antes de qualquer protocolo."],
            "sources": sources,
        }
    _, title, reason = candidates[0]
    confidence = "Média" if candidates[0][0] < 9 else "Alta documental"
    return {
        "title": title.title(),
        "confidence": confidence,
        "reason": reason,
        "cautions": [
            "Sugestão documental, não decisão automática de cabimento.",
            "Conferir a íntegra da intimação/decisão, data da ciência, prazo, legitimidade e competência.",
            "Antes de protocolar, validar legislação e jurisprudência em fontes oficiais.",
        ],
        "sources": sources,
    }


def hard_truth(excerpts: list[dict]) -> dict:
    findings = []
    for e in excerpts:
        low = normalize(e["text"])
        hits = []
        weight = 0
        for term, w in ADVERSE_TERMS.items():
            if normalize(term) in low:
                hits.append(term)
                weight += w
        if hits:
            findings.append({
                "weight": weight,
                "terms": hits,
                "citation": e["citation"],
                "document": e["document"],
                "page": e["page"],
                "snippet": re.sub(r"\s+", " ", e["text"])[:650],
            })
    findings.sort(key=lambda x: -x["weight"])
    total = sum(f["weight"] for f in findings[:8])
    level = "Baixo sinal adverso"
    if total >= 20:
        level = "Alto sinal adverso"
    elif total >= 9:
        level = "Sinal adverso moderado"
    return {
        "level": level,
        "findings": findings[:8],
        "note": "O Hard Truth procura linguagem potencialmente desfavorável nos autos. A presença de um termo não significa que a tese esteja perdida; exige leitura contextual pelo advogado.",
    }



def hard_truth_enhanced(excerpts: list[dict], case_meta: dict, pdf_paths: Optional[list[Path]] = None) -> dict:
    result = hard_truth(excerpts)
    if not ai_available():
        return result
    pdf_paths = [Path(p) for p in (pdf_paths or []) if p]
    prompt = (
        f"CASO: {json.dumps(case_meta, ensure_ascii=False)}\n\n"
        "Execute HARD TRUTH: procure ativamente fatos, documentos, decisões, inconsistências e lacunas que possam prejudicar a tese do cliente. "
        "Separe: riscos processuais, probatórios, jurídicos e estratégicos; argumentos prováveis da parte contrária; pontos que precisam de prova; "
        "e providências defensivas. Não invente jurisprudência. Cite a fonte documental quando houver. Se algo não estiver nos autos, diga não localizado."
    )
    try:
        if pdf_paths:
            supplemental = ("\n\nTRECHOS LOCAIS RECUPERADOS (use apenas como apoio e confira no PDF):\n" + format_context(excerpts, 22000)) if excerpts else ""
            result["ai_text"] = call_ai_with_pdfs(SYSTEM_RULES, prompt + supplemental + "\n\nLeia diretamente os PDFs anexados e confira o conteúdo integral, inclusive páginas sem camada textual.", pdf_paths)
        elif excerpts:
            result["ai_text"] = call_ai(SYSTEM_RULES, prompt + "\n\nTRECHOS RECUPERADOS:\n" + format_context(excerpts, 42000))
        else:
            result["ai_text"] = "Nenhum conteúdo documental disponível para análise IA."
    except Exception as exc:
        if excerpts:
            try:
                result["ai_text"] = call_ai(SYSTEM_RULES, prompt + "\n\nA leitura direta do PDF falhou; use estes trechos locais como fallback:\n" + format_context(excerpts, 42000))
            except Exception:
                result["ai_text"] = f"A camada IA do Hard Truth não pôde ser executada: {str(exc)}"
        else:
            result["ai_text"] = f"A camada IA do Hard Truth não pôde ser executada: {str(exc)}"
    return result

def format_context(excerpts: list[dict], max_chars: int = 36000) -> str:
    blocks = []
    used = 0
    for e in excerpts:
        block = f"FONTE {e['citation']} | {e['label']}\n{e['text'].strip()}\n"
        if used + len(block) > max_chars:
            remaining = max_chars - used
            if remaining > 800:
                blocks.append(block[:remaining])
            break
        blocks.append(block)
        used += len(block)
    return "\n---\n".join(blocks)


def ai_available() -> bool:
    return gateway_configured()


def ai_model() -> str:
    return gateway_legal_model()


def call_ai(instructions: str, user_input: str) -> str:
    result = gateway_ask(instructions, user_input, profile="legal", reasoning_effort="high")
    return result.text


def call_ai_with_pdfs(instructions: str, user_input: str, pdf_paths: Iterable[Path]) -> str:
    result = gateway_ask_with_pdf_files(
        instructions, user_input, pdf_paths, profile="legal", reasoning_effort="high"
    )
    return result.text


SYSTEM_RULES = """
Você é o JARBAS Copiloto Jurídico. Trabalhe como ferramenta auxiliar de advogado brasileiro.
REGRAS ABSOLUTAS:
1. Não invente fatos, documentos, datas, prazos, eventos, números de processo, jurisprudência, súmulas, temas, artigos ou citações.
2. Baseie afirmações factuais SOMENTE nos trechos fornecidos. Toda afirmação sobre os autos deve trazer a fonte no formato [arquivo.pdf · p. N].
3. Quando não houver suporte documental, escreva explicitamente: NÃO LOCALIZADO NOS AUTOS FORNECIDOS.
4. Não trate hipótese de peça ou prazo como certeza. Diferencie: fato localizado, inferência, risco e ponto a confirmar.
5. Jurisprudência: não crie nem cite precedentes de memória. Se não houver precedentes fornecidos/validados, escreva PESQUISA JURISPRUDENCIAL OFICIAL PENDENTE.
6. Legislação: se houver dúvida sobre dispositivo específico, sinalize CONFERIR TEXTO LEGAL ATUALIZADO antes de protocolar.
7. A saída é minuta técnica auxiliar e exige revisão humana antes de qualquer protocolo ou orientação definitiva.
8. Seja técnico, objetivo, estratégico e crítico; considere também argumentos contrários.
""".strip()


def answer_question(question: str, excerpts: list[dict], case_meta: dict, pdf_paths: Optional[list[Path]] = None, pdf_review_required: bool = False) -> str:
    pdf_paths = [Path(p) for p in (pdf_paths or []) if p]
    pdf_direct_preferred = os.getenv("JARBAS_AI_PDF_ALWAYS", "1").strip() != "0"
    if ai_available() and pdf_paths and (pdf_direct_preferred or pdf_review_required or not excerpts):
        supplemental = ("\n\nTRECHOS LOCAIS RECUPERADOS (podem ser parciais):\n" + format_context(excerpts, 18000)) if excerpts else ""
        try:
            return call_ai_with_pdfs(
                SYSTEM_RULES,
                f"CASO: {case_meta.get('title','')} | Nº {case_meta.get('number') or 'não informado'}\n"
                f"PERGUNTA DO ADVOGADO: {question}\n" + supplemental + "\n\n"
                "Leia diretamente os PDFs anexados e confira também páginas digitalizadas ou sem camada textual. "
                "Responda apenas com o que estiver nos arquivos e cite arquivo/página quando possível. "
                "Se não localizar, diga NÃO LOCALIZADO NOS AUTOS FORNECIDOS.",
                pdf_paths,
            )
        except Exception:
            # A API pode recusar PDFs muito grandes. Se houver índice local, continue pelo RAG textual.
            if not excerpts:
                raise
    if not excerpts:
        return "NÃO LOCALIZADO NOS AUTOS FORNECIDOS. Nenhum trecho indexado correspondeu à pergunta. Se o PDF estiver digitalizado, configure a chave da Anthropic (CONFIGURAR_IA.cmd) ou instale o Tesseract para OCR local."
    context = format_context(excerpts)
    if ai_available():
        return call_ai(
            SYSTEM_RULES,
            f"CASO: {case_meta.get('title','')} | Nº {case_meta.get('number') or 'não informado'}\n"
            f"PERGUNTA DO ADVOGADO: {question}\n\nTRECHOS RECUPERADOS:\n{context}\n\n"
            "Responda primeiro de forma direta; depois indique as fontes e, se necessário, o que ainda precisa ser conferido.",
        )
    # Modo documental sem IA: devolve evidência ranqueada, sem inferência livre.
    lines = ["Modo documental local: foram localizados os seguintes trechos relevantes:"]
    for e in excerpts[:6]:
        snippet = re.sub(r"\s+", " ", e["text"]).strip()[:650]
        lines.append(f"\n{e['citation']} — {e['label']}\n{snippet}")
    lines.append("\nA interpretação conclusiva requer o módulo de IA conectado ou revisão direta pelo advogado.")
    return "\n".join(lines)


def deep_analysis(excerpts: list[dict], case_meta: dict, matrix: dict, pdf_paths: Optional[list[Path]] = None) -> str:
    pdf_paths = [Path(p) for p in (pdf_paths or []) if p]
    if ai_available() and pdf_paths:
        supplemental = ("\n\nTRECHOS LOCAIS RELEVANTES (apoio; o PDF original prevalece):\n" + format_context(excerpts, 24000)) if excerpts else ""
        try:
            return call_ai_with_pdfs(
                SYSTEM_RULES,
                f"CASO: {json.dumps(case_meta, ensure_ascii=False)}\nMATRIZ CADASTRADA: {json.dumps(matrix, ensure_ascii=False)}" + supplemental + "\n\n"
                "Faça análise estratégica do CONJUNTO DOS PDFs anexados. Não limite a análise ao índice local. Leia páginas com texto e páginas digitalizadas. "
                "Estruture: resumo executivo; cronologia; fatos e fontes; pedidos/teses das partes; decisões; provas; pontos favoráveis; "
                "vulnerabilidades/Hard Truth; contradições; lacunas; providências; hipótese de medida processual; prazos a confirmar; checklist. "
                "Não invente jurisprudência e cite arquivo/página quando possível.",
                pdf_paths,
            )
        except Exception:
            if not excerpts:
                raise
    if not excerpts:
        return "NÃO LOCALIZADO NOS AUTOS FORNECIDOS. Envie documentos com texto extraível, ou configure a chave da Anthropic (CONFIGURAR_IA.cmd) para ler PDFs digitalizados."
    if ai_available():
        return call_ai(
            SYSTEM_RULES,
            f"CASO: {json.dumps(case_meta, ensure_ascii=False)}\n"
            f"MATRIZ CADASTRADA: {json.dumps(matrix, ensure_ascii=False)}\n\n"
            f"TRECHOS DOS AUTOS:\n{format_context(excerpts, 48000)}\n\n"
            "Produza ANÁLISE ESTRATÉGICA em seções: (1) resumo executivo; (2) fatos comprovados e respectivas fontes; "
            "(3) pontos favoráveis; (4) vulnerabilidades/Hard Truth; (5) contradições ou lacunas; (6) providências; "
            "(7) hipótese de medida processual, deixando claro o que precisa de confirmação; (8) checklist antes do protocolo. "
            "Não cite jurisprudência que não tenha sido fornecida.",
        )
    measure = infer_measure(excerpts)
    truth = hard_truth(excerpts)
    lines = [
        "ANÁLISE DOCUMENTAL LOCAL (sem IA externa)",
        f"Hipótese de providência: {measure['title']} — confiança {measure['confidence']}.",
        measure["reason"],
        "\nHard Truth:",
        f"Classificação: {truth['level']}.",
    ]
    for f in truth["findings"][:5]:
        lines.append(f"- {', '.join(f['terms'])}: {f['citation']}")
    if not truth["findings"]:
        lines.append("- Nenhum termo adverso forte foi localizado pelo filtro textual; isso NÃO significa ausência de risco jurídico.")
    lines.append("\nConecte a IA nas configurações de ambiente para síntese jurídica aprofundada. A conferência humana permanece obrigatória.")
    return "\n".join(lines)


def draft_petition(*, draft_type: str, objective: str, excerpts: list[dict], case_meta: dict, matrix: dict, pdf_paths: Optional[list[Path]] = None, identity: Optional[dict] = None) -> str:
    """Gera peça completa quanto à forma e aos dados disponíveis.

    A identidade processual/profissional vem do cadastro estruturado do JARBAS.
    O modelo não recebe autorização para inventar partes, advogado, OAB ou escritório.
    """
    pdf_paths = [Path(p) for p in (pdf_paths or []) if p]
    identity = identity or {}
    identity_block = identity_prompt(identity) if identity else "IDENTIDADE PROCESSUAL: [DADOS NÃO FORNECIDOS/CONFERIR]"
    mandatory = (
        "\n\nREQUISITOS OBRIGATÓRIOS DA MINUTA:\n"
        "- Entregue a peça COMPLETA, e não um roteiro, resumo ou esqueleto.\n"
        "- Comece pelo endereçamento disponível no cadastro; se ausente, use [ENDEREÇAMENTO A CONFERIR].\n"
        "- Informe o número do processo quando cadastrado.\n"
        "- Identifique integralmente o cliente com a qualificação disponível e nomeie as demais partes identificadas.\n"
        "- Indique expressamente o advogado responsável, OAB e escritório conforme os dados institucionais fornecidos.\n"
        "- Estruture: endereçamento; qualificação/representação; nome da peça; síntese fática; fundamentos; pedidos; fechamento; local/data; assinatura.\n"
        "- Fatos dos autos devem manter referência [arquivo.pdf · p. N] sempre que a fonte estiver disponível.\n"
        "- NÃO invente endereço, CPF/CNPJ, RG, profissão, estado civil, advogado, OAB, fatos, prazo, evento, artigo ou precedente.\n"
        "- Onde um dado indispensável não estiver disponível, use [DADO NÃO LOCALIZADO/CONFERIR].\n"
        "- Jurisprudência somente se tiver sido fornecida/validada; caso contrário: [PESQUISA JURISPRUDENCIAL OFICIAL PENDENTE].\n"
        "- Não encerre a resposta dizendo apenas o que deveria ser feito: redija a peça em linguagem forense completa, sujeita à revisão humana."
    )
    if ai_available() and pdf_paths:
        supplemental = ("\n\nTRECHOS LOCAIS RELEVANTES (apoio; confira no PDF):\n" + format_context(excerpts, 26000)) if excerpts else ""
        try:
            return call_ai_with_pdfs(
                SYSTEM_RULES,
                f"TIPO DE PEÇA SOLICITADO: {draft_type}\nOBJETIVO DO ADVOGADO: {objective}\n"
                f"CASO: {json.dumps(case_meta, ensure_ascii=False)}\nMATRIZ: {json.dumps(matrix, ensure_ascii=False)}\n\n"
                f"{identity_block}{mandatory}" + supplemental + "\n\n"
                "Leia diretamente o CONJUNTO DOS PDFs anexados antes de redigir. Considere também páginas digitalizadas/sem texto local. "
                "Redija a peça completa para revisão humana, use os dados de identidade fornecidos sem alterá-los e cite arquivo/página quando possível.",
                pdf_paths,
            )
        except Exception:
            if not excerpts:
                raise
    if not excerpts:
        # Mesmo sem índice/IA, a peça formal é produzida com os dados estruturados, marcando o que falta.
        return complete_local_draft(draft_type=draft_type, objective=objective, excerpts=[], identity=identity, matrix=matrix)
    if ai_available():
        return call_ai(
            SYSTEM_RULES,
            f"TIPO DE PEÇA SOLICITADO: {draft_type}\nOBJETIVO DO ADVOGADO: {objective}\n"
            f"CASO: {json.dumps(case_meta, ensure_ascii=False)}\nMATRIZ: {json.dumps(matrix, ensure_ascii=False)}\n\n"
            f"{identity_block}{mandatory}\n\n"
            f"AUTOS RECUPERADOS:\n{format_context(excerpts, 52000)}\n\n"
            "Redija agora a MINUTA COMPLETA em português jurídico brasileiro. Use a qualificação completa disponível em IDENTIDADE PROCESSUAL, "
            "identifique advogado/escritório na representação e assinatura e preserve as citações documentais."
        )
    return complete_local_draft(draft_type=draft_type, objective=objective, excerpts=excerpts, identity=identity, matrix=matrix)

def store_uploaded_pdf(upload_file, destination: Path, max_bytes: int) -> tuple[int, str]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    h = hashlib.sha256()
    with destination.open("wb") as out:
        while True:
            block = upload_file.file.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            if size > max_bytes:
                out.close()
                destination.unlink(missing_ok=True)
                raise ValueError(f"Arquivo excede o limite de {max_bytes // (1024*1024)} MB.")
            h.update(block)
            out.write(block)
    with destination.open("rb") as fh:
        header = fh.read(1024)
        if b"%PDF-" not in header:
            destination.unlink(missing_ok=True)
            raise ValueError("O arquivo enviado não possui cabeçalho PDF válido.")
    return size, h.hexdigest()


def remove_document_files(path: Optional[str]) -> None:
    if not path:
        return
    try:
        p = Path(path).resolve(strict=False)
        upload_root = (DATA_DIR / "uploads").resolve(strict=False)
        p.relative_to(upload_root)
        if p.is_file():
            p.unlink(missing_ok=True)
    except Exception:
        # Caminho fora da raiz de uploads ou erro de filesystem: não remove nada.
        pass
