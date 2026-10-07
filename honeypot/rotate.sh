#!/usr/bin/env bash
# Rotation + chiffrement du journal. Lance toutes les 10 min par le timer systemd, sous 'honeypot'.
# Prend l'instantane du log en RAM, le chiffre avec la CLE PUBLIQUE de l'hote, l'ecrit (chiffre) sur
# le disque, et efface le clair. La VM ne garde donc jamais de log lisible sur disque.
# (Explications detaillees : voir README.md)
set -euo pipefail

LIVE_DIR="${LOG_DIR:-/run/honeypot}"          # tmpfs (RAM) : le clair n'existe qu'ici, brievement
LIVE="$LIVE_DIR/honeypot.jsonl"
SPOOL="${SPOOL_DIR:-/var/lib/honeypot/spool}" # disque : uniquement du chiffre
export GNUPGHOME="${GNUPGHOME:-/var/lib/honeypot/gnupg}"
RECIP="${HP_RECIPIENT:-sentinel-honeypot}"    # identite de la cle publique de l'hote

[ -s "$LIVE" ] || exit 0                        # rien de nouveau : on ne fait rien

# On fige l'instantane en le renommant. common.py rouvre le fichier a chaque ecriture (open 'a'),
# donc un nouveau honeypot.jsonl sera recree tout seul au prochain evenement.
snap="honeypot.$(date -u +%Y%m%dT%H%M%SZ).jsonl"
mv "$LIVE" "$LIVE_DIR/$snap"

# Chiffrement asymetrique : --trust-model always car la cle publique de l'hote est importee sans web-of-trust.
gpg --batch --yes --trust-model always -r "$RECIP" --encrypt \
    -o "$SPOOL/$snap.gpg" "$LIVE_DIR/$snap"

# Effacement du clair (il etait en RAM de toute facon ; shred par precaution).
shred -u "$LIVE_DIR/$snap" 2>/dev/null || rm -f "$LIVE_DIR/$snap"
