#!/bin/sh
set -e

echo "==> [start.sh] starting, PID=$$"
PORT_VAL="${PORT:-8080}"
echo "==> PORT_VAL=${PORT_VAL}"

# Ensure dirs exist
mkdir -p /etc/nginx/sites-enabled
mkdir -p /etc/nginx/templates
echo "==> dirs ok"

# Inject actual PORT into nginx config
sed "s/__PORT__/${PORT_VAL}/" /etc/nginx/templates/capmp3.template \
    > /etc/nginx/sites-enabled/capmp3
echo "==> nginx site config written"

# Validate nginx config
nginx -t
echo "==> nginx config OK"

# Start Streamlit in background — nginx doesn't wait for it
# (nginx answers /healthz directly; Streamlit routes get 502 for a few seconds
#  until Streamlit finishes booting, then nginx proxies normally)
streamlit run app.py \
    --server.port=8501 \
    --server.address=127.0.0.1 \
    --server.headless=true \
    --browser.gatherUsageStats=false \
    --server.enableXsrfProtection=true &
echo "==> Streamlit started in background (PID=$!)"

# Start nginx immediately in foreground — health check works without Streamlit
echo "==> launching nginx in foreground"
exec nginx -g 'daemon off;'
