#!/usr/bin/env bash
# A lancer sur l'HOTE (Git Bash) toutes les ~10 min. L'hote TIRE le spool chiffre du honeypot
# (le honeypot n'emet jamais : c'est l'hote qui initie le SSH) et dechiffre avec la cle PRIVEE,
# qui ne quitte jamais l'hote. Resultat lisible et PERSISTANT sur l'hote (survit a un rollback).
# Idempotent : on memorise les fichiers deja recuperes (pas de doublon), sans rien supprimer sur la VM
# (le dossier spool appartient a l'utilisateur 'honeypot' ; les .gpg ne contiennent que du chiffre).
# (Explications detaillees : voir README.md)
set -euo pipefail

HP="${HP_SSH:-sentinel-honeypot}"           # alias SSH vers la VM honeypot (~/.ssh/config)
OUT="${HP_OUT:-$HOME/honeypot-logs}"
REMOTE_SPOOL=/var/lib/honeypot/spool
STATE="$OUT/.pulled"                         # noms des fichiers deja recuperes
mkdir -p "$OUT/chiffre"
touch "$STATE"

mapfile -t files < <(ssh "$HP" "ls -1 $REMOTE_SPOOL/*.gpg 2>/dev/null" || true)
[[ ${#files[@]} -gt 0 ]] || { echo "Rien sur la VM."; exit 0; }

new=0
for remote in "${files[@]}"; do
  name="$(basename "$remote")"
  grep -qxF "$name" "$STATE" && continue      # deja recupere -> on saute (pas de doublon)
  scp -q "$HP:$remote" "$OUT/chiffre/$name"
  if gpg --quiet --decrypt "$OUT/chiffre/$name" >> "$OUT/honeypot-clair.jsonl"; then
    echo "$name" >> "$STATE"                  # marque comme recupere
    rm -f "$OUT/chiffre/$name"
    new=$((new + 1))
  else
    echo "Dechiffrement echoue pour $name (garde pour reessayer)." >&2
  fi
done

echo "$new nouveau(x) fichier(s). Journal : $OUT/honeypot-clair.jsonl ($(wc -l < "$OUT/honeypot-clair.jsonl" 2>/dev/null || echo 0) lignes)"
