# API dashboard + interface de supervision

Contrat : [`docs/fiche-api-dashboard.md`](../docs/fiche-api-dashboard.md). Rôle PostgreSQL `sentinel_dashboard`, compte MQTT `dashboard`.

```
Navigateur ──HTTPS/WSS :443──► caddy (web/ : build React + Caddyfile) ──► dashboard:8000 (api/ : FastAPI, réseau interne)
                                                                                  ├─ PostgreSQL : vues + LISTEN sentinel_mesure / sentinel_alerte
                                                                                  └─ MQTTS : publie sentinel/cmd/<serie>, lit sentinel/video/#
```

Tout est dans `dashboard/` : rien n'est modifié dans `server/`. [`docker-compose.yml`](docker-compose.yml) ajoute les
services `dashboard` et `caddy` à la stack serveur (`-f` en plus), avec ses réseaux, son `.env`, ses certificats et le volume `captures`.

## Mise en service dans la VM (une fois)

```bash
cd /opt/sentinel-x/server
# 1. Certificat de Caddy (uid 10003 = utilisateur du conteneur caddy)
sudo pki/pki.sh server caddy 10003 DNS:sentinel-server,DNS:localhost,IP:127.0.0.1,IP:192.168.137.1
# 2. server/.env : ajouter les 3 variables de dashboard/.env.example (secret JWT généré, jamais commité).
#    MQTT_DASHBOARD_PASSWORD et DASHBOARD_DB_PASSWORD y sont déjà.
# 3. Démarrage
sudo docker compose -f docker-compose.yml -f ../dashboard/docker-compose.yml up -d --build dashboard caddy
# 4. Comptes (mot de passe demandé au clavier, haché en Argon2id)
sudo ../dashboard/api/add-user.sh admin@sentinel.local "Administrateur" ADMIN
# 5. Tests sur une pile isolée (132 tests : Caddy, IP réelle, PC hôte, API, temps réel, rôles, comptes, MQTT, LED auto, injections)
sudo ../dashboard/api/tests/test_dashboard.sh
```

VirtualBox : redirection `443 -> 443` (IP hôte vide, Wi-Fi de la table). Le pare-feu (`firewall.sh`, `windows_firewall.ps1`) autorise déjà 443.
Dashboard : `https://192.168.137.1` (le navigateur doit faire confiance à `certs/ca.crt`, sinon avertissement).

## API (`/api/v1`, session = cookie HttpOnly posé par `/auth/login`)

| Méthode | Route | Rôle |
|---|---|---|
| POST | `/auth/login` `{"email","password"}`, `/auth/logout` ; GET `/auth/me` | — |
| GET | `/dispositifs` (v_dispositif_etat) | tous |
| GET | `/dispositifs/{id}/mesures?minutes=15\|60\|360\|1440` (agrégé au-delà d'1 h) | tous |
| GET | `/alertes` (ouvertes) ou `/alertes?toutes=true` ; `/alertes/{id}/capture` (JPEG) | tous |
| POST | `/alertes/{id}/acquitter`, `/alertes/{id}/resoudre` | OPERATEUR, ADMIN |
| POST | `/dispositifs/{id}/commandes` `{"commande": {"actionneur":"buzzer","etat":"on","duree_ms":3000}}` | OPERATEUR, ADMIN |
| GET | `/utilisateurs` | ADMIN |
| POST | `/utilisateurs` `{"nom","email","role","mot_de_passe","mot_de_passe_admin"}` (mot de passe de l'ADMIN ressaisi) | ADMIN |
| PATCH | `/utilisateurs/{id}` `{"actif":false}` (pas sur son propre compte) | ADMIN |
| POST / PATCH | `/images-reference?id_utilisateur=N` (corps = JPEG brut), `/images-reference/{id}` `{"active":false}` | ADMIN |
| GET | `/images-reference` (avec `utilisateur_role`, `utilisateur_actif`), `/images-reference/{id}/fichier` | ADMIN, SERVICE_VISION |

WebSocket `/ws` (même cookie) : `{"type":"mesure","data":{...}}`, `{"type":"alerte","data":{...}}`, `{"type":"resync"}`, et les images webcam en binaire (JPEG).
Code de fermeture 4401 = session absente ou expirée, 4403 = origine refusée ou compte de service.

`SERVICE_VISION` (synchronisation des visages, créé uniquement au terminal avec `add-user.sh`) : refusé partout (403) sauf les deux `GET` d'images,
et seulement depuis `SERVICE_VISION_IPS` (`10.0.2.2` par défaut : le PC hôte vu de la VM), à la connexion et à chaque requête.

Utilisateurs et images de référence : ADMIN **et** PC hôte (`HOST_ONLY_IPS`, défaut `10.0.2.2,192.168.137.1`), sinon 403.
`/auth/me` renvoie `poste_hote` pour que l'interface masque ces deux sections ailleurs.

Alarme automatique : à chaque nouvelle alerte `CRITIQUE`, l'API publie sur `sentinel/cmd/<numero_serie>` trois
commandes : `led rouge clignote` et `led vert off` (tenues 30 min par `duree_ms`), puis `buzzer on` (10 s, plafond
de l'ESP). Quand plus aucune `CRITIQUE` n'attend d'acquittement sur ce boîtier : `led rouge off`, `led vert on`
(le témoin de liaison reprend ensuite seul) et `buzzer off`.

Les instants sont envoyés en UTC (`"instant": "2026-10-07T14:02:11.482913Z"`) ; le navigateur affiche l'heure de Paris.

## Sécurité (pour la matrice du dossier)

- Aucun port publié pour l'API : seul Caddy (443, TLS 1.2+ ECDHE/AEAD, HSTS, CSP sans script en ligne, pas d'en-tête Server).
- Argon2id ; même durée de réponse si l'email est inconnu ; 5 échecs en 5 min par IP réelle ou par email → 429.
- IP réelle : uvicorn `--proxy-headers` lit le `X-Forwarded-For` posé par Caddy, qui écrase celui du client (non forgeable, testé).
- JWT HS256 (algorithme imposé) dans un cookie `HttpOnly; Secure; SameSite=Strict` ; compte relu en base à chaque requête (désactivation immédiate) ; contrôle de l'en-tête `Origin` (CSRF, détournement de WebSocket).
- Requêtes SQL paramétrées uniquement ; droits limités par PostgreSQL (vues, colonnes) en plus des rôles applicatifs.
- Commandes MQTT validées strictement (actionneur, couleur, état, durée ≤ 10 s) ; images : JPEG vérifié par ses octets magiques, 2 Mo maximum, nom choisi par le serveur.
- Conteneurs `read_only`, `cap_drop: [ALL]`, `no-new-privileges`, utilisateurs sans privilège (10002, 10003).

## Développement du frontend

`cd dashboard/web && npm install && npm run dev` : Vite relaie `/api` et `/ws` vers la pile Docker locale (`https://localhost`).
Ajouter `http://localhost:5173` à `DASHBOARD_ORIGINS` (le cookie `Secure` est accepté par les navigateurs sur `localhost`).
