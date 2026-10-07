#!/usr/bin/env bash
# Pare-feu UFW du honeypot. Entree : ports du leurre (a tous) + SSH (admin seulement).
# SORTIE : DNS + NTP uniquement -> aucun reverse shell / beacon possible depuis le leurre.
# A lancer APRES setup.sh (la sortie fermee empeche apt d'installer quoi que ce soit ensuite).
# (Explications detaillees : voir README.md)
set -euo pipefail

MGMT_CIDR="${MGMT_CIDR:-}"            # d'ou TU administres en SSH (ex. l'IP/sous-reseau de ton hote)
HONEYPOT_PORTS=(3306 8080)           # les ports du leurre, ouverts a tout le Wi-Fi de la table

[[ $EUID -eq 0 ]] || { echo "Lancer en root : sudo ... ./firewall.sh"; exit 1; }
command -v ufw >/dev/null || { apt-get update -qq && apt-get install -y -qq ufw; }

if [[ "${1:-}" == "--restore" ]]; then
  ufw --force reset
  ufw disable
  echo "UFW reinitialise et desactive."
  exit 0
fi
[[ "${1:-}" == "--apply" ]] || { echo "Usage : MGMT_CIDR=<cidr> sudo ./firewall.sh --apply | --restore"; exit 1; }
[[ -n "$MGMT_CIDR" ]] || { echo "Definir MGMT_CIDR (ex. 192.168.137.0/24) : d'ou tu te connectes en SSH."; exit 1; }

ufw --force reset
ufw default deny incoming
ufw default deny outgoing          # par defaut on ne laisse RIEN sortir ; on rouvre juste le minimum
# Sortie minimale : resolution DNS et synchro d'heure (necessaires au systeme), rien d'autre.
ufw allow out 53
ufw allow out 123/udp
# Entree : les ports du leurre, depuis n'importe ou (il doit etre decouvert et attaque).
for p in "${HONEYPOT_PORTS[@]}"; do ufw allow in "${p}/tcp"; done
# SSH d'administration : seulement depuis ton reseau d'admin (ton hote tire les logs par la).
ufw allow from "$MGMT_CIDR" to any port 22 proto tcp
ufw --force enable

echo
ufw status verbose
echo "Rappel : les reponses aux connexions entrantes passent (etat ESTABLISHED), mais le leurre ne peut INITIER aucune sortie."
