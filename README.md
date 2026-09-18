# JARBAS Jurídico Enterprise

Sistema de gestão jurídica com copiloto de IA do **CHAGAS – ADVOGADOS**,
Canela/RS. Processos, clientes, prazos, documentos, financeiro, auditoria e
análise assistida por IA ancorada nos autos.

Versão em `VERSION.txt`. Contexto técnico do projeto em `CLAUDE.md`.

---

## Índice

1. [Escolha do ambiente](#1-escolha-do-ambiente)
2. [Instalação no Windows](#2-instalação-no-windows-máquina-do-escritório)
3. [Instalação em servidor Linux](#3-instalação-em-servidor-linux-docker)
4. [Configurar a IA](#4-configurar-a-ia-claude)
5. [Segurança obrigatória com autos reais](#5-segurança-obrigatória-com-autos-reais)
6. [Backup e restauração](#6-backup-e-restauração)
7. [Trazer o acervo que já existe](#7-trazer-o-acervo-que-já-existe)
8. [Operação diária](#8-operação-diária)
9. [Atualizar](#9-atualizar)
10. [Quando algo dá errado](#10-quando-algo-dá-errado)
11. [Desenvolvimento](#11-desenvolvimento)
12. [LGPD e sigilo profissional](#12-lgpd-e-sigilo-profissional)

---

## 1. Escolha do ambiente

| | **Windows (escritório)** | **Servidor Linux** |
|---|---|---|
| Acesso | só a máquina onde está instalado | de qualquer lugar, por HTTPS |
| Banco | SQLite local | PostgreSQL |
| Usuários | um advogado / equipe na mesma máquina | equipe distribuída |
| Instalação | `INSTALAR_AGORA.cmd` | `docker compose up -d` |
| Exige | Windows 64 bits | domínio, servidor, noções de Linux |

Na dúvida, comece pelo Windows. O banco migra para PostgreSQL depois sem
perda de dados.

---

## 2. Instalação no Windows (máquina do escritório)

1. Extraia **todo** o ZIP do pacote de instalação. Não execute de dentro do ZIP.
2. Execute `VERIFICAR_PACOTE.cmd` — confere se nenhum arquivo veio corrompido.
3. Execute **`INSTALAR.cmd`** — é o único arquivo a abrir. Ele confere antes
   se o ZIP foi realmente extraído, mostra o que vai acontecer e acompanha
   passo a passo com barra de progresso. (`INSTALAR_AGORA.cmd` continua
   existindo como apelido do mesmo arquivo.)
4. Defina a senha do administrador quando for pedida. A chave da IA é
   **opcional** nesta etapa: se ainda não tiver, responda `N` e configure
   depois pelo menu *Configurar IA*, dentro do sistema.
5. O instalador abre `CREDENCIAIS_INICIAIS.txt` ao final. **Guarde e apague do
   Desktop.**

O instalador faz backup das instalações anteriores antes de migrar, baixa o
Python portátil, instala dependências, roda um self-test, valida o login real
e agenda o backup diário. Se qualquer etapa falhar, ele para e não considera o
sistema instalado.

> **Não apague manualmente** pastas ou bancos de versões anteriores. O
> instalador localiza o banco com os dados reais, pontua os candidatos e migra
> o escolhido.

Depois de instalado, na pasta `%LOCALAPPDATA%\JARBAS_Enterprise`:

| Arquivo | Para quê |
|---|---|
| `INICIAR_JARBAS.cmd` | abre o sistema no navegador |
| `PARAR_JARBAS.cmd` | encerra o servidor |
| `BACKUP_JARBAS.cmd` | cópia de segurança agora |
| `BACKUP_AUTOMATICO.ps1` | agenda ou cancela o backup diário |
| `CONFIGURAR_IA.cmd` | cadastra a chave do Claude pela linha de comando |
| `INSTALAR_OCR.cmd` | habilita a leitura de autos digitalizados |
| `DIAGNOSTICO_JARBAS.cmd` | relatório para o suporte |
| `VERIFICAR_INTEGRIDADE.cmd` | confere se os arquivos foram alterados |
| `RESETAR_SENHA.cmd` | redefine a senha de um usuário |

---

## 3. Instalação em servidor Linux (Docker)

Requisitos: Docker e Docker Compose, um domínio apontando para o servidor, e
as portas 80 e 443 alcançáveis.

```bash
git clone <este repositório> jarbas && cd jarbas

cp deploy/env.servidor.example .env
chmod 600 .env

# Gere os segredos e cole no .env
python3 -c 'import secrets;print("JARBAS_SECRET_KEY="+secrets.token_urlsafe(48))'
python3 -c 'import secrets;print("POSTGRES_PASSWORD="+secrets.token_urlsafe(24))'
```

Edite o `.env`: domínio, e-mail do administrador e senha de primeiro acesso.
Depois:

```bash
docker compose --profile https up -d --build
docker compose logs -f jarbas          # acompanhe a subida
curl -s https://SEU-DOMINIO/health     # deve responder {"status":"ok",...}
```

Acesse `https://SEU-DOMINIO/login`.

**Assim que o primeiro acesso funcionar**, apague a linha
`JARBAS_BOOTSTRAP_ADMIN_PASSWORD` do `.env` e rode `docker compose up -d`. A
senha já está no banco como hash; manter o texto puro no arquivo é risco sem
contrapartida.

O `entrypoint` recusa subir com configuração insegura — chave de sessão curta,
`JARBAS_ALLOWED_HOSTS` ausente ou `testserver` em produção — e avisa quando
HTTPS, segundo fator ou teto de gasto de IA estão desligados.

**Sem container**: `deploy/jarbas.service` e `deploy/jarbas-backup.{service,timer}`
trazem as unidades systemd equivalentes.

---

## 4. Configurar a IA (Claude)

A IA é **opcional**. Sem chave, o JARBAS opera integralmente como gestão de
processos, clientes, prazos, documentos e financeiro; ficam indisponíveis o
Copiloto, o Conselho e o Intake por IA.

A partir da 9.0 o provedor é **único: Anthropic (Claude)**. Chave da OpenAI
não funciona — são empresas distintas.

1. Gere a chave em `console.anthropic.com` (começa com `sk-ant-`).
2. **Pelo sistema, a qualquer momento**: menu **Configurar IA** (`/configurar-ia`).
   Cole a chave e salve. Ela é testada antes de ser gravada — uma chave errada
   nunca substitui a que está funcionando. Também dá para trocar os modelos e
   remover a chave por ali.
3. **Windows, pela linha de comando**: `CONFIGURAR_IA.cmd`. Mesmo efeito.
4. **Servidor**: `ANTHROPIC_API_KEY=` no `.env`, depois `docker compose up -d`.
   Mantenha `JARBAS_ALLOW_SECRET_CONFIG=0` e a tela fica somente informativa.

**Nunca** envie a chave por WhatsApp, e-mail ou commit. Quem tem a chave gasta
na sua conta. O `DIAGNOSTICO_JARBAS.cmd` mascara qualquer segredo justamente
porque existe para ser enviado ao suporte.

> **Variável de ambiente do Windows vence o arquivo.** O JARBAS lê o
> `.env.local` com `override=False`: se existir uma `ANTHROPIC_API_KEY` no
> ambiente do Windows, é ela que vale, e corrigir o arquivo não muda nada. A
> tela *Configurar IA* avisa quando os dois discordam; o
> `DIAGNOSTICO_JARBAS.cmd` mostra em qual escopo (Process, User ou Machine) a
> variável está definida.

### Autos digitalizados (OCR)

Um PDF gerado pelo eproc tem camada de texto e é lido sem IA e sem OCR. Um
auto **escaneado** — petição assinada à mão, documento antigo, ofício de outro
órgão — é só imagem. Para lê-lo sem pagar por página à IA, rode
**`INSTALAR_OCR.cmd`**: ele instala o Tesseract sem exigir administrador e
garante o idioma português, que viaja dentro do pacote de instalação.

O idioma é o que decide o resultado. O instalador do Tesseract marca só o
inglês por padrão, e com ele o OCR de um auto brasileiro **roda e devolve
letra embaralhada** — um sintoma que não aponta para a causa.

Depois de instalar, em um processo já cadastrado use *Copiloto → Reprocessar
todos os PDFs* para reler os autos que antes ficaram sem texto.

### Teto de gasto

`JARBAS_AI_TETO_USD_MES` (padrão 50) bloqueia **antes** de chamar a API,
medido por token real. Não opere com clientes reais sem teto: um laço de
análise mal configurado gasta sem limite.

### O que a crítica do Conselho garante — e o que não garante

O Conselho redige com um modelo e critica com outro. Com provedor único, os
dois são Claude: **modelos de mesma linhagem compartilham pontos cegos**. A
crítica reduz erro, não elimina.

> Toda saída da IA é **minuta técnica auxiliar**. Confira cada fato, cada
> data, cada número de processo e **cada jurisprudência na fonte oficial**
> antes de protocolar. O sistema registra isso na própria tela; a revisão
> humana não é formalidade.

---

## 5. Segurança obrigatória com autos reais

Antes de carregar o primeiro processo de cliente:

- [ ] **HTTPS ligado** (`JARBAS_HTTPS_ONLY=1`). Sem isso o cookie de sessão
      trafega em claro e qualquer rede no caminho lê a sessão de quem está com
      os autos abertos.
- [ ] **Segundo fator ativo.** Cada usuário: *Configurações → Verificação em
      duas etapas*. Para exigir de todos, `JARBAS_2FA_OBRIGATORIO=1`.
- [ ] **Códigos de recuperação guardados.** Aparecem **uma única vez** — o
      banco guarda só o hash. Sem eles, perder o celular é perder a conta.
- [ ] **Backup rodando e testado** (seção 6).
- [ ] **`JARBAS_PUBLIC_SIGNUP=0`**, para que ninguém crie workspace sozinho.
- [ ] **`JARBAS_TRUSTED_PROXY_HOPS`** igual ao número de proxies à frente do
      JARBAS (1 com o Caddy do compose; 0 sem proxy). Errado, o bloqueio de
      força bruta tranca o escritório inteiro pelo IP do proxy e a auditoria
      registra o endereço errado.
- [ ] **Senha do administrador fora do `.env`** depois do primeiro acesso.
- [ ] `docs/SEGURANCA_LGPD_IA.md` lido.

O que já vem ligado, sem configuração: CSRF em todo formulário, PBKDF2 com
600 mil iterações, cabeçalhos de segurança, bloqueio após 8 tentativas de
login, isolamento por escritório, auditoria de ações e `noindex` para
buscadores.

---

## 6. Backup e restauração

**Backup que nunca foi restaurado não é backup.** O `tools/backup.py` gera a
cópia com a API de backup online do SQLite (consistente com o servidor de pé),
inclui os PDFs dos autos, **abre e consulta a cópia recém-gerada** e falha na
hora se ela não prestar.

```bash
python tools/backup.py             # gerar e conferir
python tools/backup.py --listar    # conferir as cópias existentes
```

Windows: `BACKUP_JARBAS.cmd` (agora) ou `BACKUP_AUTOMATICO.ps1` (diário, já
agendado pela instalação). As cópias vão para `Documentos\JARBAS_Backups`.

Servidor: o timer systemd, ou `docker compose --profile backup run --rm backup`.

### Restaurar

```bash
# 1. PARE o servidor. Restaurar com o JARBAS de pé corrompe o banco novo.
docker compose stop jarbas          # ou PARAR_JARBAS.cmd

# 2. Restaure (o estado atual é preservado em uma pasta ao lado)
python tools/backup.py --restaurar CAMINHO/jarbas-AAAAMMDD-HHMMSS.tar.gz --sim

# 3. Suba e confira
docker compose start jarbas && curl -s http://127.0.0.1:8765/health
```

**Teste uma restauração por semestre**, num diretório descartável. É o único
jeito de saber que o backup funciona antes de precisar dele.

> As cópias contêm autos sob sigilo profissional. Guarde com acesso restrito e
> cifradas. Nuvem compartilhada sem cifra não serve.

---

## 7. Trazer o acervo que já existe

O escritório já tem os autos organizados em pastas no computador. Recadastrar
isso à mão é o que faz um sistema novo nunca sair do papel.

Menu **Importar pastas** (`/importar-pastas`). Duas origens, o mesmo resultado:

| | **Enviar a pasta** | **Mapear uma pasta local** |
|---|---|---|
| Onde funciona | qualquer instalação | só a instalação local do escritório |
| Como | o navegador envia os arquivos | o servidor lê direto do disco |
| Exige | nada | `JARBAS_PERMITE_MAPEAR_PASTA=1` |
| Acervo grande | envie por partes | lê tudo sem trafegar pela rede |

**Como a estrutura é lida:** cada subpasta do primeiro nível vira um
**cliente**; cada subpasta dentro dela vira um **processo**. PDFs soltos na
pasta do cliente formam um processo único.

**Nada é cadastrado sem confirmação.** A leitura produz uma *proposta* — nome,
CPF/CNPJ, número CNJ, tribunal e um grau de confiança por processo. Você
corrige os campos, desmarca o que não quer e só então grava. Enquanto isso, a
pasta de origem não é tocada: os PDFs são **copiados**, nunca movidos,
renomeados ou apagados.

Detalhes que importam na prática:

- **CPF/CNPJ** só é atribuído ao cliente quando a parte encontrada no PDF tem
  o **mesmo nome** da pasta. Pegar o documento da primeira parte que aparece
  produziria procuração com o número do adversário.
- **Cliente existente** é reaproveitado pelo documento, nunca por nome:
  homônimo é comum e unir dois dossiês misturaria processos de pessoas
  diferentes.
- **A IA é opcional.** Marcada, lê **um** PDF por processo, com teto por
  execução (`JARBAS_IMPORT_MAX_IA`, padrão 25). Desmarcada, a leitura é local
  e não custa nada.
- **Somente PDF é importado.** O JARBAS só indexa e exibe PDF; o que ficar de
  fora aparece nominalmente na tela.
- Arquivo repetido não entra duas vezes (conferência por SHA-256).

> Em servidor com mais de um escritório, mantenha `JARBAS_PERMITE_MAPEAR_PASTA=0`.
> Ler uma pasta arbitrária do disco a pedido de quem está logado é leitura de
> arquivo do servidor. O envio pelo navegador chega ao mesmo resultado sem
> esse poder.

---

## 8. Operação diária

| Rotina | Onde |
|---|---|
| Abrir processo novo a partir do PDF do eproc | *Intake Inteligente* |
| Trazer pastas de clientes que já existem | *Importar pastas* |
| Perguntar aos autos, Hard Truth, minutas | *Copiloto* no processo |
| Prazos, simulador de contagem | *Prazos* |
| Honorários, parcelas, despesas, caixa | *Financeiro* |
| Quem fez o quê e quando | *Auditoria* |
| Gasto de IA do mês | *Conselho* |

Confira em *Auditoria* de tempos em tempos: acesso que você não reconhece é
sinal de credencial comprometida.

---

## 9. Atualizar

**Windows**: execute `ATUALIZAR_OU_REPARAR.cmd` do novo pacote. Ele faz backup,
preserva banco, PDFs, identidade visual e a chave da IA, e reindexa os PDFs
com o pipeline novo.

**Servidor**:

```bash
python tools/backup.py          # antes de qualquer coisa
git pull
docker compose up -d --build
docker compose logs -f jarbas
curl -s https://SEU-DOMINIO/health
```

Migrações de schema são automáticas e não apagam tabela existente.

---

## 10. Quando algo dá errado

| Sintoma | O que fazer |
|---|---|
| Não abre no Windows | `DIAGNOSTICO_JARBAS.cmd` |
| Container não sobe | `docker compose logs jarbas` — o entrypoint diz qual variável falta |
| `/health` não responde | banco inacessível; confira o volume e a `DATABASE_URL` |
| Erro interno com código | o código está em `logs/runtime-errors.log` |
| PDF não é lido | `DIAGNOSTICAR_PDF.cmd`; se for digitalizado, rode `INSTALAR_OCR.cmd` |
| IA não responde | `DIAGNOSTICAR_IA.cmd`; confira chave e teto de gasto |
| Arquivo alterado | `VERIFICAR_INTEGRIDADE.cmd` |
| Perdeu o celular do 2FA | use um código de recuperação no campo do código |
| Perdeu celular **e** códigos | `RESETAR_SENHA.cmd` na máquina, ou administrador no servidor |
| Esqueceu a senha | `RESETAR_SENHA.cmd` |

Guarde o código do erro e o arquivo de diagnóstico antes de tentar consertar.

---

## 11. Desenvolvimento

```bash
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env.local        # ajuste JARBAS_ENV=development
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload
```

Antes de commitar — e o CI repete no push:

```bash
pytest                            # suíte inteira
python tools/check_templates.py   # variáveis de template ausentes
python tools/build_installer.py /tmp/pacote   # o pacote Windows ainda monta
```

Armadilhas que já custaram uma instalação cada estão em `CLAUDE.md`. Leia
antes de mexer em `database.py` ou nas rotas.

---

## 12. LGPD e sigilo profissional

Este sistema guarda processos, documentos e dados pessoais de clientes. Isso
traz deveres que nenhuma configuração substitui:

- **Minimização** — colete e envie à IA apenas o necessário.
- **Finalidade** — dados de cliente servem ao caso daquele cliente.
- **Revisão humana** — nenhuma peça vai a protocolo sem conferência do
  advogado. A IA é ferramenta auxiliar, nunca decisora.
- **Zero invenção** — jurisprudência, doutrina, número de processo, data e
  valor: tudo confirmado em fonte oficial antes de usar.
- **Incidente de segurança** — havendo suspeita de vazamento, revogue as
  chaves, troque `JARBAS_SECRET_KEY` (derruba todas as sessões), preserve os
  logs e avalie o dever de comunicar à ANPD e aos titulares.

Detalhes em `docs/SEGURANCA_LGPD_IA.md` e `docs/ROADMAP_PRODUCAO.md`.

---

**CHAGAS – ADVOGADOS**
Rua Jacob Adami, nº 55, Bairro Suíça, Canela/RS, CEP 95684-196
Telefone/WhatsApp: (54) 99110-1959 · schagasadvocacia@gmail.com
Dr. Sandro D. Chagas — OAB/RS 105.040

Software proprietário. Ver `LICENSE_PROPRIETARY.txt`.
