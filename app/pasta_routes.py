"""Telas da importação de pastas de clientes.

Fluxo, igual para as duas origens:

    escolher a pasta  ->  o JARBAS lê e PROPÕE  ->  o advogado confere e
    confirma  ->  só então cliente, processo e documentos são gravados.

O passo do meio é o motivo de tudo isto existir separado do Intake: o Intake
trata um PDF por vez, com o advogado olhando. Aqui são dezenas ou centenas de
pastas de uma vez, e cadastrar sem conferência seria encher o sistema de
cliente com nome truncado e CPF do adversário — em dossiê sob sigilo
profissional, e sem ninguém perceber.

Nada é apagado da pasta de origem. O mapeamento COPIA os arquivos para a área
do JARBAS; a pasta do escritório continua exatamente como estava.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import importador_pastas as IP
from . import plan_limits
from .copilot import index_pdf, safe_filename, store_uploaded_pdf
from .database import DATA_DIR, db

router = APIRouter()

STAGING = DATA_DIR / "imports" / "pastas"
UPLOAD_ROOT = DATA_DIR / "uploads"


def _main():
    from . import main as m
    return m


def _agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _so_digitos(valor: str) -> str:
    return re.sub(r"\D", "", valor or "")


def _teto_total_mb() -> int:
    try:
        return max(50, int(os.getenv("JARBAS_IMPORT_MAX_MB", "2000") or 2000))
    except ValueError:
        return 2000


def _historico(org_id: int):
    with db() as conn:
        return conn.execute(
            """SELECT id,origem,caminho,status,resumo,created_at,completed_at
               FROM folder_imports WHERE organization_id=?
               ORDER BY id DESC LIMIT 10""", (org_id,)).fetchall()


def _erro(request: Request, mensagem: str, destino: str = "/importar-pastas"):
    request.session["pasta_erro"] = mensagem[:700]
    return RedirectResponse(destino, status_code=303)


# ------------------------------------------------------------------- telas

@router.get("/importar-pastas", response_class=HTMLResponse)
def importar_pastas(request: Request):
    c = _main()
    user, org = c.require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    # Contexto montado na própria rota: tools/check_templates.py lê a chamada
    # para conferir se o template recebe tudo o que usa, e não enxerga o que
    # estiver escondido atrás de um helper.
    return c.safe_template_response("importar_pastas.html", c.common_context(
        request, user, org,
        etapa="escolher",
        historico=_historico(org["id"]),
        mapeamento_liberado=IP.mapeamento_liberado(),
        teto_ia=IP.teto_de_ia(),
        teto_mb=_teto_total_mb(),
        erro=request.session.pop("pasta_erro", ""),
        aviso=request.session.pop("pasta_aviso", ""),
        importacao=None, propostas=[], avisos_varredura=[],
    ))


@router.get("/importar-pastas/{import_id}", response_class=HTMLResponse)
def revisar(request: Request, import_id: int):
    c = _main()
    user, org = c.require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    with db() as conn:
        linha = conn.execute(
            "SELECT * FROM folder_imports WHERE id=? AND organization_id=?",
            (import_id, org["id"])).fetchone()
    if not linha:
        return RedirectResponse("/importar-pastas", status_code=303)
    propostas = json.loads(linha["propostas_json"] or "[]")
    avisos = json.loads(linha["avisos_json"] or "[]")
    return c.safe_template_response("importar_pastas.html", c.common_context(
        request, user, org,
        etapa="revisar",
        importacao=linha,
        propostas=propostas,
        avisos_varredura=avisos,
        historico=_historico(org["id"]),
        mapeamento_liberado=IP.mapeamento_liberado(),
        teto_ia=IP.teto_de_ia(),
        teto_mb=_teto_total_mb(),
        erro=request.session.pop("pasta_erro", ""),
        aviso=request.session.pop("pasta_aviso", ""),
    ))


# ------------------------------------------------------ origem 1: mapeamento

@router.post("/importar-pastas/mapear")
def mapear(request: Request, caminho: str = Form(""), usar_ia: str = Form(""),
           csrf: str = Form("", alias="_csrf")):
    c = _main()
    if not c.valid_csrf(request, csrf):
        return c.csrf_error()
    user, org = c.require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    if not IP.mapeamento_liberado():
        return _erro(request,
                     "Esta instalação não permite mapear pastas do disco. "
                     "Use o envio de pasta pelo navegador, que funciona em "
                     "qualquer instalação.")
    if not c.can_manage_workspace(org) and not user["is_superadmin"]:
        return _erro(request, "Somente o responsável pelo escritório pode mapear "
                              "uma pasta do computador.")
    try:
        raiz = IP.validar_raiz(caminho)
    except IP.PastaRecusada as exc:
        return _erro(request, str(exc))

    achados, avisos = IP.varrer(raiz)
    if not achados:
        return _erro(request,
                     f"Não encontrei nenhum PDF em {raiz}. Confira se apontou "
                     "para a pasta que contém as pastas dos clientes.")
    grupos = IP.agrupar(achados, raiz_rotulo=raiz.name)
    propostas = IP.analisar(grupos, usar_ia=(usar_ia == "1"))
    return _gravar_proposta(request, org, user, propostas, avisos,
                            origem="mapeamento", caminho=str(raiz))


# ---------------------------------------------------------- origem 2: envio

@router.post("/importar-pastas/enviar")
async def enviar(request: Request):
    c = _main()
    formulario = await request.form()
    if not c.valid_csrf(request, str(formulario.get("_csrf") or "")):
        return c.csrf_error()
    user, org = c.require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org

    enviados = [f for f in formulario.getlist("arquivos") if getattr(f, "filename", "")]
    if not enviados:
        return _erro(request, "Nenhum arquivo foi selecionado.")

    destino = STAGING / str(org["id"]) / uuid.uuid4().hex
    destino.mkdir(parents=True, exist_ok=True)
    max_arquivo = max(5, int(os.getenv("JARBAS_MAX_UPLOAD_MB", "200") or 200)) * 1024 * 1024
    teto_total = _teto_total_mb() * 1024 * 1024

    achados: list[IP.ArquivoLido] = []
    avisos: list[str] = []
    total = 0
    for upload in enviados:
        relativo = IP.caminho_relativo_seguro(upload.filename)
        if not relativo or IP.arquivo_ignorado(relativo):
            continue
        if IP.classificar(relativo) != "pdf":
            # Fora do PDF o JARBAS não indexa nem exibe o arquivo; guardá-lo
            # daria a impressão de que os autos estão completos quando não
            # estão. Melhor dizer o que ficou de fora.
            avisos.append(f"{relativo}: não é PDF e não foi importado.")
            continue
        if len(achados) >= IP.MAX_ARQUIVOS:
            avisos.append(f"Envio limitado a {IP.MAX_ARQUIVOS} arquivos por vez; "
                          "o restante não foi lido.")
            break
        alvo = destino / relativo
        alvo.parent.mkdir(parents=True, exist_ok=True)
        alvo = alvo.with_name(safe_filename(alvo.name))
        try:
            tamanho, _ = store_uploaded_pdf(upload, alvo, max_arquivo)
        except Exception as exc:  # noqa: BLE001 — um arquivo ruim não cancela o lote
            avisos.append(f"{relativo}: {str(exc)[:160]}")
            continue
        finally:
            try:
                upload.file.close()
            except Exception:
                pass
        total += tamanho
        if total > teto_total:
            shutil.rmtree(destino, ignore_errors=True)
            return _erro(request,
                         f"O envio passou de {_teto_total_mb()} MB. Envie a pasta "
                         "em partes — por exemplo, uma letra do alfabeto por vez.")
        achados.append(IP.ArquivoLido(relativo=relativo, origem=alvo,
                                      tamanho=tamanho, tipo="pdf"))

    if not achados:
        shutil.rmtree(destino, ignore_errors=True)
        return _erro(request, "Nenhum PDF aproveitável no que foi enviado. "
                              + (" ".join(avisos[:3]) if avisos else ""))

    grupos = IP.agrupar(achados, raiz_rotulo="Arquivos enviados")
    propostas = IP.analisar(grupos, usar_ia=(str(formulario.get("usar_ia") or "") == "1"))
    return _gravar_proposta(request, org, user, propostas, avisos,
                            origem="upload", caminho=str(destino))


def _gravar_proposta(request: Request, org, user, propostas, avisos,
                     *, origem: str, caminho: str):
    c = _main()
    serializadas = IP.para_json(propostas)
    processos = sum(len(p["processos"]) for p in serializadas)
    arquivos = sum(len(x["arquivos"]) for p in serializadas for x in p["processos"])
    leituras_ia = sum(1 for p in serializadas for x in p["processos"] if x.get("ia_usada"))
    usou_ia = leituras_ia > 0
    resumo = (f"{len(serializadas)} pasta(s), {processos} processo(s), "
              f"{arquivos} PDF(s)")
    with db() as conn:
        import_id = conn.insert_id(
            """INSERT INTO folder_imports
               (organization_id,user_id,origem,caminho,status,propostas_json,
                avisos_json,resumo,ia_usada,created_at)
               VALUES (?,?,?,?,'pendente',?,?,?,?,?)""",
            (org["id"], user["id"], origem, caminho,
             json.dumps(serializadas, ensure_ascii=False),
             json.dumps(avisos, ensure_ascii=False), resumo,
             1 if usou_ia else 0, _agora()))
        if leituras_ia:
            # A leitura por IA já aconteceu; cobrar aqui, e não na confirmação,
            # porque o custo existe mesmo que o advogado descarte a proposta.
            conn.execute(
                """INSERT INTO usage_ledger
                   (organization_id,user_id,kind,credits,description,created_at)
                   VALUES (?,?, 'intake_ia_pasta',?,?,?)""",
                (org["id"], user["id"], leituras_ia,
                 f"Importação de pastas — {leituras_ia} PDF(s) lidos pela IA", _agora()))
    c.log_action(request, f"Importação de pastas #{import_id} analisada ({origem}): {resumo}")
    return RedirectResponse(f"/importar-pastas/{import_id}", status_code=303)


# ---------------------------------------------------------------- confirmar

def _origem_permitida(bruto: str, raiz: Path) -> Optional[Path]:
    """Só aceita arquivo que continue dentro da pasta lida nesta importação."""
    try:
        alvo = Path(bruto).resolve(strict=True)
        alvo.relative_to(raiz)
    except (OSError, ValueError):
        return None
    return alvo if alvo.is_file() else None


@router.post("/importar-pastas/{import_id}/confirmar")
async def confirmar(request: Request, import_id: int):
    c = _main()
    formulario = await request.form()
    if not c.valid_csrf(request, str(formulario.get("_csrf") or "")):
        return c.csrf_error()
    user, org = c.require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    with db() as conn:
        linha = conn.execute(
            "SELECT * FROM folder_imports WHERE id=? AND organization_id=? AND status='pendente'",
            (import_id, org["id"])).fetchone()
    if not linha:
        return RedirectResponse("/importar-pastas", status_code=303)

    propostas = json.loads(linha["propostas_json"] or "[]")
    try:
        raiz = Path(linha["caminho"] or "").resolve(strict=True)
    except OSError:
        return _erro(request,
                     "A pasta lida nesta importação não está mais acessível. "
                     "Refaça a leitura.", f"/importar-pastas/{import_id}")

    marcadas = set(formulario.getlist("incluir"))
    marcados_proc = set(formulario.getlist("incluir_proc"))
    if not marcadas:
        return _erro(request, "Nenhuma pasta foi marcada para cadastro.",
                     f"/importar-pastas/{import_id}")

    novos_clientes = reaproveitados = criados_casos = criados_docs = 0
    falhas: list[str] = []
    agora = _agora()

    for i, proposta in enumerate(propostas):
        if str(i) not in marcadas:
            continue
        nome = str(formulario.get(f"nome_{i}") or proposta.get("cliente_nome") or "").strip()
        documento = str(formulario.get(f"doc_{i}") or proposta.get("documento") or "").strip()
        if not nome:
            falhas.append(f"{proposta.get('pasta')}: sem nome de cliente; pasta ignorada.")
            continue
        cliente_id, novo = _cliente(org["id"], nome, documento, agora)
        if cliente_id is None:
            falhas.append(f"{nome}: não consegui gravar o cliente.")
            continue
        if novo:
            novos_clientes += 1
        else:
            reaproveitados += 1

        for j, processo in enumerate(proposta.get("processos") or []):
            if f"{i}:{j}" not in marcados_proc:
                continue
            titulo = str(formulario.get(f"titulo_{i}_{j}") or processo.get("titulo") or nome).strip()
            numero = str(formulario.get(f"numero_{i}_{j}") or processo.get("numero") or "").strip()
            with db() as conn:
                caso_id = conn.insert_id(
                    """INSERT INTO cases
                       (organization_id,client_id,number,title,area,court,case_class,subject,
                        claim_value,status,risk,facts,evidence,strategy,next_step,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,'','',0,'Ativo','Médio','','','',
                               'Cadastro vindo de importação de pasta: conferir dados e prazos.',?,?)""",
                    (org["id"], cliente_id, numero, titulo[:240],
                     str(processo.get("area") or "Outro"),
                     str(processo.get("tribunal") or ""), agora, agora))
            criados_casos += 1
            docs, erros = _documentos(org["id"], caso_id, processo, raiz)
            criados_docs += docs
            falhas.extend(erros)

    resumo = (f"{novos_clientes} cliente(s) novo(s), {reaproveitados} já existente(s), "
              f"{criados_casos} processo(s), {criados_docs} PDF(s)")
    with db() as conn:
        conn.execute(
            """UPDATE folder_imports SET status='concluida',completed_at=?,
               resumo=? WHERE id=? AND organization_id=?""",
            (agora, resumo, import_id, org["id"]))
    # O que foi enviado pelo navegador já está copiado para a área dos
    # processos; manter a cópia do staging dobraria o espaço ocupado por autos
    # sob sigilo sem nenhum ganho.
    if linha["origem"] == "upload":
        _limpar_staging(linha["caminho"] or "")
    c.log_action(request, f"Importação de pastas #{import_id} confirmada: {resumo}")
    request.session["pasta_aviso"] = (
        resumo + ". Confira nome, CPF/CNPJ e número do processo antes de usar "
        "esses dados em peça, prazo ou procuração."
        + (" Nem tudo entrou: " + " | ".join(falhas[:10]) if falhas else ""))
    return RedirectResponse("/importar-pastas", status_code=303)


def _limpar_staging(caminho: str) -> None:
    """Apaga só o que o próprio JARBAS gravou no staging. Nunca pasta mapeada."""
    try:
        alvo = Path(caminho).resolve()
        if alvo.is_relative_to(STAGING.resolve()):
            shutil.rmtree(alvo, ignore_errors=True)
    except (OSError, ValueError):
        pass


def _cliente(org_id: int, nome: str, documento: str, agora: str) -> tuple[Optional[int], bool]:
    """Reaproveita o cliente existente pelo CPF/CNPJ; nunca por nome.

    Homônimo é comum, e unir dois dossiês porque dois clientes se chamam
    'João da Silva' mistura processos de pessoas diferentes. Sem documento,
    cadastra-se um novo e o advogado funde depois, se for o caso.
    """
    digitos = _so_digitos(documento)
    with db() as conn:
        if digitos:
            candidatos = conn.execute(
                "SELECT id,document FROM clients WHERE organization_id=? AND COALESCE(document,'')<>''",
                (org_id,)).fetchall()
            for linha in candidatos:
                if _so_digitos(linha["document"]) == digitos:
                    return linha["id"], False
        tipo = "Pessoa Jurídica" if len(digitos) == 14 else "Pessoa Física"
        try:
            return conn.insert_id(
                """INSERT INTO clients
                   (organization_id,name,person_type,document,notes,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (org_id, nome[:180], tipo, documento.strip()[:40],
                 "Cadastrado pela importação de pasta — conferir qualificação completa.",
                 agora, agora)), True
        except Exception:
            return None, False


def _sha256(caminho: Path) -> str:
    h = hashlib.sha256()
    with caminho.open("rb") as fh:
        for bloco in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(bloco)
    return h.hexdigest()


def _documentos(org_id: int, case_id: int,
                processo: dict[str, Any], raiz: Path) -> tuple[int, list[str]]:
    """Copia os PDFs para a área do JARBAS e indexa. A pasta do escritório
    não é tocada: nada é movido nem apagado de lá."""
    destino = UPLOAD_ROOT / str(org_id) / str(case_id)
    destino.mkdir(parents=True, exist_ok=True)
    gravados = 0
    erros: list[str] = []
    for arquivo in processo.get("arquivos") or []:
        if arquivo.get("tipo") != "pdf":
            continue
        origem = _origem_permitida(str(arquivo.get("origem") or ""), raiz)
        if origem is None:
            erros.append(f"{arquivo.get('relativo')}: arquivo não está mais no lugar lido.")
            continue
        try:
            tamanho = origem.stat().st_size
            plan_limits.checar_cota_armazenamento(org_id, tamanho)
        except plan_limits.CotaDeArmazenamentoExcedida as exc:
            erros.append(f"{arquivo.get('relativo')}: {exc}")
            break
        except OSError as exc:
            erros.append(f"{arquivo.get('relativo')}: {exc}")
            continue
        nome_guardado = (f"{datetime.now().strftime('%Y%m%d%H%M%S')}_"
                         f"{uuid.uuid4().hex[:10]}_{safe_filename(origem.name)}")
        alvo = destino / nome_guardado
        try:
            shutil.copy2(origem, alvo)
        except OSError as exc:
            erros.append(f"{arquivo.get('relativo')}: não consegui copiar ({exc}).")
            continue
        exibicao = str(arquivo.get("relativo") or origem.name)
        try:
            assinatura = _sha256(alvo)
        except OSError:
            assinatura = ""
        with db() as conn:
            # Mesma pasta importada duas vezes é o engano mais provável aqui.
            # Sem esta conferência, o processo ficaria com o mesmo PDF repetido
            # e o Copiloto citaria a mesma página como se fossem duas fontes.
            if assinatura:
                repetido = conn.execute(
                    "SELECT id FROM case_documents WHERE organization_id=? AND case_id=? AND sha256=?",
                    (org_id, case_id, assinatura)).fetchone()
                if repetido:
                    alvo.unlink(missing_ok=True)
                    continue
            doc_id = conn.insert_id(
                """INSERT INTO case_documents
                   (organization_id,case_id,original_name,stored_name,stored_path,sha256,
                    mime_type,size_bytes,page_count,text_chars,status,extraction_note,created_at)
                   VALUES (?,?,?,?,?,?, 'application/pdf',?,0,0,'uploaded','',?)""",
                (org_id, case_id, exibicao[:240], nome_guardado, str(alvo),
                 assinatura, tamanho, _agora()))
            try:
                index_pdf(conn, org_id=org_id, case_id=case_id,
                          document_id=doc_id, path=alvo)
            except Exception as exc:  # noqa: BLE001
                conn.execute(
                    "UPDATE case_documents SET status='error',extraction_note=? WHERE id=? AND organization_id=?",
                    (f"Falha de extração: {type(exc).__name__}: {str(exc)[:250]}",
                     doc_id, org_id))
                erros.append(f"{exibicao}: guardado, mas sem leitura de texto.")
        gravados += 1
    return gravados, erros


@router.post("/importar-pastas/{import_id}/descartar")
def descartar(request: Request, import_id: int, csrf: str = Form("", alias="_csrf")):
    c = _main()
    if not c.valid_csrf(request, csrf):
        return c.csrf_error()
    user, org = c.require_workspace(request)
    if isinstance(org, RedirectResponse):
        return org
    with db() as conn:
        linha = conn.execute(
            "SELECT * FROM folder_imports WHERE id=? AND organization_id=?",
            (import_id, org["id"])).fetchone()
        if not linha:
            return RedirectResponse("/importar-pastas", status_code=303)
        conn.execute(
            "UPDATE folder_imports SET status='descartada',completed_at=? WHERE id=? AND organization_id=?",
            (_agora(), import_id, org["id"]))
    # Só o que o JARBAS gravou é apagado. Pasta mapeada do escritório, jamais.
    if linha["origem"] == "upload":
        _limpar_staging(linha["caminho"] or "")
    c.log_action(request, f"Importação de pastas #{import_id} descartada")
    request.session["pasta_aviso"] = "Importação descartada. Nada foi cadastrado."
    return RedirectResponse("/importar-pastas", status_code=303)
