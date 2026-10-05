FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# Utilisateur sans shell ; /data/captures lui appartient (copié dans le volume nommé à sa création).
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin ingest \
    && mkdir -p /data/captures && chown ingest:ingest /data/captures

WORKDIR /srv
COPY requirements.txt .
RUN pip install --root-user-action=ignore -r requirements.txt
COPY app ./app

USER 10001
EXPOSE 8443
# TLS 1.2+ (défaut Python), suites ECDHE + AEAD uniquement, pas d'en-tête Server.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8443", \
     "--ssl-keyfile", "/certs/server.key", "--ssl-certfile", "/certs/server.crt", \
     "--ssl-ciphers", "ECDHE+AESGCM:ECDHE+CHACHA20", \
     "--no-server-header", "--limit-concurrency", "64", "--timeout-keep-alive", "5"]
