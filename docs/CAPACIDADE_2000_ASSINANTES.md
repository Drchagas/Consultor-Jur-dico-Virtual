# JARBAS SaaS — capacidade e viabilidade para 2.000 assinantes

Documento de dimensionamento. Todos os números de página, caractere e banco
vêm de **medição real** na instalação de Canela (10 processos, 3.279 páginas,
03/09/2026), não de estimativa. O único valor estimado é o tamanho em disco
dos PDFs — veja "O número que falta medir" no fim.

---

## 1. Base de medição

| Métrica | Valor medido |
|---|---|
| Processos indexados | 10 |
| Páginas | 3.279 |
| Caracteres extraídos | 2.800.038 |
| Chunks | 3.338 |
| **Média por processo** | **328 páginas** |
| Média por página | 854 caracteres |
| Banco SQLite | 16,2 MB |
| **Banco por página** | **5.189 bytes** |

Um processo médio tem ~280 mil caracteres, ou **~78 mil tokens de texto**.
Enviado como PDF nativo ao Gemini (258 tokens/página), fica em **~85 mil
tokens de entrada**.

---

## 2. Custo de IA por análise completa

Uma "análise completa" é o ciclo de cinco etapas do Conselho: extração →
estratégia → redação → crítica → revisão.

| Faixa | Redação | Raciocínio | US$/análise | R$/análise |
|---|---|---|---|---|
| `premium` | Opus 5 | Sol | **2,28** | 12,33 |
| `padrao` | Sonnet 5 | Sol | **0,98** | 5,27 |
| `economico` | Sonnet 5 | Terra | **0,75** | 4,03 |
| `basico` | Haiku 4.5 | Luna | **0,32** | 1,71 |

A etapa de extração custa igual em todas (US$ 0,19): é sempre Gemini Flash,
porque é ela que engole o processo inteiro e o preço de entrada é o que manda.
O que separa as faixas é redação e revisão.

### Projeção: 2.000 assinantes × 10 análises/mês

| Faixa | US$/assinante/mês | US$/mês total | R$/mês total |
|---|---|---|---|
| `premium` | 22,84 | 45.680 | 246.672 |
| `padrao` | 9,76 | 19.520 | 105.408 |
| `economico` | 7,46 | 14.920 | 80.568 |
| `basico` | 3,16 | 6.328 | 34.171 |

**Conclusão que muda o produto:** a configuração que eu havia definido como
padrão (Opus na redação) custa US$ 22,84 por assinante por mês. Num plano de
R$ 150, isso é praticamente toda a receita. Ela serve para um escritório
usando o próprio orçamento; **não serve como padrão de SaaS**. Por isso a
faixa passou a ser atributo do plano (`plans.ai_tier`), não do código.

---

## 3. Armazenamento

### Sem cota — o cenário que quebra

10 processos novos por assinante por mês, nada apagado:

| Mês | Processos | PDFs | Banco | Total |
|---|---|---|---|---|
| 1 | 20.000 | 1,2 TB | 32 GB | 1,3 TB |
| 6 | 120.000 | 7,3 TB | 190 GB | 7,5 TB |
| 12 | 240.000 | 14,7 TB | 380 GB | **15,0 TB** |
| 24 | 480.000 | 29,3 TB | 761 GB | 30,1 TB |

O PDF é **97% do volume**. O banco cresce devagar porque guarda só texto.

### Com cota — o cenário vendável

| Plano | R$/mês | % da base | Assinantes | Cota | Total |
|---|---|---|---|---|---|
| Essencial | 50 | 50% | 1.000 | 20 GB | 20 TB |
| Profissional | 150 | 35% | 700 | 60 GB | 42 TB |
| Escritório | 400 | 15% | 300 | 200 GB | 60 TB |
| **Total** | | | **2.000** | | **119 TB vendidos** |

Uso típico fica em 50–70% da cota: **~71 TB provisionados**.

O mix de planos acima é hipótese minha, não previsão. Troque os percentuais
pelo que você observar nos primeiros 100 assinantes.

---

## 4. Infraestrutura mensal

| Item | US$/mês |
|---|---|
| Object storage 72 TB (S3/R2/Spaces) | 1.080 |
| Egress / CDN | 400 |
| PostgreSQL gerenciado (400 GB, HA + réplica) | 600 |
| App servers 4× (8 vCPU / 32 GB) | 800 |
| Workers de IA e PDF 4× | 600 |
| Redis (fila + cache) | 120 |
| Backup off-site | 300 |
| Monitoramento e logs | 200 |
| **Subtotal** | **4.100** |

Infra é ~7% do custo. **A IA é o custo do negócio**, não o servidor.

---

## 5. Resultado mensal

Receita bruta do mix acima: **R$ 275.000/mês**.

| Faixa de IA | Custo total R$ | Margem R$ | Margem % |
|---|---|---|---|
| `premium` | 268.812 | 6.188 | **2%** |
| `padrao` | 127.548 | 147.452 | 54% |
| `economico` | 102.708 | 172.292 | **63%** |
| `basico` | 56.311 | 218.689 | 80% |

Recomendação: `economico` no Essencial, `padrao` no Profissional, `premium` no
Escritório. Assim a faixa cara é vendida a quem paga por ela.

Não inclui impostos, gateway (~4%), suporte, marketing ou desenvolvimento.
É margem de contribuição técnica, não lucro.

---

## 6. O que já está aplicado (8.6)

`app/plan_limits.py`, com 16 testes:

- **Cota de armazenamento aplicada no upload.** Conferida depois de conhecer o
  tamanho real e antes de registrar o documento; se estourar, o arquivo é
  removido para não virar órfão. Antes disso `storage_gb` existia no banco e
  nunca era verificado — o assinante de 20 GB podia subir 500 GB.
- **Faixa de modelos por plano** (`plans.ai_tier`), com precedência sobre
  variável de ambiente. Todas as faixas mantêm redator e crítico em
  fornecedores diferentes — a regra que dá sentido ao conselho.
- **Orçamento de IA por escritório** (`plans.monthly_ai_usd`), cobrado por
  token real e bloqueado antes da chamada à API.
- **Escritório sem assinatura ativa** cai num mínimo de 1 GB e faixa `basico`,
  em vez de herdar limites generosos.

---

## 7. O que ainda impede vender

Em ordem de bloqueio:

1. **Não existe gateway de pagamento.** `subscription_orders` tem colunas
   `provider` e `external_id`, mas não há checkout, webhook nem cobrança
   recorrente. Nada é cobrado de ninguém hoje. É o bloqueador nº 1.
2. **SQLite.** A abstração para PostgreSQL existe e o schema está pronto nos
   dois bancos, mas 2.000 assinantes exigem Postgres com pool de conexões —
   hoje cada request abre conexão nova.
3. **Processo único, sem fila.** Uma análise de 328 páginas leva minutos e roda
   dentro do request. Com dezenas simultâneas, o servidor trava. Precisa de
   fila (Redis + workers) e resultado assíncrono.
4. **Sem 2FA e sem recuperação de senha.** Inaceitável para dado sob sigilo
   profissional de terceiros.
5. **Sem RLS no Postgres.** O isolamento hoje é aplicação-only. Está correto e
   testado, mas uma query futura sem `organization_id` vaza. RLS é a rede.
6. **Sem retenção nem expurgo.** Ninguém apaga nada, e a cota do item 6 não
   resolve o direito de eliminação da LGPD.
7. **Busca não escala.** `search_case` carrega todos os chunks do caso em
   memória a cada consulta. FTS5 resolve.
8. **DPA com os três provedores**, opt-out de treinamento e cláusula de
   transferência internacional no contrato. Sem isso não se vende a escritório
   nenhum que leia o contrato.

Itens 1 a 3 são pré-requisitos de lançamento. Os demais são pré-requisitos do
primeiro cliente que fizer due diligence.

---

## 8. O número que falta medir

O tamanho em disco dos PDFs é o único valor estimado (usei 200 KB/página).
Ele determina 97% do armazenamento, então vale medir o real. No PowerShell:

```powershell
$d = "$env:LOCALAPPDATA\JARBAS_Enterprise\data\uploads"
$b = (Get-ChildItem $d -Recurse -Filter *.pdf | Measure-Object Length -Sum).Sum
"{0:N1} MB em PDFs / 3279 paginas = {1:N0} KB por pagina" -f ($b/1MB), ($b/1KB/3279)
```

Se der bem abaixo de 200 KB/página, os 72 TB caem proporcionalmente e o custo
de storage junto.
