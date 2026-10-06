# Fiche réseau et flux — Sentinel-X (pour le dossier technique)

Tout ce qu'il faut pour rédiger le **schéma réseau**, le **plan d'adressage** et la partie réseau de la **matrice de sécurité**.
Les valeurs ont été relevées sur le système réel le 6 octobre. Les détails complets sont dans `CLAUDE.md`, section par section.

---

## 1. Topologie (option B : PC apprenant)

```
                     Wi-Fi de la table « SentinelX-G2 » (2,4 GHz, WPA2) — 192.168.137.0/24
   ┌───────────────┐        ┌─────────────────────┐       ┌───────────────────────────┐
   │ ESP8266        │        │ PC / téléphones      │       │ Wi-Fi de l'école (amont)   │
   │ SX-G2-01       │        │ (navigateur          │       │ PC hôte : 10.60.60.97      │
   │ IP DHCP .x     │        │  dashboard)          │       │ (sert seulement d'accès    │
   │ MAC 40:F5:20:  │        └──────────┬──────────┘       │  Internet au PC hôte)      │
   │     0D:5F:21   │                   │ HTTPS 443          └───────────────────────────┘
   └───────┬───────┘                   │
           │ MQTTS 8883                 │
           ▼                            ▼
   ┌───────────────────────────────────────────────────────────────────────────────────┐
   │ PC SERVEUR LOCAL — Windows 11, 192.168.137.1 (point d'accès mobile)               │
   │  • Vision IA (YOLOv8 + ByteTrack + reconnaissance faciale), GPU RTX 5050          │
   │  • IA réseau (IDS) : capture du Wi-Fi de la table (Npcap)                          │
   │  • Maintenance prédictive (séries temporelles des capteurs)                        │
   │  • Webcam USB                                                                      │
   │         │  redirections NAT VirtualBox (tableau §3)                                │
   │         ▼                                                                          │
   │  ┌─────────────────────────────────────────────────────────────────────────────┐  │
   │  │ VM « sentinel-server » — Linux Mint 22.3, 10.0.2.15 (NAT VirtualBox)        │  │
   │  │ Passerelle vue par la VM : 10.0.2.2 (= le PC hôte)                          │  │
   │  │  Docker : Mosquitto (MQTTS) · API d'ingestion · PostgreSQL 17 ·              │  │
   │  │           API dashboard · Caddy (HTTPS)                                     │  │
   │  └─────────────────────────────────────────────────────────────────────────────┘  │
   └───────────────────────────────────────────────────────────────────────────────────┘
```

## 2. Plan d'adressage

| Réseau | Plage | Machine | Adresse | Remarque |
|---|---|---|---|---|
| Wi-Fi de la table | 192.168.137.0/24 | PC hôte (point d'accès) | **192.168.137.1** | Passerelle et DHCP de la table |
| | | ESP8266 `SX-G2-01` | DHCP (vu en .2 puis .6) | Identifié par sa **MAC `40:F5:20:0D:5F:21`**, son IP change à chaque reconnexion |
| | | Navigateurs (PC, téléphones) | DHCP | Pas d'accès Internet par la table (réseau isolé) |
| NAT VirtualBox | 10.0.2.0/24 | VM `sentinel-server` | 10.0.2.15 | Invisible depuis le Wi-Fi |
| | | PC hôte vu depuis la VM | **10.0.2.2** | Les connexions venant du PC hôte arrivent avec cette adresse |
| Réseaux Docker (dans la VM) | attribués par Docker | voir §5 | — | Internes ou sans sortie vers Internet |
| Wi-Fi de l'école | 10.60.60.0/22 | PC hôte | 10.60.60.97 | Accès Internet du PC hôte uniquement |

Point important : VirtualBox **conserve l'IP source réelle** des appareils du Wi-Fi sur les ports redirigés. La VM voit donc l'ESP avec sa vraie adresse `192.168.137.x`. Seul le trafic du PC hôte lui-même apparaît en `10.0.2.2`.

## 3. Redirections NAT VirtualBox (capture de la configuration)

| Nom | Protocole | IP hôte | Port hôte | Port invité | Pour qui | Pourquoi cette IP hôte |
|---|---|---|---|---|---|---|
| `https` | TCP | *(toutes)* | 443 | 443 | Navigateurs du Wi-Fi → Caddy (dashboard) | Doit être joignable depuis la table |
| `mqtts` | TCP | *(toutes)* | 8883 | 8883 | ESP8266 et vision → Mosquitto | L'ESP arrive par le Wi-Fi |
| `ingest` | TCP | **127.0.0.1** | 8443 | 8443 | Vision, IDS, maintenance prédictive → API d'ingestion | **Jamais joignable depuis le Wi-Fi** : seuls les programmes du PC hôte écrivent des alertes |
| `ssh` | TCP | **127.0.0.1** | 2222 | 22 | Administration de la VM | **Jamais joignable depuis le Wi-Fi** |

Les ports « toutes interfaces » (443, 8883) sont **restreints au réseau de la table** par les pare-feu (§6).

## 4. Matrice des flux

| # | Source | Destination | Port | Protocole | Chiffrement | Authentification | Ce qui circule |
|---|---|---|---|---|---|---|---|
| 1 | ESP8266 | Mosquitto | 8883 | MQTT | TLS 1.2, certificat vérifié par notre CA | Compte `esp` + mot de passe | Télémétrie `sentinel/telemetry` (temp., humidité, gaz, PIR) toutes les 5 s |
| 2 | Mosquitto | ESP8266 | (même connexion) | MQTT | TLS | ACL | Commandes `sentinel/cmd/SX-G2-01` (buzzer, LED) |
| 3 | Vision (PC hôte) | Mosquitto | 8883 | MQTT | TLS | Compte `vision` | Lit le PIR, publie la vidéo annotée `sentinel/video/cam1` (5 img/s) |
| 4 | Maintenance prédictive (PC hôte) | Mosquitto | 8883 | MQTT | TLS | Compte `capteurs` | Lit la télémétrie |
| 5 | Vision / IDS / maintenance prédictive | API d'ingestion | 8443 | HTTPS | TLS | **Un jeton par programme**, limité à ses origines d'alerte | Alertes (`POST /api/v1/alerts`) |
| 6 | API d'ingestion | Mosquitto | 8883 interne | MQTT | TLS | Compte `ingest` | Lit la télémétrie pour l'écrire en base |
| 7 | API d'ingestion | PostgreSQL | 5432 interne | SQL | réseau Docker interne | Rôle `sentinel_ingest` | Écrit mesures et alertes |
| 8 | Navigateurs | Caddy | 443 | HTTPS + WebSocket | TLS 1.2+, HSTS | Session (cookie `HttpOnly`), mots de passe Argon2id, rôles LECTEUR/OPERATEUR/ADMIN | Dashboard, temps réel, vidéo, commandes |
| 9 | Caddy | API dashboard | interne | HTTP | réseau Docker interne | — | Relais |
| 10 | API dashboard | PostgreSQL | 5432 interne | SQL | réseau Docker interne | Rôle `sentinel_dashboard` (lecture via vues, création de comptes limitée) | Mesures, alertes, comptes |
| 11 | API dashboard | Mosquitto | 8883 interne | MQTT | TLS | Compte `dashboard` | Publie les commandes, lit la vidéo |
| 12 | Synchronisation des visages (PC hôte) | Caddy → API dashboard | 443 via `127.0.0.1` | HTTPS | TLS | Compte `SERVICE_VISION` (lecture des photos de référence uniquement, depuis le PC hôte seulement) | Photos de référence |
| 13 | Administrateur (PC hôte) | VM | 2222 → 22 | SSH | SSH | Clé uniquement (mot de passe désactivé jeudi) | Administration |

## 5. Réseaux Docker (cloisonnement dans la VM)

| Réseau | Type | Services | Rôle |
|---|---|---|---|
| `net_ingest` | **interne** (aucune sortie) | API d'ingestion, Mosquitto, PostgreSQL | Chaîne d'écriture |
| `net_dashboard` | **interne** | API dashboard, Caddy, Mosquitto, PostgreSQL | Chaîne de lecture et de commande |
| `net_mqtt_edge` | publication de 8883, **sans NAT sortant** | Mosquitto | Reçoit des connexions, ne peut pas joindre Internet |
| `net_ingest_edge` | publication de 8443, sans NAT sortant | API d'ingestion | idem |
| `net_web_edge` | publication de 443, sans NAT sortant | Caddy | idem |

- **Les deux API ne partagent aucun réseau** : elles ne peuvent pas se joindre, même si l'une est compromise.
- **PostgreSQL ne publie aucun port** : il est invisible depuis le PC hôte et depuis le Wi-Fi.

## 6. Pare-feu (trois couches)

| Couche | Outil | Règle principale | Quand |
|---|---|---|---|
| PC hôte | Pare-feu Windows (`host/hardening/windows_firewall.ps1`) | Désactive 109 règles d'entrée superflues ; n'autorise que 8883, 443, DHCP 67 et DNS 53 **depuis `192.168.137.0/24`** | Mercredi soir (réversible) |
| VM | UFW (`server/hardening/firewall.sh`) | Refus par défaut ; SSH depuis `10.0.2.2` seulement | Mercredi soir |
| Conteneurs | Chaîne `DOCKER-USER` (même script) | 8883 et 443 depuis `10.0.2.2` et `192.168.137.0/24` ; 8443 depuis `10.0.2.2` seulement ; le reste est journalisé puis bloqué | Mercredi soir |
| Sorties de la VM | `firewall.sh --egress` | Bloque toute sortie sauf DNS et NTP (empêche un reverse shell) | Jeudi, après le gel du code |

Docker publie ses ports **avant** UFW : c'est pour ça que les ports des conteneurs sont filtrés dans `DOCKER-USER`, et pas dans UFW.

Une liste blanche a été **observée en conditions réelles avant d'être appliquée** (mode `--observe`). Cette observation a évité de bloquer l'ESP.

## 7. Comptes MQTT et droits (liste blanche)

| Compte | Peut publier sur | Peut lire | Utilisé par |
|---|---|---|---|
| `esp` | `sentinel/telemetry` | `sentinel/cmd/+` | ESP8266 |
| `ingest` | — | `sentinel/telemetry` | API d'ingestion |
| `dashboard` | `sentinel/cmd/+` | `sentinel/video/#` | API dashboard |
| `vision` | `sentinel/video/#` | `sentinel/telemetry` | Vision (PC hôte) |
| `capteurs` | — | `sentinel/telemetry` | Maintenance prédictive (PC hôte) |
| `monitor` | — | `$SYS/broker/#` | Supervision (réseau Docker interne) |

Tout ce qui n'est pas listé est refusé. C'est vérifié par des tests : par exemple, la vision ne peut **ni recevoir les commandes, ni se faire passer pour un ESP**.

## 8. Certificats (PKI maison)

- **Autorité** : « Sentinel-X Root CA », EC P-256, valable jusqu'en octobre 2029. Empreinte SHA-256 `36:A8:B4:4A:07:7E:30:A1:E7:7A:6A:88:1E:C5:29:A9:AD:9A:69:D1:CF:08:59:62:BA:3D:90:78:5A:68:55:AD`.
- **Certificats serveur** : Mosquitto, API d'ingestion, Caddy. Ils sont signés par l'autorité, et chacun contient les adresses par lesquelles on le joint. Mosquitto et Caddy : `IP:127.0.0.1` et `IP:192.168.137.1`. API d'ingestion : `IP:127.0.0.1`, puisqu'elle n'est joignable que depuis le PC hôte.
- **Sur l'ESP** : le certificat de l'autorité est intégré au firmware comme ancre de confiance. L'ESP vérifie la chaîne et la date. Il se connecte par IP, car sa bibliothèque TLS (BearSSL) ne vérifie pas une IP dans le certificat : c'est un compromis documenté.
- **Navigateurs** : installer le certificat de l'autorité (`ca.crt`, public) comme « autorité racine de confiance », en vérifiant d'abord son empreinte :
  - Windows (Edge, Chrome) : double-clic sur `ca.crt`, puis Installer un certificat → Placer dans « Autorités de certification racines de confiance » ;
  - Firefox : Paramètres → Certificats → Autorités → Importer → cocher « identifier des sites web » ;
  - Mac : Trousseaux d'accès → « Toujours approuver » ;
  - Android / iPhone : installer comme certificat CA, puis activer la confiance.
  Ensuite, `https://192.168.137.1` s'ouvre sans avertissement depuis le Wi-Fi de la table.

## 9. Points à faire figurer dans le dossier

- **Isolation** : le réseau de la table n'a pas d'accès Internet, et les conteneurs n'ont aucune sortie.
- **Défense en profondeur** : 3 pare-feu, TLS partout, un compte par programme, droits minimaux en base et sur MQTT.
- **Détection** : l'IA réseau capture tout le trafic de la table, reconnaît l'ESP par sa MAC, et peut bloquer temporairement une IP au pare-feu Windows (liste blanche protégée).
- **Limites connues** (à assumer devant le jury) :
  - pas de vérification de nom sur le TLS de l'ESP ;
  - une adresse MAC peut être usurpée (l'ESP est donc alerté, jamais ignoré) ;
  - le Wi-Fi de la table est protégé par WPA2 personnel (mot de passe partagé).
