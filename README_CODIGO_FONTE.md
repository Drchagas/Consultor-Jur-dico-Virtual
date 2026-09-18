# JARBAS Jurídico Enterprise 8.3.1 — Código-fonte completo

Snapshot integral do código-fonte da aplicação JARBAS Jurídico Enterprise 8.3.1, incluindo o hotfix do instalador PowerShell identificado no diagnóstico de 31/08/2026 (`Test-Path ... -and ...`).

## Conteúdo

- `app/` — aplicação FastAPI, banco, Intake, Copiloto, PDF Engine, OpenAI Gateway, geradores de documentos/petições, templates e CSS.
- `tools/` — bootstrap, validação, diagnóstico, reindexação, migração de caminhos, reset de senha e teste OpenAI.
- `scripts/` — inicialização, parada, backup, diagnóstico, configuração OpenAI, reprocessamento de PDFs e manutenção no Windows.
- `installer/` — código-fonte do instalador Windows e seus wrappers.
- `docs/` — arquitetura, auditoria, segurança/LGPD, matriz funcional e roadmap.
- `tests/fixtures/` — PDF de teste não confidencial usado no self-test.

## O que NÃO está neste pacote

Por segurança e sigilo, não são incluídos:

- bancos SQLite reais;
- PDFs/processos reais de clientes;
- `.env.local` de instalações;
- API Keys da OpenAI;
- senhas ou hashes exportados de usuários;
- logs da máquina do usuário;
- runtime/binários do Python portátil.

## Stack

- Python 3.12
- FastAPI / Starlette / Uvicorn
- SQLite local; estrutura preparada para evolução PostgreSQL
- Jinja2 + HTML + CSS
- OpenAI Python SDK / Responses API
- PyMuPDF + pypdf + pdfplumber; OCR Tesseract opcional
- python-docx + ReportLab

## Execução de desenvolvimento

1. Crie um ambiente virtual Python 3.12.
2. Instale `requirements.txt`.
3. Copie `.env.example` para `.env.local` e configure os valores de desenvolvimento.
4. Use `JARBAS_ENV=development` para desenvolvimento local.
5. Execute:

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload
```

A OpenAI é opcional para subir a aplicação; a chave deve ser configurada apenas no ambiente local/servidor e nunca commitada.

## Segurança

Antes de colocar em produção pública, revisar `docs/SEGURANCA_LGPD_IA.md` e `docs/ROADMAP_PRODUCAO.md`. O código-fonte não contém credenciais secretas.
