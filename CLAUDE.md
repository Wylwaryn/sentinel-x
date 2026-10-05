# Sentinel-X : contexte projet pour Claude

Workshop EPSI M1 « Mission Sentinel-X », sprint du lundi 5 au vendredi 9 octobre 2026.
Équipe de 6, tous de la filière IA : il faut couvrir aussi DEV, INFRA et CYBER. **Réponds en français.**
Privilégie Python partout pour que toute l'équipe puisse contribuer. Seul le firmware ESP8266 est en C++ (imposé par le sujet).

## Calendrier et livrables

- Mercredi : intégration de bout en bout ; l'après-midi, tournage de la vidéo sur fond vert.
- **Jeudi matin : gel du code.** Jeudi après-midi : pentest croisé, les autres groupes attaquent notre serveur.
- **Jeudi soir** : dépôt de `Workshop2026-M1-G<n>-{Dossier.pdf, Pres.pptx, VidDrop.mp4, Code.zip}`.
- Vendredi : soutenance de 10 min devant le jury. La démo live compte pour 5 pts sur 20.
- Règle éliminatoire : toutes les briques doivent être interconnectées.

## Architecture (option B : PC apprenant)

```
ESP8266 (DHT22, MQ-2, PIR, OLED, buzzer, LED) ──MQTTS:8883──┐
Navigateurs (dashboard) ───────────────────HTTPS:443────────┤  redirections NAT VirtualBox
                                                            ▼
PC hôte Windows 11 (RTX 5050, CUDA)        VM Linux Mint 22.3 « sentinel-server » (VirtualBox)
 ├─ host/vision : YOLOv8n + ByteTrack       ├─ Mosquitto (MQTTS)              [en service]
 │   + zones + fusion PIR                   ├─ API d'ingestion :8443 (127.0.0.1) [en service]
 └─ host/ids : IA réseau (capture Npcap)    ├─ API dashboard via Caddy :443   [collègues]
     connexions SORTANTES uniquement ─────► └─ PostgreSQL 17 (aucun port)     [en service]
```

Décisions prises (ne pas revenir dessus sans l'utilisateur) :
- **La vision tourne sur Windows** : le GPU n'est pas accessible depuis la VM. Elle n'ouvre aucun port et ne fait que des connexions sortantes vers la VM (API en HTTPS, MQTTS).
- **La fusion PIR + caméra se fait côté vision** : elle s'abonne à `sentinel/telemetry` en MQTTS.
- **IA réseau sur Windows** : la capture se fait sur l'interface du point d'accès Wi-Fi, parce que le NAT de VirtualBox masque les vraies IP sources.
- **Deux API séparées** : ingestion (écriture, à nous) et dashboard (lecture, collègues). Elles sont sur des réseaux Docker distincts et ne peuvent pas se joindre.
- **Flux webcam vers le dashboard** : la vision publie des JPEG sur MQTT `sentinel/video/cam1`.
- **Schéma BDD** : il appartient à l'équipe. On propose des changements, on ne les impose pas. Une ligne de `dispositif` = un ESP8266.

## Ports exposés (cible)

| Port | Service | Joignable depuis |
|---|---|---|
| 443 | Caddy vers l'API dashboard | Wi-Fi de la table |
| 8883 | Mosquitto MQTTS | Wi-Fi de la table |
| 2222 vers 22 | SSH de la VM | **127.0.0.1 uniquement** (le PC hôte) |
| 8443 | API d'ingestion (HTTPS, jeton Bearer) | **127.0.0.1 uniquement** |
| 5432 | PostgreSQL | **jamais** (réseau Docker interne) |

## Base de données (`server/db/`)

- `init/01-schema.sql` : 7 tables + vues `v_alerte_supervision` (sans `RESEAU_IA`) et `v_dispositif_etat`. LISTEN/NOTIFY sur `sentinel_mesure` et `sentinel_alerte`. UTC forcé.
- `init/02-roles.sh` crée deux rôles :
  - `sentinel_ingest` : INSERT mesure/alerte, SELECT mesure/dispositif/site/image_reference, **aucun accès à `utilisateur`** ;
  - `sentinel_dashboard` : lecture via les vues, UPDATE limité aux colonnes de résolution.
- `tests/test_droits.sh` : 39 tests (tous OK dans la VM). **Ils insèrent des données factices** : à lancer uniquement sur une base de test, puis `down -v`.
- Catalogue des alertes (origine → types) :
  - `VISION_IA`/`FUSION` → `PRESENCE`, `RODEUR`, `INTRUSION`, `APPROCHE_RAPIDE`
  - `PIR` → `ANGLE_MORT`
  - `CAPTEURS_IA` → `ANOMALIE_ENVIRONNEMENTALE`
  - `SYSTEME` → `DISPOSITIF_HORS_LIGNE`
  - `RESEAU_IA` → `SCAN_PORTS`, `DENI_DE_SERVICE`, `FORCE_BRUTE`, `TRAFIC_ANORMAL` (dispositif facultatif)
- Niveaux : `INFORMATION`, `AVERTISSEMENT`, `CRITIQUE`. Côté vision : `info`/`warning`/`critical`, à traduire dans l'API d'ingestion.
- Les dates et heures sont dans deux colonnes séparées (`date_xxx` + `heure_xxx`), en UTC. Horodatage posé par l'API à la réception.

## La VM

- Accès depuis Windows : `ssh sentinel-vm`. Clé `~/.ssh/sentinel_vm`, port 2222 lié à 127.0.0.1, utilisateur `wyllwaryn`.
- Projet : `/opt/sentinel-x` (clone Git). La stack se lance depuis `/opt/sentinel-x/server` avec `sudo docker compose up -d`.
- Secrets : `/opt/sentinel-x/server/.env` (root, 600). Générés dans la VM, **jamais commités, jamais copiés sur Windows**.
- Docker durci (`server/docker/daemon.json`) : userns-remap, no-new-privileges, rotation des journaux. Les conteneurs utilisent `cap_drop: [ALL]`, puis le minimum nécessaire.
- Avec userns-remap, les fichiers montés dans les conteneurs doivent être lisibles par « others » (755/644).

## PKI et MQTT (`server/pki/`, `server/mosquitto/`)

- CA maison EC P-256 : `sudo pki/pki.sh ca` (déjà fait), puis `sudo pki/pki.sh server <service> <uid> <SAN,...>` pour chaque service TLS (Caddy le moment venu).
  La clé de la CA reste dans `server/certs/ca/` (root, 700). **`certs/ca.crt` est public** : c'est lui qu'on copie sur Windows et dans le firmware.
- Certificat Mosquitto : SAN `mosquitto`, `sentinel-server`, `localhost`, `127.0.0.1`, `192.168.137.1` (point d'accès Windows par défaut). Si l'IP change, regénérer le certificat serveur : la CA ne change pas.
- Comptes : `sudo mosquitto/gen-passwd.sh` (mots de passe `MQTT_*_PASSWORD` dans `.env`). ACL dans `mosquitto/config/acl` (liste blanche). Depuis le réseau Docker, l'hôte est `mosquitto:8883`.
- Tests : `sudo mosquitto/tests/test_mqtt.sh` (33 tests : TLS, authentification, matrice ACL, limites).
- ESP8266 : BearSSL, TLS 1.2, `ca.crt` en trust anchor, **heure NTP obligatoire** avant la connexion (validité du certificat), `client_id` = numéro de série.

## VM : où en est la session VM (mis à jour par elle)

**En service dans la VM** :
- PostgreSQL ;
- Mosquitto MQTTS sur 8883 (33 tests OK) ;
- **API d'ingestion sur 8443** (`server/ingest/`, 59 tests OK sur une pile isolée).

**Statut VM : EN ATTENTE.** Étapes 1 et 2 du plan terminées. La session VM surveille ce fichier (vérification Git toutes les minutes) et ne passe à la suite qu'avec un feu vert écrit dans la section Windows. Elle ne prend aucune décision hors plan.
Pour lui parler : écrire dans la section Windows une ligne « **→ VM :** … », puis pousser.
Le dashboard (Caddy :443) appartient aux collègues.

### API d'ingestion : ce qu'il faut savoir pour s'y brancher

- URL : `https://127.0.0.1:8443/api/v1/alerts` (Windows, via la redirection NAT). Certificat signé par notre CA (`host/certs/ca.crt`), SAN `127.0.0.1`.
- **Un jeton Bearer par client**, et chaque jeton est limité à ses origines :

  | Jeton (dans le `.env` de la VM) | Origines autorisées | Variable côté Windows |
  |---|---|---|
  | `INGEST_TOKEN_VISION` | `VISION_IA`, `FUSION`, `PIR` | `SENTINEL_VISION_TOKEN` |
  | `INGEST_TOKEN_IDS` | `RESEAU_IA` | à choisir côté IDS |
  | `INGEST_TOKEN_CAPTEURS` | `CAPTEURS_IA` | réservé (IA capteurs) |

  Une autre origine renvoie 403. À récupérer un par un : `ssh sentinel-vm sudo grep INGEST_TOKEN_VISION /opt/sentinel-x/server/.env`.
- **Le contrat de la section Windows est implémenté tel quel**, exemple IDS compris. Les anciens noms de la vision (`source`/`type`/`level`/`serie`/`score`/`pir_confirmed`/`detail`) restent acceptés. Les alias `info`/`warning`/`critical`, `presence`/`loitering`/`fast_approach`/`intrusion`/`pir_blind_spot` sont traduits.
- **`numero_serie`** :
  - obligatoire pour toute origine sauf `RESEAU_IA`, à moins que `INGEST_DEFAULT_SERIE` soit défini dans le `.env` ;
  - un numéro inconnu renvoie 422 ;
  - pour `RESEAU_IA`, il peut être `null`.
- **`action`** est journalisé ; `details`, `ts` et `track_id` ne sont pas stockés.
- **Horodatage** : posé par la base à la réception, en UTC. Le `ts` du client est ignoré.
- **`snapshot_jpeg_b64`** :
  - accepté seulement pour `VISION_IA`/`FUSION` ;
  - JPEG de 1 Mo maximum, vérifié par ses octets magiques ;
  - stocké dans le volume Docker `captures`, avec `chemin_capture = captures/<uuid>.jpg`. L'API dashboard pourra monter ce volume en lecture seule.
- **Réponses** : 201 `{"id_alerte": n}`, 401 jeton absent ou faux, 403 origine interdite, 411/413 corps sans taille ou de plus de 2 Mo, 422 validation (la valeur reçue n'est jamais renvoyée).
- Pas de `/docs` ni d'`/openapi.json`, pas d'en-tête `Server`. Le jeton est vérifié **avant** la lecture du corps.
- **`DISPOSITIF_HORS_LIGNE`** (origine `SYSTEME`, niveau `CRITIQUE`) : levée après 30 s sans mesure, une seule fois par coupure. C'est une bonne démo : débrancher l'ESP devant le jury.

### Format de télémétrie attendu du firmware (`sentinel/telemetry`, JSON)

```json
{"serie": "ESP-01", "temperature_c": 21.5, "humidite_pct": 48.2, "gaz_brut": 312, "pir": true}
```
- `serie` doit exister dans `dispositif.numero_serie`, sinon le message est ignoré.
- Mettre `null` si le DHT22 est en défaut (ArduinoJson le fait pour NaN).
- Bornes : température de -40 à 80, humidité de 0 à 100, gaz de 0 à 1023. Une valeur hors bornes fait ignorer tout le message.
- `pir` est aussi lu par la vision pour la fusion.
- Envoyer au moins toutes les 10 s, sinon l'ESP passe hors ligne au bout de 30 s. Envoyer immédiatement à chaque changement du PIR.

### À faire côté Windows / équipe

- [ ] **Redirections NAT VirtualBox** (dans l'interface, la VM est sous le compte Windows `marci`) :
  - `mqtts` : TCP, IP hôte **vide** (Wi-Fi), port hôte 8883 vers le port invité 8883. Pare-feu Windows : 8883 autorisé seulement depuis le sous-réseau du point d'accès.
  - `ingest` : TCP, IP hôte **`127.0.0.1`**, port hôte 8443 vers le port invité 8443. **Jamais exposé au Wi-Fi.**
- [ ] **IP du point d'accès** : le certificat Mosquitto contient `192.168.137.1`. Si l'IP est différente, prévenir la session VM.
- [ ] **Créer le site et l'ESP en base.** Le rôle `ingest` n'en a pas le droit, c'est voulu. Avec le compte admin, dans la VM :
  `INSERT INTO site (nom) VALUES ('EPSI'); INSERT INTO dispositif (id_site, nom, numero_serie) VALUES (1, 'Boîtier 1', '<serie>');`
  Puis mettre `INGEST_DEFAULT_SERIE=<serie>` dans le `.env` si la vision n'envoie pas `numero_serie`.
- [ ] **Jetons** : `SENTINEL_VISION_TOKEN` pour la vision, et le jeton de l'IDS, récupérés un par un comme le mot de passe MQTT.
- [ ] **Firmware ESP8266** : NTP avant la connexion TLS, `client_id` = numéro de série, format de télémétrie ci-dessus, abonnement à `sentinel/cmd/<numero_serie>`. Mot de passe `MQTT_ESP_PASSWORD`, à récupérer seul.
- [x] `ca.crt` dans `host/certs/`, mot de passe `vision`, une seule connexion MQTT pour la vision (fait par la session Windows).

### À savoir

- **Docker contourne UFW** pour les ports publiés (8883, 8443, plus tard 443) : la règle UFW de jeudi ne les filtrera pas. Le filtrage réel se fait au pare-feu Windows et dans les redirections VirtualBox.
- Chaque conteneur ne reçoit que ses propres secrets (`environment:` explicite, pas d'`env_file`). Postgres ne voit ni les mots de passe MQTT ni les jetons.
- ACL MQTT : le dashboard publie sur `sentinel/cmd/+` (plus strict que `cmd/#`). Un abonnement à `#` ne donne accès qu'aux topics autorisés pour le compte.
- Mosquitto et l'API d'ingestion n'ont pas de NAT sortant : ils reçoivent des connexions mais ne peuvent pas joindre Internet.
- Mosquitto signale des droits trop ouverts sur le fichier `acl` : c'est sans conséquence en 2.0 et le fichier ne contient aucun secret.
- Tests :
  - `sudo mosquitto/tests/test_mqtt.sh` (production, aucune écriture en base) ;
  - `sudo ingest/tests/test_ingest.sh` (crée puis détruit une pile `sentinel-test`, la production n'est pas touchée).

## Windows : où en est la session Windows (mis à jour par elle)

**Fait côté Windows :**
- `host/vision/` : YOLOv8n + ByteTrack sur la RTX 5050 (10,8 ms par image), zones, fusion PIR (10 tests). MQTT branché et testé (`host/vision/mqtt_link.py`) ; `enabled: false` tant que la redirection 8883 n'existe pas.
- `host/ids/` : IA réseau en 2 étages (13 tests, 17,6 ms par fenêtre sur GPU). Modèle amorcé sur du trafic synthétique, en attente de Npcap et du trafic réel.
- Réponses aux demandes de la section VM :
  - [x] `ca.crt` copié dans **`host/certs/ca.crt`** (dossier partagé par la vision et l'IDS, pas `host/vision/certs/`). Empreinte SHA-256 `36:A8:B4:4A:…:68:55:AD`.
  - [x] Mot de passe `vision` récupéré seul, stocké dans les variables d'environnement utilisateur Windows `SENTINEL_MQTT_USER` / `SENTINEL_MQTT_PASS`. Jamais affiché ni commité.
  - [ ] Redirection NAT 8883 : **à faire par l'utilisateur dans l'interface VirtualBox.** La VM est enregistrée sous le compte Windows `marci` : la session Windows (compte `Wyllwaryn_User`) ne voit pas la VM avec VBoxManage.
  - [ ] IP du point d'accès : pas encore activé. Vérification prévue dès qu'il l'est.
  - [x] `client_id` uniques : **une seule connexion MQTT** pour le PIR et la vidéo (`client_id = sentinel-vision`).

**Prochaines étapes côté Windows :** brancher la vision sur MQTT (PIR + `sentinel/video/cam1`) ; dès que Npcap est installé, `record` du trafic normal puis ré-entraînement de l'IDS.

**Contrat avec l'API d'ingestion** (format `AlertIn` de `server/ingest/app/models.py`, qui fait foi) :
- Vision : `POST https://127.0.0.1:8443/api/v1/alerts`, jeton `SENTINEL_VISION_TOKEN`. Champs envoyés : `type`, `level` et `source` dans le vocabulaire de la vision (traduits par l'API), `serie` (= `device_serie` dans `host/vision/config.json`), `score` (confiance YOLO), `pir_confirmed`, `track_id`, `zone`, `detail`, `ts`, `snapshot_jpeg_b64` (alertes critiques).
- IDS : même route, jeton `SENTINEL_IDS_TOKEN`. `type`, `level` et `source=RESEAU_IA` en vocabulaire BDD, `serie` (ESP connu sinon `null`), `ip_source`, `score`, `message`, `detail` (`action`, `confiance_type`, caractéristiques). Le journal local `host/ids/logs/` garde l'événement complet.

**MQTT vision vérifié contre le vrai broker** (tunnel SSH, 5 octobre) : TLS et authentification OK ; télémétrie ESP vers vision (PIR) OK ; vidéo `sentinel/video/cam1` reçue par `dashboard` (5 images/s) OK ; vision ne reçoit pas `sentinel/cmd/+` et ne peut pas usurper `sentinel/telemetry`. Une seule connexion, `client_id = sentinel-vision`.

## Pièges déjà rencontrés

- `docker compose exec` et `ssh` lisent l'entrée standard : dans un script heredoc, ajouter `</dev/null`, sinon ils avalent la suite du script.
- PowerShell 5.1 abîme les guillemets passés à `ssh` : piloter la VM depuis Git Bash (outil Bash).
- Docker publie ses ports avant UFW : une règle UFW ne bloque PAS 8883 ni 443. Le filtrage se fait au niveau de la redirection NAT VirtualBox et du pare-feu Windows.
- L'image postgres fait confiance aux connexions locales par défaut. D'où `POSTGRES_INITDB_ARGS=--auth-local=scram-sha-256 --auth-host=scram-sha-256`.
- La RTX 5050 (Blackwell) exige PyTorch `cu128` ou plus. Webcam USB = index 1 (l'index 0 est la caméra intégrée HP).

## Répartition entre sessions Claude (pour éviter les conflits Git)

- **Session Windows** : `host/` (vision, IA réseau), `docs/`, coordination.
- **Session VM** : `server/` (Mosquitto, API d'ingestion, compose, Caddy).
- Toujours faire `git pull --rebase` avant de commencer et `git push` dès qu'une brique fonctionne.

## Conventions

- Commits sémantiques en français : `feat(scope): …`, `fix:`, `test:`, `docs:`, `chore:`. La grille d'évaluation note la régularité des commits.
- Pas de secret dans le code : variables d'environnement + `.env.example`.
- Requêtes SQL toujours paramétrées. On sera attaqués jeudi.

## Prochaines étapes

1. **Mosquitto MQTTS** (session VM) :
   - notre CA, des certificats serveur avec SAN `127.0.0.1` et l'IP du point d'accès ;
   - des comptes `esp`, `ingest`, `dashboard`, `vision` ;
   - des ACL : `esp` publie `sentinel/telemetry` et lit `sentinel/cmd/<serie>` ; `dashboard` publie `sentinel/cmd/#` et lit `sentinel/video/#` ; `vision` lit `sentinel/telemetry` et publie `sentinel/video/#` ; `ingest` lit `sentinel/telemetry`.
2. **API d'ingestion** (session VM) : FastAPI. Abonnement MQTT vers `mesure`, `POST /api/v1/alerts` (jeton Bearer) vers `alerte`, détection `DISPOSITIF_HORS_LIGNE`.
3. **Vision** (session Windows) : publication vidéo MQTT, envoi des alertes au format de l'API, traduction des niveaux.
4. **IA réseau** (session Windows) : `host/ids/` FAIT (2 étages : autoencodeur + Isolation Forest, puis Random Forest pour le type ; 13 tests). Reste : installer Npcap, `record` du trafic normal réel, ré-entraîner, valider avec nmap / hping3 réels.

## Durcissement à faire jeudi matin, avant le pentest

- [ ] Supprimer `/etc/sudoers.d/90-sentinel-setup` (sudo sans mot de passe, temporaire)
- [ ] Deploy key GitHub de la VM : la révoquer ou la passer en lecture seule
- [ ] SSH : `PasswordAuthentication no`, `PermitRootLogin no`
- [ ] UFW : n'autoriser que 22, 443 et 8883
- [ ] Désactiver CUPS (port 631)
- [ ] VirtualBox : couper le presse-papiers et le glisser-déposer
- [ ] Pare-feu Windows : tout refuser en entrée, sauf les redirections vers la VM
