# Arquitetura SaaS — JARBAS 7.0

## Núcleo lógico

Usuário → Workspace → Cliente → Processo → Autos/Documentos → Copiloto/IA → Tarefas/Prazos → Honorários/Despesas → Relatórios.

Todos os registros relevantes carregam organization_id. O Super Admin é separado do papel de administrador do escritório.

## Implantação recomendada para produção

Internet → WAF/CDN opcional → HTTPS/Reverse Proxy → JARBAS/FastAPI → PostgreSQL → armazenamento privado de documentos → backups externos.

Componentes recomendados para escala:

- PostgreSQL gerenciado com backup point-in-time;
- object storage privado para PDFs/documentos;
- Redis para sessões/rate limits/filas;
- worker assíncrono para PDFs extensos, OCR e IA;
- logs centralizados e alertas;
- RLS ou política equivalente de isolamento no banco;
- 2FA/SSO para contas profissionais;
- secrets manager;
- antivírus/malware scanning de uploads;
- política de retenção e descarte;
- trilha de auditoria imutável para operações críticas.

## Escala comercial

A aplicação já separa planos, usuários, limites, consumo de IA e workspaces. O pedido/ativação manual permite piloto comercial. Para cobrança automática, integrar gateway recorrente com webhook idempotente e nunca ativar plano apenas pelo retorno do navegador.
