-- Sentinel-X : comptes créés depuis le dashboard (ADMIN) et compte de service SERVICE_VISION
-- (synchronisation automatique des visages vers la vision). Décision de l'utilisateur, 6 oct.
--
-- Idempotent. Exécuté automatiquement sur une base neuve (après 02-roles.sh) ; sur la base en service,
-- depuis server/ :
--   sudo docker compose exec -T postgres sh -c \
--     'PGPASSWORD=$POSTGRES_PASSWORD psql -X -v ON_ERROR_STOP=1 -U $POSTGRES_USER -d $POSTGRES_DB' \
--     < db/init/03-comptes-dashboard.sql
BEGIN;

-- Rôle applicatif de service : n'accède qu'aux images de référence (contrôlé par l'API dashboard).
INSERT INTO role (code, libelle) VALUES ('SERVICE_VISION', 'Service de synchronisation des visages')
ON CONFLICT (code) DO NOTHING;

-- Créer un compte : uniquement ces 4 colonnes (actif et dates gardent leurs valeurs par défaut).
GRANT INSERT (id_role, nom, email, mot_de_passe_hash) ON utilisateur TO sentinel_dashboard;
-- Désactiver / réactiver un compte. PAS d'UPDATE sur id_role ni sur mot_de_passe_hash :
-- un dashboard compromis ne peut ni promouvoir un compte existant ni en changer le mot de passe.
GRANT UPDATE (actif) ON utilisateur TO sentinel_dashboard;

-- Défense en profondeur : un compte SERVICE_VISION ne se crée que depuis le terminal de la VM
-- (add-user.sh, compte administrateur PostgreSQL), jamais par l'API dashboard.
CREATE OR REPLACE FUNCTION interdire_compte_service_par_dashboard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF current_user = 'sentinel_dashboard'
       AND NEW.id_role = (SELECT id_role FROM role WHERE code = 'SERVICE_VISION') THEN
        RAISE EXCEPTION 'compte SERVICE_VISION : création réservée au terminal de la VM'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_utilisateur_compte_service ON utilisateur;
CREATE TRIGGER trg_utilisateur_compte_service BEFORE INSERT ON utilisateur
    FOR EACH ROW EXECUTE FUNCTION interdire_compte_service_par_dashboard();

COMMIT;
