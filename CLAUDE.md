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
 │   + zones + fusion PIR                   ├─ API d'ingestion (127.0.0.1)    [à faire]
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
| à définir | API d'ingestion | **127.0.0.1 uniquement** |
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

**En service dans la VM** : PostgreSQL, Mosquitto MQTTS (8883, 33 tests OK).
**En cours** : API d'ingestion.

### À faire côté Windows (session locale ou utilisateur)

- [ ] **Redirection NAT VirtualBox pour MQTTS** (sans elle, ni l'ESP ni la vision ne joignent le broker) :
  `VBoxManage controlvm "sentinel-server" natpf1 "mqtts,tcp,,8883,,8883"`.
  Le port écoute sur toutes les interfaces de Windows, puisque l'ESP passe par le Wi-Fi : le pare-feu Windows ne doit autoriser 8883 que depuis le sous-réseau du point d'accès.
- [ ] **Vérifier l'IP du point d'accès Windows.** Le certificat contient `192.168.137.1`. Si l'IP est différente, le dire à la session VM, qui régénérera le certificat serveur (la CA ne change pas, donc le firmware reste valide).
- [ ] **Copier `ca.crt`** (public) depuis la VM : `scp sentinel-vm:/opt/sentinel-x/server/certs/ca.crt host/vision/certs/`, et l'intégrer au firmware.
- [ ] **Mots de passe `vision` et `esp`** : exception assumée à la règle « secrets jamais copiés sur Windows », puisque ces deux comptes tournent hors de la VM.
  Les récupérer **un par un** (`ssh sentinel-vm sudo grep MQTT_VISION_PASSWORD /opt/sentinel-x/server/.env`), puis les ranger dans les variables d'environnement Windows `SENTINEL_MQTT_USER=vision` et `SENTINEL_MQTT_PASS`, ou dans le firmware. Ne jamais copier le `.env` entier ni le committer.
- [ ] **Vision** : `client_id` uniques. Si la vision ouvre deux connexions (PIR et vidéo), leur donner deux identifiants différents, sinon chacune déconnecte l'autre.
- [ ] **Firmware ESP8266** : NTP avant la connexion TLS, `client_id` = numéro de série, publication sur `sentinel/telemetry`, abonnement à `sentinel/cmd/<numero_serie>`.

### À savoir

- **Docker contourne UFW** pour les ports publiés (8883, plus tard 443) : la règle UFW de jeudi ne les filtrera pas. Le filtrage réel se fait au pare-feu Windows et dans la redirection VirtualBox.
- ACL MQTT : le dashboard publie sur `sentinel/cmd/+` (plus strict que `cmd/#`). Un abonnement à `#` ne donne accès qu'aux topics autorisés pour le compte.
- Mosquitto signale des droits trop ouverts sur le fichier `acl` : c'est sans conséquence en 2.0 et le fichier ne contient aucun secret.
- Le broker n'a pas de NAT sortant : il reçoit des connexions mais ne peut pas joindre Internet.

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
