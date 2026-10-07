# Honeypot défensif Sentinel-X

Un **leurre** : des services d'apparence crédible, **vides**, qui **enregistrent** toute tentative
entrante (IP, port, horodatage, données envoyées) et **n'émettent jamais rien vers l'extérieur**.
But : détourner un attaquant du vrai serveur, et garder une **preuve** des tentatives (utile au dossier
et à la soutenance : « voici ce qu'on a subi pendant le pentest »).

> Ce dossier est **100 % défensif**. Aucun outil d'attaque ici. Pour apprendre le côté offensif,
> on lance les outils standards (nmap…) **contre ce honeypot** ou un bac à sable local (Juice Shop) :
> on voit l'outil ET ce qu'il déclenche dans les logs, sans viser personne.

## Placement réseau

Le honeypot vit **sur le Wi-Fi de la table** (`192.168.137.0/24`), **au même endroit que le vrai
serveur** Sentinel-X. Un leurre isolé sur un autre réseau crie « piège » ; ici il se fond dans la cible
que l'attaquant explore. Le pare-feu ouvre les ports du leurre à tout le Wi-Fi (il **doit** être
découvert) et ne laisse SSH qu'à ton poste d'admin.

## Ce que ça expose

| Port | Service d'apparence | Réalité |
|---|---|---|
| 3306 | « MySQL » (handshake crédible, `nmap -sV` l'identifie) | un **journal** : renvoie une bannière, lit et logge ce que le client envoie, ferme. Aucune base. |
| 8080 | « Dashboard » (page de login HTTP) | un **journal** : fausse page, logge chaque requête (méthode, chemin, en-têtes, corps, identifiants tentés). |

Ajouter un port (SSH 22 factice, FTP 21…) : copier `honeypot-mysql.service` en changeant `--port`/`--banner`.

## Les pièces

```
honeypot/
├── common.py                   journal JSONL append-only, horodatage UTC
├── tcp_tarpit.py               écoute un port TCP, bannière optionnelle, logge tout
├── http_honeypot.py            faux dashboard HTTP, logge chaque requête
├── rotate.sh                   chiffre l'instantané du log (clé publique) et efface le clair
├── setup.sh                    installe : utilisateur non-root, services, timer, import de clé
├── firewall.sh                 UFW strict : entrée = leurre + SSH admin ; SORTIE = DNS/NTP seulement
├── pull-logs.sh                CÔTÉ HÔTE : tire le spool chiffré et le déchiffre (clé privée locale)
├── systemd/
│   ├── honeypot-mysql.service
│   ├── honeypot-http.service
│   ├── honeypot-rotate.service
│   └── honeypot-rotate.timer   toutes les 10 min
└── README.md
```

## Journaux : chiffrés dans la VM, récupérés toutes les 10 min

Modèle choisi (plus robuste qu'un dossier chiffré classique, qui serait lisible pendant que la VM tourne) :

1. **Clair uniquement en RAM.** Les services écrivent dans `/run/honeypot` (tmpfs) : le log lisible
   n'existe qu'en mémoire, jamais sur le disque.
2. **Chiffrement par clé publique, toutes les 10 min.** `honeypot-rotate.timer` lance `rotate.sh` :
   il fige l'instantané, le **chiffre avec la clé PUBLIQUE de l'hôte** (GPG) vers
   `/var/lib/honeypot/spool/*.gpg`, puis efface le clair. **La VM ne peut pas relire ses propres logs** :
   la clé privée n'est pas dessus. Un attaquant qui prend la VM ne récupère pas l'historique.
3. **L'hôte tire, le honeypot n'émet jamais.** Sur l'hôte, `pull-logs.sh` récupère le spool par SSH
   (c'est l'hôte qui initie), le **déchiffre avec la clé privée** (qui ne quitte jamais l'hôte), et
   efface de la VM ce qui est récupéré. Résultat lisible et **persistant sur l'hôte → survit à un
   rollback** de snapshot de la VM.

### Mise en place des clés (une fois)

Sur l'**hôte** (Git Bash ; GPG fourni avec Git pour Windows) :
```bash
# Paire dédiée. La clé PRIVÉE reste ici, jamais sur la VM.
gpg --batch --quick-generate-key "sentinel-honeypot" default encrypt never
# Exporter la clé PUBLIQUE à copier sur la VM (à placer dans honeypot/ avant setup.sh) :
gpg --armor --export sentinel-honeypot > host-pub.asc
```

## Déploiement (VM Linux Mint sur le Wi-Fi de la table)

Sur la VM, utilisateur normal (sudo quand demandé) :
```bash
cd honeypot
# host-pub.asc doit être présent dans ce dossier (clé publique de l'hôte)
sudo ./setup.sh                                   # services + timer de rotation chiffrée
sudo MGMT_CIDR=192.168.137.0/24 ./firewall.sh --apply   # réseau durci (lancer APRÈS setup.sh)
systemctl status honeypot-mysql honeypot-http
systemctl list-timers honeypot-rotate
```
Retour arrière : `sudo ./firewall.sh --restore` et `sudo ./setup.sh --remove`.

Sur l'**hôte**, planifier la récupération toutes les 10 min :
```bash
# Test manuel (alias SSH 'sentinel-honeypot' à mettre dans ~/.ssh/config, comme sentinel-vm) :
HP_SSH=sentinel-honeypot ./pull-logs.sh
```
- **Windows** : Planificateur de tâches → tâche toutes les 10 min →
  `"C:\Program Files\Git\bin\bash.exe" -lc "/c/.../honeypot/pull-logs.sh"`.
- Les logs déchiffrés s'accumulent dans `~/honeypot-logs/honeypot-clair.jsonl`.

## Principes de sécurité (appliqués)

- **Séparation des privilèges** : services sous l'utilisateur **`honeypot`** (non-root, pas de shell,
  pas de sudo). systemd durcit (`NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`,
  `MemoryDenyWriteExecute`…).
- **Aucune sortie** : le pare-feu bloque tout le sortant sauf DNS/NTP → pas de reverse shell/beacon.
- **Aucune donnée réelle** : les services ne font qu'écrire leur journal. Pas de base, pas de secret,
  pas de proxy vers une autre machine.
- **Logs illisibles sur la VM** : chiffrés par clé publique ; déchiffrables uniquement sur l'hôte.

## Apprendre avec (le but pédagogique)

Le honeypot est ta cible d'entraînement **autorisée** (c'est le tien). Depuis ta machine :
```bash
nmap -sV -p 3306,8080 <IP_DU_HONEYPOT>    # que voit un attaquant ?
curl -i http://<IP_DU_HONEYPOT>:8080/     # le faux dashboard
```
En parallèle, après le prochain `pull-logs.sh`, regarde `honeypot-clair.jsonl` : tu vois ta propre
connexion arriver, l'IP, l'heure, les octets envoyés. Tu comprends d'un coup ce que fait `-sV` (il
envoie des sondes pour deviner le service) **et** à quoi ressemble une détection côté défense.

## Format du journal (une ligne JSON par événement)

```json
{"ts":"2026-10-08T09:12:33Z","service":"mysql","event":"data","src_ip":"192.168.137.50","src_port":51324,"dst_port":3306,"bytes":62,"data_hex":"...","data_txt":"..."}
```
Événements : `listen`, `connect`, `data`, `close` (TCP) ; `http` (requête web). Facile à relire, à
compter par IP, à mettre dans le dossier (`jq`, ou un petit script Python).
