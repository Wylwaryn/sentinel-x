#!/usr/bin/env bash
# Tests de sécurité Mosquitto : TLS, authentification, matrice ACL, limites.
# Usage (dans la VM, stack lancée) : sudo mosquitto/tests/test_mqtt.sh
#
# N'écrit rien en base : publie seulement des messages "test-..." sur les topics.
# Attention : l'API d'ingestion (si elle tourne) recevra la télémétrie de test
# et doit l'ignorer (JSON invalide).
# Pas de pipefail : on teste la sortie des commandes refusées (code retour non nul).
set -u

cd "$(dirname "$0")/../.."
[ "$(id -u)" -eq 0 ] || { echo "À lancer avec sudo." >&2; exit 1; }

IMAGE=eclipse-mosquitto:2.0
NET=sentinel_net_ingest
C=sentinel-mqtt-test
PASS=0 FAIL=0

ok()  { echo "  OK    $1"; PASS=$((PASS+1)); }
ko()  { echo "  ÉCHEC $1"; FAIL=$((FAIL+1)); }
check() { if [ "$1" = "$2" ]; then ok "$3"; else ko "$3 (attendu $2, obtenu $1)"; fi; }

cleanup() { docker rm -fv "$C" >/dev/null 2>&1; }
trap cleanup EXIT
cleanup

# Conteneur client sur le réseau interne (comme le fera l'API d'ingestion)
docker run -d --name "$C" --network "$NET" --user 1883 --read-only --tmpfs /tmp \
    --cap-drop ALL -v "$PWD/certs/ca.crt:/ca.crt:ro" --entrypoint sleep "$IMAGE" 600 >/dev/null

# Un profil de connexion par compte, dans un fichier (pas de mot de passe dans argv).
# mosquitto_pub/sub lisent $XDG_CONFIG_HOME/mosquitto_{pub,sub}.
profile() {  # nom user mdp [options supplémentaires]
    local opts="-h mosquitto
-p 8883
--cafile /ca.crt
-V mqttv5"
    [ -n "$2" ] && opts+=$'\n'"-u $2"$'\n'"-P $3"
    [ -n "${4:-}" ] && opts+=$'\n'"$4"
    printf '%s\n' "$opts" | docker exec -i "$C" sh -c \
        "mkdir -p /tmp/cfg/$1 && cat > /tmp/cfg/$1/mosquitto_pub && cp /tmp/cfg/$1/mosquitto_pub /tmp/cfg/$1/mosquitto_sub"
}
envpass() { grep -E "^MQTT_${1^^}_PASSWORD=" .env | tail -n1 | cut -d= -f2-; }

for u in esp ingest dashboard vision; do profile "$u" "$u" "$(envpass "$u")"; done
profile anon "" ""
profile badpw esp "mauvais-mot-de-passe"

pub() { docker exec "$C" env XDG_CONFIG_HOME=/tmp/cfg/$1 mosquitto_pub -q 1 -t "$2" "${@:3}" 2>&1; }

# flow <publieur> <topic pub> <abonné> <filtre sub> : affiche "recu" ou "bloque"
flow() {
    local msg="test-$RANDOM$RANDOM" out
    out=$(mktemp)
    docker exec "$C" env XDG_CONFIG_HOME=/tmp/cfg/$3 \
        mosquitto_sub -q 1 -t "$4" -C 1 -W 4 >"$out" 2>&1 &
    local pid=$!
    sleep 1
    pub "$1" "$2" -m "$msg" >/dev/null
    wait "$pid"
    if grep -qx "$msg" "$out"; then echo recu; else echo bloque; fi
    rm -f "$out"
}

echo "== TLS (port publié 127.0.0.1:8883, vérifié par l'hôte) =="
s_client() { timeout 5 openssl s_client -connect 127.0.0.1:8883 -CAfile certs/ca.crt "$@" </dev/null 2>&1; }
s_client -verify_ip 127.0.0.1 -verify_return_error | grep -q "Verify return code: 0" \
    && ok "certificat valide pour IP 127.0.0.1 (SAN) signé par notre CA" || ko "vérification certificat 127.0.0.1"
s_client -verify_ip 10.9.9.9 -verify_return_error | grep -q "Verify return code: 0" \
    && ko "IP hors SAN acceptée" || ok "IP hors SAN refusée par le client"
# SECLEVEL=0 : force notre client à tenter TLS 1.1, pour que ce soit le SERVEUR qui refuse.
s_client -tls1_1 -cipher 'DEFAULT@SECLEVEL=0' | grep -q "alert protocol version" \
    && ok "TLS 1.1 refusé par le serveur" || ko "TLS 1.1 accepté"
s_client -tls1_2 -cipher AES128-SHA | grep -q "alert handshake failure" \
    && ok "TLS 1.2 sans ECDHE (pas de PFS) refusé" || ko "suite sans PFS acceptée"
s_client -tls1_2 | grep -q "Protocol *: TLSv1.2\|TLSv1.2, Cipher is ECDHE" \
    && ok "TLS 1.2 ECDHE accepté (ESP8266)" || ko "TLS 1.2 ECDHE refusé"

echo "== Transport et authentification (réseau interne, nom « mosquitto ») =="
docker exec "$C" timeout 5 mosquitto_pub -h mosquitto -p 1883 -t x -m x >/dev/null 2>&1
check "$?" 1 "aucun MQTT en clair sur 1883"
docker exec "$C" timeout 5 mosquitto_pub -h mosquitto -p 8883 -t x -m x >/dev/null 2>&1
[ $? -ne 0 ] && ok "MQTT en clair sur 8883 refusé" || ko "MQTT en clair sur 8883 accepté"
ip=$(docker exec "$C" getent hosts mosquitto | awk '{print $1}')
docker exec "$C" env XDG_CONFIG_HOME=/tmp/cfg/esp timeout 5 mosquitto_pub -h "$ip" -t sentinel/telemetry -m x >/dev/null 2>&1
[ $? -ne 0 ] && ok "connexion par IP interne (hors SAN) refusée par le client" || ko "nom d'hôte non vérifié"
pub anon sentinel/telemetry -m x | grep -qi "not authorized\|refused" \
    && ok "anonyme refusé" || ko "anonyme accepté"
pub badpw sentinel/telemetry -m x | grep -qi "not authorized\|refused\|bad user" \
    && ok "mauvais mot de passe refusé" || ko "mauvais mot de passe accepté"
pub esp sentinel/telemetry -m '{}' >/dev/null && ok "compte esp accepté" || ko "compte esp refusé"

echo "== Matrice ACL =="
#      publieur  topic publié           abonné     filtre               attendu
check "$(flow esp       sentinel/telemetry   ingest    sentinel/telemetry)"   recu   "esp -> telemetry, lu par ingest"
check "$(flow esp       sentinel/telemetry   vision    sentinel/telemetry)"   recu   "esp -> telemetry, lu par vision"
check "$(flow esp       sentinel/telemetry   dashboard sentinel/telemetry)"   bloque "dashboard ne lit pas telemetry"
check "$(flow esp       sentinel/telemetry   esp       sentinel/telemetry)"   bloque "esp ne lit pas telemetry"
check "$(flow dashboard sentinel/cmd/ESP-01  esp       sentinel/cmd/ESP-01)"  recu   "dashboard -> cmd, lu par esp"
check "$(flow vision    sentinel/video/cam1  dashboard sentinel/video/cam1)"  recu   "vision -> video, lu par dashboard"
check "$(flow ingest    sentinel/telemetry   vision    sentinel/telemetry)"   bloque "ingest ne publie pas (usurpation ESP)"
check "$(flow vision    sentinel/telemetry   ingest    sentinel/telemetry)"   bloque "vision ne publie pas telemetry"
check "$(flow dashboard sentinel/telemetry   ingest    sentinel/telemetry)"   bloque "dashboard ne publie pas telemetry"
check "$(flow vision    sentinel/cmd/ESP-01  esp       sentinel/cmd/ESP-01)"  bloque "vision ne commande pas l'ESP"
check "$(flow esp       sentinel/cmd/ESP-01  esp       sentinel/cmd/ESP-01)"  bloque "esp ne se commande pas lui-même"
check "$(flow esp       sentinel/video/cam1  dashboard sentinel/video/cam1)"  bloque "esp ne publie pas de vidéo"
check "$(flow vision    sentinel/video/cam1  ingest    sentinel/video/cam1)"  bloque "ingest ne lit pas la vidéo"
check "$(flow vision    sentinel/video/cam1  vision    sentinel/video/cam1)"  bloque "vision ne relit pas la vidéo"
check "$(flow dashboard sentinel/cmd/ESP-01  dashboard sentinel/cmd/ESP-01)"  bloque "dashboard ne relit pas les commandes"
# Jokers : Mosquitto filtre chaque message livré selon l'ACL de lecture,
# un abonnement à # ne donne donc accès qu'aux topics déjà autorisés.
check "$(flow esp       sentinel/telemetry   dashboard '#')"                  bloque "dashboard abonné à # ne voit pas telemetry"
check "$(flow vision    sentinel/video/cam1  ingest    'sentinel/#')"         bloque "ingest abonné à sentinel/# ne voit pas la vidéo"
check "$(flow dashboard sentinel/cmd/ESP-01  vision    '#')"                  bloque "vision abonné à # ne voit pas les commandes"
check "$(flow esp       sentinel/telemetry   vision    '#')"                  recu   "vision abonné à # reçoit telemetry (autorisé)"
sys=$(docker exec "$C" env XDG_CONFIG_HOME=/tmp/cfg/dashboard mosquitto_sub -t '$SYS/#' -C 1 -W 3 2>/dev/null)
[ -z "$sys" ] && ok "\$SYS/# illisible" || ko "\$SYS/# lisible"

echo "== Limites =="
out=$(mktemp)
docker exec "$C" env XDG_CONFIG_HOME=/tmp/cfg/dashboard mosquitto_sub -t sentinel/video/cam1 -C 1 -W 5 >"$out" 2>&1 &
pid=$!; sleep 1
docker exec "$C" sh -c 'head -c 2000000 /dev/zero | tr "\0" A > /tmp/big'
docker exec "$C" env XDG_CONFIG_HOME=/tmp/cfg/vision mosquitto_pub -t sentinel/video/cam1 -f /tmp/big >/dev/null 2>&1
wait "$pid"
[ "$(wc -c <"$out")" -lt 1000000 ] && ok "message de 2 Mo rejeté (max_packet_size)" || ko "message de 2 Mo accepté"
rm -f "$out"
docker exec "$C" sh -c 'head -c 60000 /dev/zero | tr "\0" A > /tmp/img'
check "$(docker exec "$C" env XDG_CONFIG_HOME=/tmp/cfg/vision mosquitto_pub -t sentinel/video/cam1 -f /tmp/img >/dev/null 2>&1; echo $?)" 0 \
    "image de 60 Ko acceptée (flux vidéo)"

echo
echo "Résultat : $PASS OK, $FAIL échec(s)"
[ "$FAIL" -eq 0 ]
