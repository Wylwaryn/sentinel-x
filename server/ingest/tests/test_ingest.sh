#!/usr/bin/env bash
# Tests de l'API d'ingestion sur une pile de TEST isolée (base jetable, aucun port publié).
# Usage (dans la VM) : sudo ingest/tests/test_ingest.sh
# La pile "sentinel-test" est créée, testée puis détruite (down -v) : la production n'est pas touchée.
set -u

cd "$(dirname "$0")/../.."
[ "$(id -u)" -eq 0 ] || { echo "À lancer avec sudo." >&2; exit 1; }

DC=(docker compose -p sentinel-test -f docker-compose.yml -f docker-compose.test.yml)
NET=sentinel-test_net_ingest
C=sentinel-ingest-tester
M=sentinel-mqtt-tester
SERIE=TEST-ESP-01
PASS=0 FAIL=0

ok()  { echo "  OK    $1"; PASS=$((PASS+1)); }
ko()  { echo "  ÉCHEC $1"; FAIL=$((FAIL+1)); }
check() { if [ "$1" = "$2" ]; then ok "$3"; else ko "$3 (attendu « $2 », obtenu « $1 »)"; fi; }

cleanup() {
    docker rm -fv "$C" "$M" >/dev/null 2>&1
    "${DC[@]}" down -v >/dev/null 2>&1
}
trap cleanup EXIT

echo "== Démarrage de la pile de test =="
"${DC[@]}" up -d --build --wait postgres mosquitto ingest >/dev/null 2>&1 \
    || { "${DC[@]}" logs ingest | tail -20; echo "Pile de test indisponible" >&2; exit 1; }

sql() {
    "${DC[@]}" exec -T postgres sh -c \
        'PGPASSWORD=$POSTGRES_PASSWORD psql -X -q -U $POSTGRES_USER -d $POSTGRES_DB -Atc "$1"' _ "$1" </dev/null
}
sql "INSERT INTO site (nom) VALUES ('Site de test');
     INSERT INTO dispositif (id_site, nom, numero_serie) VALUES (1, 'ESP de test', '$SERIE');"
DEV=$(sql "SELECT id_dispositif FROM dispositif WHERE numero_serie = '$SERIE'")

envval() { grep -E "^$1=" .env | tail -n1 | cut -d= -f2-; }

# Client HTTPS sur le réseau interne, CA vérifiée, jetons dans des fichiers (pas dans argv).
docker run -d --name "$C" --network "$NET" --read-only --tmpfs /tmp --cap-drop ALL \
    -v "$PWD/certs/ca.crt:/ca.crt:ro" --entrypoint sleep curlimages/curl:latest 900 >/dev/null
for client in vision ids; do
    printf 'header = "Authorization: Bearer %s"\n' "$(envval "INGEST_TOKEN_${client^^}")" \
        | docker exec -i "$C" sh -c "cat > /tmp/auth_$client"
done
printf 'header = "Authorization: Bearer %s"\n' "jeton-invente-par-un-attaquant-0123456789abcdef" \
    | docker exec -i "$C" sh -c "cat > /tmp/auth_bad"
: | docker exec -i "$C" sh -c "cat > /tmp/auth_none"

URL=https://ingest:8443
# post <client> <json> : affiche "code corps"
post() {
    printf '%s' "$2" | docker exec -i "$C" sh -c "cat > /tmp/body"
    docker exec "$C" curl -s --cacert /ca.crt -K "/tmp/auth_$1" -o /tmp/resp -w '%{http_code}' \
        -H 'Content-Type: application/json' --data-binary @/tmp/body "$URL/api/v1/alerts"
    echo -n " "; docker exec "$C" cat /tmp/resp
}
code() { post "$@" | cut -d' ' -f1; }
last_alerte() { sql "SELECT origine||'/'||type_alerte||'/'||niveau||'/'||coalesce(id_dispositif::text,'-') FROM alerte ORDER BY id_alerte DESC LIMIT 1"; }

echo "== HTTPS et surface exposée =="
check "$(docker exec "$C" curl -s --cacert /ca.crt -o /dev/null -w '%{http_code}' $URL/healthz)" 200 "healthz en HTTPS, certificat vérifié (SAN « ingest »)"
check "$(docker exec "$C" curl -s --cacert /ca.crt -o /dev/null -w '%{http_code}' $URL/docs)" 404 "/docs désactivé"
check "$(docker exec "$C" curl -s --cacert /ca.crt -o /dev/null -w '%{http_code}' $URL/openapi.json)" 404 "/openapi.json désactivé"
docker exec "$C" curl -s --cacert /ca.crt --tlsv1.1 --tls-max 1.1 $URL/healthz >/dev/null 2>&1
[ $? -ne 0 ] && ok "TLS 1.1 refusé" || ko "TLS 1.1 accepté"
docker exec "$C" curl -s --cacert /ca.crt --tls-max 1.2 --ciphers AES128-SHA $URL/healthz >/dev/null 2>&1
[ $? -ne 0 ] && ok "TLS 1.2 sans ECDHE refusé" || ko "suite sans PFS acceptée"
docker exec "$C" curl -s -o /dev/null http://ingest:8443/healthz >/dev/null 2>&1
check "$(docker exec "$C" curl -s -o /dev/null -w '%{http_code}' http://ingest:8443/healthz)" 000 "HTTP en clair refusé"
docker exec "$C" curl -sI --cacert /ca.crt $URL/healthz | grep -qi '^server:' && ko "en-tête Server présent" || ok "pas d'en-tête Server"

echo "== Authentification =="
VISION_FUSION='{"type":"intrusion","level":"critical","source":"fusion","pir_confirmed":true,"ts":1.0,"track_id":7,"zone":"zone_interdite","detail":{}}'
check "$(code none "$VISION_FUSION")" 401 "sans jeton : 401"
check "$(code bad "$VISION_FUSION")" 401 "jeton inventé : 401"
check "$(sql 'SELECT count(*) FROM alerte')" 0 "aucune alerte écrite par les requêtes refusées"

echo "== Alertes vision (format de host/vision/alerts.py) =="
check "$(code vision "$VISION_FUSION")" 201 "fusion/intrusion/critical : 201"
check "$(last_alerte)" "FUSION/INTRUSION/CRITIQUE/$DEV" "traduit en FUSION/INTRUSION/CRITIQUE, dispositif par défaut"
check "$(sql 'SELECT zone||'"'"'/'"'"'||pir_confirme||'"'"'/'"'"'||message FROM alerte ORDER BY id_alerte DESC LIMIT 1')" \
    "zone_interdite/true/INTRUSION piste 7 zone zone_interdite" "zone, PIR et message enregistrés"
code vision '{"type":"presence","level":"info","source":"vision","pir_confirmed":false,"track_id":1}' >/dev/null
check "$(last_alerte)" "VISION_IA/PRESENCE/INFORMATION/$DEV" "vision/presence/info -> VISION_IA/PRESENCE/INFORMATION"
code vision '{"type":"loitering","level":"warning","source":"vision","pir_confirmed":false}' >/dev/null
check "$(last_alerte)" "VISION_IA/RODEUR/AVERTISSEMENT/$DEV" "loitering -> RODEUR"
code vision '{"type":"fast_approach","level":"critical","source":"fusion","pir_confirmed":true}' >/dev/null
check "$(last_alerte)" "FUSION/APPROCHE_RAPIDE/CRITIQUE/$DEV" "fast_approach -> APPROCHE_RAPIDE"
code vision '{"type":"pir_blind_spot","level":"warning","source":"pir","pir_confirmed":true,"detail":{"message":"PIR déclenché sans personne visible"}}' >/dev/null
check "$(last_alerte)" "PIR/ANGLE_MORT/AVERTISSEMENT/$DEV" "pir_blind_spot -> PIR/ANGLE_MORT"
check "$(sql 'SELECT message FROM alerte ORDER BY id_alerte DESC LIMIT 1')" "PIR déclenché sans personne visible" "message repris de detail.message"

echo "== Cloisonnement des clients =="
check "$(code vision '{"type":"port_scan","level":"critical","source":"network"}')" 403 "vision ne peut pas écrire RESEAU_IA"
check "$(code ids '{"type":"intrusion","level":"critical","source":"vision","serie":"TEST-ESP-01"}')" 403 "ids ne peut pas écrire VISION_IA"
check "$(code ids '{"type":"port_scan","level":"critical","source":"network","ip_source":"192.168.137.50","score":0.93}')" 201 "ids : scan de ports accepté"
check "$(last_alerte)" "RESEAU_IA/SCAN_PORTS/CRITIQUE/-" "RESEAU_IA sans dispositif"
check "$(sql 'SELECT host(ip_source)||'"'"' '"'"'||score_ia FROM alerte ORDER BY id_alerte DESC LIMIT 1')" "192.168.137.50 0.93" "ip_source et score enregistrés"
code ids '{"type":"FORCE_BRUTE","level":"AVERTISSEMENT","source":"RESEAU_IA","ip_source":"10.0.0.9"}' >/dev/null
check "$(last_alerte)" "RESEAU_IA/FORCE_BRUTE/AVERTISSEMENT/-" "vocabulaire de la base accepté tel quel"

echo "== Contrat de CLAUDE.md (noms de colonnes BDD) =="
IDS_CONTRAT='{"origine": "RESEAU_IA", "type_alerte": "SCAN_PORTS", "niveau": "CRITIQUE",
 "ip_source": "192.168.137.66", "numero_serie": null, "score_ia": 0.97,
 "message": "SCAN_PORTS depuis 192.168.137.66 (score 0.97) : test", "action": "bloquee",
 "details": {"confiance_type": 0.98, "principales_deviations": ["syn_ratio"], "caracteristiques": {}}}'
check "$(code ids "$IDS_CONTRAT")" 201 "exemple IDS du contrat accepté tel quel"
check "$(sql "SELECT origine||'/'||type_alerte||'/'||niveau||'/'||host(ip_source)||'/'||score_ia||'/'||message FROM alerte ORDER BY id_alerte DESC LIMIT 1")" \
    "RESEAU_IA/SCAN_PORTS/CRITIQUE/192.168.137.66/0.97/SCAN_PORTS depuis 192.168.137.66 (score 0.97) : test" "champs du contrat stockés"
code ids '{"origine":"RESEAU_IA","type_alerte":"DENI_DE_SERVICE","niveau":"CRITIQUE","ip_source":"192.168.137.66","numero_serie":"TEST-ESP-01"}' >/dev/null
check "$(last_alerte)" "RESEAU_IA/DENI_DE_SERVICE/CRITIQUE/$DEV" "alerte réseau rattachée à l'ESP via numero_serie"
code vision '{"origine":"FUSION","type_alerte":"RODEUR","niveau":"AVERTISSEMENT","numero_serie":"TEST-ESP-01","zone":"perimetre","pir_confirme":true,"score_ia":0.81}' >/dev/null
check "$(last_alerte)" "FUSION/RODEUR/AVERTISSEMENT/$DEV" "vision au format BDD acceptée"
check "$(sql "SELECT zone||'/'||pir_confirme||'/'||score_ia FROM alerte ORDER BY id_alerte DESC LIMIT 1")" "perimetre/true/0.81" "zone, pir_confirme, score_ia stockés"
check "$(code ids '{"origine":"RESEAU_IA","type_alerte":"SCAN_PORTS","niveau":"CRITIQUE","action":"rm -rf /"}')" 422 "action hors format : 422"

echo "== Personne reconnue (reconnaissance faciale de la vision) =="
MEMBRE=$(sql "INSERT INTO utilisateur (id_role, nom, email, mot_de_passe_hash)
              VALUES (2, 'Membre test', 'membre@test.local', '\$argon2id\$test') RETURNING id_utilisateur")
check "$(code vision '{"type":"intrusion","level":"info","source":"fusion","serie":"TEST-ESP-01","personne_reconnue":'"$MEMBRE"',"message":"Personne autorisée : Membre test"}')" 201 "alerte avec personne_reconnue acceptée"
check "$(sql "SELECT id_personne_reconnue FROM alerte ORDER BY id_alerte DESC LIMIT 1")" "$MEMBRE" "id_personne_reconnue enregistré"
code vision '{"origine":"VISION_IA","type_alerte":"PRESENCE","niveau":"INFORMATION","numero_serie":"TEST-ESP-01","id_personne_reconnue":'"$MEMBRE"'}' >/dev/null
check "$(sql "SELECT origine||'/'||id_personne_reconnue FROM alerte ORDER BY id_alerte DESC LIMIT 1")" "VISION_IA/$MEMBRE" "nom BDD id_personne_reconnue accepté"
check "$(code vision '{"type":"presence","level":"info","source":"vision","personne_reconnue":999999}')" 422 "personne inconnue : 422 (pas 500)"
check "$(code vision '{"type":"presence","level":"info","source":"vision","personne_reconnue":0}')" 422 "personne_reconnue = 0 : 422"
check "$(code vision '{"type":"presence","level":"info","source":"vision","personne_reconnue":"1 OR 1=1"}')" 422 "personne_reconnue non entière : 422"
check "$(code vision '{"type":"pir_blind_spot","level":"warning","source":"pir","personne_reconnue":'"$MEMBRE"'}')" 422 "personne_reconnue refusée hors alerte caméra (PIR)"
check "$(code ids '{"origine":"RESEAU_IA","type_alerte":"SCAN_PORTS","niveau":"CRITIQUE","personne_reconnue":'"$MEMBRE"'}')" 422 "personne_reconnue refusée pour l'IDS"
denied=$("${DC[@]}" exec -T postgres sh -c 'PGPASSWORD=$INGEST_DB_PASSWORD psql -X -h 127.0.0.1 -U sentinel_ingest -d $POSTGRES_DB -Atc "SELECT count(*) FROM utilisateur"' </dev/null 2>&1)
grep -qi "permission denied\|droit refusé" <<<"$denied" && ok "le rôle ingest n'a toujours aucun accès à utilisateur" || ko "ingest lit utilisateur : $denied"

echo "== Validation =="
check "$(code vision '{"type":"port_scan","level":"info","source":"vision"}')" 422 "type incompatible avec l'origine : 422"
check "$(code vision '{"type":"presence","level":"apocalypse","source":"vision"}')" 422 "niveau inconnu : 422"
check "$(code vision '{"type":"presence","level":"info","source":"vision","serie":"ESP-FANTOME"}')" 422 "dispositif inconnu : 422"
check "$(code ids '{"type":"port_scan","level":"info","source":"network","ip_source":"pas-une-ip"}')" 422 "ip_source invalide : 422"
check "$(code ids '{"type":"port_scan","level":"info","source":"network","score":1.5}')" 422 "score hors [0,1] : 422"
check "$(code vision 'pas du json')" 422 "JSON invalide : 422"
resp=$(post vision '{"type":"presence","level":"info","source":"vision","zone":"MARQUEUR-ECHO-'"$(printf 'x%.0s' {1..60})"'"}')
check "${resp%% *}" 422 "zone trop longue : 422"
grep -q "MARQUEUR-ECHO" <<<"$resp" && ko "l'erreur renvoie la valeur reçue" || ok "l'erreur ne renvoie pas la valeur reçue"

echo "== Injection SQL =="
INJ="x'); DROP TABLE alerte; --"
check "$(code vision '{"type":"presence","level":"info","source":"vision","zone":"'"${INJ:0:30}"'","message":"'"'"' OR 1=1; DELETE FROM mesure; --"}')" 201 "charge d'injection acceptée comme simple texte"
check "$(sql "SELECT zone FROM alerte ORDER BY id_alerte DESC LIMIT 1")" "${INJ:0:30}" "texte stocké tel quel, table alerte intacte"

echo "== Captures JPEG =="
JPEG_B64=$(printf '\xff\xd8\xff\xe0\x00\x10JFIF\x00test-sentinel\xff\xd9' | base64 -w0)
check "$(code vision '{"type":"intrusion","level":"critical","source":"fusion","pir_confirmed":true,"snapshot_jpeg_b64":"'"$JPEG_B64"'"}')" 201 "capture JPEG acceptée"
cap=$(sql "SELECT chemin_capture FROM alerte ORDER BY id_alerte DESC LIMIT 1")
[[ $cap =~ ^captures/[0-9a-f]{32}\.jpg$ ]] && ok "chemin_capture généré par le serveur ($cap)" || ko "chemin_capture inattendu : $cap"
"${DC[@]}" exec -T ingest test -s "/data/$cap" </dev/null && ok "fichier présent dans le volume captures" || ko "fichier absent"
check "$(code vision '{"type":"intrusion","level":"critical","source":"fusion","snapshot_jpeg_b64":"'"$(printf 'GIF89a...' | base64 -w0)"'"}')" 422 "capture non JPEG : 422"
check "$(code vision '{"type":"intrusion","level":"critical","source":"fusion","snapshot_jpeg_b64":"@@@pas-du-base64@@@"}')" 422 "base64 invalide : 422"
check "$(code vision '{"type":"pir_blind_spot","level":"warning","source":"pir","snapshot_jpeg_b64":"'"$JPEG_B64"'"}')" 422 "capture refusée hors alerte caméra"

echo "== Limites =="
docker exec "$C" sh -c 'head -c 3000000 /dev/zero | tr "\0" a > /tmp/big'
check "$(docker exec "$C" curl -s --cacert /ca.crt -K /tmp/auth_vision -o /dev/null -w '%{http_code}' --data-binary @/tmp/big $URL/api/v1/alerts)" 413 "corps de 3 Mo : 413"
check "$(docker exec "$C" curl -s --cacert /ca.crt -K /tmp/auth_none -o /dev/null -w '%{http_code}' --data-binary @/tmp/big $URL/api/v1/alerts)" 401 "corps de 3 Mo sans jeton : 401 (jeton vérifié avant le corps)"
check "$(docker exec "$C" curl -s --cacert /ca.crt -K /tmp/auth_vision -o /dev/null -w '%{http_code}' -H 'Transfer-Encoding: chunked' --data-binary @/tmp/body $URL/api/v1/alerts)" 411 "corps sans Content-Length : 411"

echo "== Télémétrie MQTT -> mesure =="
docker run -d --name "$M" --network "$NET" --user 1883 --read-only --tmpfs /tmp --cap-drop ALL \
    -v "$PWD/certs/ca.crt:/ca.crt:ro" --entrypoint sleep eclipse-mosquitto:2.0 900 >/dev/null
printf -- '-h mosquitto\n-p 8883\n--cafile /ca.crt\n-u esp\n-P %s\n' "$(envval MQTT_ESP_PASSWORD)" \
    | docker exec -i "$M" sh -c 'mkdir -p /tmp/cfg && cat > /tmp/cfg/mosquitto_pub'
mqtt_raw() { docker exec "$M" env XDG_CONFIG_HOME=/tmp/cfg mosquitto_pub -q 1 -t sentinel/telemetry -m "$1"; }
mqtt() { mqtt_raw "$1"; sleep 1; }
nb_mesures() { sql "SELECT count(*) FROM mesure"; }

mqtt '{"serie":"TEST-ESP-01","temperature_c":21.5,"humidite_pct":48.2,"gaz_brut":312,"pir":true}'
check "$(sql "SELECT temperature_c||'/'||humidite_pct||'/'||gaz_brut||'/'||mouvement_detecte||'/'||id_dispositif FROM mesure ORDER BY id_mesure DESC LIMIT 1")" \
    "21.50/48.20/312/true/$DEV" "mesure complète enregistrée"
mqtt '{"serie":"TEST-ESP-01","temperature_c":null,"humidite_pct":null,"gaz_brut":290,"pir":false}'
check "$(sql "SELECT coalesce(temperature_c::text,'null')||'/'||gaz_brut FROM mesure ORDER BY id_mesure DESC LIMIT 1")" "null/290" "DHT22 en défaut (null) accepté"
before=$(nb_mesures)
mqtt_raw 'pas du json'
mqtt_raw '{"serie":"TEST-ESP-01","temperature_c":500}'
mqtt_raw '{"serie":"TEST-ESP-01","gaz_brut":-3}'
mqtt_raw '{"serie":"ESP-FANTOME","temperature_c":20}'
mqtt_raw "{\"serie\":\"x'; DROP TABLE mesure; --\",\"temperature_c\":20}"
mqtt '{"temperature_c":20}'
check "$(nb_mesures)" "$before" "6 messages invalides / inconnus / hostiles ignorés"
mqtt '{"serie":"TEST-ESP-01","temperature_c":22,"pir":false}'
check "$(nb_mesures)" "$((before+1))" "le service continue après les messages invalides"
check "$(sql "SELECT count(*) > 0 AND bool_and(usename = 'sentinel_ingest') FROM pg_stat_activity WHERE application_name = 'sentinel-ingest'")" t \
    "toutes les connexions BDD de l'API utilisent le rôle sentinel_ingest"

echo "== DISPOSITIF_HORS_LIGNE (seuil de test : 10 s) =="
check "$(sql "SELECT count(*) FROM alerte WHERE type_alerte='DISPOSITIF_HORS_LIGNE'")" 0 "aucune alerte tant que les mesures arrivent"
sleep 16
check "$(sql "SELECT count(*) FROM alerte WHERE origine='SYSTEME' AND type_alerte='DISPOSITIF_HORS_LIGNE' AND id_dispositif=$DEV")" 1 "alerte levée après 10 s sans mesure"
sleep 6
check "$(sql "SELECT count(*) FROM alerte WHERE type_alerte='DISPOSITIF_HORS_LIGNE'")" 1 "pas de doublon tant que le dispositif reste hors ligne"
mqtt '{"serie":"TEST-ESP-01","temperature_c":22}'
sleep 3
"${DC[@]}" logs ingest 2>&1 | grep -q "de nouveau en ligne" && ok "retour en ligne détecté" || ko "retour en ligne non détecté"
sleep 16
check "$(sql "SELECT count(*) FROM alerte WHERE type_alerte='DISPOSITIF_HORS_LIGNE'")" 2 "nouvelle alerte à la coupure suivante"

echo
echo "Résultat : $PASS OK, $FAIL échec(s)"
[ "$FAIL" -eq 0 ]
