#!/usr/bin/env bash
# Génère secrets/mosquitto/passwd à partir des MQTT_*_PASSWORD du .env.
#
# Usage (dans la VM) : sudo mosquitto/gen-passwd.sh
#   - un mot de passe manquant dans .env est généré et ajouté au .env ;
#   - les mots de passe passent par stdin vers mosquitto_passwd : jamais dans argv/ps ;
#   - fichier possédé par l'uid mosquitto remappé (1883 + base dockremap), en 600.
# Puis : sudo docker compose restart mosquitto
set -euo pipefail
umask 077

cd "$(dirname "$0")/.."
[ "$(id -u)" -eq 0 ] || { echo "À lancer avec sudo." >&2; exit 1; }
[ -f .env ] || { echo ".env introuvable (copier .env.example)." >&2; exit 1; }

IMAGE=eclipse-mosquitto:2.0
USERS=(esp ingest dashboard vision capteurs monitor)
OUT=secrets/mosquitto/passwd

base=$(awk -F: '$1=="dockremap"{print $2}' /etc/subuid)
host_uid=$((base + 1883))

mkdir -p "$(dirname "$OUT")"
chmod 755 secrets "$(dirname "$OUT")"

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT

for u in "${USERS[@]}"; do
    var="MQTT_${u^^}_PASSWORD"
    pass=$(grep -E "^${var}=" .env | tail -n1 | cut -d= -f2- || true)
    if [ -z "$pass" ] || [ "$pass" = "changez-moi" ]; then
        pass=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
        sed -i "/^${var}=/d" .env
        echo "${var}=${pass}" >> .env
        echo "  $var : généré et ajouté au .env"
    fi
    # Une ligne "user:hash" par compte ; le mot de passe arrive sur stdin.
    printf '%s\n%s\n' "$pass" "$pass" | docker run --rm -i --network none --user 1883 \
        --entrypoint sh "$IMAGE" -c \
        'f=/tmp/p; : > "$f"; chmod 600 "$f"; mosquitto_passwd "$f" "$1" >/dev/null && cat "$f"' \
        _ "$u" >> "$tmp"
done

[ "$(wc -l < "$tmp")" -eq "${#USERS[@]}" ] || { echo "Échec de génération." >&2; exit 1; }
install -o "$host_uid" -g "$host_uid" -m 600 "$tmp" "$OUT"
chmod 600 .env
echo "OK : $OUT (${#USERS[@]} comptes : ${USERS[*]})"
