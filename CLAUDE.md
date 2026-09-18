# JARBAS Jurídico Enterprise — contexto do projeto

Sistema de gestão jurídica com copiloto de IA. Escritório CHAGAS – ADVOGADOS,
Canela/RS. Single-tenant hoje; multi-tenant já implementado e testado.

## Rodar

```
pip install -r requirements.txt          # runtime
pip install -r requirements-ia.txt       # SDKs de IA (OPCIONAL, tolerante a falha)
pip install -r requirements-dev.txt      # pytest
pytest -q                                # 186 testes
python tools/minipytest.py               # sem pytest instalado
python tools/check_templates.py          # variáveis de template ausentes
python tools/build_installer.py          # monta o pacote Windows
```

## Antes de commitar

1. `pytest -q` — precisa passar inteiro
2. `python tools/check_templates.py` — precisa dizer OK
3. Se mexeu no schema: rode o teste que EXECUTA `initialize_schema`
   (`test_initialize_schema_roda_de_verdade_nos_dois_caminhos`)

## Armadilhas que já custaram uma instalação quebrada cada

- **`ensure_column(conn, tabela, "coluna TIPO")` tem TRÊS argumentos**, nome e
  tipo na mesma string. Chamar com quatro derruba `init_db` inteiro com
  TypeError. Quebrou 8.6.0, 8.7.0 e 8.8.0.
- **Teste que só casa string no arquivo não vale.** Foi o que deixou o bug
  acima passar. Se a função pode ser chamada, chame.
- **`require_workspace` devolve o redirect nos DOIS lugares** quando falha.
  Nunca reintroduza `return user, None`: as rotas checam só `org`.
- **Todo `safe_template_response` precisa passar as variáveis que o template
  usa em atributo ou iteração.** `ocr_status` faltando derrubou 3 rotas.
- **`testserver` NÃO vai no `JARBAS_ALLOWED_HOSTS` de produção.** O self-test
  usa `base_url=http://localhost`.
- **`_pg_sql` troca todo `?` por `%s`.** Nada de `?` literal dentro de SQL.
- **Erro de IA nunca vira conteúdo.** `ai_council` levanta `CouncilError`.
- **Quem redige não critica.** A crítica cai sempre em outro fornecedor.

## Layout

`app/` código · `tools/` utilitários e build · `tests/` suíte ·
`installer/` scripts Windows · `scripts/` operação · `docs/` documentação

Árvore de fontes tem `app/` na raiz; o pacote de instalação tem `payload/app/`.
Os testes detectam os dois layouts.
