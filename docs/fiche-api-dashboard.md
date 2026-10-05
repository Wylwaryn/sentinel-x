# Fiche API Dashboard — Sentinel-X

Tout ce qu'il faut pour développer l'**API dashboard** et l'**interface de supervision** sans attendre le reste de la stack.
Source de vérité : [`server/db/init/01-schema.sql`](../server/db/init/01-schema.sql) et [`02-roles.sh`](../server/db/init/02-roles.sh).

---

## 1. Où se place l'API dashboard

```
 Navigateurs ──HTTPS/WSS :443──► Caddy ──► API DASHBOARD ──► PostgreSQL (rôle sentinel_dashboard)
                                               │
                                               └──MQTTS──► Mosquitto  (compte "dashboard")
                                                            ├─ publie  sentinel/cmd/#    (buzzer, LEDs)
                                                            └─ lit     sentinel/video/#  (images webcam)

 API INGESTION (autre équipe) : écrit les mesures et les alertes. Elle n'est PAS joignable depuis le Wi-Fi.
 Les deux API ne se parlent pas : elles partagent seulement la base.
```

- **Seule porte d'entrée réseau : 443 via Caddy** (TLS terminé par Caddy). L'API elle-même n'expose aucun port publié.
- **Réseau Docker `net_dashboard`** : Caddy, API dashboard, PostgreSQL, Mosquitto.

## 2. Connexion à la base

| Paramètre | Valeur |
|---|---|
| Hôte | `postgres` (nom du service Docker) |
| Port | `5432` |
| Base | `sentinel` |
| Utilisateur | `sentinel_dashboard` |
| Mot de passe | variable d'environnement `DASHBOARD_DB_PASSWORD` (dans le `.env`, **jamais dans Git**) |

**Toutes les dates et heures sont en UTC.** La conversion vers `Europe/Paris` se fait dans le navigateur.
Les dates et les heures sont dans deux colonnes séparées (`date_xxx` + `heure_xxx`). Pour obtenir un instant, on les additionne : `date_mesure + heure_mesure`.

## 3. Ce que l'API dashboard peut faire

| Objet | Lecture | Écriture |
|---|---|---|
| `v_dispositif_etat` (vue) | ✅ | — |
| `mesure` | ✅ | ❌ |
| `v_alerte_supervision` (vue) | ✅ | ✅ **seulement** `statut`, `id_resolu_par`, `date_resolution`, `heure_resolution` |
| `site`, `dispositif` | ✅ | ❌ (ajout d'ESP par l'administrateur) |
| `utilisateur`, `role` | ✅ (connexion) | ❌ (comptes créés par l'administrateur) |
| `image_reference` | ✅ | ✅ ajout + `active` |
| table `alerte` brute | ❌ | ❌ |

Tout le reste est **refusé par PostgreSQL lui-même** : supprimer, créer des tables, insérer des mesures ou des alertes, changer le niveau d'une alerte, modifier un rôle utilisateur. La preuve est dans [`server/db/tests/test_droits.sh`](../server/db/tests/test_droits.sh).

Les **alertes réseau** (`RESEAU_IA`) ne sont **pas visibles** dans la vue : le dashboard ne supervise que le physique.

## 4. Les données

### `v_dispositif_etat` : une ligne par ESP, avec sa dernière mesure

| Colonne | Type | Remarque |
|---|---|---|
| `id_dispositif`, `nom`, `numero_serie` | | |
| `id_site`, `site_nom` | | |
| `date_mesure`, `heure_mesure` | DATE, TIME | NULL si aucune mesure reçue |
| `temperature_c` | DECIMAL | -40 à 80 |
| `humidite_pct` | DECIMAL | 0 à 100 |
| `gaz_brut` | INTEGER | 0 à 1023 (valeur brute de l'ADC, plus haut = plus de gaz) |
| `mouvement_detecte` | BOOLEAN | état du PIR |
| `en_ligne` | BOOLEAN | `true` si une mesure a été reçue il y a moins de 30 s |

### `mesure` : historique (pour les courbes)

`id_mesure`, `id_dispositif`, `date_mesure`, `heure_mesure`, `temperature_c`, `humidite_pct`, `gaz_brut`, `mouvement_detecte`.
Fréquence prévue : environ 1 mesure toutes les 2 s par ESP. Il faut toujours **filtrer par période** (voir les requêtes plus bas).

### `v_alerte_supervision` : alertes physiques

| Colonne | Type | Remarque |
|---|---|---|
| `id_alerte`, `id_dispositif`, `id_mesure` | | `id_mesure` renseigné pour les anomalies capteurs |
| `date_alerte`, `heure_alerte` | | |
| `type_alerte`, `origine`, `niveau`, `statut` | | voir catalogue ci-dessous |
| `message` | VARCHAR(500) | texte lisible |
| `score_ia` | REAL 0–1 | confiance / score d'anomalie de l'IA |
| `zone` | VARCHAR | vision : `perimetre`, `zone_interdite` |
| `pir_confirme` | BOOLEAN | le capteur PIR a confirmé ce que voit la caméra |
| `chemin_capture` | VARCHAR | image de l'intrus (alertes critiques vision) |
| `id_personne_reconnue` | | si la reconnaissance faciale est en place |
| `id_resolu_par`, `date_resolution`, `heure_resolution` | | remplis à la résolution |

## 5. Catalogue des valeurs

| `origine` | `type_alerte` possibles | Signification |
|---|---|---|
| `VISION_IA` | `PRESENCE`, `RODEUR`, `INTRUSION`, `APPROCHE_RAPIDE` | Détecté par la caméra seule |
| `FUSION` | `PRESENCE`, `RODEUR`, `INTRUSION`, `APPROCHE_RAPIDE` | Caméra **+ PIR** (alerte confirmée) |
| `PIR` | `ANGLE_MORT` | PIR déclenché alors que la caméra ne voit personne |
| `CAPTEURS_IA` | `ANOMALIE_ENVIRONNEMENTALE` | Le modèle prédictif détecte une dérive (température, gaz) |
| `SYSTEME` | `DISPOSITIF_HORS_LIGNE` | L'ESP n'envoie plus de mesures |
| `RESEAU_IA` | *(invisible pour le dashboard)* | |

- **`niveau`** : `INFORMATION`, `AVERTISSEMENT`, `CRITIQUE`. Couleurs suggérées : bleu, orange, rouge.
- **`statut`** : `NOUVELLE` → `ACQUITTEE` → `RESOLUE`.
- **Rôles utilisateurs** (`role.code`) : `ADMIN`, `OPERATEUR`, `LECTEUR`.

### Droits applicatifs suggérés (à faire respecter par l'API)

| Action | LECTEUR | OPERATEUR | ADMIN |
|---|:-:|:-:|:-:|
| Voir dispositifs, courbes, alertes, vidéo | ✅ | ✅ | ✅ |
| Acquitter / résoudre une alerte | ❌ | ✅ | ✅ |
| Déclencher buzzer / LEDs | ❌ | ✅ | ✅ |
| Gérer les images de référence | ❌ | ❌ | ✅ |

## 6. Requêtes prêtes à l'emploi

```sql
-- Tuiles d'état (une par ESP)
SELECT * FROM v_dispositif_etat ORDER BY nom;

-- Courbe des 15 dernières minutes d'un ESP
SELECT date_mesure + heure_mesure AS instant, temperature_c, humidite_pct, gaz_brut, mouvement_detecte
FROM mesure
WHERE id_dispositif = $1
  AND date_mesure + heure_mesure > (now() AT TIME ZONE 'UTC') - INTERVAL '15 minutes'
ORDER BY date_mesure, heure_mesure;

-- Alertes ouvertes, les plus graves et les plus récentes d'abord
SELECT *
FROM v_alerte_supervision
WHERE statut <> 'RESOLUE'
ORDER BY CASE niveau WHEN 'CRITIQUE' THEN 0 WHEN 'AVERTISSEMENT' THEN 1 ELSE 2 END,
         date_alerte DESC, heure_alerte DESC
LIMIT 50;

-- Acquitter
UPDATE v_alerte_supervision SET statut = 'ACQUITTEE' WHERE id_alerte = $1 AND statut = 'NOUVELLE';

-- Résoudre ($2 = id de l'utilisateur connecté)
UPDATE v_alerte_supervision
SET statut = 'RESOLUE', id_resolu_par = $2,
    date_resolution = CURRENT_DATE, heure_resolution = LOCALTIME(6)
WHERE id_alerte = $1 AND statut <> 'RESOLUE';

-- Connexion (on vérifie ensuite le hash côté API)
SELECT u.id_utilisateur, u.nom, u.mot_de_passe_hash, u.actif, r.code AS role
FROM utilisateur u JOIN role r ON r.id_role = u.id_role
WHERE lower(trim(u.email)) = lower(trim($1));
```

⚠️ **Toujours des requêtes paramétrées** (`$1`, `$2`…), jamais de concaténation de chaînes : on se fera attaquer en injection SQL jeudi.

## 7. Temps réel : `LISTEN / NOTIFY`

La base **prévient l'API** à chaque nouvel événement. Il n'est pas nécessaire de l'interroger en boucle. L'API garde une connexion ouverte, fait `LISTEN`, puis relaie les messages au navigateur en WebSocket.

```sql
LISTEN sentinel_mesure;
LISTEN sentinel_alerte;
```

**`sentinel_mesure`** : une mesure insérée (la ligne complète en JSON)
```json
{"id_mesure": 1532, "id_dispositif": 1, "date_mesure": "2026-10-07", "heure_mesure": "14:02:11.482913",
 "temperature_c": 22.50, "humidite_pct": 41.00, "gaz_brut": 180, "mouvement_detecte": false}
```

**`sentinel_alerte`** : une alerte physique créée (`INSERT`) ou mise à jour (`UPDATE`, par exemple acquittée)
```json
{"operation": "INSERT", "id_alerte": 87, "id_dispositif": 1, "type_alerte": "INTRUSION", "origine": "FUSION",
 "niveau": "CRITIQUE", "statut": "NOUVELLE", "message": null, "zone": "zone_interdite", "pir_confirme": true,
 "date_alerte": "2026-10-07", "heure_alerte": "14:02:12.004211"}
```

Bibliothèques : Python `psycopg` (`conn.notifies()`), Node `pg` (`client.on('notification')`), Rust `sqlx::postgres::PgListener` / `tokio-postgres`.

## 8. MQTT : commandes et vidéo

Compte Mosquitto **`dashboard`** (mot de passe dans le `.env`), connexion **MQTTS port 8883**, certificat vérifié avec `ca.crt`.
Droits limités par ACL : **publier** sur `sentinel/cmd/#` et **lire** `sentinel/video/#`, rien d'autre.

### Commandes actionneurs : *à valider avec la personne qui fait le firmware*

Topic : `sentinel/cmd/<numero_serie>`
```json
{"actionneur": "buzzer", "etat": "on", "duree_ms": 3000}
{"actionneur": "led", "couleur": "rouge", "etat": "clignote"}
{"actionneur": "led", "couleur": "vert", "etat": "on"}
```

### Flux webcam

Topic : `sentinel/video/cam1`, environ 5 images/s. Chaque message est **une image JPEG brute** (640x480, déjà annotée par l'IA : cadres, zones, identifiants).
Côté navigateur : relayer en WebSocket, puis `URL.createObjectURL(new Blob([data], {type: "image/jpeg"}))` dans une balise `<img>`.

## 9. Interface : ce qui est attendu par le jury

Rappel du sujet, *« Dashboard de Supervision »* et *« Contrôle Réactif »* :
- **Courbes en temps réel** : température, humidité, gaz (et le PIR en bandeau d'événements).
- **Statut logique du boîtier** : en ligne / hors ligne, dernière mesure, IP.
- **Retour visuel de la webcam** avec les détections de l'IA.
- **Panneau de commande** : buzzer, LEDs.
- **Réactivité immédiate aux alertes** (Axe 1 du jury, 5 points) : une alerte `CRITIQUE` doit sauter aux yeux en moins d'une seconde (bandeau rouge, son, clignotement).

## 10. Développer en local sans la VM

Avec Docker installé, depuis la racine du dépôt :

```bash
docker run -d --name sentinel-pg-dev -p 127.0.0.1:5432:5432 \
  -e POSTGRES_DB=sentinel -e POSTGRES_USER=sentinel_admin -e POSTGRES_PASSWORD=dev-admin \
  -e POSTGRES_INITDB_ARGS="--auth-local=scram-sha-256 --auth-host=scram-sha-256" \
  -e INGEST_DB_PASSWORD=dev-ingest -e DASHBOARD_DB_PASSWORD=dev-dashboard \
  -v "$PWD/server/db/init:/docker-entrypoint-initdb.d:ro" postgres:17

# Données de test + vérification des droits
docker exec -i sentinel-pg-dev bash < server/db/tests/test_droits.sh
```

Connexion : `postgresql://sentinel_dashboard:dev-dashboard@127.0.0.1:5432/sentinel`.
Pour simuler des mesures en direct : `docker exec -it sentinel-pg-dev psql -U sentinel_admin -d sentinel`, puis des `INSERT INTO mesure (...)`.
Les notifications partent immédiatement.

## 11. Ce qui reste à décider ensemble

- [ ] Algorithme de hachage des mots de passe : **argon2id** proposé, le même pour le script de création des comptes et pour l'API.
- [ ] Format des commandes MQTT (section 8), avec la personne qui fait le firmware.
- [ ] Langage de l'API dashboard. Le sujet cite Node.js, Python ou Go : vérifier auprès du coach si Rust est accepté.
