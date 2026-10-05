-- Sentinel-X : déploiement PostgreSQL 17, modèle V3 (7 tables + 2 vues de supervision).
-- Base sentinel créée par POSTGRES_DB dans Docker ; ce script crée son schéma.
-- À exécuter UNE FOIS dans une base vide. Aucune suppression de données.
-- Toutes les dates et heures représentent des instants UTC (forcé au niveau de la base).
--
-- Changements V2 -> V3 :
--   * UTC forcé pour toutes les sessions (ALTER DATABASE), plus seulement pour ce script.
--   * mesure : bornes physiques DHT22 (-40..80 °C) et ADC ESP8266 (0..1023), horodatage par défaut.
--   * alerte : id_dispositif facultatif pour les alertes RESEAU_IA uniquement,
--     colonnes de détail (ip_source, score_ia, zone, pir_confirme, chemin_capture),
--     catalogue origine / type_alerte validé par l'équipe, cohérence type <-> origine.
--   * Notifications temps réel (LISTEN/NOTIFY) pour le dashboard.
--   * Vues de supervision (sans les alertes réseau) pour l'API dashboard.
BEGIN;
SET TIME ZONE 'UTC';
SET LOCAL search_path TO public;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

DO $$
BEGIN
    EXECUTE format('ALTER DATABASE %I SET timezone TO %L', current_database(), 'UTC');
END $$;

CREATE TABLE role (
    id_role INTEGER GENERATED ALWAYS AS IDENTITY,
    code VARCHAR(30) NOT NULL,
    libelle VARCHAR(100) NOT NULL,
    CONSTRAINT pk_role PRIMARY KEY (id_role),
    CONSTRAINT uq_role_code UNIQUE (code)
);

CREATE TABLE utilisateur (
    id_utilisateur INTEGER GENERATED ALWAYS AS IDENTITY,
    id_role INTEGER NOT NULL,
    nom VARCHAR(100) NOT NULL,
    email VARCHAR(255) NOT NULL,
    mot_de_passe_hash VARCHAR(255) NOT NULL,
    actif BOOLEAN NOT NULL DEFAULT TRUE,
    date_creation DATE NOT NULL DEFAULT CURRENT_DATE,
    heure_creation TIME(6) NOT NULL DEFAULT LOCALTIME(6),
    CONSTRAINT pk_utilisateur PRIMARY KEY (id_utilisateur),
    CONSTRAINT fk_utilisateur_id_role FOREIGN KEY (id_role) REFERENCES role (id_role),
    CONSTRAINT uq_utilisateur_email UNIQUE (email),
    CONSTRAINT ck_email_non_vide CHECK (length(trim(email)) > 0),
    CONSTRAINT ck_hash_non_vide CHECK (length(trim(mot_de_passe_hash)) > 0)
);

CREATE TABLE site (
    id_site INTEGER GENERATED ALWAYS AS IDENTITY,
    nom VARCHAR(100) NOT NULL,
    localisation VARCHAR(255),
    description VARCHAR(500),
    CONSTRAINT pk_site PRIMARY KEY (id_site)
);

CREATE TABLE dispositif (
    id_dispositif INTEGER GENERATED ALWAYS AS IDENTITY,
    id_site INTEGER NOT NULL,
    id_createur INTEGER,
    nom VARCHAR(100) NOT NULL,
    numero_serie VARCHAR(100) NOT NULL,
    date_installation DATE,
    heure_installation TIME(6),
    CONSTRAINT pk_dispositif PRIMARY KEY (id_dispositif),
    CONSTRAINT fk_dispositif_id_site FOREIGN KEY (id_site) REFERENCES site (id_site),
    CONSTRAINT fk_dispositif_id_createur FOREIGN KEY (id_createur) REFERENCES utilisateur (id_utilisateur),
    CONSTRAINT uq_dispositif_numero_serie UNIQUE (numero_serie),
    CONSTRAINT ck_installation CHECK ((date_installation IS NULL AND heure_installation IS NULL) OR (date_installation IS NOT NULL AND heure_installation IS NOT NULL))
);

-- Horodatage posé par l'API d'ingestion à la réception (l'ESP8266 n'a pas d'horloge fiable).
CREATE TABLE mesure (
    id_mesure INTEGER GENERATED ALWAYS AS IDENTITY,
    id_dispositif INTEGER NOT NULL,
    date_mesure DATE NOT NULL DEFAULT CURRENT_DATE,
    heure_mesure TIME(6) NOT NULL DEFAULT LOCALTIME(6),
    temperature_c DECIMAL(6,2),
    humidite_pct DECIMAL(5,2),
    gaz_brut INTEGER,
    mouvement_detecte BOOLEAN,
    CONSTRAINT pk_mesure PRIMARY KEY (id_mesure),
    CONSTRAINT fk_mesure_id_dispositif FOREIGN KEY (id_dispositif) REFERENCES dispositif (id_dispositif),
    CONSTRAINT uq_mesure_dispositif UNIQUE (id_mesure, id_dispositif),
    CONSTRAINT ck_temperature CHECK (temperature_c BETWEEN -40 AND 80),
    CONSTRAINT ck_humidite CHECK (humidite_pct BETWEEN 0 AND 100),
    CONSTRAINT ck_gaz_brut CHECK (gaz_brut BETWEEN 0 AND 1023)
);

CREATE TABLE image_reference (
    id_image_reference INTEGER GENERATED ALWAYS AS IDENTITY,
    id_utilisateur INTEGER NOT NULL,
    id_ajoute_par INTEGER NOT NULL,
    chemin_fichier VARCHAR(500) NOT NULL,
    date_ajout DATE NOT NULL DEFAULT CURRENT_DATE,
    heure_ajout TIME(6) NOT NULL DEFAULT LOCALTIME(6),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT pk_image_reference PRIMARY KEY (id_image_reference),
    CONSTRAINT fk_image_reference_id_utilisateur FOREIGN KEY (id_utilisateur) REFERENCES utilisateur (id_utilisateur),
    CONSTRAINT fk_image_reference_id_ajoute_par FOREIGN KEY (id_ajoute_par) REFERENCES utilisateur (id_utilisateur),
    CONSTRAINT uq_image_reference_chemin_fichier UNIQUE (chemin_fichier)
);

CREATE TABLE alerte (
    id_alerte INTEGER GENERATED ALWAYS AS IDENTITY,
    id_dispositif INTEGER,
    id_mesure INTEGER,
    id_personne_reconnue INTEGER,
    id_resolu_par INTEGER,
    date_alerte DATE NOT NULL DEFAULT CURRENT_DATE,
    heure_alerte TIME(6) NOT NULL DEFAULT LOCALTIME(6),
    type_alerte VARCHAR(30) NOT NULL,
    origine VARCHAR(30) NOT NULL,
    niveau VARCHAR(20) NOT NULL,
    statut VARCHAR(20) NOT NULL DEFAULT 'NOUVELLE',
    message VARCHAR(500),
    ip_source INET,
    score_ia REAL,
    zone VARCHAR(50),
    pir_confirme BOOLEAN,
    chemin_capture VARCHAR(500),
    date_resolution DATE,
    heure_resolution TIME(6),
    CONSTRAINT pk_alerte PRIMARY KEY (id_alerte),
    CONSTRAINT fk_alerte_id_dispositif FOREIGN KEY (id_dispositif) REFERENCES dispositif (id_dispositif),
    CONSTRAINT fk_alerte_id_mesure FOREIGN KEY (id_mesure, id_dispositif) REFERENCES mesure (id_mesure, id_dispositif),
    CONSTRAINT fk_alerte_id_personne_reconnue FOREIGN KEY (id_personne_reconnue) REFERENCES utilisateur (id_utilisateur),
    CONSTRAINT fk_alerte_id_resolu_par FOREIGN KEY (id_resolu_par) REFERENCES utilisateur (id_utilisateur),
    CONSTRAINT ck_origine CHECK (origine IN ('VISION_IA', 'FUSION', 'PIR', 'CAPTEURS_IA', 'SYSTEME', 'RESEAU_IA')),
    CONSTRAINT ck_type_selon_origine CHECK (
        (origine IN ('VISION_IA', 'FUSION') AND type_alerte IN ('PRESENCE', 'RODEUR', 'INTRUSION', 'APPROCHE_RAPIDE'))
        OR (origine = 'PIR' AND type_alerte = 'ANGLE_MORT')
        OR (origine = 'CAPTEURS_IA' AND type_alerte = 'ANOMALIE_ENVIRONNEMENTALE')
        OR (origine = 'SYSTEME' AND type_alerte = 'DISPOSITIF_HORS_LIGNE')
        OR (origine = 'RESEAU_IA' AND type_alerte IN ('SCAN_PORTS', 'DENI_DE_SERVICE', 'FORCE_BRUTE', 'TRAFIC_ANORMAL'))
    ),
    -- Alerte réseau : dispositif facultatif (attaque "sniper" depuis un ESP), jamais de mesure.
    -- Toute autre alerte : rattachée à un dispositif physique.
    CONSTRAINT ck_dispositif_selon_origine CHECK (
        (origine = 'RESEAU_IA' AND id_mesure IS NULL)
        OR (origine <> 'RESEAU_IA' AND id_dispositif IS NOT NULL)
    ),
    CONSTRAINT ck_niveau CHECK (niveau IN ('INFORMATION', 'AVERTISSEMENT', 'CRITIQUE')),
    CONSTRAINT ck_statut CHECK (statut IN ('NOUVELLE', 'ACQUITTEE', 'RESOLUE')),
    CONSTRAINT ck_score_ia CHECK (score_ia BETWEEN 0 AND 1),
    CONSTRAINT ck_resolution CHECK (
        (statut <> 'RESOLUE' AND date_resolution IS NULL AND heure_resolution IS NULL AND id_resolu_par IS NULL)
        OR (statut = 'RESOLUE' AND date_resolution IS NOT NULL AND heure_resolution IS NOT NULL AND id_resolu_par IS NOT NULL)
    ),
    CONSTRAINT ck_chronologie_resolution CHECK ((date_resolution + heure_resolution) >= (date_alerte + heure_alerte))
);


-- Pas de compte utilisateur applicatif ni de mot de passe prédéfini.
INSERT INTO role (code, libelle) VALUES
    ('ADMIN', 'Administrateur'),
    ('OPERATEUR', 'Opérateur'),
    ('LECTEUR', 'Lecteur');

CREATE UNIQUE INDEX uq_utilisateur_email_normalise ON utilisateur (lower(trim(email)));
CREATE INDEX idx_utilisateur_role ON utilisateur (id_role);
CREATE INDEX idx_dispositif_site ON dispositif (id_site);
CREATE INDEX idx_dispositif_createur ON dispositif (id_createur);
CREATE INDEX idx_mesure_dispositif_instant ON mesure (id_dispositif, date_mesure DESC, heure_mesure DESC);
CREATE INDEX idx_alerte_dispositif_instant ON alerte (id_dispositif, date_alerte DESC, heure_alerte DESC);
CREATE INDEX idx_alerte_statut ON alerte (statut);
CREATE INDEX idx_alerte_origine ON alerte (origine);
CREATE INDEX idx_alerte_ip_source ON alerte (ip_source) WHERE ip_source IS NOT NULL;
CREATE INDEX idx_alerte_mesure ON alerte (id_mesure, id_dispositif);
CREATE INDEX idx_alerte_personne ON alerte (id_personne_reconnue);
CREATE INDEX idx_alerte_resolveur ON alerte (id_resolu_par);
CREATE INDEX idx_image_utilisateur ON image_reference (id_utilisateur);
CREATE INDEX idx_image_ajoute_par ON image_reference (id_ajoute_par);


-- ---------------------------------------------------------------------------
-- Vues de supervision physique (lues par l'API dashboard).
-- Les alertes RESEAU_IA n'y figurent pas : elles restent dans la table et les journaux.
-- ---------------------------------------------------------------------------
CREATE VIEW v_alerte_supervision WITH (security_barrier) AS
    SELECT id_alerte, id_dispositif, id_mesure, id_personne_reconnue, id_resolu_par,
           date_alerte, heure_alerte, type_alerte, origine, niveau, statut, message,
           score_ia, zone, pir_confirme, chemin_capture, date_resolution, heure_resolution
    FROM alerte
    WHERE origine <> 'RESEAU_IA'
    WITH CASCADED CHECK OPTION;

-- Dernier état connu de chaque dispositif ; en_ligne = mesure reçue depuis moins de 30 s.
CREATE VIEW v_dispositif_etat AS
    SELECT d.id_dispositif, d.nom, d.numero_serie, d.id_site, s.nom AS site_nom,
           m.date_mesure, m.heure_mesure, m.temperature_c, m.humidite_pct, m.gaz_brut, m.mouvement_detecte,
           COALESCE((m.date_mesure + m.heure_mesure) > (now() AT TIME ZONE 'UTC') - INTERVAL '30 seconds', FALSE) AS en_ligne
    FROM dispositif d
    JOIN site s ON s.id_site = d.id_site
    LEFT JOIN LATERAL (
        SELECT date_mesure, heure_mesure, temperature_c, humidite_pct, gaz_brut, mouvement_detecte
        FROM mesure
        WHERE mesure.id_dispositif = d.id_dispositif
        ORDER BY date_mesure DESC, heure_mesure DESC
        LIMIT 1
    ) m ON TRUE;


-- ---------------------------------------------------------------------------
-- Temps réel : LISTEN sentinel_mesure / sentinel_alerte côté API dashboard.
-- Les alertes RESEAU_IA ne sont pas notifiées sur ce canal.
-- ---------------------------------------------------------------------------
CREATE FUNCTION notifier_mesure() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('sentinel_mesure', row_to_json(NEW)::text);
    RETURN NEW;
END $$;

CREATE FUNCTION notifier_alerte() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.origine <> 'RESEAU_IA' THEN
        PERFORM pg_notify('sentinel_alerte', json_build_object(
            'operation', TG_OP,
            'id_alerte', NEW.id_alerte,
            'id_dispositif', NEW.id_dispositif,
            'type_alerte', NEW.type_alerte,
            'origine', NEW.origine,
            'niveau', NEW.niveau,
            'statut', NEW.statut,
            'message', NEW.message,
            'zone', NEW.zone,
            'pir_confirme', NEW.pir_confirme,
            'date_alerte', NEW.date_alerte,
            'heure_alerte', NEW.heure_alerte
        )::text);
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER trg_mesure_notify AFTER INSERT ON mesure
    FOR EACH ROW EXECUTE FUNCTION notifier_mesure();
CREATE TRIGGER trg_alerte_notify AFTER INSERT OR UPDATE ON alerte
    FOR EACH ROW EXECUTE FUNCTION notifier_alerte();
COMMIT;
