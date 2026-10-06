#!/usr/bin/env bash
# Crée un compte du dashboard (le rôle sentinel_dashboard n'a pas le droit de le faire : c'est voulu).
#
# Usage (dans la VM, pile démarrée) : sudo dashboard/api/add-user.sh <email> "<nom>" <ADMIN|OPERATEUR|LECTEUR|SERVICE_VISION>
#   - le mot de passe est demandé au clavier (jamais dans argv, l'historique ou les journaux) ;
#   - il est haché en Argon2id DANS le conteneur dashboard (même bibliothèque que la connexion) ;
#   - l'insertion se fait avec le compte administrateur PostgreSQL, valeurs passées en variables psql.
set -euo pipefail

cd "$(dirname "$0")/../../server"
[ $# -eq 3 ] || { echo "Usage : $0 <email> \"<nom>\" <ADMIN|OPERATEUR|LECTEUR|SERVICE_VISION>" >&2; exit 1; }
email=$1 nom=$2 role=$3
[[ $role =~ ^(ADMIN|OPERATEUR|LECTEUR|SERVICE_VISION)$ ]] || { echo "Rôle invalide : $role" >&2; exit 1; }

DC=(docker compose ${COMPOSE_ARGS:--f docker-compose.yml -f ../dashboard/docker-compose.yml})

if [ -n "${SENTINEL_PASSWORD:-}" ]; then
    pass=$SENTINEL_PASSWORD   # tests automatisés uniquement
else
    read -rsp "Mot de passe (12 caractères minimum) : " pass; echo
    read -rsp "Confirmation : " pass2; echo
    [ "$pass" = "$pass2" ] || { echo "Les mots de passe diffèrent." >&2; exit 1; }
fi
[ ${#pass} -ge 12 ] || { echo "Mot de passe trop court." >&2; exit 1; }

hash=$(printf '%s' "$pass" | "${DC[@]}" exec -T dashboard \
    python -c 'import sys; from app.security import hash_password; print(hash_password(sys.stdin.read()))')
unset pass pass2

"${DC[@]}" exec -T postgres sh -c \
    'PGPASSWORD=$POSTGRES_PASSWORD psql -X -q -v ON_ERROR_STOP=1 -U $POSTGRES_USER -d $POSTGRES_DB \
       -v email="$1" -v nom="$2" -v role="$3" -v hash="$4"' _ "$email" "$nom" "$role" "$hash" <<'EOSQL'
INSERT INTO utilisateur (id_role, nom, email, mot_de_passe_hash)
SELECT id_role, :'nom', lower(trim(:'email')), :'hash' FROM role WHERE code = :'role';
EOSQL
echo "Compte $email ($role) créé."
