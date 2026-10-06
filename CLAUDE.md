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
- Mosquitto MQTTS sur 8883 (45 tests OK, 6 comptes) ;
- **API d'ingestion sur 8443** (`server/ingest/`, 59 tests OK sur une pile isolée).
- Supervision `sentinel-monitor` (timer chaque minute).

**Statut VM : EN ATTENTE.** Étapes 1, 2, 5 (préparée), 6 et 7 terminées, plus le script de pare-feu de la VM (demandé par l'utilisateur le 6 oct.). Prochaine action VM : « → VM : appliquer le durcissement », ou le résultat du test d'observation ci-dessous.

**Incident du 6 oct. (résolu)** : après le redémarrage de la VM à 09:35, **Mosquitto ne s'est pas relancé** jusqu'à 10:39. Cause probable : un rechargement par `docker kill -s HUP` la veille, qui marque le conteneur comme « arrêté manuellement ». Corrigé : `gen-passwd.sh` recharge maintenant de l'intérieur (`docker compose exec mosquitto kill -HUP 1`). Si la vision, l'ESP ou la maintenance prédictive ont « perdu » le broker ce matin, c'est cette coupure.

**→ Windows : pare-feu de la VM prêt, pendant de `windows_firewall.ps1` (rien n'est appliqué).** `server/hardening/firewall.sh` :
- **Simulation par défaut**, `--apply`, `--restore` (sauvegarde complète faite au premier `--apply`). `apply.sh` l'appelle pour sa partie pare-feu.
- **Entrées de la VM (UFW)** : refus par défaut, SSH 22 autorisé **seulement depuis `10.0.2.2`** (la passerelle NAT VirtualBox, donc via `127.0.0.1:2222` côté Windows).
- **Ports des conteneurs (`DOCKER-USER`)** : Docker contourne UFW. Liste blanche 8883, 8443 et 443 (Caddy, plus tard) depuis `10.0.2.2` ; tout le reste vers un conteneur est journalisé (`journalctl -k | grep SENTINEL`) puis bloqué, en IPv4 et IPv6. Un port publié par erreur, comme 5432, resterait bloqué. Règles stockées dans `/etc/ufw/after*.rules`, donc persistantes.
- **`--egress` (option, NON décidée)** : bloque les sorties de la VM et des conteneurs sauf DNS et NTP. Ça empêche un reverse shell pendant le pentest, mais coupe aussi `git pull`, `apt` et `docker pull`. À n'activer qu'après le gel du code, si l'utilisateur le décide.
- **`--observe`** : même liste blanche, mais **journalise au lieu de bloquer** (non persistant). **Actif depuis le 6 oct. 10:50**, sans aucun effet sur le trafic.
- **→ Windows : test demandé.** Pendant que l'observation tourne, ouvrir au moins une **nouvelle** connexion depuis Windows vers 8883 (vision, maintenance prédictive ou ESP) et vers 8443 (une alerte, ou `curl https://127.0.0.1:8443/healthz`). Puis écrire « → VM : test observation fait » : la VM vérifiera que les compteurs des règles d'autorisation montent et que rien de légitime n'apparaît dans les « serait bloqué ».
- **Quand appliquer ?** C'est à l'utilisateur de décider. Proposition : mercredi soir, en même temps que `windows_firewall.ps1 -Apply`, pour détecter une casse avant le pentest.

**→ Windows : compte capteurs prêt.** Compte MQTT `capteurs`, lecture seule de `sentinel/telemetry`, `client_id` libre (`sentinel-capteurs` conseillé). Mot de passe `MQTT_CAPTEURS_PASSWORD` à récupérer seul : `ssh sentinel-vm sudo grep MQTT_CAPTEURS_PASSWORD /opt/sentinel-x/server/.env`.

**→ Windows : étape 5 prête (rien n'est appliqué).**
- `sudo hardening/apply.sh` : simulation par défaut. `--apply` applique SSH (clé uniquement, pas de root, `AllowUsers wyllwaryn`), UFW (refus en entrée, 22 autorisé) et CUPS masqué. `--apply --remove-sudoers` supprime en plus le sudo sans mot de passe, tout à la fin, par l'utilisateur.
- Garde-fous :
  - arrêt s'il n'y a aucune clé SSH autorisée ;
  - `sshd -t` avant le rechargement, avec retour arrière en cas d'erreur ;
  - le sudoers n'est supprimé que si `wyllwaryn` est dans le groupe `sudo` et a un mot de passe.
  **L'utilisateur doit connaître le mot de passe de `wyllwaryn` avant jeudi.**
- `sudo hardening/verify.sh` (`--markdown` pour le dossier) : aujourd'hui **36 OK, 10 À FAIRE (exactement ceux d'`apply.sh` et de `firewall.sh`), 0 KO**, plus 4 contrôles manuels (redirections VirtualBox, presse-papiers et glisser-déposer, deploy key, pare-feu Windows).
- Écart avec la liste du bas de ce fichier : UFW n'autorise que 22, comme demandé dans la section Windows. Ouvrir 443 et 8883 dans UFW ne servirait à rien, puisque Docker publie ces ports en contournant UFW.

**→ Windows : étape 6 prête.** Supervision installée et active (`server/monitoring/`) :
- un relevé chaque minute (timer systemd), avec CPU, RAM et disque de la VM, puis CPU, RAM, PIDs, réseau, redémarrages et taille des journaux par conteneur ;
- Mosquitto via `$SYS` : clients, messages/min, octets ;
- PostgreSQL : taille, mesures et alertes, croissance par jour ;
- journal `/var/log/sentinel-x/metrics.jsonl`, rotation quotidienne par logrotate (7 jours, 10 Mo maximum) ;
- journaux des conteneurs plafonnés par Docker (10 Mo x 3), et Mosquitto ne journalise que les connexions : pas de croissance avec l'afflux de messages.
- **Résumé en une commande** : `sudo sentinel-monitor report` (`--hours 24`, `--markdown`). Code retour 1 si un seuil est dépassé (CPU/RAM 85 %, disque 80 %, conteneur arrêté ou redémarré, journal > 25 Mo, aucun client MQTT).
- Nouveau compte MQTT `monitor` : lecture de `$SYS/broker/#` uniquement, depuis le réseau Docker interne. Aucun port exposé.
- Le collecteur est copié dans `/usr/local/lib/sentinel-x/` (root) : root n'exécute jamais un fichier du dépôt.

### Matrice : ports réellement exposés (relevé du 5 oct., `ss -tlnp` + `docker compose ps`)

| Port VM | Service | Écoute | Joignable depuis | Protection |
|---|---|---|---|---|
| 22 | sshd | 0.0.0.0 | Windows seulement (NAT `127.0.0.1:2222`) | clé uniquement après jeudi, UFW |
| 8883 | Mosquitto (docker-proxy) | 0.0.0.0 | Wi-Fi (NAT `0.0.0.0:8883`) | TLS 1.2+, 6 comptes, ACL en liste blanche |
| 8443 | API d'ingestion (docker-proxy) | 0.0.0.0 | Windows seulement (NAT `127.0.0.1:8443`) | TLS 1.2+, jeton Bearer par client |
| 5432 | PostgreSQL | réseau Docker interne | personne (aucun port publié) | rôles séparés, scram-sha-256 |
| 631 | CUPS | 127.0.0.1 | local | **supprimé jeudi** (`apply.sh`) |
| 53 | systemd-resolved | 127.0.0.53/54 | local | — |
| éphémères | serveur VS Code Remote | 127.0.0.1 | local | disparaît quand VS Code se déconnecte |

### Tests côté VM

| Composant | Script | Résultat |
|---|---|---|
| Droits PostgreSQL | `db/tests/test_droits.sh` (base de test) | 39 OK |
| Mosquitto (TLS, authentification, ACL des 6 comptes, limites) | `mosquitto/tests/test_mqtt.sh` | 45 OK |
| API d'ingestion (HTTPS, jetons, contrat, validation, injection, captures, télémétrie, hors ligne) | `ingest/tests/test_ingest.sh` (pile isolée) | 59 OK |
| Conformité de la VM | `hardening/verify.sh` | 36 OK, 10 à faire jeudi, 0 KO |

La session VM surveille ce fichier (vérification Git toutes les minutes) et ne passe à la suite qu'avec un feu vert écrit dans la section Windows. Elle ne prend aucune décision hors plan.
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
  - [x] Redirections NAT faites par l'utilisateur : `0.0.0.0:8883` (MQTTS) et `127.0.0.1:8443` (ingestion), en plus de `127.0.0.1:2222` (SSH). Vérifié : certificat MQTTS valide, `/healthz` de l'ingestion à 200.
  - [x] **Point d'accès actif (6 oct.) : `192.168.137.1/24`** sur l'interface Windows « Connexion au réseau local* 4 », en 2,4 GHz. C'est l'IP du certificat Mosquitto, donc **rien à régénérer côté VM**. Vérifié : TLS sur `192.168.137.1:8883` OK, certificat validé pour cette IP.
  - [x] `client_id` uniques : **une seule connexion MQTT** pour le PIR et la vidéo (`client_id = sentinel-vision`).

**Réponses à « À faire côté Windows / équipe » (section VM) :** redirections NAT ✅ ; site et ESP créés en base ✅ (`SX-G2-01`, voir plus bas) ; jetons vision et IDS ✅ ; IP du point d'accès ⏳ (en attente que l'utilisateur l'active) ; firmware ⏳ (équipe, pas commencé). `INGEST_DEFAULT_SERIE` est inutile : la vision envoie toujours `serie`.

**Prochaines étapes côté Windows :** l'utilisateur active le point d'accès (2,4 GHz) ; ensuite capture IDS sur cette interface, `record` du trafic normal réel, ré-entraînement, validation nmap/hping3 ; essai réel de la vision avec une personne devant la caméra.

### Demandes à la session VM

**→ VM : feu vert étape 5, « préparer le durcissement de jeudi SANS l'appliquer ».** Écrire `server/hardening/apply.sh` et `server/hardening/verify.sh`, idempotents, avec un mode `--dry-run` par défaut. Ils couvrent la liste « Durcissement à faire jeudi matin » de ce fichier, côté VM :
- suppression de `/etc/sudoers.d/90-sentinel-setup` ;
- sshd : `PasswordAuthentication no`, `PermitRootLogin no`, `KbdInteractiveAuthentication no`, `AllowUsers wyllwaryn` ;
- UFW : refus par défaut en entrée, 22 autorisé ;
- désactivation de CUPS ;
- rappel pour la deploy key (action GitHub, pas script).

`verify.sh` doit produire un rapport lisible, pour la matrice de sécurité du dossier. **Ne rien appliquer** avant un « → VM : appliquer le durcissement » écrit ici jeudi matin. Attention : supprimer le sudoers temporaire te retire `sudo` sans mot de passe, donc c'est la toute dernière action, faite par l'utilisateur.

**→ VM : feu vert étape 6, « supervision et maintien en condition opérationnelle »** (exigence du sujet, filière Cyber : « consommation CPU/RAM, gestion des volumes de logs MQTT face à l'afflux continu des messages »). Proposition à adapter :
- relevé périodique CPU, RAM et disque de la VM et de chaque conteneur (`docker stats --no-stream`), taille des journaux Docker et Mosquitto, nombre de messages reçus par Mosquitto (`$SYS` via un compte d'administration local, ou les journaux) ;
- écriture dans un journal tournant sous `server/monitoring/` ;
- un résumé en une commande, pour le dossier et la démo ;
- **aucun nouveau port exposé**.

**→ VM : conditionnel.** Si l'IP du point d'accès n'est pas `192.168.137.1`, elle sera écrite ici. Il faudra alors régénérer le certificat serveur Mosquitto (la CA ne change pas).

**→ VM : en fin d'étape**, mettre à jour ta section avec la liste des ports réellement exposés (`ss -tlnp` + `docker compose ps`) et le nombre de tests par composant, pour la matrice de sécurité du dossier.

**→ VM : feu vert étape 7, « compte MQTT `capteurs` »** pour la maintenance prédictive (validée par l'utilisateur, développée côté Windows dans `host/predictive/`) :
- compte `capteurs` : ACL **lecture seule** de `sentinel/telemetry`, rien d'autre ;
- mot de passe `MQTT_CAPTEURS_PASSWORD` dans le `.env` (via `gen-passwd.sh`) et dans `.env.example` ;
- tests ajoutés à `mosquitto/tests/test_mqtt.sh` : lecture OK, publication refusée, `cmd`/`video` inaccessibles ;
- `client_id` utilisé côté Windows : `sentinel-capteurs`.
Écris « compte capteurs prêt » dans ta section : la session Windows récupérera le mot de passe seule.
Côté API, rien à faire : le jeton `INGEST_TOKEN_CAPTEURS` (origine `CAPTEURS_IA`) existe et a déjà été copié sur Windows (`SENTINEL_CAPTEURS_TOKEN`).

**Dispositif enregistré en base (5 oct.)** : site `Avant-poste Sentinel-X G2`, dispositif `id 1`, **numéro de série `SX-G2-01`** (groupe 2). C'est la valeur à mettre dans le firmware (`serie`, `client_id`), à graver sur le boîtier, et à utiliser pour `INGEST_DEFAULT_SERIE` si besoin.

**Vision et IDS activés** (`enabled: true`). Jetons `INGEST_TOKEN_VISION` et `INGEST_TOKEN_IDS` copiés seuls dans les variables d'environnement Windows `SENTINEL_VISION_TOKEN` / `SENTINEL_IDS_TOKEN`. Test de bout en bout contre l'API : vision `FUSION/INTRUSION` donne 201 ; IDS `RESEAU_IA/SCAN_PORTS` donne 201 ; jeton vision qui tente `RESEAU_IA` donne 403 ; sans jeton, 401. Les lignes de test ont été supprimées.

**Npcap installé**, capture OK. IDS configuré sur « Connexion au réseau local* 4 » (IP protégée `192.168.137.1`). Le nom de cette interface peut changer après un redémarrage : vérifier avec `python sentinel_ids.py interfaces`.

**→ VM : rien à faire pour l'IP du point d'accès** : c'est bien `192.168.137.1`, la condition de régénération du certificat ne s'applique pas.

**Maintenance prédictive `host/predictive/` (5 oct., 11 tests)** :
- **Caractéristiques cinétiques** sur 1 et 5 min : écart à la normale, pentes, accélération, volatilité, corrélations temp/gaz et temp/hum, valeurs manquantes ou figées.
- **Étage 1** : Isolation Forest + écart statistique (max). **Étage 2** : Random Forest qui distingue SURCHAUFFE, FUITE_GAZ, CORRELATION_TEMP_GAZ et CAPTEUR_DEFAILLANT.
- **Prévision** : délai avant les limites d'exploitation (`config.json`, 45 °C / gaz 700), qui ne déclenchent jamais rien seules. Persistance sur 2 évaluations, une alerte par épisode (début, aggravation, type précisé, prévision disponible, rappel toutes les 10 min).
- **Mesures sur des données synthétiques jamais vues** : détection 90 %, typage 97,5 %, moins d'une fausse alerte par heure. Surchauffe à +1 °C/min : alerte à +30 s, délai annoncé 20,9 min pour 19,0 min réelles.
- Envoi vers l'API : jeton `SENTINEL_CAPTEURS_TOKEN`, `source=CAPTEURS_IA`, `type=ANOMALIE_ENVIRONNEMENTALE`, `serie`, `score`, `message`, `detail` (sous-type, prévision, épisode).
- ✅ **Raccordé** : mot de passe `capteurs` récupéré seul (`SENTINEL_CAPTEURS_USER` / `SENTINEL_CAPTEURS_PASS`). Contre le vrai broker : TLS OK, télémétrie reçue, publication refusée. Contre l'API : 201 en `CAPTEURS_IA` sur `SX-G2-01`, 403 si le jeton tente `VISION_IA`. Ligne de test supprimée. Merci à la session VM pour l'étape 7.
- Reste : le modèle est amorcé sur du synthétique. Le ré-entraîner sur la télémétrie réelle dès que l'ESP émet (`record --minutes 30`, puis `train`).

**Firmware ESP8266 `firmware/` (6 oct., PlatformIO, compile : RAM 37 %, flash 40 %)** :
- Fait par la session Windows, compilé et téléversé depuis le PC hôte (`.venv\Scripts\pio.exe run -t upload`).
- `serie` = `client_id` = `SX-G2-01` ; MQTTS `192.168.137.1:8883`, compte `esp`, CA intégrée (`include/ca_cert.h`, publique) ; NTP, avec repli sur la date de compilation.
- Télémétrie toutes les 5 s et à chaque changement du PIR ; commandes `sentinel/cmd/SX-G2-01` (buzzer, LED rouge/verte on/off/clignote) ; OLED ; mode secours local après 30 s sans broker.
- Secrets dans `include/secrets.h` (ignoré par Git) : Wi-Fi saisi par l'utilisateur, mot de passe `esp` récupéré seul depuis la VM.
- Brochage : OLED D2/D1, DHT22 D5, PIR D6, LED rouge D7, LED verte D0, buzzer D8, MQ-2 A0 via pont diviseur, PIR et MQ-2 sur VU (5 V USB).
- Prochaine étape : téléverser dès que l'ESP est branché sur le PC hôte, puis vérifier les mesures en base.

**Contrat avec l'API d'ingestion** (format `AlertIn` de `server/ingest/app/models.py`, qui fait foi) :
- Vision : `POST https://127.0.0.1:8443/api/v1/alerts`, jeton `SENTINEL_VISION_TOKEN`. Champs envoyés : `type`, `level` et `source` dans le vocabulaire de la vision (traduits par l'API), `serie` (= `device_serie` dans `host/vision/config.json`), `score` (confiance YOLO), `pir_confirmed`, `track_id`, `zone`, `detail`, `ts`, `snapshot_jpeg_b64` (alertes critiques).
- IDS : même route, jeton `SENTINEL_IDS_TOKEN`. `type`, `level` et `source=RESEAU_IA` en vocabulaire BDD, `serie` (ESP connu sinon `null`), `ip_source`, `score`, `message`, `detail` (`action`, `confiance_type`, caractéristiques). Le journal local `host/ids/logs/` garde l'événement complet.

**MQTT vision vérifié contre le vrai broker** (tunnel SSH, 5 octobre) : TLS et authentification OK ; télémétrie ESP vers vision (PIR) OK ; vidéo `sentinel/video/cam1` reçue par `dashboard` (5 images/s) OK ; vision ne reçoit pas `sentinel/cmd/+` et ne peut pas usurper `sentinel/telemetry`. Une seule connexion, `client_id = sentinel-vision`.

## Pièges déjà rencontrés

- `docker compose exec` et `ssh` lisent l'entrée standard : dans un script heredoc, ajouter `</dev/null`, sinon ils avalent la suite du script.
- PowerShell 5.1 abîme les guillemets passés à `ssh` : piloter la VM depuis Git Bash (outil Bash).
- Docker publie ses ports avant UFW : une règle UFW ne bloque PAS 8883 ni 443. Côté VM, le filtrage passe par la chaîne `DOCKER-USER` (`server/hardening/firewall.sh`), en plus de la redirection NAT VirtualBox et du pare-feu Windows.
- Ne jamais recharger un conteneur avec `docker kill -s <signal>` : Docker le marque « arrêté manuellement » et `restart: unless-stopped` ne le relance plus au démarrage. Utiliser `docker compose exec <service> kill -HUP 1`.
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

1. ✅ Mosquitto MQTTS (VM). 2. ✅ API d'ingestion (VM). 3. ✅ Vision raccordée : MQTT + API (Windows). 4. ✅ IA réseau écrite (Windows).
5. Durcissement de jeudi **préparé, pas appliqué** (VM, feu vert donné).
6. Supervision et maintien en condition opérationnelle (VM, feu vert donné).
7. Point d'accès 2,4 GHz, puis entraînement de l'IDS sur le trafic réel (utilisateur puis Windows).
8. Firmware ESP8266 : écrit et compilé (session Windows, `firmware/`). Reste à téléverser et vérifier.
9. Maintenance prédictive `CAPTEURS_IA` : session Windows (`host/predictive/`) ; compte MQTT `capteurs` demandé à la VM.
10. Dashboard + Caddy :443 (collègues), d'après `docs/fiche-api-dashboard.md`.

## Durcissement à faire jeudi matin, avant le pentest

- [ ] Supprimer `/etc/sudoers.d/90-sentinel-setup` (sudo sans mot de passe, temporaire)
- [ ] Deploy key GitHub de la VM : la révoquer ou la passer en lecture seule
- [ ] SSH : `PasswordAuthentication no`, `PermitRootLogin no`
- [ ] UFW : n'autoriser que 22, 443 et 8883
- [ ] Désactiver CUPS (port 631)
- [ ] VirtualBox : couper le presse-papiers et le glisser-déposer
- [ ] **Pare-feu Windows : `host/hardening/windows_firewall.ps1`** (PowerShell administrateur).
  - Sans option : simulation. `-Apply` : sauvegarde complète, puis désactivation des règles entrantes « Autoriser » du profil Public (109 aujourd'hui : jeux, adb, Node, Docker, diffusion sans fil…), sauf la gestion réseau de base de Windows.
  - Crée 5 règles **limitées à `192.168.137.0/24`** : 8883, 443, DHCP 67, DNS 53 (UDP et TCP).
  - `-Restore` remet tout comme avant.
  - À appliquer **dès mercredi soir**, pour détecter une casse avant le pentest ; puis vérifier : DHCP sur le point d'accès, ESP connecté sur 8883, dashboard joignable.
