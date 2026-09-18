# Segurança, LGPD e uso de IA — JARBAS 7.0

O JARBAS manipula potencialmente dados pessoais, documentos judiciais e informações protegidas por sigilo profissional. O desenho de produção deve aplicar minimização, finalidade, controle de acesso, criptografia, logging e revisão humana.

## OpenAI

A integração é pela OpenAI API. O conteúdo enviado à IA deve ser limitado ao necessário para a tarefa. O sistema registra consumo, mas a chave de API permanece em variável/secret, nunca no cadastro do cliente.

## Controles antes de venda ampla

- contrato SaaS, termos e política de privacidade;
- definição contratual dos papéis LGPD (controlador/operador conforme fluxo);
- DPA com fornecedores;
- 2FA e recuperação segura de conta;
- testes de isolamento entre tenants;
- RLS no PostgreSQL;
- criptografia de backups e storage;
- plano de resposta a incidentes;
- política de retenção e exclusão;
- gestão de suboperadores;
- auditoria de permissões;
- revisão humana obrigatória das saídas jurídicas da IA.
