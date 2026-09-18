# Atendimento JARBAS — o chatbot do CHAGAS – ADVOGADOS

Tela: **`/atendimento`** · Módulos: `app/chatbot.py` (motor) e
`app/chatbot_routes.py` (rotas) · Diretrizes JARBAS V3.5 §3, §13, §14, §15,
§17, §18 e §21.

## Por que ele existe, se já há a Central IA

São lados diferentes do balcão.

| | Central IA (`/ai`) | Atendimento JARBAS (`/atendimento`) |
|---|---|---|
| Fala com | o advogado | quem procura o escritório |
| Contexto | autos, financeiro, clientes | só o que o contato relatou |
| Produz | análise, rascunho, resposta técnica | mensagem a enviar ao cliente |
| Pode citar precedente | sim, com fonte rastreável | **não**, nunca |
| Revisão | antes do protocolo | **antes do envio** |

A diferença que muda o projeto: uma peça inventada é revista pelo advogado
antes do protocolo; uma resposta inventada ao cliente **já foi enviada**. Por
isso as travas aqui são mais duras do que em qualquer outra tela do sistema.

## O que o chatbot faz

1. **Triagem de área.** Onze áreas (criminal, família, trabalhista,
   consumidor, bancário, previdenciário, tributário, empresarial, ambiental,
   administrativo, cível), cada uma com as perguntas que faltam para abrir o
   caso e os documentos que o cliente já pode separar. Cada área aponta a
   skill JARBAS correspondente, para o advogado seguir dali.
2. **Classificação de risco** (§3): BAIXO, MÉDIO, ALTO, CRÍTICO — sempre com
   o motivo declarado. Prisão, flagrante, custódia, medida protetiva, prazo
   vencendo, leilão e ordem de desocupação sobem para CRÍTICO. A fila de
   atendimento é ordenada por risco, não por data.
3. **Agenda real** (§15): dias úteis, 8h–17h, 60 minutos, feriados nacionais,
   estaduais (RS) e recesso forense já descontados pelo mesmo motor de
   `app/prazos.py` que calcula prazo processual. Propõe horários concretos em
   vez de perguntar "qual prefere?".
4. **Honorários** (§13): informa a consulta de R$ 250,00, abatível em caso de
   contratação. **Não orça o serviço** — isso sai depois da consulta, pela
   Tabela OAB/RS.
5. **Encaminhamento ao CRM**: a conversa vira lead com um clique, com o
   resumo já mascarado.

## As três travas

**Zero invenção.** O prompt proíbe citar jurisprudência, súmula, tema,
processo, ementa, relator, doutrina, estimar valor de indenização, chance de
êxito, tempo de tramitação ou prometer resultado. E `revisar_saida()` relê o
texto produzido atrás de quatro coisas: promessa de resultado, promessa de
prazo de decisão do Judiciário, citação de tribunal ou precedente, e valor em
reais diferente da consulta. O que ele encontra vira alerta visível na tela —
não reescreve o texto, porque quem decide o que sai para o cliente é o
advogado.

**Erro de IA nunca vira conteúdo.** Falhou a chamada, `chatbot.responder()`
levanta `ChatbotError`. A fala do cliente fica gravada (o relato não se
perde), nenhuma resposta é registrada, e a tela avisa o operador. O que não
acontece é um pedido de desculpas do modelo ser gravado como se fosse
resposta do escritório.

**Sem chave, o chatbot continua atendendo.** `chatbot.roteiro()` é um
atendimento escrito, determinístico, sem IA nenhuma: triagem por área,
perguntas certas, documentos, horário livre, valor da consulta e rodapé
institucional. Não é degradação — é o piso. A IA melhora a redação, não
sustenta o serviço. E o roteiro **não consome crédito**, porque não chama
ninguém.

## LGPD (§17)

- A triagem pede só o que a área exige. CPF, renda e dados de saúde apenas
  quando indispensáveis ao caso.
- `mascarar_sensiveis()` cobre CPF, CNPJ, telefone, e-mail e cartão. O resumo
  que vai ao CRM e a trilha de auditoria saem mascarados; a transcrição
  íntegra fica na conversa, que é onde o escritório tem motivo para lê-la.
- Isolamento por workspace em toda consulta, como no resto do sistema.

## Configuração

| Variável | Efeito |
|---|---|
| `ANTHROPIC_API_KEY` | ausente → modo roteiro |
| `JARBAS_CHATBOT_IA=0` | força o modo roteiro mesmo com chave |
| `JARBAS_UF` | UF dos feriados da agenda (padrão `RS`) |

Perfil de modelo: `intake` para conversa de risco alto ou crítico, `routine`
para o restante. Peça e parecer continuam no perfil `legal`, em outras telas.

Custo: 2 créditos por resposta com IA (a pergunta aos autos custa 5), sempre
**reservados antes** da chamada. Sem crédito, a resposta sai pelo roteiro e o
operador é avisado — o cliente não fica esperando por causa de limite de
plano.

## Esquema

`chat_conversations` (contato, canal, fluxo, área, risco, situação, resumo) e
`chat_messages` (transcrição, modelo, alertas, tokens). Área, risco e fluxo
ficam desnormalizados na conversa de propósito: ordenar a fila por um campo
que só existe dentro do texto da última mensagem não é fila, é varredura.

## Limites — o que ele NÃO faz

- Não responde ao cliente sozinho: **toda** resposta passa pelo operador.
- Não consulta o andamento processual. Se o contato cita um número de
  processo, o chatbot registra e manda conferir nos autos.
- Não integra WhatsApp nem Google Calendar. Os horários saem da regra da
  diretriz §15, não de uma agenda conectada; o texto é copiado e enviado à
  mão.
- Não estipula honorários do serviço nem estima indenização.
