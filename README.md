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
7. [Operação diária](#7-operação-diária)
8. [Atualizar](#8-atualizar)
9. [Quando algo dá errado](#9-quando-algo-dá-errado)
10. [Desenvolvimento](#10-desenvolvimento)
11. [LGPD e sigilo profissional](#11-lgpd-e-sigilo-profissional)

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
3. Execute `INSTALAR_AGORA.cmd`.
4. Defina a senha do administrador quando for pedida.
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
| `CONFIGURAR_IA.cmd` | cadastra a chave do Claude |
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
2. **Windows**: `CONFIGURAR_IA.cmd`. A chave é testada online antes de ser
   salva; se o teste falhar, ela não é gravada.
3. **Servidor**: `ANTHROPIC_API_KEY=` no `.env`, depois `docker compose up -d`.

**Nunca** envie a chave por WhatsApp, e-mail ou commit. Quem tem a chave gasta
na sua conta.

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

## 7. Operação diária

| Rotina | Onde |
|---|---|
| Abrir processo novo a partir do PDF do eproc | *Intake Inteligente* |
| Perguntar aos autos, Hard Truth, minutas | *Copiloto* no processo |
| Prazos, simulador de contagem | *Prazos* |
| Honorários, parcelas, despesas, caixa | *Financeiro* |
| Quem fez o quê e quando | *Auditoria* |
| Gasto de IA do mês | *Conselho* |

Confira em *Auditoria* de tempos em tempos: acesso que você não reconhece é
sinal de credencial comprometida.

---

## 8. Atualizar

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

## 9. Quando algo dá errado

| Sintoma | O que fazer |
|---|---|
| Não abre no Windows | `DIAGNOSTICO_JARBAS.cmd` |
| Container não sobe | `docker compose logs jarbas` — o entrypoint diz qual variável falta |
| `/health` não responde | banco inacessível; confira o volume e a `DATABASE_URL` |
| Erro interno com código | o código está em `logs/runtime-errors.log` |
| PDF não é lido | `DIAGNOSTICAR_PDF.cmd`; digitalizado precisa de OCR ou IA |
| IA não responde | `DIAGNOSTICAR_IA.cmd`; confira chave e teto de gasto |
| Arquivo alterado | `VERIFICAR_INTEGRIDADE.cmd` |
| Perdeu o celular do 2FA | use um código de recuperação no campo do código |
| Perdeu celular **e** códigos | `RESETAR_SENHA.cmd` na máquina, ou administrador no servidor |
| Esqueceu a senha | `RESETAR_SENHA.cmd` |

Guarde o código do erro e o arquivo de diagnóstico antes de tentar consertar.

---

## 10. Desenvolvimento

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

## 11. LGPD e sigilo profissional

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
