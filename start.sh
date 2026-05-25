#!/bin/sh
set -e

PORT_VAL="${PORT:-8080}"

# Inject actual PORT into nginx config
sed "s/__PORT__/${PORT_VAL}/" /etc/nginx/templates/capmp3.template \
    > /etc/nginx/sites-enabled/capmp3

# Validate nginx config
nginx -t

# Start Streamlit in background — nginx doesn't wait for it
# (nginx answers /healthz directly; Streamlit routes get 502 for a few seconds
#  until Streamlit finishes booting, then nginx proxies normally)
streamlit run app.py \
    --server.port=8501 \
    --server.address=127.0.0.1 \
    --server.headless=true \
    --browser.gatherUsageStats=false \
    --server.enableXsrfProtection=true &

# Start nginx immediately in foreground — health check works without Streamlit
exec nginx -g 'daemon off;'
