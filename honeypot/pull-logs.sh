#!/usr/bin/env bash
# A lancer sur l'HOTE (Git Bash) toutes les ~10 min. L'hote TIRE le spool chiffre du honeypot
# (le honeypot n'emet jamais : c'est l'hote qui initie la connexion SSH) et dechiffre avec la cle PRIVEE,
# qui ne quitte jamais l'hote. Resultat lisible et PERSISTANT sur l'hote (survit a un rollback de la VM).
# (Explications detaillees : voir README.md)
set -euo pipefail

HP="${HP_SSH:-sentinel-honeypot}"          # alias SSH vers la VM honeypot (dans ~/.ssh/config)
OUT="${HP_OUT:-$HOME/honeypot-logs}"
REMOTE_SPOOL=/var/lib/honeypot/spool
mkdir -p "$OUT/chiffre"

# 1. Liste des fichiers chiffres presents sur la VM (rien a faire s'il n'y en a pas).
mapfile -t files < <(ssh "$HP" "ls -1 $REMOTE_SPOOL/*.gpg 2>/dev/null" || true)
[[ ${#files[@]} -gt 0 ]] || { echo "Rien a recuperer."; exit 0; }

for remote in "${files[@]}"; do
  name="$(basename "$remote")"
  # 2. Rapatrier le fichier chiffre.
  scp -q "$HP:$remote" "$OUT/chiffre/$name"
  # 3. Dechiffrer (cle privee locale) et concatener au journal lisible.
  if gpg --quiet --decrypt "$OUT/chiffre/$name" >> "$OUT/honeypot-clair.jsonl"; then
    rm -f "$OUT/chiffre/$name"
    # 4. Effacer de la VM SEULEMENT ce qu'on a bien recupere et dechiffre.
    ssh "$HP" "rm -f '$remote'"
  else
    echo "Dechiffrement echoue pour $name : on le garde pour reessayer." >&2
  fi
done

echo "Journal a jour : $OUT/honeypot-clair.jsonl ($(wc -l < "$OUT/honeypot-clair.jsonl" 2>/dev/null || echo 0) lignes)"
