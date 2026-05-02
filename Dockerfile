FROM python:3.11-slim

# Install ffmpeg + nginx
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nginx \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY static/ ./static/

# nginx config — replace default site
COPY nginx.conf /etc/nginx/sites-available/capmp3
RUN ln -s /etc/nginx/sites-available/capmp3 /etc/nginx/sites-enabled/capmp3 \
    && rm -f /etc/nginx/sites-enabled/default

EXPOSE 8080

# Start Streamlit on internal port 8501, nginx on public port 8080
CMD sh -c "streamlit run app.py \
        --server.port=8501 \
        --server.address=127.0.0.1 \
        --server.headless=true \
        --browser.gatherUsageStats=false \
        --server.enableXsrfProtection=true \
    & nginx -g 'daemon off;'"
