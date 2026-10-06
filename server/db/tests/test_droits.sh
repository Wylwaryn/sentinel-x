#!/usr/bin/env bash
# Sentinel-X : preuve du cloisonnement des droits PostgreSQL entre les deux API.
# À lancer DANS le conteneur postgres :
#   docker compose exec -T postgres bash < server/db/tests/test_droits.sh
# Insère des données de test : à exécuter sur une base de test, pas en production.
set -uo pipefail

DB="${POSTGRES_DB:-sentinel}"
PASS=0
FAIL=0

# Connexion en TCP avec le mot de passe de chaque rôle (lus dans l'environnement du conteneur) :
# on teste l'authentification scram réelle, comme le font les API.
pw_for() {
    case "$1" in
        sentinel_ingest) echo "${INGEST_DB_PASSWORD:-}" ;;
        sentinel_dashboard) echo "${DASHBOARD_DB_PASSWORD:-}" ;;
        *) echo "${POSTGRES_PASSWORD:-}" ;;
    esac
}

run() { PGPASSWORD="$(pw_for "$1")" psql -X -q -v ON_ERROR_STOP=1 -h 127.0.0.1 -U "$1" -d "$DB" -tA -c "$2" 2>&1; }

expect_ok() {
    local who="$1" label="$2" sql="$3" out
    if out=$(run "$who" "$sql"); then
        echo "  OK      [$who] $label"; PASS=$((PASS + 1))
    else
        echo "  ECHEC   [$who] $label -> devait réussir : $out"; FAIL=$((FAIL + 1))
    fi
}

expect_denied() {
    local who="$1" label="$2" sql="$3" pattern="$4" out
    if out=$(run "$who" "$sql"); then
        echo "  ECHEC   [$who] $label -> devait être refusé"; FAIL=$((FAIL + 1))
    elif grep -qiE "$pattern" <<<"$out"; then
        echo "  REFUSÉ  [$who] $label"; PASS=$((PASS + 1))
    else
        echo "  ECHEC   [$who] $label -> refusé pour une autre raison : $out"; FAIL=$((FAIL + 1))
    fi
}

expect_value() {
    local who="$1" label="$2" sql="$3" expected="$4" out
    out=$(run "$who" "$sql")
    if [[ "$out" == "$expected" ]]; then
        echo "  OK      [$who] $label (= $expected)"; PASS=$((PASS + 1))
    else
        echo "  ECHEC   [$who] $label -> attendu '$expected', obtenu '$out'"; FAIL=$((FAIL + 1))
    fi
}

DENIED="permission denied|droit refusé"
CHECK="violates check constraint|viole la contrainte de vérification"

echo "== Données de test (administrateur) =="
run "$POSTGRES_USER" "
    INSERT INTO site (nom, localisation) VALUES ('Centrale test', 'Labo');
    INSERT INTO dispositif (id_site, nom, numero_serie) VALUES (1, 'Sentinel-X 001', 'SX-TEST-001');
    INSERT INTO utilisateur (id_role, nom, email, mot_de_passe_hash)
        VALUES (1, 'Admin test', 'admin@test.local', '\$argon2id\$hash-de-test');" >/dev/null

expect_value "$POSTGRES_USER" "rôle SERVICE_VISION présent (03-comptes-dashboard.sql)" \
    "SELECT count(*) FROM role WHERE code = 'SERVICE_VISION'" "1"

echo "== Authentification =="
for who in sentinel_ingest sentinel_dashboard "$POSTGRES_USER"; do
    if out=$(PGPASSWORD="mauvais-mot-de-passe" psql -X -h 127.0.0.1 -U "$who" -d "$DB" -tAc "SELECT 1" 2>&1); then
        echo "  ECHEC   [$who] mauvais mot de passe accepté (TCP)"; FAIL=$((FAIL + 1))
    else
        echo "  REFUSÉ  [$who] mauvais mot de passe (TCP)"; PASS=$((PASS + 1))
    fi
    if out=$(PGPASSWORD="" psql -X -U "$who" -d "$DB" -w -tAc "SELECT 1" 2>&1); then
        echo "  ECHEC   [$who] connexion locale sans mot de passe acceptée"; FAIL=$((FAIL + 1))
    else
        echo "  REFUSÉ  [$who] connexion locale sans mot de passe"; PASS=$((PASS + 1))
    fi
done

echo "== Fuseau horaire =="
expect_value sentinel_ingest    "session en UTC" "SHOW timezone" "UTC"
expect_value sentinel_dashboard "session en UTC" "SHOW timezone" "UTC"

echo "== API d'ingestion : ce qu'elle doit pouvoir faire =="
expect_ok sentinel_ingest "retrouver l'ESP par numéro de série" \
    "SELECT id_dispositif FROM dispositif WHERE numero_serie = 'SX-TEST-001'"
expect_ok sentinel_ingest "insérer une mesure" \
    "INSERT INTO mesure (id_dispositif, temperature_c, humidite_pct, gaz_brut, mouvement_detecte) VALUES (1, 22.5, 41.0, 180, false)"
expect_ok sentinel_ingest "lire l'historique (ML prédictif)" "SELECT count(*) FROM mesure"
expect_ok sentinel_ingest "insérer une alerte vision + RETURNING" \
    "INSERT INTO alerte (id_dispositif, type_alerte, origine, niveau, zone, pir_confirme, score_ia) VALUES (1, 'INTRUSION', 'FUSION', 'CRITIQUE', 'zone_interdite', true, 0.91) RETURNING id_alerte"
expect_ok sentinel_ingest "insérer une alerte réseau sans dispositif" \
    "INSERT INTO alerte (type_alerte, origine, niveau, ip_source, score_ia, message) VALUES ('SCAN_PORTS', 'RESEAU_IA', 'AVERTISSEMENT', '192.168.137.42', 0.97, 'Scan SYN 1000 ports')"
expect_ok sentinel_ingest "insérer une alerte réseau liée à un ESP (attaque sniper)" \
    "INSERT INTO alerte (id_dispositif, type_alerte, origine, niveau, ip_source) VALUES (1, 'DENI_DE_SERVICE', 'RESEAU_IA', 'CRITIQUE', '192.168.137.10')"

echo "== API d'ingestion : ce qui doit lui être refusé =="
expect_denied sentinel_ingest "lire les utilisateurs / hash" "SELECT email, mot_de_passe_hash FROM utilisateur" "$DENIED"
expect_denied sentinel_ingest "relire toutes les alertes" "SELECT * FROM alerte" "$DENIED"
expect_denied sentinel_ingest "modifier une alerte" "UPDATE alerte SET niveau = 'INFORMATION'" "$DENIED"
expect_denied sentinel_ingest "supprimer des mesures" "DELETE FROM mesure" "$DENIED"
expect_denied sentinel_ingest "créer une table" "CREATE TABLE pirate (x int)" "$DENIED"

echo "== Contraintes métier (même avec les droits) =="
expect_denied sentinel_ingest "température hors plage DHT22" \
    "INSERT INTO mesure (id_dispositif, temperature_c) VALUES (1, 150)" "$CHECK"
expect_denied sentinel_ingest "valeur gaz hors plage ADC" \
    "INSERT INTO mesure (id_dispositif, gaz_brut) VALUES (1, 5000)" "$CHECK"
expect_denied sentinel_ingest "alerte vision sans dispositif" \
    "INSERT INTO alerte (type_alerte, origine, niveau) VALUES ('INTRUSION', 'VISION_IA', 'CRITIQUE')" "$CHECK"
expect_denied sentinel_ingest "type incohérent avec l'origine" \
    "INSERT INTO alerte (id_dispositif, type_alerte, origine, niveau) VALUES (1, 'SCAN_PORTS', 'PIR', 'CRITIQUE')" "$CHECK"
expect_denied sentinel_ingest "score IA hors [0,1]" \
    "INSERT INTO alerte (id_dispositif, type_alerte, origine, niveau, score_ia) VALUES (1, 'PRESENCE', 'VISION_IA', 'INFORMATION', 3)" "$CHECK"

echo "== API dashboard : ce qu'elle doit pouvoir faire =="
expect_ok sentinel_dashboard "état des dispositifs" "SELECT * FROM v_dispositif_etat"
expect_ok sentinel_dashboard "courbes (mesures)" "SELECT * FROM mesure ORDER BY date_mesure, heure_mesure"
expect_ok sentinel_dashboard "connexion (lecture utilisateur)" \
    "SELECT id_utilisateur, mot_de_passe_hash FROM utilisateur WHERE email = 'admin@test.local'"
expect_value sentinel_dashboard "les alertes réseau sont invisibles" \
    "SELECT count(*) FROM v_alerte_supervision WHERE origine = 'RESEAU_IA'" "0"
expect_value sentinel_dashboard "les alertes physiques sont visibles" \
    "SELECT count(*) FROM v_alerte_supervision" "1"
expect_ok sentinel_dashboard "acquitter une alerte" \
    "UPDATE v_alerte_supervision SET statut = 'ACQUITTEE' WHERE id_alerte = 1"
expect_ok sentinel_dashboard "résoudre une alerte" \
    "UPDATE v_alerte_supervision SET statut = 'RESOLUE', id_resolu_par = 1, date_resolution = CURRENT_DATE, heure_resolution = LOCALTIME(6) WHERE id_alerte = 1"
expect_value sentinel_dashboard "une alerte réseau ne peut pas être touchée via la vue" \
    "WITH u AS (UPDATE v_alerte_supervision SET statut = 'ACQUITTEE' WHERE id_alerte = 2 RETURNING 1) SELECT count(*) FROM u" "0"

expect_ok sentinel_dashboard "créer un compte LECTEUR (page ADMIN Utilisateurs)" \
    "INSERT INTO utilisateur (id_role, nom, email, mot_de_passe_hash) SELECT id_role, 'Compte créé', 'cree@test.local', '\$argon2id\$hash-de-test' FROM role WHERE code = 'LECTEUR' RETURNING id_utilisateur"
expect_ok sentinel_dashboard "désactiver un compte" "UPDATE utilisateur SET actif = false WHERE email = 'cree@test.local'"
expect_ok sentinel_dashboard "réactiver un compte" "UPDATE utilisateur SET actif = true WHERE email = 'cree@test.local'"

echo "== API dashboard : ce qui doit lui être refusé =="
expect_denied sentinel_dashboard "lire la table alerte brute (alertes réseau)" "SELECT * FROM alerte" "$DENIED"
expect_denied sentinel_dashboard "changer le niveau d'une alerte" \
    "UPDATE v_alerte_supervision SET niveau = 'INFORMATION' WHERE id_alerte = 1" "$DENIED"
expect_denied sentinel_dashboard "fabriquer une fausse mesure" \
    "INSERT INTO mesure (id_dispositif, temperature_c) VALUES (1, 20)" "$DENIED"
expect_denied sentinel_dashboard "fabriquer une fausse alerte" \
    "INSERT INTO alerte (id_dispositif, type_alerte, origine, niveau) VALUES (1, 'PRESENCE', 'VISION_IA', 'INFORMATION')" "$DENIED"
expect_denied sentinel_dashboard "s'élever en ADMIN" "UPDATE utilisateur SET id_role = 1" "$DENIED"
expect_denied sentinel_dashboard "changer le mot de passe d'un compte" \
    "UPDATE utilisateur SET mot_de_passe_hash = 'x' WHERE email = 'admin@test.local'" "$DENIED"
expect_denied sentinel_dashboard "supprimer un compte" "DELETE FROM utilisateur WHERE email = 'cree@test.local'" "$DENIED"
expect_denied sentinel_dashboard "créer un compte en forçant d'autres colonnes (actif, date)" \
    "INSERT INTO utilisateur (id_role, nom, email, mot_de_passe_hash, actif) VALUES (3, 'X', 'x@test.local', 'h', false)" "$DENIED"
expect_denied sentinel_dashboard "créer un compte SERVICE_VISION" \
    "INSERT INTO utilisateur (id_role, nom, email, mot_de_passe_hash) SELECT id_role, 'Faux service', 'svc@test.local', 'h' FROM role WHERE code = 'SERVICE_VISION'" "réservée au terminal|$DENIED"
expect_denied sentinel_ingest "API d'ingestion : créer un compte" \
    "INSERT INTO utilisateur (id_role, nom, email, mot_de_passe_hash) VALUES (1, 'X', 'y@test.local', 'h')" "$DENIED"
expect_denied sentinel_dashboard "supprimer l'historique" "DELETE FROM mesure" "$DENIED"
expect_denied sentinel_dashboard "créer une table" "CREATE TABLE pirate (x int)" "$DENIED"

echo
echo "Résultat : $PASS réussis, $FAIL en échec"
[[ $FAIL -eq 0 ]]
