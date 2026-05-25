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
COPY start.sh .
RUN chmod +x start.sh

# nginx config — store as template, start.sh injects real PORT at runtime
RUN mkdir -p /etc/nginx/templates /etc/nginx/sites-enabled
COPY nginx.conf /etc/nginx/templates/capmp3.template
RUN rm -f /etc/nginx/sites-enabled/default

EXPOSE 8080
