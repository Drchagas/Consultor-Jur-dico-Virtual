# Conselho de IA — JARBAS 9.0 (Claude, provedor único)

## O que mudou e o que se perdeu

Até a 8.9 o conselho usava três fornecedores, e a crítica adversarial caía
obrigatoriamente em modelo de **outra empresa**. O motivo não era preferência
técnica: um modelo tende a aprovar o próprio texto, porque reconhece o próprio
raciocínio. Um modelo treinado por outra equipe, com outros dados, encontra a
citação inventada que o autor não vê.

A partir da 9.0 o sistema opera só com Claude. **A crítica continua existindo**
e continua sendo feita por um modelo diferente do redator, mas os dois
compartilham linhagem de treino. Isso reduz, e não elimina, o ponto cego comum.

Em uma linha: melhor que autorrevisão pelo mesmo modelo, pior que revisão entre
fornecedores.

**Consequência prática para o advogado.** A conferência humana de fundamento
legal, que já era obrigatória, passa a ser mais necessária ainda. Cada ponto
marcado `[CONFERIR FUNDAMENTO]` precisa ser checado um a um. Alucinação de
precedente inexistente já rendeu sanção disciplinar em vários tribunais, e a
rede que existia contra isso ficou mais fina.

O aviso aparece na tela de resultado de toda deliberação, não escondido aqui.

## Divisão de papéis

| Papel | Modelo | Por quê |
|---|---|---|
| `extracao` | `claude-sonnet-5` | Etapa que mais consome entrada: os autos inteiros. |
| `estrategia` | `claude-opus-5` | Raciocínio sobre o material já extraído. |
| `redacao` | `claude-opus-5` | Redação longa em registro forense. |
| `critica` | `claude-sonnet-5` | Modelo diferente do redator, por construção. |
| `rotina` | `claude-haiku-4-5-20251001` | Resumo e classificação em volume. |

Sobrescrevível no `.env.local`:

```
JARBAS_PAPEL_REDACAO=anthropic:claude-sonnet-5
JARBAS_PAPEL_CRITICA=anthropic:claude-opus-5
```

## Custo por análise completa

Processo de 328 páginas, cinco etapas:

| Faixa | Redige | Critica | US$/análise |
|---|---|---|---|
| `premium` | Opus 5 | Sonnet 5 | 2,72 |
| `padrao` | Sonnet 5 | Opus 5 | 1,67 |
| `economico` | Sonnet 5 | Haiku 4.5 | 0,61 |
| `basico` | Haiku 4.5 | Sonnet 5 | 0,40 |

## Se um modelo cair

O fluxo tenta outro modelo do catálogo e registra `fallback_from`. A única
exceção é a crítica: ela nunca cai no modelo que redigiu. Se só houver um
modelo disponível, a crítica é **pulada com aviso** — autorrevisão pelo mesmo
modelo seria pior que não ter revisão, porque daria confiança injustificada.

## Voltar à revisão entre fornecedores

A abstração de provedores continua íntegra em `ai_council.py`. Acrescentar uma
entrada em `PROVEDORES` e a chave correspondente devolve a independência real
sem tocar no resto do fluxo.

## LGPD

Um fornecedor em vez de três simplifica: um DPA, um opt-out de treinamento, uma
transferência internacional a documentar. Segue necessário antes de operar com
autos de clientes:

- DPA assinado com a Anthropic;
- opt-out de uso para treinamento confirmado por escrito;
- cláusula de uso de IA e transferência internacional no contrato de honorários
  (o modelo gerado pelo sistema já traz essa cláusula).
