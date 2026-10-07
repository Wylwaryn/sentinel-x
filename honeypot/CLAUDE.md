# Honeypot Sentinel-X — contexte pour Claude (session de la VM honeypot)

**Réponds en français.** Tu es dans la VM « honeypot » (un **leurre défensif**), prise en Remote-SSH.
Ce dossier (`~/honeypot`) est **autonome** : pas besoin du reste du dépôt Sentinel-X — volontairement,
un leurre ne doit pas contenir tout le code du projet (s'il est compromis, l'attaquant n'a rien).

## Rôle
Déployer et maintenir un **honeypot** : de **faux** services (MySQL 3306, dashboard HTTP 8080) qui
**enregistrent** les tentatives entrantes et **n'émettent JAMAIS rien** vers l'extérieur.
**100 % défensif. Aucun outil d'attaque ici, et on n'en ajoute pas.**

## État (déjà fait côté hôte Windows, avant ton arrivée)
- **Clé GPG** : paire générée sur l'hôte (la privée reste sur l'hôte). La **publique est déjà ici** :
  `host-pub.asc`. Elle sert à chiffrer les logs ; seul l'hôte peut les déchiffrer.
- **Réseau** : VM en **NAT**. Redirections VirtualBox déjà posées : **3306** et **8080** (joignables
  depuis le réseau de l'école et le hotspot), **SSH sur `127.0.0.1:2223`**. En NAT, le SSH et la
  récupération des logs arrivent de la **passerelle `10.0.2.2`**.
- Ce dossier a été copié par `scp` depuis l'hôte (SSH par clé en place).

## Ce que tu dois faire (détail complet dans `README.md`)
1. `sudo ./setup.sh` — crée l'utilisateur non-root `honeypot`, installe les services + le timer de
   rotation chiffrée (importe `host-pub.asc`). Logs **en clair en RAM** (`/run/honeypot`), **chiffrés**
   sur disque (`/var/lib/honeypot/spool/*.gpg`).
2. `sudo MGMT_CIDR=10.0.2.0/24 ./firewall.sh --apply` — UFW : entrée = 3306/8080 (ouverts) + SSH
   depuis `10.0.2.2` ; **sortie = DNS/NTP seulement**. ⚠️ `MGMT_CIDR=10.0.2.0/24` (NAT), sinon tu te
   coupes ton propre SSH. À lancer **APRÈS** `setup.sh` (le pare-feu ferme le sortant → `apt` ne marche
   plus ensuite).
3. Vérifier : `systemctl status honeypot-mysql honeypot-http`, `systemctl list-timers honeypot-rotate`.
   Test : `printf 'x' | nc 127.0.0.1 3306` doit apparaître dans `/run/honeypot/honeypot.jsonl`.
4. Dire quand c'est en service.

## Contraintes (non négociables)
- **Aucune sortie** de la VM vers des cibles (pas de reverse shell, pas de beacon). Le pare-feu le
  garantit : ne l'ouvre pas.
- **Aucune donnée réelle**, aucun secret sur cette VM (elle est faite pour être attaquée).
- **Séparation des privilèges** : services sous l'utilisateur `honeypot`, `sudo` seulement pour
  `setup.sh`/`firewall.sh`.
- Explications détaillées dans `README.md`, pas dans le code.

## Besoin de quelque chose côté Windows / hôte ?
La **session Claude « Windows »** gère l'hôte (redirections VirtualBox, génération de clés, et la
**planification de `pull-logs.sh` toutes les 10 min** qui tire + déchiffre les logs). Écris-lui via la
messagerie inter-sessions, ou laisse une note ici et préviens l'utilisateur. **Ne touche pas** au
dépôt Sentinel-X principal ni à son `CLAUDE.md` : ceci est une brique séparée.
