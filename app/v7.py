from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, HTMLResponse

from .database import db
from .smart_intake import detect_case_metadata
from .copilot import index_pdf, safe_filename, store_uploaded_pdf, search_case, recent_context, resolve_uploaded_pdf_path
from .document_generator import create_power_of_attorney, create_ajg_declaration
from .ai_gateway import (
    configured as ai_configured, model_name, ask as ai_ask, ask_with_pdf_files as ai_ask_with_pdf_files, OFFICE_RULES,
    connection_status as ai_connection_status, friendly_error, test_connection as ai_test_connection, test_credentials as ai_test_credentials,
    save_local_config as ai_save_local_config, clear_local_api_key as ai_clear_local_api_key,
    legal_model_name, intake_model_name, routine_model_name,
)

router = APIRouter()
BASE_DIR = Path(__file__).resolve().parent.parent
IMPORT_ROOT = BASE_DIR / "data" / "imports"
GENERATED_ROOT = BASE_DIR / "data" / "generated"
UPLOAD_ROOT = BASE_DIR / "data" / "uploads"
for p in (IMPORT_ROOT, GENERATED_ROOT, UPLOAD_ROOT):
    p.mkdir(parents=True, exist_ok=True)


def core():
    from . import main
    return main


def _org_user(request: Request):
    c = core()
    return c.require_workspace(request)


def _csrf(request: Request, token: str):
    c = core()
    if not c.valid_csrf(request, token):
        return c.csrf_error()
    return None


def _money(v) -> float:
    try:
        return round(float(v or 0), 2)
    except Exception:
        return 0.0


def _brl_money(v) -> float:
    if isinstance(v, (int, float)):
        return max(_money(v), 0)
    raw=str(v or "").strip().replace("R$", "").replace(" ", "")
    if not raw: return 0.0
    if "," in raw:
        raw=raw.replace(".", "").replace(",", ".")
    try: return max(round(float(raw),2),0)
    except Exception: return 0.0


def _normalize_identity(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _client_financial_summary(conn, org_id: int, client_id: int) -> dict:
    c = core()
    rows = [r for r in c.financial_transaction_rows(conn, org_id, 5000) if r["client_id"] == client_id]
    contracted = conn.execute("SELECT COALESCE(SUM(contract_value),0) s FROM fee_contracts WHERE organization_id=? AND client_id=? AND status!='Cancelado'", (org_id, client_id)).fetchone()["s"]
    paid = sum(_money(r["paid_amount"]) for r in rows if r["direction"] == "receivable")
    receivable = sum(max(_money(r["original_amount"]) - _money(r["paid_amount"]), 0) for r in rows if r["direction"] == "receivable" and r["status"] != "Cancelado")
    expenses = sum(_money(r["paid_amount"]) for r in rows if r["direction"] == "payable")
    overdue = sum(max(_money(r["original_amount"]) - _money(r["paid_amount"]), 0) for r in rows if r["direction"] == "receivable" and r["status"] == "Vencido")
    return {"contracted": _money(contracted), "paid": round(paid,2), "receivable": round(receivable,2), "expenses": round(expenses,2), "overdue": round(overdue,2), "margin_realized": round(paid-expenses,2), "rows": rows}


@router.get("/intake", response_class=HTMLResponse)
def intelligent_intake(request: Request):
    c = core(); user, org = _org_user(request)
    if isinstance(org, RedirectResponse): return org
    with db() as conn:
        imports = conn.execute("SELECT * FROM case_imports WHERE organization_id=? ORDER BY id DESC LIMIT 12", (org["id"],)).fetchall()
    intake_error = request.session.pop("intake_error", "")
    return c.safe_template_response("smart_intake.html", c.common_context(
        request,user,org,imports=imports,step="upload",intake_error=intake_error,ai_status=ai_connection_status()
    ))


@router.post("/intake/process-pdf")
def intelligent_intake_upload(request: Request, file: UploadFile = File(...), analysis_mode: str = Form("auto"), csrf: str = Form("", alias="_csrf")):
    c = core(); bad = _csrf(request, csrf)
    if bad: return bad
    user, org = _org_user(request)
    if isinstance(org, RedirectResponse): return org
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        return RedirectResponse("/intake?error=pdf", status_code=303)
    max_mb = max(5, int(os.getenv("JARBAS_MAX_UPLOAD_MB", "200")))
    dest_dir = IMPORT_ROOT / str(org["id"]); dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:10]}_{safe_filename(file.filename)}"
    try:
        size, sha = store_uploaded_pdf(file, dest, max_mb * 1024 * 1024)
        del size
        parsed = detect_case_metadata(dest, prefer_ai=(analysis_mode != "local"))
        with db() as conn:
            import_id = conn.insert_id(
                "INSERT INTO case_imports (organization_id,user_id,original_name,temp_path,sha256,parsed_json,status,created_at) VALUES (?,?,?,?,?,?,'pending_client',?)",
                (org["id"], user["id"], file.filename, str(dest), sha, json.dumps(parsed, ensure_ascii=False), datetime.now().isoformat(timespec="seconds")),
            )
        if parsed.get("ai_used"):
            credits = max(1, int((int(parsed.get("ai_input_tokens",0))+int(parsed.get("ai_output_tokens",0)))/12000)+1)
            with db() as conn:
                conn.execute(
                    "INSERT INTO usage_ledger (organization_id,user_id,kind,credits,description,created_at) VALUES (?,?, 'intake_ai',?,?,?)",
                    (org["id"],user["id"],credits,f"Intake PDF — {parsed.get('ai_model') or 'OpenAI'}",datetime.now().isoformat(timespec="seconds")),
                )
        c.log_action(request, f"Intake inteligente: PDF analisado — {file.filename} — {parsed.get('extraction_method','local')}")
        return RedirectResponse(f"/intake/{import_id}", status_code=303)
    except Exception as exc:
        try: dest.unlink(missing_ok=True)
        except Exception: pass
        request.session["intake_error"] = str(exc)[:300]
        return RedirectResponse("/intake?error=analysis", status_code=303)


@router.get("/intake/{import_id}", response_class=HTMLResponse)
def intelligent_intake_choose_client(request: Request, import_id: int):
    c = core(); user, org = _org_user(request)
    if isinstance(org, RedirectResponse): return org
    with db() as conn:
        row = conn.execute("SELECT * FROM case_imports WHERE id=? AND organization_id=?", (import_id, org["id"])).fetchone()
        clients = conn.execute("SELECT id,name,document FROM clients WHERE organization_id=? ORDER BY name", (org["id"],)).fetchall()
    if not row: return RedirectResponse("/intake", status_code=303)
    parsed = json.loads(row["parsed_json"] or "{}")
    intake_error = request.session.pop("intake_error", "")
    return c.safe_template_response("smart_intake.html", c.common_context(
        request,user,org,import_row=row,parsed=parsed,clients=clients,step="choose",intake_error=intake_error,ai_status=ai_connection_status()
    ))


@router.post("/intake/{import_id}/confirm")
def intelligent_intake_confirm(
    request: Request, import_id: int, party_index: str = Form(""), existing_client_id: str = Form(""), manual_client_name: str = Form(""),
    manual_document: str = Form(""), title: str = Form(""), area: str = Form(""), court: str = Form(""), number: str = Form(""),
    csrf: str = Form("", alias="_csrf"),
):
    c = core(); bad = _csrf(request, csrf)
    if bad: return bad
    user, org = _org_user(request)
    if isinstance(org, RedirectResponse): return org
    now = datetime.now().isoformat(timespec="seconds")
    with db() as conn:
        imp = conn.execute("SELECT * FROM case_imports WHERE id=? AND organization_id=? AND status='pending_client'", (import_id, org["id"])).fetchone()
        if not imp: return RedirectResponse("/intake", status_code=303)
        parsed = json.loads(imp["parsed_json"] or "{}")
        client_id = None
        if existing_client_id:
            found = conn.execute("SELECT id FROM clients WHERE id=? AND organization_id=?", (int(existing_client_id), org["id"])).fetchone()
            client_id = found["id"] if found else None
        selected = None
        if client_id is None and party_index != "":
            try:
                selected = (parsed.get("parties") or [])[int(party_index)]
            except Exception:
                selected = None
        candidate_name = (selected or {}).get("name", "").strip() or manual_client_name.strip()
        candidate_doc = (selected or {}).get("document", "").strip() or manual_document.strip()
        if client_id is None and candidate_name:
            found = None
            if candidate_doc:
                found = conn.execute("SELECT id FROM clients WHERE organization_id=? AND document=? LIMIT 1", (org["id"], candidate_doc)).fetchone()
            selected_data = selected or {}
            if not found and selected_data.get("email"):
                found = conn.execute("SELECT id FROM clients WHERE organization_id=? AND lower(email)=lower(?) LIMIT 1", (org["id"], selected_data.get("email", "").strip())).fetchone()
            if not found and selected_data.get("phone"):
                phone_digits=_normalize_identity(selected_data.get("phone", ""))
                if phone_digits:
                    candidates=conn.execute("SELECT id,phone FROM clients WHERE organization_id=? AND COALESCE(phone,'')<>''", (org["id"],)).fetchall()
                    found=next((r for r in candidates if _normalize_identity(r["phone"])==phone_digits), None)
            # Não há merge implícito apenas por nome: homônimos são comuns e isso pode unir dossiês distintos.
            person_type = (selected_data.get("person_type") or ("Pessoa Jurídica" if len(re.sub(r"\D","",candidate_doc))==14 else "Pessoa Física")).strip()
            if found:
                client_id = found["id"]
                # Enriquece somente campos ainda vazios; nunca sobrescreve cadastro previamente revisado.
                conn.execute(
                    """UPDATE clients SET
                       document=CASE WHEN COALESCE(document,'')='' THEN ? ELSE document END,
                       phone=CASE WHEN COALESCE(phone,'')='' THEN ? ELSE phone END,
                       email=CASE WHEN COALESCE(email,'')='' THEN ? ELSE email END,
                       person_type=CASE WHEN COALESCE(person_type,'')='' THEN ? ELSE person_type END,
                       nationality=CASE WHEN COALESCE(nationality,'')='' THEN ? ELSE nationality END,
                       marital_status=CASE WHEN COALESCE(marital_status,'')='' THEN ? ELSE marital_status END,
                       profession=CASE WHEN COALESCE(profession,'')='' THEN ? ELSE profession END,
                       rg=CASE WHEN COALESCE(rg,'')='' THEN ? ELSE rg END,
                       address=CASE WHEN COALESCE(address,'')='' THEN ? ELSE address END,
                       city=CASE WHEN COALESCE(city,'')='' THEN ? ELSE city END,
                       state=CASE WHEN COALESCE(state,'')='' THEN ? ELSE state END,
                       zip_code=CASE WHEN COALESCE(zip_code,'')='' THEN ? ELSE zip_code END,
                       updated_at=? WHERE id=? AND organization_id=?""",
                    (candidate_doc, selected_data.get("phone", ""), selected_data.get("email", ""), person_type,
                     selected_data.get("nationality", ""), selected_data.get("marital_status", ""), selected_data.get("profession", ""),
                     selected_data.get("rg", ""), selected_data.get("address", ""), selected_data.get("city", ""), selected_data.get("state", ""),
                     selected_data.get("zip_code", ""), now, client_id, org["id"]),
                )
            else:
                client_id = conn.insert_id(
                    """INSERT INTO clients
                       (organization_id,name,document,phone,email,notes,created_at,person_type,nationality,marital_status,profession,rg,address,city,state,zip_code,updated_at)
                       VALUES (?,?,?,?,?,'Criado automaticamente pelo Intake Inteligente',?,?,?,?,?,?,?,?,?,?,?)""",
                    (org["id"], candidate_name, candidate_doc, selected_data.get("phone", ""), selected_data.get("email", ""), now, person_type,
                     selected_data.get("nationality", ""), selected_data.get("marital_status", ""), selected_data.get("profession", ""),
                     selected_data.get("rg", ""), selected_data.get("address", ""), selected_data.get("city", ""), selected_data.get("state", ""),
                     selected_data.get("zip_code", ""), now),
                )
        if client_id is None:
            request.session["intake_error"] = "Selecione ou informe quem será o cliente."
            return RedirectResponse(f"/intake/{import_id}", status_code=303)
        chosen_client = conn.execute("SELECT * FROM clients WHERE id=? AND organization_id=?", (client_id,org["id"])).fetchone()
        if chosen_client and not candidate_name:
            candidate_name = chosen_client["name"] or ""
            candidate_doc = chosen_client["document"] or ""
        case_id = conn.insert_id(
            """INSERT INTO cases (organization_id,client_id,number,title,area,court,case_class,subject,claim_value,status,risk,facts,evidence,strategy,next_step,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,'Ativo','Médio','','','','',?,?)""",
            (org["id"], client_id, number.strip() or parsed.get("number", ""), title.strip() or parsed.get("title", "Processo importado"), area.strip() or parsed.get("area", "Outro"), court.strip() or parsed.get("court", ""),
             str(parsed.get("case_class") or "").strip(), str(parsed.get("subject") or "").strip(), _brl_money(parsed.get("claim_value")), now, now),
        )
        # Persiste todas as partes detectadas, não apenas o cliente escolhido. Isso evita perder o polo contrário e a fonte da extração.
        selected_key = re.sub(r"\W+", "", (candidate_name or "").lower())
        for p in parsed.get("parties") or []:
            if not isinstance(p, dict) or not str(p.get("name") or "").strip():
                continue
            pname=str(p.get("name") or "").strip()[:180]
            is_client=1 if re.sub(r"\W+", "", pname.lower()) == selected_key else 0
            linked_client=client_id if is_client else None
            conn.execute(
                """INSERT INTO case_parties (organization_id,case_id,client_id,name,role,document,person_type,nationality,marital_status,profession,rg,address,city,state,zip_code,phone,email,source_page,source_excerpt,is_client,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (org["id"],case_id,linked_client,pname,str(p.get("role") or "Parte")[:80],str(p.get("document") or "")[:40],str(p.get("person_type") or "")[:50],
                 str(p.get("nationality") or "")[:80],str(p.get("marital_status") or "")[:80],str(p.get("profession") or "")[:140],str(p.get("rg") or "")[:80],str(p.get("address") or "")[:360],
                 str(p.get("city") or "")[:120],str(p.get("state") or "")[:40],str(p.get("zip_code") or "")[:30],str(p.get("phone") or "")[:80],str(p.get("email") or "")[:180],
                 p.get("source_page") if isinstance(p.get("source_page"), int) else None,str(p.get("source_excerpt") or "")[:500],is_client,now),
            )
        marked = conn.execute("SELECT id FROM case_parties WHERE organization_id=? AND case_id=? AND is_client=1 LIMIT 1",(org["id"],case_id)).fetchone()
        if not marked:
            cli=chosen_client or conn.execute("SELECT * FROM clients WHERE id=? AND organization_id=?",(client_id,org["id"])).fetchone()
            if cli:
                conn.execute("INSERT INTO case_parties (organization_id,case_id,client_id,name,role,document,person_type,phone,email,is_client,created_at) VALUES (?,?,?,?,?,?,?,?,?,1,?)",
                             (org["id"],case_id,client_id,cli["name"],"Cliente principal",cli["document"],cli["person_type"],cli["phone"],cli["email"],now))
        source = Path(imp["temp_path"])
        case_dir = UPLOAD_ROOT / str(org["id"]) / str(case_id); case_dir.mkdir(parents=True, exist_ok=True)
        stored_name = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}_{safe_filename(imp['original_name'])}"
        target = case_dir / stored_name
        shutil.move(str(source), str(target))
        size = target.stat().st_size
        doc_id = conn.insert_id(
            """INSERT INTO case_documents (organization_id,case_id,original_name,stored_name,stored_path,sha256,mime_type,size_bytes,page_count,text_chars,status,extraction_note,created_at)
               VALUES (?,?,?,?,?,?, 'application/pdf',?,0,0,'uploaded','',?)""",
            (org["id"],case_id,imp["original_name"],stored_name,str(target),imp["sha256"],size,now),
        )
        try:
            index_pdf(conn, org_id=org["id"], case_id=case_id, document_id=doc_id, path=target)
        except Exception as exc:
            conn.execute("UPDATE case_documents SET status='error',extraction_note=? WHERE id=? AND organization_id=?", (f"Falha de extração: {type(exc).__name__}: {str(exc)[:250]}", doc_id, org["id"]))
        conn.execute("UPDATE case_imports SET status='completed' WHERE id=? AND organization_id=?", (import_id, org["id"]))
    c.log_action(request, f"Intake inteligente concluído: cliente #{client_id}, caso #{case_id}")
    return RedirectResponse(f"/cases/{case_id}?smart_import=1", status_code=303)


@router.post("/clients/{client_id}/profile")
def update_client_profile(
    request: Request, client_id: int, name: str = Form(...), person_type: str = Form("Pessoa Física"), document: str = Form(""), rg: str = Form(""),
    nationality: str = Form(""), marital_status: str = Form(""), profession: str = Form(""), birth_date: str = Form(""), phone: str = Form(""), email: str = Form(""),
    address: str = Form(""), address_number: str = Form(""), complement: str = Form(""), neighborhood: str = Form(""), city: str = Form(""), state: str = Form(""), zip_code: str = Form(""),
    responsible_name: str = Form(""), notes: str = Form(""), csrf: str = Form("", alias="_csrf"),
):
    c = core(); bad = _csrf(request, csrf)
    if bad: return bad
    user, org = _org_user(request)
    if isinstance(org, RedirectResponse): return org
    with db() as conn:
        conn.execute(
            """UPDATE clients SET name=?,person_type=?,document=?,rg=?,nationality=?,marital_status=?,profession=?,birth_date=?,phone=?,email=?,address=?,address_number=?,complement=?,neighborhood=?,city=?,state=?,zip_code=?,responsible_name=?,notes=?,updated_at=? WHERE id=? AND organization_id=?""",
            (name.strip(),person_type,document.strip(),rg.strip(),nationality.strip(),marital_status.strip(),profession.strip(),birth_date or None,phone.strip(),email.strip(),address.strip(),address_number.strip(),complement.strip(),neighborhood.strip(),city.strip(),state.strip().upper(),zip_code.strip(),responsible_name.strip(),notes.strip(),datetime.now().isoformat(timespec="seconds"),client_id,org["id"]),
        )
    c.log_action(request, f"Cliente #{client_id}: cadastro completo atualizado")
    return RedirectResponse(f"/clients/{client_id}?saved=1", status_code=303)


@router.post("/clients/{client_id}/documents/generate")
def generate_client_document(
    request: Request, client_id: int, document_type: str = Form(...), case_id: str = Form(""), special_powers: Optional[list[str]] = Form(None), csrf: str = Form("", alias="_csrf")
):
    c=core(); bad=_csrf(request,csrf)
    if bad: return bad
    user,org=_org_user(request)
    if isinstance(org,RedirectResponse): return org
    with db() as conn:
        client=conn.execute("SELECT * FROM clients WHERE id=? AND organization_id=?",(client_id,org["id"])).fetchone()
        org_full=conn.execute("SELECT * FROM organizations WHERE id=?",(org["id"],)).fetchone()
        case=None
        if case_id:
            case=conn.execute("SELECT * FROM cases WHERE id=? AND organization_id=? AND client_id=?",(int(case_id),org["id"],client_id)).fetchone()
        if not client: return RedirectResponse("/clients",status_code=303)
    target_dir=GENERATED_ROOT/str(org["id"])/str(client_id); target_dir.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now().strftime("%Y%m%d-%H%M%S")
    if document_type=="procuracao":
        filename=f"Procuracao_{safe_filename(client['name']).replace('.pdf','')}_{stamp}.docx"
        target=target_dir/filename
        create_power_of_attorney(client,org_full,target,special_powers=special_powers or [],case_number=(case["number"] if case else ""))
        title="Procuração"
    elif document_type=="ajg":
        filename=f"Declaracao_AJG_{safe_filename(client['name']).replace('.pdf','')}_{stamp}.docx"
        target=target_dir/filename
        create_ajg_declaration(client,org_full,target)
        title="Declaração de hipossuficiência / AJG"
    else:
        return RedirectResponse(f"/clients/{client_id}?error=document_type",status_code=303)
    with db() as conn:
        doc_id=conn.insert_id(
            "INSERT INTO generated_documents (organization_id,client_id,case_id,user_id,document_type,title,stored_path,file_name,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (org["id"],client_id,case["id"] if case else None,user["id"],document_type,title,str(target),filename,datetime.now().isoformat(timespec="seconds")),
        )
    c.log_action(request,f"Documento automático gerado para cliente #{client_id}: {title}")
    return RedirectResponse(f"/clients/{client_id}?generated={doc_id}",status_code=303)


@router.get("/clients/{client_id}/generated/{document_id}/download")
def download_generated_document(request: Request, client_id: int, document_id: int):
    c = core()
    user,org=_org_user(request)
    if isinstance(org,RedirectResponse): return org
    with db() as conn:
        row=conn.execute("SELECT * FROM generated_documents WHERE id=? AND client_id=? AND organization_id=?",(document_id,client_id,org["id"])).fetchone()
    path = c.safe_data_file(row["stored_path"], GENERATED_ROOT) if row else None
    if not row or not path or not path.is_file():
        return RedirectResponse(f"/clients/{client_id}",status_code=303)
    return FileResponse(path,filename=row["file_name"],media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


def _create_fee_contract(conn, org_id:int, client_id:int, case_id:Optional[int], title:str, contract_value:float, success_percent:float, entry_amount:float, installment_count:int, first_due_date:str, notes:str):
    c=core(); c.ensure_financial_setup(conn,org_id)
    contract_value=max(_money(contract_value),0); success_percent=min(max(_money(success_percent),0),100); entry_amount=min(max(_money(entry_amount),0),contract_value); installment_count=min(max(int(installment_count or 0),0),120)
    contract_id=conn.insert_id("""INSERT INTO fee_contracts (organization_id,client_id,case_id,title,contract_value,success_percent,entry_amount,installment_count,first_due_date,status,notes,created_at) VALUES (?,?,?,?,?,?,?,?,?,'Ativo',?,?)""",
        (org_id,client_id,case_id,title.strip(),contract_value,success_percent,entry_amount,installment_count,first_due_date or None,notes.strip(),datetime.now().isoformat(timespec="seconds")))
    cat=conn.execute("SELECT id FROM financial_categories WHERE organization_id=? AND name='Honorários contratuais' LIMIT 1",(org_id,)).fetchone(); cat_id=cat["id"] if cat else None
    now=datetime.now().isoformat(timespec="seconds")
    if entry_amount>0:
        conn.execute("""INSERT INTO financial_transactions (organization_id,client_id,case_id,contract_id,category_id,direction,description,original_amount,due_date,competence_date,status,payment_method,notes,legacy_finance_id,created_at) VALUES (?,?,?,?,?,'receivable',?,?,?,?,'Pendente','','Entrada contratual',NULL,?)""",
                     (org_id,client_id,case_id,contract_id,cat_id,f"{title.strip()} — Entrada",entry_amount,date.today().isoformat(),date.today().isoformat(),now))
    balance=max(contract_value-entry_amount,0)
    if balance>0 and installment_count>0:
        first=first_due_date or date.today().isoformat(); base=round(balance/installment_count,2); allocated=0.0
        for i in range(installment_count):
            value=base if i<installment_count-1 else round(balance-allocated,2); allocated+=value; due=c.add_months_iso(first,i)
            conn.execute("""INSERT INTO financial_transactions (organization_id,client_id,case_id,contract_id,category_id,direction,description,original_amount,due_date,competence_date,status,payment_method,notes,legacy_finance_id,created_at) VALUES (?,?,?,?,?,'receivable',?,?,?,?,'Pendente','','Parcela de contrato de honorários',NULL,?)""",
                         (org_id,client_id,case_id,contract_id,cat_id,f"{title.strip()} — Parcela {i+1}/{installment_count}",value,due,due,now))
    return contract_id


@router.post("/clients/{client_id}/finance/contract")
def client_fee_contract(request: Request, client_id:int, title:str=Form(...), case_id:str=Form(""), contract_value:float=Form(0), success_percent:float=Form(0), entry_amount:float=Form(0), installment_count:int=Form(0), first_due_date:str=Form(""), notes:str=Form(""), csrf:str=Form("",alias="_csrf")):
    c=core(); bad=_csrf(request,csrf)
    if bad: return bad
    user,org=_org_user(request)
    if isinstance(org,RedirectResponse): return org
    with db() as conn:
        if not conn.execute("SELECT 1 FROM clients WHERE id=? AND organization_id=?",(client_id,org["id"])).fetchone(): return RedirectResponse("/clients",status_code=303)
        caseid=int(case_id) if case_id else None
        if caseid and not conn.execute("SELECT 1 FROM cases WHERE id=? AND organization_id=? AND client_id=?",(caseid,org["id"],client_id)).fetchone(): caseid=None
        _create_fee_contract(conn,org["id"],client_id,caseid,title,contract_value,success_percent,entry_amount,installment_count,first_due_date,notes)
    c.log_action(request,f"Cliente #{client_id}: contrato de honorários criado")
    return RedirectResponse(f"/clients/{client_id}?contract=1",status_code=303)


@router.post("/clients/{client_id}/finance/expense")
def client_expense(request: Request, client_id:int, description:str=Form(...), amount:float=Form(...), due_date:str=Form(""), case_id:str=Form(""), category_id:str=Form(""), cost_center_id:str=Form(""), counterparty:str=Form(""), document_number:str=Form(""), notes:str=Form(""), csrf:str=Form("",alias="_csrf")):
    c=core(); bad=_csrf(request,csrf)
    if bad: return bad
    user,org=_org_user(request)
    if isinstance(org,RedirectResponse): return org
    if _money(amount)<=0: return RedirectResponse(f"/clients/{client_id}?error=amount",status_code=303)
    with db() as conn:
        if not conn.execute("SELECT 1 FROM clients WHERE id=? AND organization_id=?",(client_id,org["id"])).fetchone(): return RedirectResponse("/clients",status_code=303)
        caseid=int(case_id) if case_id else None; catid=int(category_id) if category_id else None; ccid=int(cost_center_id) if cost_center_id else None
        if caseid and not conn.execute("SELECT 1 FROM cases WHERE id=? AND organization_id=? AND client_id=?",(caseid,org["id"],client_id)).fetchone(): caseid=None
        if catid and not conn.execute("SELECT 1 FROM financial_categories WHERE id=? AND organization_id=? AND active=1",(catid,org["id"])).fetchone(): catid=None
        if ccid and not conn.execute("SELECT 1 FROM cost_centers WHERE id=? AND organization_id=? AND active=1",(ccid,org["id"])).fetchone(): ccid=None
        conn.execute("""INSERT INTO financial_transactions (organization_id,client_id,case_id,contract_id,category_id,direction,description,original_amount,due_date,competence_date,status,payment_method,notes,legacy_finance_id,created_at,cost_center_id,counterparty,document_number)
                        VALUES (?,?,?,NULL,?,'payable',?,?,?,?,'Pendente','',?,NULL,?,?,?,?)""",
                     (org["id"],client_id,caseid,catid,description.strip(),_money(amount),due_date or None,date.today().isoformat(),notes.strip(),datetime.now().isoformat(timespec="seconds"),ccid,counterparty.strip(),document_number.strip()))
    c.log_action(request,f"Cliente #{client_id}: despesa registrada — {description.strip()}")
    return RedirectResponse(f"/clients/{client_id}?expense=1",status_code=303)


@router.get("/finance/chart", response_class=HTMLResponse)
def finance_chart(request: Request):
    c=core(); user,org=_org_user(request)
    if isinstance(org,RedirectResponse): return org
    with db() as conn:
        categories=conn.execute("SELECT * FROM financial_categories WHERE organization_id=? AND active=1 ORDER BY COALESCE(code,'999'),name",(org["id"],)).fetchall()
        centers=conn.execute("SELECT * FROM cost_centers WHERE organization_id=? AND active=1 ORDER BY code",(org["id"],)).fetchall()
        expense_by_category=conn.execute("""SELECT COALESCE(cat.code,'') code,COALESCE(cat.name,'Sem categoria') name,COALESCE(SUM(p.amount),0) total
            FROM financial_payments p JOIN financial_transactions t ON t.id=p.transaction_id AND t.organization_id=p.organization_id
            LEFT JOIN financial_categories cat ON cat.id=t.category_id AND cat.organization_id=t.organization_id
            WHERE p.organization_id=? AND t.direction='payable' GROUP BY cat.code,cat.name ORDER BY total DESC""",(org["id"],)).fetchall()
    return c.safe_template_response("finance_chart.html",c.common_context(request,user,org,categories=categories,cost_centers=centers,expense_by_category=expense_by_category))


@router.post("/finance/chart/categories")
def finance_chart_category(request:Request,code:str=Form(...),name:str=Form(...),direction:str=Form(...),csrf:str=Form("",alias="_csrf")):
    c=core(); bad=_csrf(request,csrf)
    if bad:return bad
    user,org=_org_user(request)
    if isinstance(org,RedirectResponse):return org
    direction=direction if direction in ("income","expense","both") else "both"
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO financial_categories (organization_id,name,direction,active,created_at,code,parent_id) VALUES (?,?,?,1,?,?,NULL)",(org["id"],name.strip(),direction,datetime.now().isoformat(timespec="seconds"),code.strip()))
    return RedirectResponse("/finance/chart",status_code=303)


@router.post("/finance/chart/cost-centers")
def finance_cost_center(request:Request,code:str=Form(...),name:str=Form(...),csrf:str=Form("",alias="_csrf")):
    c=core(); bad=_csrf(request,csrf)
    if bad:return bad
    user,org=_org_user(request)
    if isinstance(org,RedirectResponse):return org
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO cost_centers (organization_id,code,name,active,created_at) VALUES (?,?,?,1,?)",(org["id"],code.strip().upper(),name.strip(),datetime.now().isoformat(timespec="seconds")))
    return RedirectResponse("/finance/chart",status_code=303)


def _office_context(conn, org, *, case_id=None, client_id=None, prompt="") -> str:
    c=core(); blocks=[f"ESCRITÓRIO: {org['name']}"]
    if case_id:
        case=conn.execute("SELECT c.*,cl.name client_name FROM cases c LEFT JOIN clients cl ON cl.id=c.client_id AND cl.organization_id=c.organization_id WHERE c.id=? AND c.organization_id=?",(case_id,org["id"])).fetchone()
        if case:
            blocks.append(f"PROCESSO: {case['number'] or 'sem número'} | {case['title']} | área {case['area']} | risco {case['risk']} | cliente {case['client_name'] or 'não vinculado'}")
            for label, key in (("FATOS CADASTRADOS","facts"),("PROVAS CADASTRADAS","evidence"),("ESTRATÉGIA CADASTRADA","strategy"),("RISCOS CADASTRADOS","risk_notes"),("PRÓXIMO PASSO","next_step")):
                value = case[key] if key in case.keys() else ""
                if value:
                    blocks.append(f"{label}: {value}")
            if case["client_id"]:
                fin=_client_financial_summary(conn,org["id"],case["client_id"])
                blocks.append(f"FINANCEIRO VINCULADO AO CLIENTE: contratado R$ {fin['contracted']:.2f}; recebido R$ {fin['paid']:.2f}; a receber R$ {fin['receivable']:.2f}; vencido R$ {fin['overdue']:.2f}; despesas pagas R$ {fin['expenses']:.2f}.")
            deadlines=conn.execute("SELECT title,due_date,status FROM deadlines WHERE organization_id=? AND case_id=? ORDER BY due_date LIMIT 12",(org["id"],case_id)).fetchall()
            if deadlines:
                blocks.append("PRAZOS/TAREFAS VINCULADOS:\n"+"\n".join(f"- {d['title']} | {d['due_date'] or 'sem data'} | {d['status']}" for d in deadlines))
            excerpts=search_case(conn,org_id=org["id"],case_id=case_id,query=prompt,limit=8) or recent_context(conn,org_id=org["id"],case_id=case_id,limit=8)
            for e in excerpts:
                blocks.append(f"FONTE {e['citation']}\n{e['text'][:2500]}")
    elif client_id:
        cl=conn.execute("SELECT * FROM clients WHERE id=? AND organization_id=?",(client_id,org["id"])).fetchone()
        if cl:
            fin=_client_financial_summary(conn,org["id"],client_id)
            blocks.append(f"CLIENTE: {cl['name']} | CPF/CNPJ {cl['document'] or 'não informado'} | telefone {cl['phone'] or 'não informado'} | e-mail {cl['email'] or 'não informado'}")
            blocks.append(f"FINANCEIRO DO CLIENTE: contratado R$ {fin['contracted']:.2f}; recebido R$ {fin['paid']:.2f}; a receber R$ {fin['receivable']:.2f}; vencido R$ {fin['overdue']:.2f}; despesas pagas R$ {fin['expenses']:.2f}.")
            cases=conn.execute("SELECT number,title,area,status,risk FROM cases WHERE organization_id=? AND client_id=? ORDER BY id DESC LIMIT 20",(org["id"],client_id)).fetchall()
            blocks.append("PROCESSOS DO CLIENTE:\n"+"\n".join(f"- {x['number'] or 's/n'} | {x['title']} | {x['area']} | {x['status']} | risco {x['risk']}" for x in cases))
    else:
        snap=c.financial_snapshot(conn,org["id"])
        counts={
            "clientes":conn.execute("SELECT COUNT(*) c FROM clients WHERE organization_id=?",(org["id"],)).fetchone()["c"],
            "processos":conn.execute("SELECT COUNT(*) c FROM cases WHERE organization_id=? AND status!='Encerrado'",(org["id"],)).fetchone()["c"],
            "tarefas":conn.execute("SELECT COUNT(*) c FROM activities WHERE organization_id=? AND status='Pendente'",(org["id"],)).fetchone()["c"],
        }
        blocks.append(f"INDICADORES: {json.dumps(counts,ensure_ascii=False)}")
        blocks.append(f"FINANCEIRO: a receber R$ {snap['receivable_open']:.2f}; a pagar R$ {snap['payable_open']:.2f}; caixa R$ {snap['cash_balance']:.2f}; resultado do mês R$ {snap['result_month']:.2f}.")
    return "\n\n".join(blocks)[:60000]


@router.get("/ai", response_class=HTMLResponse)
def ai_center(request:Request,thread_id:int=0):
    c=core(); user,org=_org_user(request)
    if isinstance(org,RedirectResponse):return org
    with db() as conn:
        threads=conn.execute("SELECT * FROM ai_threads WHERE organization_id=? ORDER BY updated_at DESC LIMIT 50",(org["id"],)).fetchall()
        selected=None; messages=[]
        if thread_id:
            selected=conn.execute("SELECT * FROM ai_threads WHERE id=? AND organization_id=?",(thread_id,org["id"])).fetchone()
            if selected: messages=conn.execute("SELECT * FROM ai_messages WHERE thread_id=? AND organization_id=? ORDER BY id",(thread_id,org["id"])).fetchall()
        clients=conn.execute("SELECT id,name FROM clients WHERE organization_id=? ORDER BY name",(org["id"],)).fetchall()
        cases=conn.execute("SELECT id,number,title FROM cases WHERE organization_id=? ORDER BY id DESC LIMIT 200",(org["id"],)).fetchall()
    return c.safe_template_response("ai_center.html",c.common_context(request,user,org,threads=threads,selected=selected,messages=messages,clients=clients,cases=cases,ai_connected=ai_configured(),ai_model=model_name(),ai_status=ai_connection_status()))


@router.get("/ai/quick")
def ai_quick(request:Request,case_id:int=0,client_id:int=0):
    c=core(); user,org=_org_user(request)
    if isinstance(org,RedirectResponse): return org
    with db() as conn:
        cid=client_id or None; caseid=case_id or None
        title="Conversa geral do escritório"
        if caseid:
            row=conn.execute("SELECT id,number,title,client_id FROM cases WHERE id=? AND organization_id=?",(caseid,org["id"])).fetchone()
            if not row: return RedirectResponse("/ai",status_code=303)
            if not cid: cid=row["client_id"]
            title=f"IA — {row['number'] or row['title']}"
        elif cid:
            row=conn.execute("SELECT id,name FROM clients WHERE id=? AND organization_id=?",(cid,org["id"])).fetchone()
            if not row: return RedirectResponse("/ai",status_code=303)
            title=f"IA — Cliente {row['name']}"
        now=datetime.now().isoformat(timespec="seconds")
        tid=conn.insert_id("INSERT INTO ai_threads (organization_id,user_id,case_id,client_id,title,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",(org["id"],user["id"],caseid,cid,title,now,now))
    c.log_action(request,f"Central IA: thread rápido #{tid} criado")
    return RedirectResponse(f"/ai?thread_id={tid}",status_code=303)


@router.post("/ai/threads")
def ai_new_thread(request:Request,title:str=Form("Nova conversa"),case_id:str=Form(""),client_id:str=Form(""),csrf:str=Form("",alias="_csrf")):
    c=core();bad=_csrf(request,csrf)
    if bad:return bad
    user,org=_org_user(request)
    if isinstance(org,RedirectResponse):return org
    now=datetime.now().isoformat(timespec="seconds")
    with db() as conn:
        cid=int(client_id) if client_id else None; caseid=int(case_id) if case_id else None
        if caseid:
            row=conn.execute("SELECT client_id FROM cases WHERE id=? AND organization_id=?",(caseid,org["id"])).fetchone()
            if not row:
                caseid=None
            else:
                # O cliente do thread deve pertencer ao workspace e, se o caso possui cliente principal, deve ser o mesmo.
                case_client=row["client_id"]
                if case_client: cid=case_client
        if cid and not conn.execute("SELECT 1 FROM clients WHERE id=? AND organization_id=?",(cid,org["id"])).fetchone():
            cid=None
        tid=conn.insert_id("INSERT INTO ai_threads (organization_id,user_id,case_id,client_id,title,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",(org["id"],user["id"],caseid,cid,title.strip() or "Nova conversa",now,now))
    return RedirectResponse(f"/ai?thread_id={tid}",status_code=303)


@router.post("/ai/threads/{thread_id}/message")
def ai_send_message(request:Request,thread_id:int,message:str=Form(...),csrf:str=Form("",alias="_csrf")):
    c=core();bad=_csrf(request,csrf)
    if bad:return bad
    user,org=_org_user(request)
    if isinstance(org,RedirectResponse):return org
    now=datetime.now().isoformat(timespec="seconds")
    pdf_paths=[]; indexed_chunks=0; pdf_review_required=False
    with db() as conn:
        thread=conn.execute("SELECT * FROM ai_threads WHERE id=? AND organization_id=?",(thread_id,org["id"])).fetchone()
        if not thread:return RedirectResponse("/ai",status_code=303)
        conn.execute("INSERT INTO ai_messages (organization_id,thread_id,role,content,model,input_tokens,output_tokens,created_at) VALUES (?,?,'user',?,'',0,0,?)",(org["id"],thread_id,message.strip(),now))
        context=_office_context(conn,org,case_id=thread["case_id"],client_id=thread["client_id"],prompt=message)
        if thread["case_id"]:
            indexed_chunks=conn.execute(
                "SELECT COUNT(*) c FROM document_chunks WHERE organization_id=? AND case_id=?",
                (org["id"],thread["case_id"]),
            ).fetchone()["c"]
            pdf_rows=conn.execute(
                "SELECT stored_path,stored_name,status,page_count,text_chars FROM case_documents WHERE organization_id=? AND case_id=? ORDER BY id DESC LIMIT 6",
                (org["id"],thread["case_id"]),
            ).fetchall()
            pdf_paths=[p for r in pdf_rows if (p:=resolve_uploaded_pdf_path(r["stored_path"] or "",org_id=org["id"],case_id=thread["case_id"],stored_name=r["stored_name"] or "")) is not None]
            pdf_review_required=any(
                str(r["status"] or "").lower() in {"needs_ocr","partial_ocr","error","encrypted"}
                or (int(r["page_count"] or 0)>0 and int(r["text_chars"] or 0)/max(int(r["page_count"] or 1),1)<80)
                for r in pdf_rows
            )
    if ai_configured():
        try:
            prompt=(
                f"CONTEXTO INTERNO DO JARBAS:\n{context}\n\n"
                f"PERGUNTA/COMANDO DO USUÁRIO:\n{message.strip()}\n\n"
                "Responda sem inventar. Diferencie fato localizado, inferência e ponto não localizado. "
                "Se houver PDF anexado, cite arquivo e página sempre que possível. "
                "Se o pedido depender de jurisprudência atual, sinalize pesquisa oficial validada."
            )
            pdf_direct_preferred = os.getenv("JARBAS_AI_PDF_ALWAYS", "1").strip() != "0"
            if thread["case_id"] and pdf_paths and (pdf_direct_preferred or indexed_chunks == 0 or pdf_review_required):
                result=ai_ask_with_pdf_files(OFFICE_RULES,prompt,pdf_paths,profile="legal",reasoning_effort="high")
            else:
                profile="legal" if thread["case_id"] else "routine"
                result=ai_ask(OFFICE_RULES,prompt,profile=profile,reasoning_effort="high" if profile=="legal" else "medium")
            answer=result.text; model=result.model; inp=result.input_tokens; out=result.output_tokens
        except Exception as exc:
            answer=f"A integração com o Claude apresentou erro: {friendly_error(exc)}"; model=model_name(); inp=out=0
    else:
        answer="Integração OpenAI ainda não configurada. Abra Configurações → OpenAI, informe a API key e use o botão Testar conexão. Os módulos locais permanecem ativos."
        model="local"; inp=out=0
    with db() as conn:
        conn.execute("INSERT INTO ai_messages (organization_id,thread_id,role,content,model,input_tokens,output_tokens,created_at) VALUES (?,?,'assistant',?,?,?,?,?)",(org["id"],thread_id,answer,model,inp,out,datetime.now().isoformat(timespec="seconds")))
        conn.execute("UPDATE ai_threads SET updated_at=? WHERE id=? AND organization_id=?",(datetime.now().isoformat(timespec="seconds"),thread_id,org["id"]))
        credits=max(1,int((inp+out)/12000)+1) if model!="local" else 0
        if credits:
            conn.execute("INSERT INTO usage_ledger (organization_id,user_id,kind,credits,description,created_at) VALUES (?,?, 'office_ai',?,?,?)",(org["id"],user["id"],credits,f"Central IA — {model}",datetime.now().isoformat(timespec="seconds")))
    c.log_action(request,f"Central IA: mensagem processada no thread #{thread_id} ({model})")
    return RedirectResponse(f"/ai?thread_id={thread_id}",status_code=303)



def _allow_secret_config(user) -> bool:
    # Configuração de segredo é global à instalação. Somente Super Admin pode alterá-la.
    # Em SaaS/produção, a variável permanece 0 e a chave é gerenciada pelo provedor/secret manager.
    return bool(user and user["is_superadmin"] and os.getenv("JARBAS_ALLOW_SECRET_CONFIG", "0") == "1")


@router.post("/settings/openai")
def settings_openai_save(
    request: Request,
    api_key: str = Form(""),
    legal_model: str = Form("claude-opus-5"),
    intake_model: str = Form("claude-sonnet-5"),
    routine_model: str = Form("claude-haiku-4-5-20251001"),
    csrf: str = Form("", alias="_csrf"),
):
    c=core(); bad=_csrf(request,csrf)
    if bad:return bad
    user,org=_org_user(request)
    if isinstance(org,RedirectResponse):return org
    if not _allow_secret_config(user):
        return RedirectResponse("/settings?ai_error=permission", status_code=303)
    key = api_key.strip() or os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return RedirectResponse("/settings?ai_error=missing_key", status_code=303)
    # Lista branca: modelo fora dela seria salvo e so falharia na 1a chamada.
    allowed_models={"claude-opus-5","claude-sonnet-5","claude-haiku-4-5-20251001"}
    if legal_model not in allowed_models: legal_model="claude-opus-5"
    if intake_model not in allowed_models: intake_model="claude-sonnet-5"
    if routine_model not in allowed_models: routine_model="claude-haiku-4-5-20251001"
    try:
        # A nova chave é testada antes de substituir a configuração vigente.
        if api_key.strip():
            result=ai_test_credentials(api_key.strip(), routine_model)
        else:
            result=ai_test_connection()
        ai_save_local_config(api_key=key, legal_model=legal_model, intake_model=intake_model, routine_model=routine_model)
        request.session["ai_flash"] = f"Conexão OpenAI validada e salva com sucesso ({result.model})."
        c.log_action(request,"OpenAI API configurada/validada pelo Super Admin")
        return RedirectResponse("/settings?ai_saved=1", status_code=303)
    except Exception as exc:
        request.session["ai_flash"] = f"A nova configuração NÃO foi aplicada porque o teste falhou: {str(exc)[:450]}"
        return RedirectResponse("/settings?ai_error=test", status_code=303)


@router.post("/settings/openai/test")
def settings_openai_test(request: Request, csrf: str = Form("", alias="_csrf")):
    c=core(); bad=_csrf(request,csrf)
    if bad:return bad
    user,org=_org_user(request)
    if isinstance(org,RedirectResponse):return org
    if not _allow_secret_config(user):
        return RedirectResponse("/settings?ai_error=permission", status_code=303)
    try:
        result=ai_test_connection()
        request.session["ai_flash"] = f"OpenAI conectada. Teste respondeu corretamente com {result.model}."
    except Exception as exc:
        request.session["ai_flash"] = f"Falha no teste OpenAI: {str(exc)[:450]}"
    return RedirectResponse("/settings?ai_test=1", status_code=303)


@router.post("/settings/openai/clear")
def settings_openai_clear(request: Request, csrf: str = Form("", alias="_csrf")):
    c=core(); bad=_csrf(request,csrf)
    if bad:return bad
    user,org=_org_user(request)
    if isinstance(org,RedirectResponse):return org
    if not _allow_secret_config(user):
        return RedirectResponse("/settings?ai_error=permission", status_code=303)
    ai_clear_local_api_key()
    request.session["ai_flash"] = "Chave OpenAI removida desta instalação local."
    c.log_action(request,"OpenAI API removida pelo Super Admin")
    return RedirectResponse("/settings?ai_cleared=1", status_code=303)

@router.get("/search", response_class=HTMLResponse)
def global_search(request:Request,q:str=""):
    c=core(); user,org=_org_user(request)
    if isinstance(org,RedirectResponse):return org
    q=(q or "").strip(); clients=[];cases=[];docs=[];finance=[];autos=[]
    if q:
        like=f"%{q}%"
        with db() as conn:
            clients=conn.execute("SELECT * FROM clients WHERE organization_id=? AND (name LIKE ? OR document LIKE ? OR phone LIKE ? OR email LIKE ?) ORDER BY name LIMIT 40",(org["id"],like,like,like,like)).fetchall()
            cases=conn.execute("SELECT * FROM cases WHERE organization_id=? AND (title LIKE ? OR number LIKE ? OR area LIKE ? OR court LIKE ? OR case_class LIKE ? OR subject LIKE ?) ORDER BY id DESC LIMIT 40",(org["id"],like,like,like,like,like,like)).fetchall()
            docs=conn.execute("SELECT d.*,c.title case_title FROM case_documents d JOIN cases c ON c.id=d.case_id AND c.organization_id=d.organization_id WHERE d.organization_id=? AND d.original_name LIKE ? ORDER BY d.id DESC LIMIT 40",(org["id"],like)).fetchall()
            finance=conn.execute("SELECT * FROM financial_transactions WHERE organization_id=? AND (description LIKE ? OR counterparty LIKE ? OR document_number LIKE ?) ORDER BY id DESC LIMIT 40",(org["id"],like,like,like)).fetchall()
            autos=conn.execute("""SELECT dc.case_id,dc.document_id,dc.page_number,substr(dc.text,1,420) excerpt,d.original_name,c.title case_title
                                  FROM document_chunks dc JOIN case_documents d ON d.id=dc.document_id AND d.organization_id=dc.organization_id
                                  JOIN cases c ON c.id=dc.case_id AND c.organization_id=dc.organization_id
                                  WHERE dc.organization_id=? AND dc.text LIKE ? ORDER BY dc.id DESC LIMIT 30""",(org["id"],like)).fetchall()
    return c.safe_template_response("global_search.html",c.common_context(request,user,org,q=q,clients=clients,cases=cases,docs=docs,finance=finance,autos=autos))

@router.post("/billing/order")
def create_subscription_order(request:Request,plan_code:str=Form(...),csrf:str=Form("",alias="_csrf")):
    c=core(); bad=_csrf(request,csrf)
    if bad:return bad
    user,org=_org_user(request)
    if isinstance(org,RedirectResponse):return org
    if not c.can_manage_workspace(org):return RedirectResponse("/billing",status_code=303)
    with db() as conn:
        plan=conn.execute("SELECT * FROM plans WHERE code=? AND active=1",(plan_code,)).fetchone()
        if not plan:return RedirectResponse("/billing?error=plan",status_code=303)
        regular=_money(plan["regular_price"] or plan["monthly_price"]); promo=_money(plan["monthly_price"])
        order_id=conn.insert_id("INSERT INTO subscription_orders (organization_id,plan_code,regular_price,promo_price,discount_percent,billing_cycle,status,provider,external_id,created_at) VALUES (?,?,?,?,50,'monthly','pending','manual','',?)",(org["id"],plan_code,regular,promo,datetime.now().isoformat(timespec="seconds")))
    c.log_action(request,f"Assinatura: pedido promocional #{order_id} criado para plano {plan_code}")
    return RedirectResponse(f"/billing?order={order_id}",status_code=303)


@router.post("/platform/orders/{order_id}/activate")
def activate_subscription_order(request:Request,order_id:int,csrf:str=Form("",alias="_csrf")):
    c=core(); bad=_csrf(request,csrf)
    if bad:return bad
    user=c.require_user(request)
    if isinstance(user,RedirectResponse):return user
    if not user["is_superadmin"]:return RedirectResponse("/",status_code=303)
    with db() as conn:
        order=conn.execute("SELECT * FROM subscription_orders WHERE id=? AND status='pending'",(order_id,)).fetchone()
        if not order:return RedirectResponse("/platform",status_code=303)
        plan=conn.execute("SELECT id FROM plans WHERE code=?",(order["plan_code"],)).fetchone()
        if not plan:return RedirectResponse("/platform",status_code=303)
        conn.execute("UPDATE subscriptions SET plan_id=?,status='active',started_at=?,current_period_end=? WHERE organization_id=?",(plan["id"],datetime.now().isoformat(timespec="seconds"),core().add_months_iso(date.today().isoformat(),1),order["organization_id"]))
        conn.execute("UPDATE subscription_orders SET status='activated' WHERE id=?",(order_id,))
    c.log_action(request,f"Super Admin ativou pedido de assinatura #{order_id}",None)
    return RedirectResponse("/platform",status_code=303)
