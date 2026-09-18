# JARBAS Jurídico Enterprise 8.3.1

Versão auditada e consolidada para gestão jurídica, financeira e Copiloto IA.

## Núcleo
- Multi-workspace com isolamento por escritório.
- Cliente 360, CRM, processos, partes processuais, prazos, agenda, documentos e timesheet.
- Financeiro jurídico com contratos, parcelas, contas, plano de contas, centros de custo e pagamentos.
- Intake Inteligente por PDF com extração multi-engine, confirmação humana e persistência de classe, assunto, valor da causa e todas as partes detectadas.
- Motor de PDFs com PyMuPDF + pypdf + pdfplumber, OCR local Tesseract quando disponível e leitura direta do PDF pela OpenAI quando necessária.
- Copiloto: pesquisa nos autos, linha do tempo, Hard Truth, análise estratégica e minutas.
- OpenAI Responses API centralizada em `app/ai_gateway.py`; sem chave, os recursos documentais locais continuam ativos.
- Procuração e declaração de AJG geradas a partir do cadastro mestre do cliente.

## Segurança e PDF Engine 8.3
- CSRF, sessões assinadas, TrustedHost e headers de segurança.
- Segregação de tenant em leituras e validação de IDs vinculados em gravações críticas.
- Swagger desativado em produção.
- Upload PDF por assinatura/extensão e logo por assinatura binária.
- Chave OpenAI mascarada, gravada localmente com permissão restritiva quando possível.
- Alteração de senha dentro do sistema.
- Planos não são ativados por autoatendimento sem pedido/ativação de assinatura.

## Produção SaaS
A edição local usa SQLite. Para venda em escala, migrar para PostgreSQL gerenciado, armazenamento de objetos, Secret Manager, Redis/rate limiting, 2FA e gateway de cobrança recorrente.
