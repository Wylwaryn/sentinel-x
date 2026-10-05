# sentinel-x

Prototype **SENTINEL-X** (Workshop EPSI M1 2026-27) : boîtier de surveillance autonome ESP8266 relié à un serveur local durci, avec vision IA, maintenance prédictive et détection d'intrusion réseau.

> README en construction. Il sera complété tout au long de la semaine (installation, schémas, sécurité, démo).

## Architecture (option B : PC apprenant)

```
ESP8266 (DHT22, MQ-2, PIR, OLED, buzzer, LED) ──MQTTS:8883──┐
Navigateurs (dashboard) ───────────────────HTTPS:443────────┤
                                                            ▼
PC hôte Windows (GPU RTX 5050)              VM Linux Mint « sentinel-server »
 ├─ host/vision : YOLOv8 + ByteTrack         ├─ Mosquitto (MQTTS)
 │   + zones + fusion PIR                    ├─ API d'ingestion   (127.0.0.1 uniquement)
 └─ host/ids    : IA réseau (à venir)        ├─ API dashboard     (via Caddy :443)
     └── connexions sortantes uniquement ──► └─ PostgreSQL 17     (aucun port publié)
```

## Arborescence

| Dossier | Contenu |
|---|---|
| `host/vision/` | Service de vision (Windows + CUDA) : détection, suivi, zones, fusion PIR, réglage webcam |
| `server/` | Stack Docker de la VM : `docker-compose.yml`, configuration du moteur Docker |
| `server/db/init/` | Schéma PostgreSQL et rôles à moindre privilège (chargés au premier démarrage) |
| `server/db/tests/` | Preuve du cloisonnement des droits (39 tests) |
| `docs/` | Documentation d'équipe (fiche API dashboard…) |

## Démarrage rapide

### Vision (PC hôte Windows)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
.\.venv\Scripts\python.exe -m pip install -r host/requirements.txt
cd host/vision
..\..\.venv\Scripts\python.exe camera_setup.py      # réglage exposition webcam
..\..\.venv\Scripts\python.exe sentinel_vision.py   # service de vision
..\..\.venv\Scripts\python.exe -m pytest            # tests fusion / zones
```

### Serveur (VM)

```bash
cd server
cp .env.example .env    # puis remplacer chaque secret
sudo docker compose up -d
sudo docker compose exec -T postgres bash < db/tests/test_droits.sh   # base de test uniquement
```

## Sécurité

- Aucun secret dans ce dépôt : `.env`, certificats et clés sont exclus par `.gitignore`.
- Base de données : un rôle par API, droits minimaux, vérifiés par `server/db/tests/test_droits.sh`.
- Docker : userns-remap, no-new-privileges, capacités Linux réduites, rotation des journaux.
