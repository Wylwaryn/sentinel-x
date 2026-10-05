#!/usr/bin/env bash
# Sentinel-X : séparation des privilèges entre les deux API.
# Exécuté automatiquement par l'image postgres au premier démarrage (docker-entrypoint-initdb.d),
# après 01-schema.sql. Les mots de passe viennent du .env (jamais dans Git).
#
#   sentinel_ingest     API d'ingestion (MQTT / vision / IA réseau -> BDD) : écrit mesures et alertes,
#                       n'a AUCUN accès aux utilisateurs.
#   sentinel_dashboard  API dashboard (BDD -> navigateur) : lecture de la supervision physique,
#                       acquittement / résolution d'alertes uniquement, ne voit pas les alertes réseau.
set -euo pipefail

: "${INGEST_DB_PASSWORD:?INGEST_DB_PASSWORD manquant dans le .env}"
: "${DASHBOARD_DB_PASSWORD:?DASHBOARD_DB_PASSWORD manquant dans le .env}"

# Les mots de passe passent par des variables psql (:'...') : échappement géré par psql,
# aucune injection possible via le contenu du .env.
psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v ingest_pw="$INGEST_DB_PASSWORD" \
     -v dashboard_pw="$DASHBOARD_DB_PASSWORD" <<'EOSQL'
BEGIN;

CREATE ROLE sentinel_ingest LOGIN PASSWORD :'ingest_pw'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION CONNECTION LIMIT 10;
CREATE ROLE sentinel_dashboard LOGIN PASSWORD :'dashboard_pw'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION CONNECTION LIMIT 20;

-- Personne ne se connecte ni ne crée d'objets par défaut
REVOKE ALL ON DATABASE :"DBNAME" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"DBNAME" TO sentinel_ingest, sentinel_dashboard;
GRANT USAGE ON SCHEMA public TO sentinel_ingest, sentinel_dashboard;

-- ---------------- API d'ingestion ----------------
GRANT SELECT ON site, dispositif TO sentinel_ingest;          -- retrouver l'ESP par numero_serie
GRANT SELECT, INSERT ON mesure TO sentinel_ingest;            -- télémétrie + historique pour le ML
GRANT INSERT ON alerte TO sentinel_ingest;                    -- toutes origines, y compris RESEAU_IA
GRANT SELECT (id_alerte) ON alerte TO sentinel_ingest;        -- INSERT ... RETURNING id_alerte
GRANT SELECT ON image_reference TO sentinel_ingest;           -- reconnaissance des personnes autorisées

-- ---------------- API dashboard ----------------
GRANT SELECT ON role, utilisateur, site, dispositif, mesure TO sentinel_dashboard;
GRANT SELECT ON v_alerte_supervision, v_dispositif_etat TO sentinel_dashboard;
-- Acquitter / résoudre une alerte : seules ces colonnes sont modifiables (pas le niveau, le type, etc.)
GRANT UPDATE (statut, id_resolu_par, date_resolution, heure_resolution) ON v_alerte_supervision TO sentinel_dashboard;
GRANT SELECT, INSERT ON image_reference TO sentinel_dashboard;
GRANT UPDATE (active) ON image_reference TO sentinel_dashboard;

COMMIT;
EOSQL

echo "[02-roles] Rôles sentinel_ingest et sentinel_dashboard créés."
