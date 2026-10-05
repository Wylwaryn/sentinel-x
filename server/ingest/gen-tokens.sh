#!/usr/bin/env bash
# Ajoute au .env les jetons de l'API d'ingestion qui manquent (un par client).
# Usage (dans la VM) : sudo ingest/gen-tokens.sh   puis   sudo docker compose up -d ingest
set -euo pipefail
cd "$(dirname "$0")/.."
[ "$(id -u)" -eq 0 ] || { echo "À lancer avec sudo." >&2; exit 1; }
for var in INGEST_TOKEN_VISION INGEST_TOKEN_IDS INGEST_TOKEN_CAPTEURS; do
    current=$(grep -E "^${var}=" .env | tail -n1 | cut -d= -f2- || true)
    if [ -z "$current" ] || [ "$current" = "changez-moi" ]; then
        sed -i "/^${var}=/d" .env
        echo "${var}=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" >> .env
        echo "  $var : généré"
    fi
done
chmod 600 .env
