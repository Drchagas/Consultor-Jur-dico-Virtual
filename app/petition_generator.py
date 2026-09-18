from __future__ import annotations

import html
import re
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

BASE_DIR = Path(__file__).resolve().parent.parent


def row_to_dict(row: Any) -> dict:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    try:
        return {k: row[k] for k in row.keys()}
    except Exception:
        return {}


def _s(value: Any) -> str:
    return str(value or "").strip()


def _join_nonempty(parts: Iterable[str], sep: str = ", ") -> str:
    return sep.join(p for p in parts if p)


def _address_from(data: dict) -> str:
    street = _join_nonempty([
        _s(data.get("address")),
        _s(data.get("address_number")),
        _s(data.get("complement")),
        _s(data.get("neighborhood")),
    ])
    city_state = "/".join(x for x in [_s(data.get("city")), _s(data.get("state"))] if x)
    tail = _join_nonempty([city_state, f"CEP {_s(data.get('zip_code'))}" if _s(data.get("zip_code")) else ""])
    return _join_nonempty([street, tail])


def party_qualification(data: Any, *, fallback_role: str = "Parte") -> str:
    d = row_to_dict(data)
    name = _s(d.get("name")) or "[NOME NÃO LOCALIZADO]"
    person_type = _s(d.get("person_type"))
    doc = _s(d.get("document"))
    rg = _s(d.get("rg"))
    nationality = _s(d.get("nationality"))
    marital = _s(d.get("marital_status"))
    profession = _s(d.get("profession"))
    phone = _s(d.get("phone"))
    email = _s(d.get("email"))
    address = _address_from(d)

    is_company = "jur" in person_type.lower() or (doc and len(re.sub(r"\D", "", doc)) == 14)
    pieces = [name]
    if is_company:
        pieces.append("pessoa jurídica de direito privado")
        if doc:
            pieces.append(f"inscrita no CNPJ sob nº {doc}")
        if _s(d.get("responsible_name")):
            pieces.append(f"neste ato representada por {_s(d.get('responsible_name'))}")
        if address:
            pieces.append(f"com sede em {address}")
    else:
        pieces.extend(x for x in [nationality, marital, profession] if x)
        if rg:
            pieces.append(f"RG nº {rg}")
        if doc:
            pieces.append(f"CPF nº {doc}")
        if address:
            pieces.append(f"residente e domiciliado(a) em {address}")
    if phone:
        pieces.append(f"telefone {phone}")
    if email:
        pieces.append(f"e-mail {email}")
    text = _join_nonempty(pieces)
    return text + ("." if text and not text.endswith(".") else "")


def build_identity_context(*, case: Any, client: Any, parties: Iterable[Any], organization: Any) -> dict:
    case_d = row_to_dict(case)
    client_d = row_to_dict(client)
    org_d = row_to_dict(organization)
    parties_d = [row_to_dict(p) for p in (parties or [])]

    # O cadastro mestre do cliente é mais completo; usa-o na parte marcada como cliente.
    normalized_parties = []
    client_id = client_d.get("id")
    for p in parties_d:
        merged = dict(p)
        if client_id and (p.get("client_id") == client_id or p.get("is_client")):
            for key, value in client_d.items():
                if value not in (None, ""):
                    merged[key] = value
            merged["is_client"] = 1
        merged["qualification"] = party_qualification(merged)
        normalized_parties.append(merged)

    if client_d and not any(p.get("is_client") for p in normalized_parties):
        c = dict(client_d)
        c["role"] = "Cliente"
        c["is_client"] = 1
        c["qualification"] = party_qualification(c)
        normalized_parties.insert(0, c)

    office_name = _s(org_d.get("brand_name")) or _s(org_d.get("name")) or "[ESCRITÓRIO A CONFIGURAR]"
    lawyer = _s(org_d.get("lawyer_name")) or "[ADVOGADO RESPONSÁVEL A CONFIGURAR]"
    oab = _s(org_d.get("oab_number")) or "[OAB A CONFIGURAR]"
    office_address = _s(org_d.get("address")) or "[ENDEREÇO PROFISSIONAL A CONFIGURAR]"
    office = {
        "name": office_name,
        "lawyer_name": lawyer,
        "oab_number": oab,
        "address": office_address,
        "city": _s(org_d.get("city")),
        "phone": _s(org_d.get("phone")),
        "email": _s(org_d.get("email")),
        "website": _s(org_d.get("website")),
        "logo_path": _s(org_d.get("logo_path")),
    }
    court = _s(case_d.get("court"))
    case_info = {
        "number": _s(case_d.get("number")),
        "title": _s(case_d.get("title")),
        "area": _s(case_d.get("area")),
        "court": court,
        "case_class": _s(case_d.get("case_class")),
        "subject": _s(case_d.get("subject")),
        "claim_value": case_d.get("claim_value") or 0,
        "addressing": f"AO JUÍZO DE {court.upper()}" if court else "[ENDEREÇAMENTO A CONFERIR]",
    }
    client_entry = next((p for p in normalized_parties if p.get("is_client")), None)
    opponents = [p for p in normalized_parties if not p.get("is_client")]
    return {
        "office": office,
        "case": case_info,
        "client": client_entry or {},
        "parties": normalized_parties,
        "opponents": opponents,
        "generation_date": date.today().strftime("%d/%m/%Y"),
    }


def identity_prompt(identity: dict) -> str:
    office = identity.get("office") or {}
    case = identity.get("case") or {}
    parts = []
    for p in identity.get("parties") or []:
        parts.append(
            f"- Papel processual: {_s(p.get('role')) or 'Parte'}; {'CLIENTE DO ESCRITÓRIO' if p.get('is_client') else 'OUTRA PARTE'}; "
            f"qualificação: {_s(p.get('qualification'))}"
        )
    return (
        "IDENTIDADE PROCESSUAL E PROFISSIONAL - USE ESTES DADOS EXATAMENTE, SEM INVENTAR:\n"
        f"Endereçamento disponível: {_s(case.get('addressing'))}\n"
        f"Processo: {_s(case.get('number')) or '[NÃO LOCALIZADO]'}\n"
        f"Classe: {_s(case.get('case_class')) or '[NÃO LOCALIZADO]'}\n"
        f"Assunto: {_s(case.get('subject')) or '[NÃO LOCALIZADO]'}\n"
        f"Escritório: {_s(office.get('name'))}\n"
        f"Advogado: {_s(office.get('lawyer_name'))}\n"
        f"OAB: {_s(office.get('oab_number'))}\n"
        f"Endereço profissional: {_s(office.get('address'))}\n"
        f"Telefone: {_s(office.get('phone')) or '[NÃO INFORMADO]'}\n"
        f"E-mail: {_s(office.get('email')) or '[NÃO INFORMADO]'}\n"
        "PARTES:\n" + ("\n".join(parts) if parts else "- [PARTES NÃO LOCALIZADAS]") + "\n"
        "A peça deve vir COMPLETA quanto à forma: endereçamento, identificação/qualificação disponível das partes, representação pelo advogado, "
        "nome da peça, síntese fática, fundamentos, pedidos, fechamento, local/data e assinatura do advogado/escritório. "
        "Não use 'já qualificado' quando a qualificação completa estiver disponível acima. Onde faltar dado, escreva [DADO NÃO LOCALIZADO/CONFERIR]."
    )


def complete_local_draft(*, draft_type: str, objective: str, excerpts: list[dict], identity: dict, matrix: dict) -> str:
    office = identity.get("office") or {}
    case = identity.get("case") or {}
    client = identity.get("client") or {}
    opponents = identity.get("opponents") or []
    client_q = _s(client.get("qualification")) or "[QUALIFICAÇÃO DO CLIENTE NÃO LOCALIZADA]"
    role = _s(client.get("role")) or "parte"
    opponent_text = "; ".join(f"{_s(p.get('role')) or 'Parte'}: {_s(p.get('qualification'))}" for p in opponents) or "[OUTRAS PARTES NÃO LOCALIZADAS]"
    lawyer = _s(office.get("lawyer_name"))
    oab = _s(office.get("oab_number"))
    office_name = _s(office.get("name"))
    representation = (
        f"por seu advogado {lawyer}, {oab}, integrante de {office_name}, com endereço profissional em {_s(office.get('address'))}, "
        f"e-mail {_s(office.get('email')) or '[NÃO INFORMADO]'}, vem, respeitosamente, à presença de Vossa Excelência, apresentar a presente"
    )
    facts = []
    for e in excerpts[:10]:
        snippet = re.sub(r"\s+", " ", _s(e.get("text"))).strip()[:700]
        facts.append(f"- {snippet} {_s(e.get('citation'))}")
    if not facts:
        facts = ["- NÃO LOCALIZADO NOS AUTOS FORNECIDOS."]
    city = _s(office.get("city")) or "[CIDADE]"
    return f"""{case.get('addressing') or '[ENDEREÇAMENTO A CONFERIR]'}

Processo nº {case.get('number') or '[NÃO LOCALIZADO]'}

{client_q}

{representation}

{draft_type.upper()}

nos autos em que figura como {role}. Demais partes identificadas: {opponent_text}

I - SÍNTESE E OBJETO
{objective or matrix.get('next_step') or '[OBJETIVO PROCESSUAL A CONFERIR]'}

II - FATOS E ELEMENTOS DOCUMENTAIS LOCALIZADOS
{chr(10).join(facts)}

III - FUNDAMENTAÇÃO JURÍDICA
A fundamentação jurídica específica deve ser conferida pelo advogado responsável à luz do último ato processual, da legislação vigente e da estratégia definida para o caso.
[PESQUISA JURISPRUDENCIAL OFICIAL PENDENTE, SE NECESSÁRIA]

IV - PEDIDOS
Diante do exposto, requer-se a apreciação da presente manifestação nos termos do objetivo processual acima, com a adoção das providências juridicamente cabíveis após a conferência final do conteúdo dos autos.
[PEDIDOS ESPECÍFICOS A CONFERIR/COMPLETAR CONFORME O ÚLTIMO ATO PROCESSUAL]

Termos em que,
Pede deferimento.

{city}, {identity.get('generation_date') or date.today().strftime('%d/%m/%Y')}.

{lawyer}
{oab}
{office_name}

CHECKLIST DE REVISÃO
- Confirmar endereçamento, competência e número do processo.
- Conferir a qualificação das partes e a representação processual.
- Confirmar última intimação, termo inicial e prazo.
- Conferir todas as referências documentais e páginas.
- Validar legislação atualizada e inserir somente jurisprudência oficial rastreável.
- Revisar pedidos e consequências processuais antes do protocolo.
"""


def _logo_file(organization: Any) -> Optional[Path]:
    org = row_to_dict(organization)
    logo = _s(org.get("logo_path"))
    candidates: list[Path] = []
    if logo.startswith("/static/"):
        candidates.append(BASE_DIR / "app" / "static" / logo[len("/static/"):])
    elif logo:
        candidates.append(Path(logo))
    candidates.append(BASE_DIR / "app" / "static" / "chagas_logo.jpeg")
    for p in candidates:
        try:
            if p.is_file():
                return p
        except Exception:
            continue
    return None


def _sanitize_markup(text: str) -> str:
    # ReportLab Paragraph aceita um subconjunto de HTML; escapar primeiro.
    return html.escape(text, quote=False).replace("\n", "<br/>")


def create_draft_pdf(*, content: str, draft_title: str, case: Any, organization: Any, parties: Iterable[Any], target: Path) -> None:
    org = row_to_dict(organization)
    case_d = row_to_dict(case)
    target.parent.mkdir(parents=True, exist_ok=True)
    primary = _s(org.get("primary_color")) or "#7b1836"
    try:
        accent = colors.HexColor(primary)
    except Exception:
        accent = colors.HexColor("#7b1836")

    styles = getSampleStyleSheet()
    normal = ParagraphStyle("JarbasNormal", parent=styles["Normal"], fontName="Helvetica", fontSize=10.5, leading=16, alignment=TA_JUSTIFY, spaceAfter=6)
    heading = ParagraphStyle("JarbasHeading", parent=normal, fontName="Helvetica-Bold", fontSize=11.5, leading=16, spaceBefore=9, spaceAfter=6, textColor=accent)
    title_style = ParagraphStyle("JarbasTitle", parent=normal, fontName="Helvetica-Bold", fontSize=14, leading=18, alignment=TA_CENTER, spaceAfter=10, textColor=accent)
    meta_style = ParagraphStyle("JarbasMeta", parent=normal, fontSize=8.5, leading=11, alignment=TA_CENTER, textColor=colors.HexColor("#555555"))
    signature_style = ParagraphStyle("JarbasSignature", parent=normal, alignment=TA_CENTER, leading=14)

    office_name = _s(org.get("brand_name")) or _s(org.get("name"))
    lawyer = _s(org.get("lawyer_name")) or "[ADVOGADO RESPONSÁVEL A CONFIGURAR]"
    oab = _s(org.get("oab_number")) or "[OAB A CONFIGURAR]"
    footer_text = " | ".join(x for x in [office_name, _s(org.get("address")), _s(org.get("phone")), _s(org.get("email"))] if x)

    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(accent)
        canvas.setLineWidth(0.7)
        canvas.line(2.0 * cm, 1.45 * cm, A4[0] - 2.0 * cm, 1.45 * cm)
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#555555"))
        canvas.drawCentredString(A4[0] / 2, 1.02 * cm, footer_text[:150])
        canvas.drawRightString(A4[0] - 2.0 * cm, 0.68 * cm, f"Página {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(str(target), pagesize=A4, leftMargin=2.2*cm, rightMargin=2.2*cm, topMargin=1.8*cm, bottomMargin=1.8*cm, title=draft_title, author=office_name or "JARBAS")
    story = []
    logo = _logo_file(org)
    if logo:
        try:
            img = Image(str(logo), width=5.3*cm, height=1.75*cm, kind="proportional")
            img.hAlign = "CENTER"
            story += [img, Spacer(1, 0.15*cm)]
        except Exception:
            pass
    if office_name:
        story.append(Paragraph(_sanitize_markup(office_name), meta_style))
    story.append(Spacer(1, 0.15*cm))
    story.append(Paragraph(_sanitize_markup(draft_title), title_style))
    if _s(case_d.get("number")):
        story.append(Paragraph(_sanitize_markup(f"Processo nº {_s(case_d.get('number'))}"), meta_style))
    story.append(Spacer(1, 0.35*cm))

    lines = (content or "").replace("\r\n", "\n").split("\n")
    for raw in lines:
        line = raw.strip()
        if not line:
            story.append(Spacer(1, 0.16*cm))
            continue
        cleaned = re.sub(r"^#{1,6}\s*", "", line)
        is_heading = bool(re.match(r"^(?:[IVXLCDM]+\s*[-–—.]|\d+[.)-]|CHECKLIST|PEDIDOS|FUNDAMENTAÇÃO|SÍNTESE|FATOS|PRELIMINAR|MÉRITO|REQUERIMENTOS|CONCLUSÃO)", cleaned, re.I)) or (cleaned.isupper() and len(cleaned) <= 100)
        if line.startswith(("- ", "• ", "* ")):
            story.append(Paragraph("• " + _sanitize_markup(line[2:].strip()), normal))
        elif is_heading:
            story.append(Paragraph(_sanitize_markup(cleaned), heading))
        else:
            story.append(Paragraph(_sanitize_markup(cleaned), normal))

    # Assinatura institucional garantida quando a IA não a produziu claramente.
    signature_blob = (content or "").lower()
    if lawyer.lower() not in signature_blob or oab.lower() not in signature_blob:
        story += [Spacer(1, 0.5*cm), Paragraph("Termos em que,<br/>Pede deferimento.", normal), Spacer(1, 0.45*cm)]
        city = _s(org.get("city")) or "[CIDADE]"
        story.append(Paragraph(_sanitize_markup(f"{city}, {date.today().strftime('%d/%m/%Y')}"), ParagraphStyle("Right", parent=normal, alignment=TA_RIGHT)))
        story += [Spacer(1, 0.55*cm), Paragraph(_sanitize_markup(f"{lawyer}<br/>{oab}<br/>{office_name}"), signature_style)]

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
