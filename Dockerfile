# JARBAS Jurídico Enterprise — imagem de servidor.
#
# Duas etapas: a primeira compila as rodas, a segunda só recebe o resultado.
# Sem isso, o compilador C e os headers de desenvolvimento ficariam na imagem
# final — ferramentas de construção dentro de um servidor que guarda autos de
# clientes são superfície de ataque sem contrapartida.

# ---------------------------------------------------------------- construção
FROM python:3.12-slim-bookworm AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install --no-install-recommends -y \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /wheels
COPY requirements.txt requirements-ia.txt ./

RUN python -m pip install --upgrade pip \
    && python -m pip wheel --wheel-dir /wheels/dist -r requirements.txt

# SDK de IA em etapa TOLERANTE A FALHA, como no instalador do Windows: um
# escritório não pode ficar sem sistema de processos porque um SDK não
# resolveu. Sem ele, o JARBAS sobe e opera; apenas Copiloto, Conselho e
# Intake por IA ficam indisponíveis.
RUN python -m pip wheel --wheel-dir /wheels/dist -r requirements-ia.txt || \
    echo "AVISO: SDK de IA nao resolveu; a imagem sobe sem as funcoes de IA."


# ------------------------------------------------------------------ runtime
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    JARBAS_ENV=production \
    JARBAS_DATA_DIR=/var/lib/jarbas

# tesseract-ocr-por: sem o pacote de idioma português o OCR local devolve
# texto inútil em autos brasileiros, e o sistema passa a depender da IA para
# ler qualquer PDF digitalizado — o que custa dinheiro a cada página.
# libgl1/libglib2.0-0: exigidos pelo PyMuPDF em tempo de execução.
RUN apt-get update && apt-get install --no-install-recommends -y \
        tesseract-ocr \
        tesseract-ocr-por \
        libgl1 \
        libglib2.0-0 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Usuário sem privilégios. O processo que recebe upload da internet não pode
# ser root: uma falha na leitura de PDF viraria controle total do container.
RUN useradd --system --create-home --uid 10001 jarbas

# Instala a partir do MESMO requirements.txt, sem rede: repetir a lista de
# pacotes aqui garantiria que um dia ela ficaria fora de sincronia com o
# arquivo que o instalador do Windows usa.
COPY --from=build /wheels/dist /wheels/dist
COPY requirements.txt requirements-ia.txt ./
RUN python -m pip install --no-index --find-links=/wheels/dist -r requirements.txt \
    && (python -m pip install --no-index --find-links=/wheels/dist -r requirements-ia.txt \
        || echo "AVISO: sem SDK de IA nesta imagem; o JARBAS opera sem funcoes de IA.") \
    && rm -rf /wheels requirements.txt requirements-ia.txt

WORKDIR /app
COPY --chown=jarbas:jarbas app/ ./app/
COPY --chown=jarbas:jarbas tools/ ./tools/
COPY --chown=jarbas:jarbas docs/ ./docs/
COPY --chown=jarbas:jarbas VERSION.txt pytest.ini ./
COPY --chown=jarbas:jarbas deploy/entrypoint.sh /usr/local/bin/jarbas-entrypoint

RUN chmod +x /usr/local/bin/jarbas-entrypoint \
    && mkdir -p /var/lib/jarbas /app/logs \
    && chown -R jarbas:jarbas /var/lib/jarbas /app/logs

# Os autos ficam AQUI, fora da árvore de código: a imagem é substituída a
# cada atualização e o que estiver dentro dela se perde.
VOLUME ["/var/lib/jarbas"]

USER jarbas
EXPOSE 8765

# O /health consulta o banco, então um healthcheck verde significa
# "aplicação de pé E banco respondendo", não apenas "porta aberta".
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8765/health || exit 1

ENTRYPOINT ["/usr/local/bin/jarbas-entrypoint"]
CMD ["serve"]
