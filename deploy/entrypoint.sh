#!/bin/sh
# Entrada do container JARBAS.
#
# Faz três coisas antes de servir: recusa configuração insegura, prepara o
# volume de dados e aplica o schema. Falhar aqui, alto e claro, é melhor do
# que subir um sistema que parece funcionar e não protege os autos.
set -eu

fatal() {
    echo "ERRO: $*" >&2
    exit 1
}

aviso() {
    echo "AVISO: $*" >&2
}

DATA_DIR="${JARBAS_DATA_DIR:-/var/lib/jarbas}"
PORTA="${JARBAS_PORT:-8765}"

# --------------------------------------------------------------- validação
if [ "${JARBAS_ENV:-production}" = "production" ]; then
    # 32 caracteres é o mínimo que app/main.py exige. Verificamos aqui também
    # para a mensagem sair antes do traceback do Python, e para o operador
    # saber exatamente qual variável faltou.
    [ "${#JARBAS_SECRET_KEY}" -ge 32 ] 2>/dev/null || \
        fatal "JARBAS_SECRET_KEY ausente ou com menos de 32 caracteres.
       Gere uma com:  python -c 'import secrets;print(secrets.token_urlsafe(48))'
       e guarde-a fora do repositorio."

    [ -n "${JARBAS_ALLOWED_HOSTS:-}" ] || \
        fatal "JARBAS_ALLOWED_HOSTS ausente. Informe o dominio publico do JARBAS.
       Sem essa lista o servidor aceita qualquer Host e fica exposto a
       envenenamento de cabecalho."

    case "${JARBAS_ALLOWED_HOSTS}" in
        *testserver*)
            fatal "'testserver' nao pode aparecer em JARBAS_ALLOWED_HOSTS de producao."
            ;;
    esac

    if [ "${JARBAS_HTTPS_ONLY:-0}" != "1" ]; then
        aviso "JARBAS_HTTPS_ONLY diferente de 1. O cookie de sessao vai trafegar
         em conexao nao cifrada. Com autos de clientes, isso e incidente de
         sigilo esperando para acontecer: coloque o JARBAS atras de HTTPS."
    fi

    if [ "${JARBAS_2FA_OBRIGATORIO:-0}" != "1" ]; then
        aviso "JARBAS_2FA_OBRIGATORIO diferente de 1. Uma senha vazada basta
         para abrir os autos de todos os clientes deste workspace."
    fi

    if [ -z "${JARBAS_AI_TETO_USD_MES:-}" ]; then
        aviso "JARBAS_AI_TETO_USD_MES nao definido. Sem teto, um laco de
         analise mal configurado gasta sem limite na API de IA."
    fi
fi

# ------------------------------------------------------------------ volume
mkdir -p "$DATA_DIR"
if [ ! -w "$DATA_DIR" ]; then
    fatal "Sem permissao de escrita em $DATA_DIR.
       O volume precisa pertencer ao uid 10001 (usuario 'jarbas'):
       chown -R 10001:10001 <caminho-do-volume-no-host>"
fi

# Autos de clientes não são leitura pública nem para outros usuários da
# máquina: o diretório fica acessível apenas ao dono.
chmod 700 "$DATA_DIR" 2>/dev/null || true

# -------------------------------------------------------------- subcomandos
case "${1:-serve}" in
    serve)
        echo "JARBAS $(cat /app/VERSION.txt 2>/dev/null || echo '?') | dados em $DATA_DIR | porta $PORTA"
        # --proxy-headers faz o uvicorn enxergar o esquema (http/https) que o
        # proxy informou. Quem lê o IP do cliente é a aplicacao, via
        # JARBAS_TRUSTED_PROXY_HOPS; ver app/main.py:login_client_ip.
        exec python -m uvicorn app.main:app \
            --host 0.0.0.0 \
            --port "$PORTA" \
            --proxy-headers \
            --forwarded-allow-ips "${JARBAS_FORWARDED_ALLOW_IPS:-*}" \
            --workers "${JARBAS_WORKERS:-2}" \
            --log-level "${JARBAS_LOG_LEVEL:-info}"
        ;;
    backup)
        shift
        exec python /app/tools/backup.py "$@"
        ;;
    verificar)
        shift
        exec python /app/tools/backup.py --verificar "$@"
        ;;
    shell)
        exec /bin/sh
        ;;
    *)
        exec "$@"
        ;;
esac
