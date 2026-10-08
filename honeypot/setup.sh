#!/usr/bin/env bash
# Installe (ou retire) le honeypot : utilisateur non-root, log en RAM, spool chiffre, services + timer.
# A lancer en root (sudo). Place host-pub.asc (cle PUBLIQUE de l'hote) a cote avant de lancer.
# (Explications detaillees : voir README.md)
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST=/opt/honeypot                 # code (lecture seule pour les services)
USER_HP=honeypot
LOG_DIR="${LOG_DIR:-/run/honeypot}" # tmpfs par defaut : le CLAIR n'existe qu'en RAM
STATE=/var/lib/honeypot            # disque : spool chiffre + trousseau GPG
SPOOL="$STATE/spool"
GNUPG="$STATE/gnupg"
RECIP=sentinel-honeypot

UNITS=(honeypot-mysql honeypot-http honeypot-rotate)

if [[ "${1:-}" == "--remove" ]]; then
  systemctl disable --now honeypot-mysql honeypot-http honeypot-rotate.timer 2>/dev/null || true
  for u in "${UNITS[@]}"; do rm -f "/etc/systemd/system/$u.service"; done
  rm -f /etc/systemd/system/honeypot-rotate.timer
  systemctl daemon-reload
  rm -rf "$DEST"
  echo "Services et code retires. $STATE (spool chiffre + cle) et l'utilisateur CONSERVES (a retirer a la main)."
  exit 0
fi

[[ $EUID -eq 0 ]] || { echo "Lancer en root : sudo ./setup.sh"; exit 1; }
for pkg in python3 gnupg; do command -v "${pkg/gnupg/gpg}" >/dev/null || { apt-get update -qq && apt-get install -y -qq "$pkg"; }; done

# 1. Utilisateur de service : systeme, sans shell, sans sudo.
id "$USER_HP" &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin "$USER_HP"

# 2. Code dans /opt (lisible par tous : l'utilisateur 'honeypot' ne peut pas lire dans ton home,
#    donc on copie AUSSI la cle publique ici, depuis ou il pourra l'importer).
install -d -m 755 "$DEST"
install -m 644 "$SRC/common.py" "$SRC/tcp_tarpit.py" "$SRC/http_honeypot.py" "$DEST/"
install -m 755 "$SRC/rotate.sh" "$DEST/"
[[ -f "$SRC/host-pub.asc" ]] && install -m 644 "$SRC/host-pub.asc" "$DEST/"

# 3. Etat sur disque. Le spool ne contient que du CHIFFRE : traversable/lisible pour que l'hote le
#    recupere par scp (sans sudo). Le trousseau GPG reste prive (700).
install -d -m 755 -o "$USER_HP" -g "$USER_HP" "$STATE" "$SPOOL"
install -d -m 700 -o "$USER_HP" -g "$USER_HP" "$GNUPG"

# 4. Importer la cle PUBLIQUE de l'hote (chiffrement). Sans elle, la rotation ne peut pas chiffrer.
if [[ -f "$DEST/host-pub.asc" ]]; then
  # On importe depuis /opt (lisible par 'honeypot'). 'env' garantit que GNUPGHOME est bien pris.
  sudo -u "$USER_HP" env GNUPGHOME="$GNUPG" gpg --batch --import "$DEST/host-pub.asc"
  echo "Cle publique de l'hote importee (destinataire : $RECIP)."
else
  echo "ATTENTION : host-pub.asc absent. Genere la paire sur l'HOTE (voir README), copie host-pub.asc ici,"
  echo "            puis relance setup.sh. En attendant, la rotation chiffree ne tournera pas."
fi

# 5. Services + timer : on remplace __LOGDIR__ par le chemin du log en RAM.
for unit in honeypot-mysql honeypot-http honeypot-rotate; do
  sed "s#__LOGDIR__#$LOG_DIR#g" "$SRC/systemd/$unit.service" > "/etc/systemd/system/$unit.service"
done
cp "$SRC/systemd/honeypot-rotate.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now honeypot-mysql honeypot-http honeypot-rotate.timer

echo
echo "Installe."
echo "  Clair (RAM, ephemere)  : $LOG_DIR/honeypot.jsonl"
echo "  Chiffre (disque)       : $SPOOL/*.gpg   (illisible sur la VM, dechiffrable seulement sur l'hote)"
echo "  Etat : systemctl status honeypot-mysql honeypot-http ; systemctl list-timers honeypot-rotate"
echo "  Pare-feu : sudo MGMT_CIDR=192.168.137.0/24 ./firewall.sh --apply"
echo "  Cote hote : planifier honeypot/pull-logs.sh toutes les 10 min (voir README)."
