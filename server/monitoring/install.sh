#!/usr/bin/env bash
# Installe la supervision : collecteur chaque minute (systemd), rotation du journal (logrotate),
# commande « sentinel-monitor ». Idempotent. Usage : sudo monitoring/install.sh
#
# Le script est COPIÉ dans /usr/local/lib (root, 755) : root n'exécute jamais un fichier du dépôt,
# que l'utilisateur peut modifier (sinon, chemin vers root une fois le sudo sans mot de passe supprimé).
set -euo pipefail
cd "$(dirname "$0")/.."
[ "$(id -u)" -eq 0 ] || { echo "À lancer avec sudo." >&2; exit 1; }

pass=$(grep -E '^MQTT_MONITOR_PASSWORD=' .env | tail -n1 | cut -d= -f2-)
[ -n "$pass" ] || { echo "MQTT_MONITOR_PASSWORD absent : lancer d'abord sudo mosquitto/gen-passwd.sh" >&2; exit 1; }

install -d -m 755 /usr/local/lib/sentinel-x
install -m 755 -o root -g root monitoring/sentinel_monitor.py /usr/local/lib/sentinel-x/sentinel_monitor.py
ln -sf /usr/local/lib/sentinel-x/sentinel_monitor.py /usr/local/bin/sentinel-monitor

install -d -m 700 /etc/sentinel-x
umask 077
printf 'MQTT_MONITOR_PASSWORD=%s\nCA_FILE=%s\n' "$pass" "$PWD/certs/ca.crt" > /etc/sentinel-x/monitor.conf
umask 022

install -d -m 750 -o root -g adm /var/log/sentinel-x
install -m 644 monitoring/logrotate.conf /etc/logrotate.d/sentinel-x
install -m 644 monitoring/sentinel-monitor.service monitoring/sentinel-monitor.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now sentinel-monitor.timer
echo "Installé. Relevé chaque minute dans /var/log/sentinel-x/metrics.jsonl"
echo "Résumé : sudo sentinel-monitor report   (--hours 24, --markdown)"
