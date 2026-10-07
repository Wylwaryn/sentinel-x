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
- **IA réseau sur Windows** : la capture se fait sur l'interface du point d'accès Wi-Fi. Elle y voit **tout** le trafic qui arrive sur le PC hôte, y compris celui qui vise des ports non redirigés vers la VM, et elle peut bloquer au pare-feu Windows **avant** la VM. (Précision de la session VM : VirtualBox conserve l'IP source réelle sur les ports redirigés. Seul le trafic issu du PC hôte apparaît en `10.0.2.2` dans la VM.)
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
- **API d'ingestion sur 8443** (`server/ingest/`, 68 tests OK sur une pile isolée).
- Supervision `sentinel-monitor` (timer chaque minute).
- **Dashboard + Caddy :443** (code de la session dashboard, lancé par `COMPOSE_FILE`).

**Statut VM : EN ATTENTE.** Étapes 1, 2, 5 (préparée), 6 et 7 terminées, plus le script de pare-feu de la VM (demandé par l'utilisateur le 6 oct.). Prochaine action VM : « → VM : appliquer le durcissement », ou le résultat du test d'observation ci-dessous.

**Incident du 6 oct. (résolu)** : après le redémarrage de la VM à 09:35, **Mosquitto ne s'est pas relancé** jusqu'à 10:39. Cause probable : un rechargement par `docker kill -s HUP` la veille, qui marque le conteneur comme « arrêté manuellement ». Corrigé : `gen-passwd.sh` recharge maintenant de l'intérieur (`docker compose exec mosquitto kill -HUP 1`). Si la vision, l'ESP ou la maintenance prédictive ont « perdu » le broker ce matin, c'est cette coupure.

**→ Windows : pare-feu de la VM prêt, pendant de `windows_firewall.ps1` (rien n'est appliqué).** `server/hardening/firewall.sh` :
- **Simulation par défaut**, `--apply`, `--restore` (sauvegarde complète faite au premier `--apply`). `apply.sh` l'appelle pour sa partie pare-feu.
- **Entrées de la VM (UFW)** : refus par défaut, SSH 22 autorisé **seulement depuis `10.0.2.2`** (la passerelle NAT VirtualBox, donc via `127.0.0.1:2222` côté Windows).
- **Ports des conteneurs (`DOCKER-USER`)** : Docker contourne UFW. Liste blanche : **8883 et 443 depuis `10.0.2.2` et `192.168.137.0/24`** (ESP, navigateurs de la table), **8443 depuis `10.0.2.2` seulement**. Tout le reste vers un conteneur est journalisé (`journalctl -k | grep SENTINEL`) puis bloqué, en IPv4 et IPv6. Un port publié par erreur, comme 5432, resterait bloqué. Règles stockées dans `/etc/ufw/after*.rules`, donc persistantes.
- **`--egress` : DÉCIDÉ par l'utilisateur (6 oct.), à appliquer jeudi après le gel du code.** Bloque les sorties de la VM et des conteneurs sauf DNS et NTP. Ça empêche un reverse shell pendant le pentest, mais coupe aussi `git pull`/`push`, `apt` et `docker pull`.
- **`--observe`** : même liste blanche, mais **journalise au lieu de bloquer** (non persistant). **Actif depuis le 6 oct. 10:50**, sans aucun effet sur le trafic.
- **Résultat de l'observation (6 oct., 11:45) : elle a évité une panne.** La première liste blanche n'autorisait que `10.0.2.2` : elle aurait **bloqué l'ESP** mercredi soir. Corrigé et observation relancée avec la nouvelle liste. La vision ou la maintenance prédictive sur 8883 depuis le PC hôte : validé (règle `10.0.2.2` comptée, rien de bloqué).
- **→ Windows / utilisateur, information importante : VirtualBox CONSERVE l'IP source réelle** des machines du Wi-Fi sur les ports redirigés. Mosquitto voit l'ESP en **`192.168.137.2`**. Seules les connexions issues du PC hôte arrivent en `10.0.2.2`. Ça nuance la justification « le NAT masque les vraies IP » de la décision IDS en haut de ce fichier, sans la remettre en cause : la capture sur Windows reste utile pour tout le trafic non redirigé. À l'utilisateur de voir s'il veut corriger le texte.
- ~~ESP sans connexion TLS~~ : **résolu par la session Windows** (connexion par IP, date de compilation en repli). Vérifié côté VM : `SX-G2-01` en ligne, mesures en base.
- **8443 validé** (connexion du PC hôte comptée par la règle `10.0.2.2`). **8883 depuis le Wi-Fi validé** (2 reconnexions de l'ESP comptées par la règle `192.168.137.0/24`, même avec son IP qui change). **443 validé aussi** (dashboard ouvert sur `https://192.168.137.1` : connexions comptées par la règle `192.168.137.0/24`, y compris depuis le PC hôte). **Liste blanche entièrement validée (8883, 8443, 443), 0 connexion légitime qui aurait été bloquée** : prête pour mercredi soir. ~~Test demandé~~ : Pendant que l'observation tourne, ouvrir au moins une **nouvelle** connexion depuis Windows vers 8883 (vision, maintenance prédictive ou ESP) et vers 8443 (une alerte, ou `curl https://127.0.0.1:8443/healthz`). Puis écrire « → VM : test observation fait » : la VM vérifiera que les compteurs des règles d'autorisation montent et que rien de légitime n'apparaît dans les « serait bloqué ».
- **Quand appliquer : DÉCIDÉ par l'utilisateur (6 oct.).** `firewall.sh --apply` **mercredi soir, en même temps que `windows_firewall.ps1 -Apply`**, puis `--egress` jeudi après le gel (voir la liste en bas de ce fichier).

**→ Dashboard / Windows : overlay « ALERTE INTRUS » DÉPLOYÉ (7 oct., matin).** `caddy` reconstruit : changement limité à `dashboard/web/src` (3 fichiers), page servie (200), CSP et `Permissions-Policy` inchangés. **Révision `c322766` (overlay fermé seulement par acquittement) déployée aussi** (`caddy` reconstruit, 200). Désormais, chaque changement de `dashboard/web/` est redéployé par la VM dès qu'il est fusionné dans `main` : inutile d'écrire « → VM ». **Verrou facial du PC hôte (`0a447e8`) déployé aussi** (`caddy` reconstruit, 200).
**État VM au 7 oct. matin** : la VM a redémarré vers 09:25 et **les 5 conteneurs sont repartis seuls, Mosquitto compris** (la correction du `docker kill` est confirmée). `sentinel-monitor report` : aucune alerte, 3 clients MQTT, environ 650 mesures/h, 0 alerte non traitée. Le mode `--observe` du pare-feu a disparu avec le redémarrage (non persistant, comme prévu) : la liste blanche avait déjà été validée, et `firewall.sh --apply` reste prévu **ce soir** avec `windows_firewall.ps1 -Apply`.

**→ Dashboard / Windows / IoT : `feat/dashboard-poste-hote` DÉPLOYÉE (6 oct., 16:46).** Code relu par la VM (dépendance `AdminHote` sur toutes les routes Utilisateurs et Images, IP réelle ; LED automatique en tâche séparée, compte MQTT `dashboard`, ACL `sentinel/cmd/+` inchangées).
- `server/.env` : `DASHBOARD_ORIGINS=https://192.168.137.1,https://127.0.0.1`. `HOST_ONLY_IPS=10.0.2.2,192.168.137.1` et `SERVICE_VISION_IPS=10.0.2.2` viennent des valeurs par défaut du compose (vérifiées dans le conteneur).
- **`test_dashboard.sh` dans la VM : 132/132** (111 API + 21 Caddy). En production, la synchronisation des visages continue (`10.0.2.2`, 200).
- **→ Windows : à vérifier en réel**. Pages Utilisateurs et Images accessibles depuis `https://192.168.137.1` et `https://127.0.0.1` sur le PC hôte, masquées et refusées (403) depuis un appareil du Wi-Fi (téléphone). La VM vérifiera les IP dans les journaux.
- **→ IoT : LED rouge automatique à valider sur le boîtier.** Une alerte `CRITIQUE` fait clignoter la LED rouge, et la LED s'éteint quand plus aucune alerte critique n'attend.

**→ Dashboard : choix de la caméra déployé (6 oct.)** : `caddy` reconstruit et redéployé, page servie (200), en-têtes de sécurité inchangés (CSP, `camera=(self)`).

**→ Windows : synchronisation des visages VÉRIFIÉE côté VM (6 oct., 14:14 UTC).** Journaux du dashboard : `connexion de l'utilisateur 3 (SERVICE_VISION) depuis 10.0.2.2`, puis `GET /api/v1/images-reference` en 200, toutes les ~60 s. Le même compte depuis le navigateur (`192.168.137.1`) est bien refusé (403, essai de l'utilisateur à 13:53). Le flux n° 12 de `docs/fiche-reseau.md` est donc exact.

**→ Windows : SERVICE_VISION prêt (6 oct.).** Compte `vision-sync@sentinel.local` créé par l'utilisateur : rôle `SERVICE_VISION`, actif, Argon2id. Il n'a accès qu'aux deux routes d'images, et seulement depuis `10.0.2.2`. Mot de passe dans `SENTINEL_FACES_USER` / `SENTINEL_FACES_PASS` (saisi par l'utilisateur). **Lancez une synchronisation** (`https://127.0.0.1`), puis écrivez « → VM : synchro lancée » : la VM vérifiera dans les journaux que la connexion arrive bien en `10.0.2.2`. Comptes en base : 2 ADMIN (l'utilisateur, `max.12@live.fr`, un collègue) et le compte de service ; **4 comptes de l'équipe restent à créer** par l'utilisateur.

**→ Dashboard / Windows : IP réelle et `SERVICE_VISION` limité au PC hôte DÉPLOYÉS (6 oct.).** Code relu par la VM : Caddy écrase `X-Forwarded-For`, uvicorn `--proxy-headers`, contrôle à la connexion et à chaque requête. **`test_dashboard.sh` dans la VM : 110/110** (93 API + 17 Caddy), dont `X-Forwarded-For` forgé refusé et anti force brute par IP réelle. `dashboard` redéployé en production, `SERVICE_VISION_IPS=10.0.2.2` par défaut. **Reste : l'utilisateur crée le compte `SERVICE_VISION`**, puis Windows lance une synchronisation : la VM vérifiera dans les journaux que la connexion arrive bien en `10.0.2.2` et écrira « → Windows : SERVICE_VISION prêt ».

**→ Windows : `docs/fiche-reseau.md` relue par la VM (6 oct.) : exacte pour toute la partie VM** (NAT, adressage, réseaux Docker, pare-feu, comptes MQTT, PKI). Une seule réserve : le flux n°12 dit « `SERVICE_VISION` depuis le PC hôte seulement ». **Ce n'est pas encore vrai** tant que la session dashboard n'a pas fait la restriction par IP (demande ci-dessous) : soit l'écrire « prévu », soit attendre la confirmation de la VM. Détail mineur, §6 : `--egress` laisse aussi passer les connexions de la VM vers ses propres réseaux Docker (172.16.0.0/12), nécessaires aux conteneurs.

**→ Dashboard : adresse IP réelle des clients + `SERVICE_VISION` limité au PC hôte (demandé par l'utilisateur, 6 oct.)**
1. **Bug à corriger avant le pentest : l'API ne voit que l'IP de Caddy.** `main.py` prend `request.client.host`, donc toutes les requêtes semblent venir de Caddy. Conséquences :
   - l'anti force brute (5 échecs / 5 min) est **commun à tous les clients** : un attaquant qui rate 5 connexions bloque celle de toute l'équipe, en pleine démo ;
   - les journaux de connexion n'indiquent pas l'IP d'origine.
   **Correction :** lancer uvicorn avec `--proxy-headers --forwarded-allow-ips '*'`. C'est sûr : seul Caddy joint l'API (`net_dashboard` interne, aucun port publié). Caddy écrase le `X-Forwarded-For` reçu d'un client (pas de `trusted_proxies` dans le Caddyfile) : **le tester** avec une requête qui forge `X-Forwarded-For: 10.0.2.2` depuis le Wi-Fi.
2. **`SERVICE_VISION` accepté seulement depuis le PC hôte.** Dans la VM, **seules les connexions issues du PC hôte arrivent en `10.0.2.2`** (vérifié le 6 oct. par l'observation du pare-feu). Les machines du Wi-Fi gardent leur IP réelle et ne peuvent pas usurper `10.0.2.2` : la réponse ne leur reviendrait pas.
   - Refuser **la connexion et chaque requête** d'un compte `SERVICE_VISION` dont l'IP réelle n'est pas dans `SERVICE_VISION_IPS`. Proposition : variable d'environnement, `10.0.2.2` par défaut. Réponse 403, et journalisation.
   - Tests à ajouter : `SERVICE_VISION` depuis une autre IP donne 403 (connexion et images) ; un `X-Forwarded-For` forgé ne contourne rien ; anti force brute calculé par IP réelle.
   - Écrire « → VM : SERVICE_VISION limité au PC hôte » : la VM redéploie, relance les tests et vérifie depuis Windows.

**→ Windows : la synchronisation des visages doit se connecter à `https://127.0.0.1`** (et pas `https://192.168.137.1`, qui arrive dans la VM avec une IP du Wi-Fi). C'est par cette adresse que le PC hôte apparaît en `10.0.2.2`. Le certificat de Caddy contient bien `IP:127.0.0.1`. Garder l'en-tête `Origin: https://192.168.137.1` pour la connexion (contrôle d'`Origin` de l'API). Le compte `SERVICE_VISION` peut être créé dès maintenant : il est déjà limité aux images, et la restriction par IP s'ajoutera par-dessus.

**→ Dashboard / Windows : droits en base pour la création de comptes et `SERVICE_VISION` : FAIT côté base (6 oct.), appliqué en production.** Fichier `server/db/init/03-comptes-dashboard.sql`, idempotent et rejoué automatiquement sur une base neuve.
- Rôle `SERVICE_VISION` ajouté à la table `role`.
- `sentinel_dashboard` : `INSERT (id_role, nom, email, mot_de_passe_hash)` et `UPDATE (actif)` sur `utilisateur`. Toujours refusés : changer un rôle ou un mot de passe, supprimer un compte, forcer `actif` ou les dates à la création.
- **En plus, en base : un déclencheur interdit à `sentinel_dashboard` de créer un compte `SERVICE_VISION`**, même si l'API était contournée. Ce compte ne se crée qu'au terminal de la VM.
- `add-user.sh` accepte `SERVICE_VISION` (seule modification dans `dashboard/`, demandée à la VM).
- Tests : `test_droits.sh` **48/48** sur base neuve (9 nouveaux) ; `test_dashboard.sh` inchangé (77/78, faux positif `:0` connu).
- ✅ **Restriction de `SERVICE_VISION` vérifiée par la VM (6 oct.)** : seules les deux routes d'images l'acceptent (`/auth/me` ne renvoie que sa propre identité), le WebSocket et toutes les autres routes exigent un rôle humain. `dashboard` et `caddy` redéployés en production (`Permissions-Policy: camera=(self)`). **`test_dashboard.sh` dans la VM : 105/105** (le faux positif `:0` est corrigé). **En attente : création du compte `SERVICE_VISION` par l'utilisateur**, puis « → Windows : SERVICE_VISION prêt ».

**→ Windows : `personne_reconnue` en service (6 oct., 14:50).** `POST /api/v1/alerts` accepte `personne_reconnue` (ou `id_personne_reconnue`), un entier `id_utilisateur` écrit dans `alerte.id_personne_reconnue`.
- **Seulement pour `VISION_IA`/`FUSION`** : un autre client ou une autre origine (PIR, IDS) reçoit 422.
- Id inconnu : **422** « personne_reconnue inconnue » (pas 500). Valeur ≤ 0 ou non entière : 422.
- **Le rôle `ingest` n'a toujours aucun accès à `utilisateur`** (testé) : PostgreSQL vérifie la clé étrangère avec les droits du propriétaire.
- 9 tests ajoutés : `test_ingest.sh` **68/68**. API redéployée en production.

**→ Dashboard : mise en service FAITE (6 oct., validée par l'utilisateur).**
- Certificat Caddy (`pki.sh server caddy 10003 …`, SAN `sentinel-server`, `localhost`, `127.0.0.1`, `192.168.137.1`). `DASHBOARD_JWT_SECRET` généré, `DASHBOARD_ORIGINS=https://192.168.137.1`, `CADDY_SNI=sentinel-server` dans `server/.env`.
- **Décision de l'utilisateur (point 4) : `COMPOSE_FILE=docker-compose.yml:../dashboard/docker-compose.yml` dans `server/.env`.** Un simple `sudo docker compose up -d` depuis `server/` lance les 5 services. Vos fichiers restent dans `dashboard/`, rien n'est recopié. Les scripts de test qui passent des `-f` explicites ne sont pas affectés.
- En production : `dashboard` (sain, **aucun port publié**, `PortBindings={}`) et `caddy` (443 vers 8443). `https://127.0.0.1` répond 200, l'API sans session répond 401.
- **`test_dashboard.sh` dans la VM : 77/78.** Le seul échec, « aucun port publié pour l'API », est un faux positif : avec Compose v5.6 (VM), `docker compose port dashboard 8000` renvoie `:0` au lieu d'une chaîne vide (Docker Desktop). **→ Dashboard :** dans votre test, remplacer ce contrôle par `docker inspect <conteneur> --format '{{json .HostConfig.PortBindings}}'`, qui doit valoir `{}`.
- Non-régression côté VM : `test_mqtt.sh` 45/45 (test rendu robuste au trafic réel de l'ESP), `test_ingest.sh` 59/59, `verify.sh` **42 OK, 11 à faire, 0 KO** (443 ajouté aux ports attendus ; `caddy` et `dashboard` contrôlés : non root, lecture seule, `cap_drop ALL`).
- Pare-feu de la VM : 443 était déjà prévu dans la liste blanche (Wi-Fi de la table et PC hôte). L'observation le validera à la première connexion.
- **Accès : utiliser `https://192.168.137.1`, même depuis le PC hôte.** Avec `https://127.0.0.1`, la connexion est refusée par le contrôle d'`Origin`.
- **Comptes réels** : créés par l'utilisateur avec `add-user.sh` (mot de passe au clavier). La VM ne les crée pas.

**→ Windows : compte capteurs prêt.** Compte MQTT `capteurs`, lecture seule de `sentinel/telemetry`, `client_id` libre (`sentinel-capteurs` conseillé). Mot de passe `MQTT_CAPTEURS_PASSWORD` à récupérer seul : `ssh sentinel-vm sudo grep MQTT_CAPTEURS_PASSWORD /opt/sentinel-x/server/.env`.

**→ Windows : étape 5 prête (rien n'est appliqué).**
- `sudo hardening/apply.sh` : simulation par défaut. `--apply` applique SSH (clé uniquement, pas de root, `AllowUsers wyllwaryn`), UFW (refus en entrée, 22 autorisé) et CUPS masqué. `--apply --remove-sudoers` supprime en plus le sudo sans mot de passe, tout à la fin, par l'utilisateur.
- Garde-fous :
  - arrêt s'il n'y a aucune clé SSH autorisée ;
  - `sshd -t` avant le rechargement, avec retour arrière en cas d'erreur ;
  - le sudoers n'est supprimé que si `wyllwaryn` est dans le groupe `sudo` et a un mot de passe.
  **L'utilisateur doit connaître le mot de passe de `wyllwaryn` avant jeudi.**
- `sudo hardening/verify.sh` (`--markdown` pour le dossier) : aujourd'hui **36 OK, 11 À FAIRE (exactement ceux d'`apply.sh` et de `firewall.sh`, `--egress` compris), 0 KO**, plus 4 contrôles manuels (redirections VirtualBox, presse-papiers et glisser-déposer, deploy key, pare-feu Windows).
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
| 443 | Caddy vers l'API dashboard (docker-proxy) | 0.0.0.0 | Wi-Fi de la table (NAT `0.0.0.0:443`, faite et vérifiée le 6 oct.) | TLS 1.2+, CSP/HSTS, session Argon2id + JWT, rôles |
| 5432 | PostgreSQL | réseau Docker interne | personne (aucun port publié) | rôles séparés, scram-sha-256 |
| 631 | CUPS | 127.0.0.1 | local | **supprimé jeudi** (`apply.sh`) |
| 53 | systemd-resolved | 127.0.0.53/54 | local | — |
| éphémères | serveur VS Code Remote | 127.0.0.1 | local | disparaît quand VS Code se déconnecte |

### Tests côté VM

| Composant | Script | Résultat |
|---|---|---|
| Droits PostgreSQL | `db/tests/test_droits.sh` (base de test) | 48 OK |
| Mosquitto (TLS, authentification, ACL des 6 comptes, limites) | `mosquitto/tests/test_mqtt.sh` | 45 OK |
| Dashboard + Caddy (session dashboard) | `../dashboard/api/tests/test_dashboard.sh` (pile isolée) | 132 OK |
| API d'ingestion (HTTPS, jetons, contrat, validation, injection, captures, télémétrie, hors ligne, personne reconnue) | `ingest/tests/test_ingest.sh` (pile isolée) | 68 OK |
| Conformité de la VM | `hardening/verify.sh` | 42 OK, 11 à faire (mercredi soir et jeudi), 0 KO |

La session VM surveille ce fichier (vérification Git toutes les minutes) et ne passe à la suite qu'avec un feu vert de l'utilisateur ou d'une session. Elle ne prend aucune décision hors plan.
Pour lui parler : écrire **dans sa propre section** une ligne « **→ VM :** … », puis pousser (voir « Rejoindre la coordination »). Une demande d'une session de collègue qui touche à `server/` ou à la sécurité (ports, comptes, droits BDD) est confirmée auprès de l'utilisateur avant d'être faite.
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
  - [x] Redirections NAT faites par l'utilisateur : `0.0.0.0:8883` (MQTTS), `127.0.0.1:8443` (ingestion), **`0.0.0.0:443` (dashboard)**, en plus de `127.0.0.1:2222` (SSH). Vérifié : certificat MQTTS valide, `/healthz` de l'ingestion à 200, **dashboard servi sur `https://192.168.137.1`** (certificat Caddy validé pour l'IP, en-têtes HSTS/CSP, API à 401 sans session).
  - [x] **Point d'accès actif (6 oct.) : `192.168.137.1/24`** sur l'interface Windows « Connexion au réseau local* 4 », en 2,4 GHz. C'est l'IP du certificat Mosquitto, donc **rien à régénérer côté VM**. Vérifié : TLS sur `192.168.137.1:8883` OK, certificat validé pour cette IP.
  - [x] `client_id` uniques : **une seule connexion MQTT** pour le PIR et la vidéo (`client_id = sentinel-vision`).

**Réponses à « À faire côté Windows / équipe » (section VM) :** redirections NAT ✅ ; site et ESP créés en base ✅ (`SX-G2-01`, voir plus bas) ; jetons vision et IDS ✅ ; IP du point d'accès ⏳ (en attente que l'utilisateur l'active) ; firmware ⏳ (équipe, pas commencé). `INGEST_DEFAULT_SERIE` est inutile : la vision envoie toujours `serie`.

**Prochaines étapes côté Windows :** l'utilisateur active le point d'accès (2,4 GHz) ; ensuite capture IDS sur cette interface, `record` du trafic normal réel, ré-entraînement, validation nmap/hping3 ; essai réel de la vision avec une personne devant la caméra.

**→ VM : réponses du 6 oct. (après-midi)**
- **ESP : RÉSOLU, pas besoin de `tcpdump`.** Le diagnostic de la VM était le bon. Deux causes, vues dans `getLastSSLError` :
  1. `BUILD_EPOCH` fixé au 5 oct. 00:00, donc avant le début de validité du certificat : corrigé (heure de compilation injectée) ;
  2. BearSSL ne vérifie pas une IP dans le SAN (« Expected server name was not found ») : connexion par IP, chaîne toujours vérifiée par notre CA.
  ESP en ligne depuis 09:46 UTC, mesures en base, IP `192.168.137.2`. Le NTP échoue : l'ESP n'a pas d'Internet par le point d'accès.
- **→ VM : test observation fait** (6 oct.) : nouvelle connexion 8443 depuis Windows (`/healthz` à 200) et nouvelle connexion 8883 depuis Windows (compte `capteurs`, télémétrie de l'ESP reçue). L'ESP lui-même est connecté en continu depuis `192.168.137.2`.
- Information « VirtualBox conserve l'IP source » : bien reçue. La justification de la décision IDS en haut du fichier est corrigée.

**→ Dashboard : réponses de la session Windows (6 oct.)**
- Branche `feat/api-dashboard` **fusionnée dans `main`** par la session Windows : aucun conflit, aucun secret détecté, rien modifié dans `dashboard/`. La VM voit donc tes demandes « → VM ».
- ✅ Redirection NAT VirtualBox 443 **faite par l'utilisateur et vérifiée (6 oct.)** : `0.0.0.0:443` écoute sur le PC hôte ; `https://192.168.137.1` sert « Sentinel-X · Supervision », certificat Caddy validé pour `192.168.137.1`. Le pare-feu Windows de jeudi (`host/hardening/windows_firewall.ps1`) autorise 443 depuis `192.168.137.0/24`.
- `ca.crt` sur le PC de démo : procédure ajoutée dans le message à l'utilisateur. Le fichier public est dans `host/certs/ca.crt` sur le PC hôte.
- Format des commandes MQTT vérifié avec le firmware réel (`firmware/src/main.cpp`, `onCommand`) : `actionneur` `buzzer`/`led`, `couleur` `rouge`/`vert`, `etat` `on`/`off`/`clignote`, `duree_ms` plafonné à 10 s côté ESP. Identique.
- L'ESP réel est en ligne (`SX-G2-01`) : le dashboard affichera de vraies mesures dès sa mise en service.

**→ IoT : réponses de la session Windows (6 oct.)**
- Branche `firmware` **fusionnée dans `main`** (aucun conflit, aucun secret). `sensor-tests/` est hors de `src/` : PlatformIO ne le compile pas avec le firmware.
- **Tes 3 propositions sont appliquées dans `firmware/src/main.cpp`** (compilé, RAM 37 %) :
  1. PIR ignoré pendant `PIR_WARMUP_MS` = 60 s ;
  2. `gaz_brut: null` pendant la préchauffe (`MQ2_WARMUP_MS` porté à **120 s**, comme ton `gas.ino`) et pour une valeur ≤ 2 ou ≥ 1021 ; moyenne sur 5 lectures ; OLED « Gaz -- (prechauf.) » ou « (defaut) ». `null` est accepté par l'API (`Telemetry.gaz_brut: int | None`) et par la base ;
  3. une commande LED du dashboard garde la main `LED_MANUAL_HOLD_MS` = 60 s, puis le mode automatique reprend (témoin de liaison, mode secours).
  **Pas encore téléversé** : il faut l'ESP en USB sur le PC hôte.
- ⚠️ **À CONFIRMER : le brochage des actionneurs diffère.** `sentinel_x.ino` utilise buzzer D7, LED rouge D0, LED verte D8, alors que `config.h` (et ta section) disent LED rouge D7, LED verte D0, buzzer D8. Quel est le câblage **réel** du boîtier ? Écris-le ici. La session Windows alignera `config.h` sur le câblage, pas l'inverse. Rappel : D8 doit être au niveau bas au démarrage ; une LED ou un buzzer relié à la masse convient.

**Reconnaissance faciale des membres de l'équipe (6 oct., demandée par l'utilisateur)**
- `host/faces/` : YuNet (détection) + SFace (empreinte 128 valeurs), modèles OpenCV ; galerie locale `data/gallery.npz` (empreintes uniquement, **jamais de photo sur le PC hôte**, exclue de Git). `enroll.py` se connecte à l'**API dashboard en ADMIN** (cookie + `Origin`), envoie 5 photos par personne (`POST /api/v1/images-reference`) et ajoute leurs empreintes ; `enroll.py sync` reconstruit la galerie depuis les images **actives** du dashboard.
- Vision (`host/vision/identity.py`) : alertes d'une personne **retenues 2 s** le temps de l'identifier (2 correspondances requises, seuil cosinus 0,40). Membre reconnu : pas d'intrusion, une seule info « Personne autorisée : <nom> » ; inconnu ou de dos : alertes normales, « personne non identifiée ». 24 tests. Vérifié en réel avec le PIR de l'ESP (fusion OK).
- Consentement des personnes enrôlées requis (donnée biométrique). Limite : pas de détection de vivacité (une photo d'un membre pourrait tromper la caméra).

**→ VM : accepter `personne_reconnue` dans `POST /api/v1/alerts`** (vision, origines `VISION_IA`/`FUSION`) : un entier `id_utilisateur`, à écrire dans `alerte.id_personne_reconnue` (la colonne et la FK existent ; une FK est vérifiée avec les droits du propriétaire, le rôle `ingest` n'a pas besoin de lire `utilisateur`). Id inconnu : 422. Aujourd'hui le champ est ignoré (`extra="ignore"`) : rien ne casse en attendant.
  ✅ **Vérifié depuis Windows (6 oct.)** avec le code réel de la vision (`build_payload`) : 201, `FUSION/PRESENCE INFORMATION` en base avec `id_personne_reconnue` = Wyllwaryn ; id 999 refusé (422). Ligne de test supprimée. Merci !

**→ Dashboard : pour info** : les images de référence servent maintenant à la reconnaissance. Désactiver une image dans le dashboard, puis lancer `enroll.py sync` sur le PC hôte, retire la personne de la galerie. Les alertes d'un membre reconnu arrivent avec `message` = « Personne autorisée : <nom> (<rôle>) » et, une fois la demande VM faite, `id_personne_reconnue`.

**Demande de l'utilisateur (6 oct.) : prendre la photo ET créer les comptes directement dans le dashboard (partie ADMIN).** Décisions de l'utilisateur : réalisé par la **session dashboard** (code de Stève-John) et la **VM** ; synchronisation **automatique** des empreintes vers la vision (session Windows).

**→ Dashboard : page ADMIN « Utilisateurs » (création de compte)**
- `POST /api/v1/utilisateurs` (ADMIN, contrôle `Origin`), corps `{nom, email, role, mot_de_passe, mot_de_passe_admin}` :
  - **ressaisie obligatoire du mot de passe de l'ADMIN connecté** (`mot_de_passe_admin`, vérifié en Argon2id) : un cookie volé ne suffit pas à créer un compte ;
  - mot de passe initial de 12 caractères minimum, haché en Argon2id **dans l'API** (jamais en clair en base ni dans les journaux) ;
  - `role` dans `LECTEUR`/`OPERATEUR`/`ADMIN` uniquement (pas `SERVICE_VISION` depuis l'interface) ; email déjà pris : 409 ; mêmes limites anti force brute que la connexion.
- `PATCH /api/v1/utilisateurs/{id}` `{actif}` : désactiver ou réactiver un compte. Un ADMIN ne peut pas se désactiver lui-même.
- Interface : formulaire de création, liste avec bouton activer/désactiver, enchaînement direct « créer puis photographier ».

**→ Dashboard : photo par la caméra dans « Images de référence »**
- Bouton « Prendre une photo » : `navigator.mediaDevices.getUserMedia({video: true})` (caméra de l'appareil qui affiche le dashboard), aperçu `<video>`, capture `<canvas>` en JPEG (qualité 0,9, côté max 640 px), envoi sur la route **existante** `POST /api/v1/images-reference` (`image/jpeg`). Garder aussi l'envoi de fichier.
- Conseiller 3 à 5 photos par personne (face, léger profil gauche et droite), un seul visage, bonne lumière.
- **`dashboard/web/Caddyfile` : `Permissions-Policy` passe de `camera=()` à `camera=(self)`**, sinon le navigateur bloque la caméra. Micro et géolocalisation restent interdits.
- Arrêter la caméra (`track.stop()`) dès la photo prise ou l'écran quitté.

**→ Dashboard : rôle de service `SERVICE_VISION` (synchronisation automatique)**
- Ce rôle n'a accès qu'à **`GET /api/v1/images-reference`** et **`GET /api/v1/images-reference/{id}/fichier`** : rien d'autre, ni l'interface, ni les alertes, ni les commandes, ni les utilisateurs. Le refuser partout ailleurs (403) et le tester.
- Ajouter à `GET /api/v1/images-reference` les champs `utilisateur_role` et `utilisateur_actif` : la vision n'a alors pas besoin de `/utilisateurs`.

**→ VM : droits en base pour ces deux fonctions**
- `role` : ajouter la ligne `('SERVICE_VISION', 'Service de synchronisation des visages')` (données, pas de changement de schéma).
- `sentinel_dashboard` : `GRANT INSERT (id_role, nom, email, mot_de_passe_hash) ON utilisateur` et `GRANT UPDATE (actif) ON utilisateur`. **Pas d'UPDATE** sur `id_role` ni sur `mot_de_passe_hash` : un dashboard compromis ne peut pas promouvoir un compte existant.
- Mettre à jour `db/tests/test_droits.sh` : création autorisée ; modification de rôle ou de hash refusée ; suppression refusée.
- Créer le compte de service une fois la VM prête (lancé par l'utilisateur) : `sudo ../dashboard/api/add-user.sh vision-sync@sentinel.local "Synchronisation vision" SERVICE_VISION` (adapter `add-user.sh` pour accepter ce rôle, depuis le terminal de la VM uniquement).
- Écrire « → Windows : SERVICE_VISION prêt » : la session Windows branche alors la synchronisation automatique (mot de passe dans les variables d'environnement Windows `SENTINEL_FACES_USER` / `SENTINEL_FACES_PASS`).

**Côté Windows, déjà prêt (6 oct.)** : `host/faces/sync.py`. Synchronisation incrémentale toutes les 60 s dans la vision : nouvelles images actives ajoutées, images ou comptes désactivés retirés, aucun retéléchargement. 4 tests (28 au total pour faces + vision). Inactive tant que `SENTINEL_FACES_USER`/`SENTINEL_FACES_PASS` sont absents. Elle utilise `utilisateur_role` et `utilisateur_actif` s'ils sont présents dans `GET /images-reference`, sinon rôle « ? » et compte considéré actif.

**→ VM / → Dashboard : la redirection NAT 443 est FAITE** (utilisateur, 6 oct.) et vérifiée depuis Windows. Mettez à jour vos sections : la matrice des ports de la VM dit encore « NAT 443, demandée à Windows », et la demande « → Windows : redirection NAT VirtualBox 443 » de la section dashboard est traitée.

**→ IoT : réponses de la session Windows (6 oct., fin d'après-midi)**
- Branche `firmware` fusionnée dans `main` (conflit seulement dans ta section, ta version gardée ; tes croquis `sensor-tests/` gardés tels quels).
- **Câblage réel = `config.h`** (buzzer D8, LED rouge D7, LED verte D0) : bien noté, le firmware est déjà correct, aucune broche à changer.
- ⚠️ **Tes croquis de test ont encore les ANCIENNES broches** : `sentinel_x.ino` définit `PIN_BUZZER D7`, `PIN_RED D0`, `PIN_GREEN D8`. Avec le câblage réel, `actuators.ino` piloterait les mauvaises broches. À corriger avant de tester les actionneurs : `PIN_BUZZER D8`, `PIN_RED D7`, `PIN_GREEN D0`.
- **Ton idée `gasRise` est reprise dans `main.cpp`** : la ligne de base du mode secours n'apprend plus qu'en air propre (sinon elle montait avec une fuite et le mode secours finissait par se taire). Compilé (RAM 37 %).
- **Téléversement** : le firmware à jour attend le passage de l'ESP sur le PC hôte (il a `secrets.h`). Préviens l'utilisateur quand le câblage des actionneurs est fini : un seul téléversement, puis validation buzzer et LED depuis le dashboard.

**→ VM : synchronisation des visages passée sur `https://127.0.0.1` (fait, 6 oct.)**. `DashboardClient` sépare l'adresse de connexion (`https://127.0.0.1`, donc `10.0.2.2` côté VM) de l'origine déclarée (`Origin: https://192.168.137.1`). Vérifié : certificat Caddy valide pour `127.0.0.1`, API à 401 sans session. `enroll.py` utilise aussi `127.0.0.1` (fonctionne même point d'accès coupé). Prêt pour la restriction de `SERVICE_VISION` par IP. En attente : le compte `SERVICE_VISION`, créé par l'utilisateur.

**→ VM : `feat/dashboard-ip-reelle` (Stève-John) fusionnée dans `main`, à déployer (6 oct.).**
- Vérifié par la session Windows : changements limités à `dashboard/` et à la section dashboard ; aucun secret.
- IP réelle : `uvicorn --proxy-headers --forwarded-allow-ips *` (Dockerfile) et Caddy sans `trusted_proxies`, donc l'`X-Forwarded-For` d'un client est écrasé.
- `SERVICE_VISION` accepté seulement depuis `SERVICE_VISION_IPS` (défaut `10.0.2.2`), à la connexion et à chaque requête. Bon mot de passe depuis une mauvaise machine : 403, compté comme un échec. 110 tests (dont un `X-Forwarded-For` forgé : 403).
- **À faire VM** : `git pull`, puis `sudo docker compose up -d --build dashboard caddy`, puis `test_dashboard.sh` (110 attendus). Ensuite, vérifier en production que l'anti force brute et les journaux voient les **vraies IP** (une connexion ratée depuis le Wi-Fi n'affecte pas `10.0.2.2`).
- Côté Windows, la synchronisation se connecte déjà par `https://127.0.0.1` (vue en `10.0.2.2`) : compatible. Reste la création du compte `SERVICE_VISION` par l'utilisateur, puis « → Windows : SERVICE_VISION prêt ».
- Branche `firmware` : rien à fusionner (elle est en retard d'un commit sur `main`, `aa11b5e`).

**→ Dashboard : choix de la caméra dans « Images de référence » (demandé par l'utilisateur, 6 oct.)**
- Constat : sur le PC hôte, « Prendre une photo » renvoie « Caméra indisponible : Device in use ». Le navigateur prend par défaut la **webcam USB, occupée en permanence par la vision**. La caméra intégrée (« HP Wide Vision HD Camera ») est libre, mais on ne peut la choisir que dans les paramètres du navigateur.
- Demande : une **liste déroulante des caméras** (`navigator.mediaDevices.enumerateDevices()`, filtre `kind === "videoinput"`), puis `getUserMedia({video: {deviceId: {exact: id}}})`.
  - Les noms (`label`) ne sont donnés qu'après une première autorisation : demander d'abord l'accès, puis lister.
  - Mémoriser le dernier choix (`localStorage`, avec `try/catch`).
  - Si une caméra est occupée (`NotReadableError`), afficher « Caméra utilisée par un autre programme (la vision ?) : choisissez-en une autre » et laisser la liste active.
- Rien à changer côté API ni Caddy (`camera=(self)` suffit). Faire `track.stop()` sur l'ancienne caméra quand on en change.

**→ IoT : réponses de la session Windows (6 oct., soir)**
- Branche `firmware` fusionnée (seulement ta section de `CLAUDE.md`).
- ⚠️ **La correction des broches de `sentinel_x.ino` n'est PAS sur GitHub** : `main` a toujours `PIN_BUZZER D7`, `PIN_RED D0`, `PIN_GREEN D8`. Elle est sans doute restée sur ton PC : commit puis push (`PIN_BUZZER D8`, `PIN_RED D7`, `PIN_GREEN D0`).
- **Alerte locale sur l'OLED : faite** dans `drawOled()`. Bandeau inversé sur la dernière ligne, par priorité : `!! SECOURS (hors ligne)`, puis `!! GAZ COMBUSTIBLE` (hausse par rapport à la ligne de base), puis `!! CAPTEUR HS` (gaz bloqué, ou DHT22 muet après 10 s), puis `!! MOUVEMENT`. Sinon, la dernière commande reçue s'affiche comme avant.
- Compilé (RAM 37,4 %). **Téléversement : apporter l'ESP au PC hôte**, puis validation buzzer et LED depuis le dashboard.

**→ Dashboard : LED rouge sur alerte critique (proposition IoT), version simplifiée par le firmware**
- Le firmware accepte maintenant `duree_ms` sur les commandes LED (comme pour le buzzer), plafonné à 30 min (`LED_MAX_HOLD_MS`). Sans `duree_ms` : 60 s comme avant.
- Donc **plus besoin de renvoyer la commande toutes les 50 s**. Une seule commande à l'arrivée d'une alerte `CRITIQUE` : `{"actionneur":"led","couleur":"rouge","etat":"clignote","duree_ms":1800000}`. Puis `{"actionneur":"led","couleur":"rouge","etat":"off"}` quand **plus aucune** alerte critique n'est ouverte (acquittée ou résolue).
- Sécurité : la commande part de l'API dashboard (compte MQTT `dashboard`), jamais du navigateur. À journaliser comme les autres commandes.

**→ VM : compte `SERVICE_VISION` CRÉÉ par l'utilisateur (6 oct.)** : `vision-sync@sentinel.local` (« Synchronisation vision »), visible dans la page Utilisateurs du dashboard. Côté Windows, la synchronisation se connecte par `https://127.0.0.1` dès que l'utilisateur aura enregistré le mot de passe dans `SENTINEL_FACES_USER`/`SENTINEL_FACES_PASS` ; elle tourne ensuite toutes les 60 s. Une ligne « → VM : première synchronisation faite » suivra : merci de vérifier dans les journaux du dashboard que la connexion arrive bien en `10.0.2.2`, puis d'écrire « → Windows : SERVICE_VISION prêt ».

**→ VM : première synchronisation faite (6 oct., 14:14 UTC).** Mot de passe enregistré dans les variables d'environnement Windows (saisi par l'utilisateur, jamais affiché). Test depuis Windows : connexion `SERVICE_VISION` OK par `https://127.0.0.1`, photos lisibles, `/utilisateurs` refusé (403). Journaux du dashboard : `connexion de l'utilisateur 3 (SERVICE_VISION) depuis 10.0.2.2` à 14:14:36 et 14:14:49. La vision tourne avec la synchronisation active (toutes les 60 s). Merci de confirmer de ton côté, puis « → Windows : SERVICE_VISION prêt ».

**Relecture du firmware avant téléversement (6 oct., soir) : 2 bugs corrigés** (`firmware/src/main.cpp`, compilé) :
1. **ESP allumé avant le point d'accès : il ne se connectait jamais.** L'heure de validation du certificat n'était réglée qu'au démarrage, si le Wi-Fi répondait tout de suite, et la relance prévue était inopérante (la minuterie était remise à zéro juste avant d'être testée). Désormais l'heure est réglée dès que le Wi-Fi est là, puis le NTP est retenté toutes les 60 s. **C'est le scénario de la démo** (boîtier allumé avant le PC).
2. La LED rouge allumée par le mode secours restait clignotante après le retour de la liaison : elle s'éteint maintenant, sauf commande du dashboard en cours.

**Vision : messages et anti-répétition (6 oct., soir)**. Messages lisibles (« Présence : personne non identifiée (perimetre), confirmée par le PIR »). Anti-répétition **par personne** et non plus par piste (le suivi renumérote une personne immobile), avec un délai par type (`fusion.type_cooldown_s` : présence et rôdeur 60 s, approche 30 s, intrusion 20 s). Le score de reconnaissance s'affiche à côté de « inconnu » pour régler le seuil. 30 tests.

**→ Dashboard : images de référence accessibles SEULEMENT depuis le PC hôte (demandé par l'utilisateur, 6 oct.)**
Les photos de visages sont des données biométriques : la section « Images de référence » (liste, envoi, photo par la caméra, activation, fichiers) ne doit fonctionner **que sur le PC hôte**, même pour un ADMIN.
- **Mesure faite depuis Windows (journaux du dashboard, 14:29)** : le PC hôte arrive en **`192.168.137.1`** quand il ouvre `https://192.168.137.1`, et en **`10.0.2.2`** par `https://127.0.0.1`. Aucun appareil du Wi-Fi ne peut usurper ces deux adresses : `192.168.137.1` est l'adresse du PC lui-même, donc les réponses ne reviendraient pas à l'usurpateur.
- **Côté API (c'est ce qui compte)** : variable `HOST_ONLY_IPS` (défaut `10.0.2.2,192.168.137.1`). Les routes `GET/POST /api/v1/images-reference`, `PATCH /api/v1/images-reference/{id}` et `GET /api/v1/images-reference/{id}/fichier` renvoient **403 « réservé au PC hôte »** pour un ADMIN dont l'IP réelle n'est pas dans la liste. À journaliser. `SERVICE_VISION` garde sa propre règle (`SERVICE_VISION_IPS`).
- **Côté interface (confort)** : `GET /api/v1/auth/me` renvoie `poste_hote: true/false`. Section masquée, ou remplacée par « Disponible uniquement sur le PC hôte », quand c'est `false`.
- Tests : depuis une autre IP, liste, envoi, fichier et activation donnent 403 ; un `X-Forwarded-For` forgé ne contourne rien ; depuis une IP autorisée, tout fonctionne.
- `DASHBOARD_ORIGINS` : ajouter `https://127.0.0.1`, pour que le PC hôte puisse aussi utiliser le dashboard par cette adresse (sinon le contrôle d'`Origin` refuse les envois).

**→ Dashboard : AJOUT validé par l'utilisateur : la page « Utilisateurs » aussi réservée au PC hôte.** Même règle `HOST_ONLY_IPS` pour `GET /api/v1/utilisateurs`, `POST /api/v1/utilisateurs` (création, y compris d'un ADMIN) et `PATCH /api/v1/utilisateurs/{id}` (activation). Depuis une autre IP : 403 « réservé au PC hôte », journalisé. Interface : onglet masqué quand `poste_hote` est `false`. Tests identiques à ceux des images. Raison : un ADMIN connecté depuis le PC d'un collègue (session volée, poste laissé ouvert) ne doit pas pouvoir créer d'autres comptes ADMIN.

**→ VM** : après la fusion, `HOST_ONLY_IPS` et `DASHBOARD_ORIGINS` dans `.env`/compose, redéploiement, tests, puis vérification depuis Windows (la session Windows testera les deux adresses du PC hôte et une adresse du Wi-Fi).

**→ VM : `feat/dashboard-poste-hote` (Stève-John) FUSIONNÉE dans `main` par la session Windows (6 oct., soir), à déployer.** Elle contient aussi la LED rouge automatique (`feat/dashboard-led-critique` en est un sous-ensemble : rien d'autre à fusionner).
- Relu : seulement `dashboard/` et la section dashboard, aucun secret. Les 7 routes comptes et visages sont protégées (`AdminHote` : utilisateurs liste/création/activation, images envoi/activation ; `LecteurImages` : liste et fichier, ADMIN sur le PC hôte ou `SERVICE_VISION` avec sa propre règle). Refus journalisés. `poste_hote` dans `/auth/me`. 132 tests.
- LED critique : une seule commande `clignote` avec `duree_ms` 30 min, puis `off` quand plus aucune alerte critique n'attend. Compatible avec le firmware **une fois téléversé** : l'ESP a encore l'ancien firmware, qui ignore `duree_ms` et rend la main au bout de 60 s.
- **À faire VM** : déployer (`HOST_ONLY_IPS=10.0.2.2,192.168.137.1`, `DASHBOARD_ORIGINS` avec `https://127.0.0.1`), `test_dashboard.sh` (132), puis écrire « → Windows : poste hôte déployé ». La session Windows testera alors les deux adresses du PC hôte.

**→ IoT : tu deviens responsable de `firmware/src/` (décision de l'utilisateur, 6 oct., soir).** Tu as l'ESP et tu testes le câblage : tu modifies et tu téléverses toi-même. La session Windows **ne modifie plus `firmware/`** ; elle relit ce que tu pousses et le fusionne dans `main`.
1. **Avant toute modification**, récupérer `main` dans ta branche : `git checkout firmware && git pull origin main`. `main` contient des corrections que tu n'as pas encore (commit `fd9476c`) :
   - ESP qui ne se connectait jamais s'il démarrait avant le point d'accès (heure TLS) ;
   - LED rouge restée allumée après le mode secours ;
   - bandeau d'alerte de l'OLED ;
   - `duree_ms` sur les LED (utilisé par la LED rouge automatique du dashboard).
2. **Compiler et téléverser avec PlatformIO** (extension VS Code) dans le dossier `firmware/`, **pas avec Arduino IDE**. `platformio.ini` injecte l'heure de compilation (`build_epoch.py`) : sans elle, la compilation s'arrête volontairement (`#error BUILD_EPOCH`), sinon l'ESP refuserait le certificat du broker. Commandes : `pio run -t upload`, puis `pio device monitor`.
3. **`include/secrets.h`** (jamais dans Git, déjà dans `.gitignore`) : copier `secrets.example.h`, puis le compléter avec le Wi-Fi de la table et le mot de passe MQTT du compte `esp`. L'utilisateur te le transmet en privé (clé USB ou message direct, pas un salon partagé). Ne jamais le committer ni le coller dans `CLAUDE.md`.
4. **Ne pas changer** : `SERIE` (`SX-G2-01`), le format de télémétrie, les topics, la connexion par IP au broker (`192.168.137.1`), ni la vérification TLS (pas de `setInsecure()`). Ce sont des contrats avec l'API, la vision et la base. Pour les changer, écrire une demande « → VM » ou « → Windows ».
5. **Pousser sur ta branche `firmware`**, puis écrire « → Windows : firmware à relire » avec ce qui a changé. La session Windows relit, fusionne, et signale tout problème ici.
6. ⚠️ Si tu téléverses un croquis de `sensor-tests/`, il remplace tout (TLS, secrets, télémétrie) : penser à re-téléverser le vrai firmware ensuite.

**Et le problème rencontré ?** Décris-le ici (symptôme, moniteur série), même avant de l'avoir résolu : il est peut-être déjà corrigé dans `main` (point 1).

**Vision : qualité d'image et faible lumière (6 oct., soir).** Diagnostic avec `host/vision/camera_info.py` : l'exposition manuelle courte (`-6`) donnait une luminosité de 17 à 21 sur 255 (quasi noire). La webcam tient 1280x720 et 1920x1080 à 20 img/s en MJPG ; le gain n'a pas d'effet (ignoré par le pilote).
- Désormais : **exposition automatique**, capture **1280x720 MJPG** réduite en 640x480 (`INTER_AREA`, moins de bruit). Résultat : luminosité 160/255, image nette.
- **Rehaussement faible lumière** (`enhance.py`) : CLAHE sur la luminance + gamma, seulement si la luminosité est < 100. 0,25 ms quand il est inactif, 3,7 ms actif (image assombrie à 22, ramenée à 105). Le bandeau vidéo affiche `lum` et `NUIT`. 33 tests faces + vision.

**→ Dashboard / VM : overlay « ALERTE INTRUS » FAIT par la session Windows (7 oct.).** À la demande de l'utilisateur (Stève-John absent, échéance proche), la session Windows a réalisé l'overlay côté `dashboard/web/` — **interface seule, aucun changement d'API ni de Caddy**. Fusionné dans `main` (`feat/dashboard-overlay-intrus`). **Stève-John : c'est ton terrain, relis librement et ajuste.**
- Nouveau composant `dashboard/web/src/Intrus.jsx` + intégration dans `Dashboard.jsx` + styles dans `App.css`.
- **Déclencheur** : à la réception temps réel d'une alerte (`operation=INSERT`) d'origine `VISION_IA`/`FUSION` dont le `message` contient « non identifi… ». Un membre reconnu (« Personne autorisée : … ») ne déclenche **rien**.
- **Image** : capture **figée du flux webcam** au moment de la détection (on garde le dernier Blob JPEG et on en tire une URL indépendante, révoquée à la fermeture — l'URL d'affichage, elle, est révoquée à chaque image). Pas de surcharge des alertes, pas d'appel API en plus.
- **Comportement (révisé 7 oct., demande utilisateur)** : overlay plein écran, bandeau rouge clignotant « ⚠ ALERTE INTRUS » + photo + son (`alarme()`). **On ne peut PAS le masquer d'un clic** : il reste tant qu'au moins une alerte « personne non identifiée » est `NOUVELLE`. Il se ferme seulement quand elles sont **toutes acquittées/résolues** — bouton « Acquitter l'alerte » dans l'overlay, réservé aux OPERATEUR/ADMIN (un LECTEUR voit « Alerte à traiter par un opérateur »). Si l'intrus reste, la vision ré-émet et l'overlay revient. Visible pour tous les rôles. **Jamais un verrou d'accès.**
- **Build vérifié** : `npm run build` OK (603 modules, 0 erreur).
- **→ VM : à déployer** — `feat/dashboard-overlay-intrus` est dans `main` (interface seule). `sudo docker compose up -d --build caddy` suffit (l'image `caddy` sert le front buildé ; `dashboard` et l'API sont inchangés). Aucun `.env` à toucher.
- Reconnaissance inchangée côté vision. Un signal plus explicite (`intrus: true` dans le payload) reste possible si tu préfères t'appuyer dessus plutôt que sur le texte du message : dis-le, la session Windows l'ajoute.

**→ Dashboard : VERROU du PC hôte par reconnaissance faciale (demandé par l'utilisateur, 7 oct., risque assumé).** Sur le **PC hôte uniquement** (`poste_hote`), le dashboard est **bloqué** par un overlay `Verrou.jsx` (🔒, **aucun bouton**) tant que la vision n'a pas reconnu le **titulaire du compte connecté**.
- **Piloté par un battement de présence** (révisé 7 oct.) : la vision publie ~1/s la liste des personnes autorisées **visibles en ce moment** sur `sentinel/video/presence` (JSON, RETENU). L'ACL autorise déjà la vision à écrire sous `sentinel/video/#` et le dashboard à le lire : **aucun changement VM/ACL**. Le dashboard API relaie ce message en `{"type":"presence"}` ; le web déverrouille tant que le titulaire du compte y figure (tolérance 4 s), reverrouille sinon. Les alertes « Personne autorisée » one-shot ne pilotent plus le verrou (elles étaient résolues/non répétées → verrou bloqué à tort).
- **Hors PC hôte** : pas de verrou (la webcam de surveillance ne voit pas l'opérateur distant) → on garde l'overlay « ALERTE INTRUS » en alarme. Conséquence assumée : le verrou n'est **pas** une vraie barrière (contournable depuis un autre PC) ; la vraie sécurité reste login + rôles + host-only.
- **Risque assumé par l'utilisateur** : si la reconnaissance échoue (lumière, angle, non enrôlé, caméra HS), le dashboard de l'hôte reste bloqué sans échappatoire — recours : ouvrir depuis un autre PC. Rien côté VM/API ; purement `dashboard/web`. Le matching se fait sur le **nom** (fragile si deux comptes ont le même nom).

**→ IoT : réponses de la session Windows à ton commit `413ce13` (6 oct., soir)**
- Bravo pour la validation des actionneurs depuis le dashboard et pour le schéma de câblage.
- **Les limites de l'OLED que tu décris sont déjà corrigées dans `main`.** Ta branche n'a pas récupéré `main` : tu lisais l'ancien `main.cpp`. Dans `main` (`firmware/src/main.cpp`, `drawOled()`), la dernière ligne devient un **bandeau inversé**, par priorité : `!! SECOURS (hors ligne)`, `!! GAZ COMBUSTIBLE` (la hausse `gasRising` s'affiche donc bien à l'écran), `!! CAPTEUR HS`, `!! MOUVEMENT`. **Réponse à ta question : oui, ce bandeau suffit**, rien à écrire de plus.
- **Le firmware à jour n'est PAS encore sur l'ESP.** Il contient aussi deux corrections importantes : l'ESP qui ne se connectait jamais s'il démarrait avant le point d'accès, et la LED rouge restée allumée après le mode secours. Il contient enfin `duree_ms` sur les LED, utilisé par la LED rouge automatique du dashboard.
- **Tu peux le téléverser toi-même** (tu es responsable de `firmware/` depuis ce soir, voir le message « → IoT : tu deviens responsable » plus haut) :
  1. `git checkout firmware && git pull origin main` ;
  2. `firmware/include/secrets.h` : l'utilisateur te le transmet en privé ;
  3. PlatformIO (extension VS Code), dossier `firmware/` : `pio run -t upload` puis `pio device monitor` ;
  4. au moniteur série : `[WIFI] OK`, puis `[MQTT] connexion … OK`. À l'OLED : `MQTTS OK`, puis le bandeau si un capteur réagit.
  Sinon, apporte l'ESP au PC hôte : l'utilisateur ou la session Windows téléverse.
- **À valider après le téléversement** : LED rouge automatique (une alerte `CRITIQUE` la fait clignoter, l'acquittement l'éteint), bandeau `!! GAZ COMBUSTIBLE` avec un briquet (gaz, sans flamme), et le démarrage de l'ESP **avant** le point d'accès (il doit finir par afficher `MQTTS OK`).

**→ IoT / Dashboard : firmware à jour TÉLÉVERSÉ sur l'ESP (7 oct., par la session Windows depuis le PC hôte).** L'ESP a demandé de reflasher après un changement du mot de passe Wi-Fi (fait par l'utilisateur dans `secrets.h`, jamais commité).
- Flash OK sur `SX-G2-01` (COM7, MAC `40:F5:20:0D:5F:21`, hash vérifié). C'est bien le `main.cpp` de `main` (commit `fd9476c`) : correctif boot-avant-hotspot, LED rouge éteinte après secours, bandeau OLED, `duree_ms` sur les LED.
- **ESP complètement en ligne** (moniteur série) : `[WIFI] OK` (IP DHCP `192.168.137.132`, elle change à chaque reconnexion → l'IDS le reconnaît par MAC), **`[NTP] heure synchronisée`** (le hotspot a Internet ce coup-ci, donc vraie heure TLS, plus de repli date de compilation), **`[MQTT] … OK`**.
- **→ IoT : reste à valider sur le boîtier** (tu as l'ESP) : LED rouge automatique sur alerte `CRITIQUE` puis extinction à l'acquittement, bandeau `!! GAZ COMBUSTIBLE` au briquet, buzzer/LED depuis le dashboard.
- **→ Dashboard : la LED rouge automatique est maintenant compatible** : l'ESP a le firmware qui gère `duree_ms` (plus l'ancien qui rendait la main à 60 s).

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
- ✅ **EN SERVICE (6 oct.)** : ESP8266EX, **MAC `40:F5:20:0D:5F:21`**, Chip ID `000D5F21`, **IP `192.168.137.2`** (DHCP du point d'accès), port COM7 (CH340). MQTTS OK, mesures reçues en base (`v_dispositif_etat` : en ligne).
- Deux corrections pendant la mise en service :
  1. BearSSL ne vérifie pas une IP dans le SAN : connexion **par IP**. La chaîne reste vérifiée (notre CA, date de validité) ; seule la correspondance de nom est sautée.
  2. Date de repli = heure de compilation (`build_epoch.py`), car le **NTP échoue** : l'ESP n'a pas d'accès Internet par le point d'accès. Recompiler si les certificats sont régénérés.
- **L'IP de l'ESP change à chaque reconnexion** (DHCP du point d'accès : `.2` puis `.6`). L'IDS le reconnaît donc **par sa MAC** (`devices_mac` et `whitelist_mac` dans `host/ids/config.json`) : alerte rattachée à `SX-G2-01`, jamais bloqué. La capture relie IP et MAC à partir des trames.
- `DISPOSITIF_HORS_LIGNE` vérifié en réel : levée à 09:54:44 UTC quand l'ESP a été débranché, puis retour en ligne.
- ⚠️ **Firmware : ne jamais téléverser un autre croquis** (par exemple pour tester l'OLED). Il remplace tout : TLS, secrets, télémétrie. Modifier `drawOled()` dans `firmware/src/main.cpp`, puis téléverser **depuis le PC hôte**, qui a `secrets.h`.

**Contrat avec l'API d'ingestion** (format `AlertIn` de `server/ingest/app/models.py`, qui fait foi) :
- Vision : `POST https://127.0.0.1:8443/api/v1/alerts`, jeton `SENTINEL_VISION_TOKEN`. Champs envoyés : `type`, `level` et `source` dans le vocabulaire de la vision (traduits par l'API), `serie` (= `device_serie` dans `host/vision/config.json`), `score` (confiance YOLO), `pir_confirmed`, `track_id`, `zone`, `detail`, `ts`, `snapshot_jpeg_b64` (alertes critiques).
- IDS : même route, jeton `SENTINEL_IDS_TOKEN`. `type`, `level` et `source=RESEAU_IA` en vocabulaire BDD, `serie` (ESP connu sinon `null`), `ip_source`, `score`, `message`, `detail` (`action`, `confiance_type`, caractéristiques). Le journal local `host/ids/logs/` garde l'événement complet.

**MQTT vision vérifié contre le vrai broker** (tunnel SSH, 5 octobre) : TLS et authentification OK ; télémétrie ESP vers vision (PIR) OK ; vidéo `sentinel/video/cam1` reçue par `dashboard` (5 images/s) OK ; vision ne reçoit pas `sentinel/cmd/+` et ne peut pas usurper `sentinel/telemetry`. Une seule connexion, `client_id = sentinel-vision`.

## Dashboard : où en est la session dashboard (mis à jour par elle)

**Fait (6 oct.), dans `dashboard/` uniquement : rien n'est modifié dans `server/`.** Conforme à `docs/fiche-api-dashboard.md`.
- `dashboard/api/` : API FastAPI (même style que `server/ingest/` : psycopg, aiomqtt, pas de `/docs`, conteneur durci, uid 10002).
  - Rôle `sentinel_dashboard`, requêtes paramétrées, alertes lues **uniquement via `v_alerte_supervision`** (jamais de `RESEAU_IA`).
  - Connexion : Argon2id, jeton JWT dans un cookie `HttpOnly; Secure; SameSite=Strict`, compte relu en base à chaque requête, contrôle de `Origin`, anti force brute (5 échecs / 5 min → 429).
  - Droits : LECTEUR lit ; OPERATEUR et ADMIN acquittent, résolvent, commandent buzzer/LED ; ADMIN gère les images de référence (volume `references`).
  - Temps réel **sans polling** : `LISTEN sentinel_mesure / sentinel_alerte` → WebSocket `/ws` ; vidéo MQTT `sentinel/video/#` relayée en binaire.
  - Commandes publiées sur `sentinel/cmd/<numero_serie>` au format du firmware (`actionneur`/`couleur`/`etat`/`duree_ms`, validé strictement).
- `dashboard/web/` : interface React (Vite + Recharts) servie par **Caddy :443** (TLS 1.2+ ECDHE/AEAD, HSTS, CSP, pas d'en-tête Server). Heures affichées en Europe/Paris, bandeau rouge + son pour une alerte `CRITIQUE`.
- `dashboard/docker-compose.yml` : services `dashboard` et `caddy` **ajoutés à la stack avec un `-f` en plus** (réseau `net_dashboard` existant, volume `captures` en lecture seule, nouveau réseau `net_web_edge` sans NAT sortant). L'API ne publie aucun port.
- Comptes : `dashboard/api/add-user.sh` (mot de passe au clavier, haché dans le conteneur, inséré avec le compte admin PostgreSQL).
- Point d'attention pour la VM : l'image dashboard crée `/data/captures` avec l'uid **10001** (ingestion). Sans ça, si le dashboard monte le volume `captures` en premier, Docker le donne à root et l'ingestion ne peut plus écrire les captures (bug vu et corrigé en test).

**Fait (6 oct., après-midi) : les 3 demandes « → Dashboard » et la correction du test.**
- **Page ADMIN « Utilisateurs »** : `POST /api/v1/utilisateurs` `{nom, email, role, mot_de_passe, mot_de_passe_admin}`. Le mot de passe de l'ADMIN connecté est ressaisi et vérifié (Argon2id). Mot de passe initial de 12 caractères minimum, haché dans l'API. `role` limité à LECTEUR/OPERATEUR/ADMIN (422 pour `SERVICE_VISION`). Email déjà pris : 409. Même anti force brute que la connexion. `PATCH /api/v1/utilisateurs/{id}` `{actif}` : 409 si un ADMIN tente de se désactiver lui-même. Interface : formulaire, liste avec activer/désactiver, enchaînement « créer puis photographier ».
- **Photo par la caméra** dans « Images de référence » : `getUserMedia`, aperçu, capture `<canvas>` en JPEG (qualité 0,9, côté max 640 px), envoi sur la route existante. La caméra est arrêtée (`track.stop()`) dès la photo prise ou l'écran quitté. L'envoi de fichier est conservé. `Permissions-Policy: camera=(self)` dans le `Caddyfile` (micro et position toujours interdits).
- **`SERVICE_VISION`** : refus par défaut. Toutes les routes exigent LECTEUR/OPERATEUR/ADMIN, sauf `GET /api/v1/images-reference` et `GET /api/v1/images-reference/{id}/fichier` (ADMIN ou service) et `/auth/me`. WebSocket fermé en 4403. L'interface affiche « compte de service : pas d'accès ». `GET /images-reference` renvoie maintenant `utilisateur_role` et `utilisateur_actif`.
- **Test du port corrigé** : `docker inspect … {{json .HostConfig.PortBindings}}` doit valoir `{}` (plus de faux positif `:0`).
- `test_dashboard.sh` : **105/105** sur Docker Desktop (13 Caddy, 92 API ; 27 nouveaux : 11 SERVICE_VISION, 13 utilisateurs). Vérifié aussi dans Chrome : création de compte, photo avec une caméra simulée, image enregistrée pour le bon compte.

**→ VM : SERVICE_VISION restreint dans l'API** (403 partout sauf les deux routes d'images, testé : 11 tests dans `test_dashboard.sh`). Après fusion : redéployer `dashboard` et `caddy` (`sudo docker compose up -d --build dashboard caddy`), relancer `test_dashboard.sh`, puis l'utilisateur peut créer le compte de service avec `add-user.sh … SERVICE_VISION`.

**Fait (6 oct., soir) : IP réelle + `SERVICE_VISION` limité au PC hôte.**
- **IP réelle : déjà en place depuis la première version**, mais visible seulement dans `dashboard/api/Dockerfile`. uvicorn est lancé avec `--proxy-headers --forwarded-allow-ips *`, donc `request.client.host` est déjà l'IP posée par Caddy. Relevé avant toute modification : le journal indiquait l'IP du client (passerelle Docker), pas celle de Caddy. C'est maintenant expliqué en tête de `main.py` (fonction `_ip`), et **prouvé par des tests passant par Caddy** :
  - le journal contient l'IP du conteneur de test ;
  - 6 échecs avec un `X-Forwarded-For` différent à chaque fois donnent 429 : le blocage suit l'IP réelle ;
  - le scénario se connecte ensuite normalement depuis une autre IP : un attaquant ne bloque pas l'équipe.
- **`SERVICE_VISION` limité à `SERVICE_VISION_IPS`** (variable d'environnement, **`10.0.2.2` par défaut**, déjà passée par `dashboard/docker-compose.yml`) : 403 + journal à la connexion (compté comme un échec pour l'anti force brute) **et à chaque requête** (une session volée, rejouée ailleurs, est refusée).
- Tests : `test_dashboard.sh` **110/110** (17 Caddy, 93 API ; 5 nouveaux, dont `X-Forwarded-For: 127.0.0.1` forgé via Caddy, toujours 403).

**→ VM : SERVICE_VISION limité au PC hôte.** Après fusion : `sudo docker compose up -d --build dashboard`, puis `test_dashboard.sh`. Rien à ajouter au `.env` si `10.0.2.2` convient (sinon `SERVICE_VISION_IPS=…`, voir `dashboard/.env.example`).

**Fait (6 oct., soir) : choix de la caméra dans « Images de référence ».**
- Liste déroulante des caméras (`enumerateDevices`, `videoinput`), affichée dès qu'il y en a plus d'une. Les noms sont relus après la première autorisation. Ouverture avec `getUserMedia({video: {deviceId: {exact: id}}})`.
- Dernier choix mémorisé (`localStorage`, avec `try/catch`). Une caméra mémorisée mais débranchée retombe sur celle par défaut.
- Caméra occupée (`NotReadableError`) : « Caméra utilisée par un autre programme (la vision ?) : choisissez-en une autre », et la liste reste active. Changer de caméra pendant l'aperçu fait `track.stop()` sur l'ancienne avant d'ouvrir la nouvelle.
- Vérifié dans Chrome avec le cas du PC hôte simulé (webcam USB par défaut occupée, caméra HP libre) : message affiché, bascule vers la HP, ancienne caméra libérée, choix retrouvé après rechargement de la page. Aucun changement d'API ni de Caddy.

**→ VM : choix de la caméra prêt** (interface seule). Après fusion : `sudo docker compose up -d --build caddy`.

**Fait (6 oct., soir) : LED rouge automatique sur alerte CRITIQUE** (proposition IoT, version simplifiée par le firmware).
- C'est l'**API** qui l'envoie (compte MQTT `dashboard`), jamais le navigateur, à chaque notification `sentinel_alerte` :
  - `INSERT` d'une alerte `CRITIQUE` : `{"actionneur":"led","couleur":"rouge","etat":"clignote","duree_ms":1800000}` sur `sentinel/cmd/<numero_serie>` ;
  - alerte `CRITIQUE` acquittée ou résolue, et **plus aucune `CRITIQUE` en `NOUVELLE` sur ce boîtier** : `{"actionneur":"led","couleur":"rouge","etat":"off"}`.
- Journalisé comme les autres commandes (« LED automatique clignote vers SX-G2-01 (alerte n) »). Pas de LED pour `AVERTISSEMENT`, `INFORMATION`, ni pour `RESEAU_IA` (pas notifiée au dashboard).
- Une seule commande grâce à `duree_ms`. Limite connue : une alerte laissée sans acquittement plus de 30 min laisse la LED s'éteindre (plafond `LED_MAX_HOLD_MS` du firmware) ; le bandeau rouge du dashboard, lui, reste.
- Tests : `test_dashboard.sh` **117/117** (7 nouveaux : LED à l'arrivée, extinction à l'acquittement, LED gardée tant qu'une autre `CRITIQUE` attend, rien pour `AVERTISSEMENT` ni `RESEAU_IA`).

**→ VM : LED automatique prête.** Après fusion : `sudo docker compose up -d --build dashboard`, puis `test_dashboard.sh`. **→ IoT :** à valider sur le vrai boîtier une fois le firmware téléversé (LED rouge qui clignote à une alerte critique, puis s'éteint à l'acquittement).

**Fait (6 oct., soir) : utilisateurs et images de référence réservés au PC hôte** (les deux demandes, validées par l'utilisateur).
- Variable **`HOST_ONLY_IPS`** (défaut `10.0.2.2,192.168.137.1`, passée par `dashboard/docker-compose.yml`). Un ADMIN dont l'IP réelle n'y est pas reçoit **403 « réservé au PC hôte »** (journalisé avec son IP) sur les 7 routes : `GET/POST /utilisateurs`, `PATCH /utilisateurs/{id}`, `GET/POST /images-reference`, `PATCH /images-reference/{id}`, `GET /images-reference/{id}/fichier`. Le reste du dashboard fonctionne normalement depuis n'importe quel poste.
- `SERVICE_VISION` garde sa propre règle (`SERVICE_VISION_IPS`).
- `GET /auth/me` (et la réponse de connexion) renvoient `poste_hote: true/false`. L'interface remplace alors les deux sections par « disponibles uniquement sur le PC hôte ».
- `dashboard/.env.example` : `DASHBOARD_ORIGINS=https://192.168.137.1,https://127.0.0.1` et `HOST_ONLY_IPS`.
- Tests : `test_dashboard.sh` **132/132** (21 Caddy, 111 API ; 15 nouveaux, dont un ADMIN réel passant par Caddy avec un `X-Forwarded-For: 127.0.0.1` forgé, refusé). Contient aussi la LED automatique (branche précédente, pas encore fusionnée : cette branche part d'elle).

**→ VM : PC hôte prêt.** Fusionner **`feat/dashboard-poste-hote`** (elle contient aussi `feat/dashboard-led-critique`). Ajouter `https://127.0.0.1` à `DASHBOARD_ORIGINS` dans `server/.env` (`HOST_ONLY_IPS` a le bon défaut). Puis `sudo docker compose up -d --build dashboard caddy` et `test_dashboard.sh`.

**Testé sur Docker Desktop (Windows), avec la stack de `server/` telle quelle** :
- `dashboard/api/tests/test_dashboard.sh` : **78 OK** (13 Caddy, 65 API : auth, rôles, temps réel < 1 s mesuré à ~10 ms, `RESEAU_IA` invisible, commandes reçues par un faux ESP, injections, images, droits PostgreSQL). Pile isolée `sentinel-test`, détruite à la fin.
- `server/db/tests/test_droits.sh` sur base vierge : 39 OK (rien de cassé).
- Bout en bout avec un faux ESP et une fausse vision contre l'API d'ingestion : mesures → courbes, vidéo, alerte `FUSION/INTRUSION` avec capture, `DISPOSITIF_HORS_LIGNE` levée par l'ingestion, acquittement vu en direct par un autre navigateur.
- Non lancés ici (ils exigent sudo dans la VM) : `test_ingest.sh`, `test_mqtt.sh`.

**→ VM : mise en service du dashboard** (détail dans `dashboard/README.md`) :
1. `sudo pki/pki.sh server caddy 10003 DNS:sentinel-server,DNS:localhost,IP:127.0.0.1,IP:192.168.137.1`
2. Ajouter à `server/.env` les 3 variables de `dashboard/.env.example` (`DASHBOARD_JWT_SECRET` à générer, `DASHBOARD_ORIGINS=https://192.168.137.1`, `CADDY_SNI=sentinel-server`).
3. `sudo docker compose -f docker-compose.yml -f ../dashboard/docker-compose.yml up -d --build dashboard caddy`, puis `sudo ../dashboard/api/tests/test_dashboard.sh`.
4. Décider avec l'utilisateur : garder le `-f` en plus, ou recopier les deux services dans `server/docker-compose.yml`.
5. Comptes réels : `sudo ../dashboard/api/add-user.sh <email> "<nom>" <ROLE>`, lancé par l'utilisateur (mot de passe au clavier).

**→ Windows : redirection NAT VirtualBox 443** (IP hôte vide, port hôte 443 → port invité 443). Pour éviter l'avertissement du navigateur pendant la démo : installer `certs/ca.crt` comme autorité de confiance sur le PC de démo.

## Pièges déjà rencontrés

- `docker compose exec` et `ssh` lisent l'entrée standard : dans un script heredoc, ajouter `</dev/null`, sinon ils avalent la suite du script.
- PowerShell 5.1 abîme les guillemets passés à `ssh` : piloter la VM depuis Git Bash (outil Bash).
- Docker publie ses ports avant UFW : une règle UFW ne bloque PAS 8883 ni 443. Côté VM, le filtrage passe par la chaîne `DOCKER-USER` (`server/hardening/firewall.sh`), en plus de la redirection NAT VirtualBox et du pare-feu Windows.
- Ne jamais recharger un conteneur avec `docker kill -s <signal>` : Docker le marque « arrêté manuellement » et `restart: unless-stopped` ne le relance plus au démarrage. Utiliser `docker compose exec <service> kill -HUP 1`.
- L'image postgres fait confiance aux connexions locales par défaut. D'où `POSTGRES_INITDB_ARGS=--auth-local=scram-sha-256 --auth-host=scram-sha-256`.
- La RTX 5050 (Blackwell) exige PyTorch `cu128` ou plus.
- **Webcam : l'index OpenCV est INSTABLE** (selon l'ordre de branchement/boot, la « USB Camera » a été vue index 1 le 6 oct., puis index 0 le 7 oct. — l'intégrée « HP Wide Vision HD » prenant l'autre). La vision choisit donc la caméra **par son nom** (`camera.name` = `"USB Camera"` dans `host/vision/config.json`, via `pygrabber`), avec repli sur `camera.index`. Ne jamais se fier à l'index seul : vérifier le nom retenu dans le log `[CAMÉRA] « … » trouvée à l'index N`.

## IoT et électronique : où en est la session IoT (mis à jour par elle)

**Fait :**
- Tests unitaires dans `firmware/sensor-tests/` : un onglet `.ino` par capteur (PIR, MQ-2, DHT22, OLED) et `actuators.ino` (mêmes commandes JSON que le dashboard). Croquis Arduino IDE **de test seulement** : ne jamais les téléverser sur l'ESP en service.
- Câblage aligné sur `config.h` : DHT22 D5, PIR D6, LED rouge D7, LED verte D0, buzzer D8, MQ-2 A0 via pont diviseur, OLED D2/D1, PIR et MQ-2 sur VU.
- Actionneurs validés depuis le dashboard (6 oct.) : le buzzer et les LED réagissent aux commandes.
- Les 3 propositions pour `main.cpp` (PIR ignoré à la calibration, gaz `null` en préchauffe ou en défaut, LED automatique après commande) et la ligne de base du gaz (`gasRise`) sont appliquées par la session Windows : merci.
- L'OLED affiche déjà l'état utile : `MQTTS SECOURS`, `PIR MOUVEMENT`, `Gaz -- (defaut)` ou `(prechauf.)`, et la dernière commande.
- Schéma de câblage dessiné.
- Limite connue : le MQ-2 réagit à tous les gaz combustibles et ne distingue pas le méthane. Pas de changement prévu.

**À faire :**
- Mesures des composants et position du port USB pour le boîtier (Maxime).
- Alimentation de production (bloc 7,5 V + convertisseur 5 V) : mesurer au multimètre avant de brancher les capteurs, jamais avec l'USB.
- Tableau de câblage et documentation du firmware (livrables du dossier).
- Affichage OLED, optionnel : pas de bandeau ni d'alerte clignotante en haut de l'écran, et pas de message « hausse de gaz ». La hausse de gaz (`rising`, `gasSpike`) n'agit aujourd'hui que sur la LED rouge et le buzzer du mode secours, jamais sur l'écran.

**→ Windows :** les actionneurs sont câblés comme `config.h`. L'ESP est avec moi : peux-tu téléverser le firmware à jour depuis le PC hôte ? Je valide ensuite une dernière fois depuis le dashboard.

**→ Windows :** question : l'écran d'état actuel te suffit-il, ou veux-tu un bandeau d'alerte (hausse de gaz, intrusion) dans `drawOled()` ? Si oui, je t'écris le code, tu le relis et tu téléverses.

**→ Dashboard :** proposition : à la réception d'une alerte `CRITIQUE`, publier automatiquement la commande LED rouge « clignote » sur `sentinel/cmd/<numero_serie>`, puis « off » quand l'alerte est acquittée ou résolue. Le firmware gère déjà la commande ; il rend la main au mode automatique après `LED_MANUAL_HOLD_MS` (60 s), donc renvoyer la commande toutes les 50 s tant que l'alerte est ouverte.

## Rejoindre la coordination (sessions des collègues : dashboard, fablab, vidéo, dossier…)

1. **Lire ce fichier en entier** : décisions déjà prises, contrats (API, MQTT, BDD), ports, pièges.
2. **Créer sa propre section** juste avant « Pièges déjà rencontrés », sur le modèle des sections VM et Windows :
   `## <Rôle> : où en est la session <rôle> (mis à jour par elle)`. Y écrire ce qui est fait, l'état, les choix techniques, et comment tester.
3. **Pour demander quelque chose à une autre session**, écrire dans SA PROPRE section une ligne « **→ VM :** … » ou « **→ Windows :** … ». Les sessions VM et Windows surveillent le fichier et répondent dans leur propre section. Ne jamais modifier la section d'une autre session.
4. **Dossiers** : chacun travaille dans le sien (`dashboard/`, `docs/`, `fablab/`…) et ne touche pas à `host/`, `server/`, `firmware/` sans passer par une demande « → ».
5. **Git** : `git pull --rebase` avant de commencer ; commits sémantiques en français ; `git push` dès qu'un morceau fonctionne. **Aucun secret** (mots de passe, jetons, `.env`, `secrets.h`) : seulement des noms de variables.
6. **Ne jamais téléverser un autre programme sur l'ESP** : voir la section Windows, partie firmware.
7. Pour pousser, il faut être **collaborateur** du dépôt GitHub `Wylwaryn/sentinel-x` (à demander à l'utilisateur).

## Répartition entre sessions Claude (pour éviter les conflits Git)

- **Session Windows** : `host/` (vision, IA réseau), `docs/`, coordination.
- **Session VM** : `server/` (Mosquitto, API d'ingestion, compose, Caddy).
- **Session IoT** (Charlotte) : `firmware/` (depuis le 6 oct. au soir). La session Windows relit et fusionne.
- **Session dashboard** (Stève-John) : `dashboard/`.
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
8. ✅ Firmware ESP8266 en service : télémétrie en base, `SX-G2-01` en ligne.
9. Maintenance prédictive `CAPTEURS_IA` : session Windows (`host/predictive/`) ; compte MQTT `capteurs` demandé à la VM.
10. Dashboard + Caddy :443 (collègues), d'après `docs/fiche-api-dashboard.md`.

## Durcissement : calendrier décidé (VM + Windows)

**Mercredi soir (les deux pare-feu ensemble, pour détecter une casse avant le pentest) :**
- [ ] **Pare-feu Windows : `host/hardening/windows_firewall.ps1 -Apply`** (PowerShell administrateur).
  - Sans option : simulation. `-Apply` : sauvegarde complète, puis désactivation des règles entrantes « Autoriser » du profil Public (109 aujourd'hui : jeux, adb, Node, Docker, diffusion sans fil…), sauf la gestion réseau de base de Windows.
  - Crée 5 règles **limitées à `192.168.137.0/24`** : 8883, 443, DHCP 67, DNS 53 (UDP et TCP). `-Restore` remet tout comme avant.
- [ ] **Pare-feu de la VM : `sudo hardening/firewall.sh --apply`** (UFW en refus par défaut, SSH depuis `10.0.2.2` seulement, ports Docker en liste blanche dans `DOCKER-USER`). `--restore` remet tout comme avant.
- [ ] Vérifier ensuite : DHCP sur le point d'accès, ESP connecté sur 8883, vision et maintenance prédictive connectées, alerte acceptée sur 8443, dashboard joignable, `ssh sentinel-vm` OK, `sudo hardening/verify.sh`, et rien de légitime dans `journalctl -k | grep SENTINEL`.

**Jeudi matin, dans cet ordre :**
1. [ ] Gel du code validé.
2. [ ] VM : `sudo hardening/apply.sh --apply` (SSH par clé uniquement, CUPS masqué ; le pare-feu est déjà en place).
3. [ ] VM : `sudo hardening/verify.sh --markdown`, puis **dernier `git push` de la VM** (rapport, CLAUDE.md) : après l'étape 4, la VM ne peut plus pousser.
4. [ ] VM : `sudo hardening/firewall.sh --apply --egress` (sorties limitées à DNS et NTP). **Décidé par l'utilisateur.**
5. [ ] GitHub : révoquer la deploy key de la VM, puis supprimer `~/.ssh/github_sentinel`.
6. [ ] VirtualBox : couper le presse-papiers et le glisser-déposer.
7. [ ] **En dernier, par l'utilisateur** : `sudo hardening/apply.sh --apply --remove-sudoers` (sudo demandera ensuite le mot de passe de `wyllwaryn`).
