#!/usr/bin/env bash
# Tests de l'API dashboard et de Caddy sur une pile de TEST isolée (base jetable, aucun port publié).
# Usage (dans la VM) : sudo dashboard/api/tests/test_dashboard.sh
# La pile "sentinel-test" est créée, testée puis détruite (down -v) : la production n'est pas touchée.
# Prérequis : .env, certs/ca.crt, certs/caddy/ (pki.sh server caddy ... DNS:sentinel-server ...), comptes MQTT.
set -u
export MSYS_NO_PATHCONV=1   # Git Bash (Windows) : ne pas réécrire les chemins /...

cd "$(dirname "$0")/../../../server"
docker info >/dev/null 2>&1 || { echo "Docker inaccessible (lancer avec sudo dans la VM)." >&2; exit 1; }

# Le scénario parle à l'API en direct (127.0.0.1, dans son conteneur) : c'est son « PC hôte ».
# Tout ce qui passe par Caddy a l'IP du conteneur de test, donc hors liste.
export SERVICE_VISION_IPS=127.0.0.1 HOST_ONLY_IPS=127.0.0.1
export COMPOSE_ARGS="-p sentinel-test -f docker-compose.yml -f docker-compose.test.yml -f ../dashboard/docker-compose.yml -f ../dashboard/docker-compose.test.yml"
DC=(docker compose $COMPOSE_ARGS)
NET=sentinel-test_net_dashboard
C=sentinel-dashboard-tester
SERIE=TEST-ESP-01
PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))' 2>/dev/null || python -c 'import secrets; print(secrets.token_urlsafe(18))')
PASS=0 FAIL=0

ok()  { echo "  OK    $1"; PASS=$((PASS+1)); }
ko()  { echo "  ÉCHEC $1"; FAIL=$((FAIL+1)); }
check() { if [ "$1" = "$2" ]; then ok "$3"; else ko "$3 (attendu « $2 », obtenu « $1 »)"; fi; }

cleanup() {
    docker rm -fv "$C" >/dev/null 2>&1
    "${DC[@]}" down -v >/dev/null 2>&1
}
trap cleanup EXIT

echo "== Démarrage de la pile de test =="
"${DC[@]}" up -d --build --wait postgres mosquitto dashboard caddy >/dev/null 2>&1 \
    || { "${DC[@]}" logs dashboard caddy | tail -20; echo "Pile de test indisponible" >&2; exit 1; }

sql() {
    "${DC[@]}" exec -T postgres sh -c \
        'PGPASSWORD=$POSTGRES_PASSWORD psql -X -q -U $POSTGRES_USER -d $POSTGRES_DB -Atc "$1"' _ "$1" </dev/null
}
sql "INSERT INTO site (nom) VALUES ('Site de test');
     INSERT INTO dispositif (id_site, nom, numero_serie) VALUES (1, 'ESP de test', '$SERIE');"
for role in ADMIN OPERATEUR LECTEUR; do
    SENTINEL_PASSWORD=$PASSWORD ../dashboard/api/add-user.sh "${role,,}@test.local" "Test $role" "$role" </dev/null >/dev/null \
        || ko "création du compte $role"
done
SENTINEL_PASSWORD=$PASSWORD ../dashboard/api/add-user.sh "inactif@test.local" "Compte désactivé" OPERATEUR </dev/null >/dev/null
SENTINEL_PASSWORD=$PASSWORD ../dashboard/api/add-user.sh "service@test.local" "Synchronisation vision" SERVICE_VISION </dev/null >/dev/null     || ko "création du compte SERVICE_VISION (terminal)"
sql "UPDATE utilisateur SET actif = false WHERE email = 'inactif@test.local'"
check "$(sql "SELECT count(*) FROM utilisateur WHERE mot_de_passe_hash LIKE '\$argon2id\$%'")" 5 "mots de passe hachés en Argon2id"

envval() { grep -E "^$1=" .env | tail -n1 | cut -d= -f2-; }

echo "== Caddy (HTTPS :443, seul point d'entrée) =="
docker run -d --name "$C" --network "$NET" --read-only --tmpfs /tmp --cap-drop ALL \
    -v "$PWD/certs/ca.crt:/ca.crt:ro" --entrypoint sleep curlimages/curl:latest 600 >/dev/null
# Le certificat de Caddy est vérifié pour le nom sentinel-server (SAN), joint sur caddy:8443.
CURL=(docker exec "$C" curl -s --cacert /ca.crt --connect-to sentinel-server:443:caddy:8443)
URL=https://sentinel-server
check "$("${CURL[@]}" -o /dev/null -w '%{http_code}' $URL/)" 200 "page du dashboard en HTTPS, certificat vérifié"
HEADERS=$("${CURL[@]}" -I $URL/)
grep -qi '^content-security-policy:.*script-src .self.' <<<"$HEADERS" && ok "CSP sans script en ligne" || ko "CSP absente"
grep -qi '^strict-transport-security:' <<<"$HEADERS" && ok "HSTS" || ko "HSTS absent"
grep -qi '^x-frame-options: DENY' <<<"$HEADERS" && ok "X-Frame-Options DENY" || ko "X-Frame-Options absent"
grep -qi '^server:' <<<"$HEADERS" && ko "en-tête Server présent" || ok "pas d'en-tête Server"
check "$("${CURL[@]}" -o /dev/null -w '%{http_code}' $URL/api/v1/dispositifs)" 401 "API relayée par Caddy, session exigée"
check "$("${CURL[@]}" -o /dev/null -w '%{http_code}' $URL/healthz)" 200 "route inconnue de Caddy : page du dashboard, pas l'API"
"${CURL[@]}" --tlsv1.1 --tls-max 1.1 -o /dev/null $URL/ >/dev/null 2>&1
[ $? -ne 0 ] && ok "TLS 1.1 refusé" || ko "TLS 1.1 accepté"
"${CURL[@]}" --tls-max 1.2 --ciphers AES128-SHA -o /dev/null $URL/ >/dev/null 2>&1
[ $? -ne 0 ] && ok "TLS 1.2 sans ECDHE refusé" || ko "suite sans PFS acceptée"
check "$(docker exec "$C" curl -s -o /dev/null -w '%{http_code}' http://caddy:8443/)" 400 "HTTP en clair refusé"
check "$(docker exec "$C" curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://dashboard:8000/healthz)" 200 \
    "API joignable seulement sur le réseau interne (aucun port publié)"
# docker compose port renvoie "" (Docker Desktop) ou ":0" (Compose v5 dans la VM) : on lit la config du conteneur.
check "$(docker inspect "$("${DC[@]}" ps -q dashboard)" --format '{{json .HostConfig.PortBindings}}')" "{}" "aucun port publié pour l'API"

echo "== IP réelle derrière Caddy =="
IP_TESTEUR=$(docker inspect "$C" --format "{{(index .NetworkSettings.Networks \"$NET\").IPAddress}}")
# login <email> <mot de passe> [en-tête en plus] : code HTTP d'une connexion passée par Caddy
login() {
    printf '{"email":"%s","password":"%s"}' "$1" "$2" | docker exec -i "$C" sh -c 'cat > /tmp/login'
    "${CURL[@]}" -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' \
        -H "Origin: $(envval DASHBOARD_ORIGINS | cut -d, -f1)" ${3:+-H "$3"} --data-binary @/tmp/login $URL/api/v1/auth/login
}
check "$(login service@test.local "$PASSWORD")" 403 "SERVICE_VISION depuis une autre machine que le PC hôte : 403"
check "$(login service@test.local "$PASSWORD" 'X-Forwarded-For: 127.0.0.1')" 403 \
    "X-Forwarded-For forgé (IP autorisée) : toujours 403, Caddy l'écrase"
# ADMIN connecté depuis une autre machine que le PC hôte (cookie gardé dans le conteneur de test)
printf '{"email":"admin@test.local","password":"%s"}' "$PASSWORD" | docker exec -i "$C" sh -c 'cat > /tmp/login'
# (curl ne renvoie pas un cookie posé pour un hôte sans point comme « sentinel-server » : on le repasse à la main)
"${CURL[@]}" -o /dev/null -D /tmp/entetes -H 'Content-Type: application/json' \
    -H "Origin: $(envval DASHBOARD_ORIGINS | cut -d, -f1)" --data-binary @/tmp/login $URL/api/v1/auth/login
SESSION_ADMIN=$(docker exec "$C" grep -io 'sentinel_session=[^;]*' /tmp/entetes)
check "$("${CURL[@]}" -H "Cookie: $SESSION_ADMIN" $URL/api/v1/auth/me | grep -o '"poste_hote":[a-z]*')" '"poste_hote":false' \
    "ADMIN hors du PC hôte : /auth/me indique poste_hote false"
check "$("${CURL[@]}" -o /dev/null -w '%{http_code}' -H "Cookie: $SESSION_ADMIN" $URL/api/v1/utilisateurs)" 403 \
    "ADMIN hors du PC hôte : utilisateurs refusés (403)"
check "$("${CURL[@]}" -o /dev/null -w '%{http_code}' -H "Cookie: $SESSION_ADMIN" -H 'X-Forwarded-For: 127.0.0.1' $URL/api/v1/images-reference)" 403 \
    "ADMIN hors du PC hôte + X-Forwarded-For forgé : images refusées (403)"
"${DC[@]}" logs dashboard 2>/dev/null | grep -q "refusé hors du PC hôte depuis $IP_TESTEUR " \
    && ok "journal : ADMIN refusé hors du PC hôte, avec son IP réelle" || ko "refus hors PC hôte absent du journal"
"${DC[@]}" logs dashboard 2>/dev/null | grep -q "refusée depuis $IP_TESTEUR " \
    && ok "journal : IP réelle du client ($IP_TESTEUR), pas celle de Caddy" || ko "IP réelle absente du journal"
for i in 1 2 3 4 5 6; do
    DERNIER=$(login "inconnu$i@test.local" "mauvais-mot-de-passe" "X-Forwarded-For: 10.0.2.$i")
done
check "$DERNIER" 429 "force brute bloquée par IP réelle, même en changeant de X-Forwarded-For"
# Le scénario ci-dessous se connecte ensuite depuis une autre IP : le blocage ne touche que l'attaquant.

echo "== Scénario API (dans le conteneur dashboard) =="
ORIGIN=$(envval DASHBOARD_ORIGINS | cut -d, -f1)
printf '{"db":"%s","admin_user":"%s","admin_pw":"%s","dashboard_pw":"%s","mqtt_esp":"%s","mqtt_vision":"%s","password":"%s","origin":"%s"}' \
    "$(envval POSTGRES_DB)" "$(envval POSTGRES_USER)" "$(envval POSTGRES_PASSWORD)" "$(envval DASHBOARD_DB_PASSWORD)" \
    "$(envval MQTT_ESP_PASSWORD)" "$(envval MQTT_VISION_PASSWORD)" "$PASSWORD" "$ORIGIN" \
    | "${DC[@]}" exec -T dashboard sh -c 'cat > /tmp/test_secrets.json'
"${DC[@]}" exec -T dashboard python - < ../dashboard/api/tests/scenario.py
SCENARIO=$?
"${DC[@]}" exec -T dashboard rm -f /tmp/test_secrets.json </dev/null

echo
echo "Caddy : $PASS OK, $FAIL échec(s)"
[ $FAIL -eq 0 ] && [ $SCENARIO -eq 0 ]
