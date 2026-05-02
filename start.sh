#!/bin/sh
set -e

PORT_VAL="${PORT:-8080}"

# Inject actual PORT into nginx config
sed "s/__PORT__/${PORT_VAL}/" /etc/nginx/templates/capmp3.template \
    > /etc/nginx/sites-enabled/capmp3

# Validate nginx config before starting anything
nginx -t

# Start Streamlit on internal port (localhost only)
streamlit run app.py \
    --server.port=8501 \
    --server.address=127.0.0.1 \
    --server.headless=true \
    --browser.gatherUsageStats=false \
    --server.enableXsrfProtection=true &

# Give Streamlit time to boot before nginx starts accepting traffic
# Railway healthcheckTimeout=30 covers this window
sleep 8

# Start nginx in foreground — container exits if nginx dies
exec nginx -g 'daemon off;'
