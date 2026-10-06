FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# Utilisateur sans shell ; /data/references lui appartient (copié dans le volume nommé à sa création).
# /data/captures : volume de l'API d'ingestion, monté ici en lecture seule. Si ce conteneur le monte
# en premier, Docker copie le propriétaire de ce dossier dans le volume : il doit rester celui de
# l'ingestion (uid 10001), sinon elle ne peut plus écrire les captures.
RUN useradd --uid 10002 --no-create-home --shell /usr/sbin/nologin dashboard \
    && mkdir -p /data/references /data/captures \
    && chown dashboard:dashboard /data/references && chown 10001:10001 /data/captures

WORKDIR /srv
COPY requirements.txt .
RUN pip install --root-user-action=ignore -r requirements.txt
COPY app ./app

USER 10002
EXPOSE 8000
# HTTP simple sur le réseau interne net_dashboard : le TLS est terminé par Caddy (seul point d'entrée).
# --proxy-headers : l'IP du navigateur vient de X-Forwarded-For posé par Caddy (anti force brute par IP).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--no-server-header", "--limit-concurrency", "200", "--timeout-keep-alive", "5", \
     "--ws-max-size", "65536"]
