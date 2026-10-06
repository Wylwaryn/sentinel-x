#!/usr/bin/env bash
# Durcissement de la VM avant le pentest de jeudi. Idempotent : relançable sans effet de bord.
#
#   sudo hardening/apply.sh                      # SIMULATION (par défaut) : affiche ce qui changerait
#   sudo hardening/apply.sh --apply              # applique SSH, pare-feu (firewall.sh), CUPS
#   sudo hardening/apply.sh --apply --remove-sudoers
#                                                # + supprime le sudo sans mot de passe (TOUT DERNIER,
#                                                #   lancé par l'utilisateur : ensuite sudo demande le mot de passe)
#
# Ne s'applique qu'après un « → VM : appliquer le durcissement » dans CLAUDE.md (jeudi matin).
# Contrôle ensuite : sudo hardening/verify.sh
set -euo pipefail
# Sorties des outils en anglais (ufw, systemctl) : comparaisons fiables quelle que soit la langue du système.
export LC_ALL=C LANGUAGE=C

ADMIN=wyllwaryn
SSHD_DROPIN=/etc/ssh/sshd_config.d/00-sentinel-hardening.conf
SUDOERS_TMP=/etc/sudoers.d/90-sentinel-setup
CUPS_UNITS=(cups.service cups.socket cups.path cups-browsed.service)

APPLY=0 REMOVE_SUDOERS=0
for arg in "$@"; do
    case "$arg" in
        --apply) APPLY=1 ;;
        --remove-sudoers) REMOVE_SUDOERS=1 ;;
        --dry-run) APPLY=0 ;;
        *) sed -n '2,11p' "$0" >&2; exit 1 ;;
    esac
done
[ "$(id -u)" -eq 0 ] || { echo "À lancer avec sudo." >&2; exit 1; }
[ $REMOVE_SUDOERS -eq 0 ] || [ $APPLY -eq 1 ] || { echo "--remove-sudoers exige --apply." >&2; exit 1; }

CHANGES=0
title() { echo; echo "== $1"; }
same()  { echo "  conforme : $1"; }
do_() {  # do_ "description" commande...
    CHANGES=$((CHANGES+1))
    if [ $APPLY -eq 1 ]; then echo "  APPLIQUÉ : $1"; "${@:2}"; else echo "  À FAIRE  : $1"; fi
}

[ $APPLY -eq 1 ] && echo "MODE APPLICATION" || echo "MODE SIMULATION (rien n'est modifié ; --apply pour appliquer)"

# ---------------------------------------------------------------------------
title "SSH : clé uniquement, pas de root, $ADMIN seul autorisé"
# Garde-fou : sans clé autorisée, couper les mots de passe nous enfermerait dehors.
keys="/home/$ADMIN/.ssh/authorized_keys"
if ! grep -qE '^(ssh-|ecdsa-|sk-)' "$keys" 2>/dev/null; then
    echo "  ARRÊT : aucune clé publique dans $keys, durcissement SSH impossible sans risque." >&2
    exit 1
fi
# 00- : chargé avant les autres fichiers (pour sshd, la première valeur lue l'emporte).
wanted="# Sentinel-X : durcissement (server/hardening/apply.sh). Ne pas modifier à la main.
PasswordAuthentication no
PermitRootLogin no
KbdInteractiveAuthentication no
AllowUsers $ADMIN"
if [ -f "$SSHD_DROPIN" ] && [ "$(cat "$SSHD_DROPIN")" = "$wanted" ]; then
    same "$SSHD_DROPIN"
else
    install_sshd() {
        local backup=""
        [ -f "$SSHD_DROPIN" ] && backup=$(mktemp) && cp "$SSHD_DROPIN" "$backup"
        printf '%s\n' "$wanted" > "$SSHD_DROPIN"
        chmod 644 "$SSHD_DROPIN"
        if ! sshd -t; then
            echo "  configuration sshd invalide : retour arrière" >&2
            if [ -n "$backup" ]; then mv "$backup" "$SSHD_DROPIN"; else rm -f "$SSHD_DROPIN"; fi
            exit 1
        fi
        # Les sessions ouvertes ne sont pas coupées ; chaque nouvelle connexion lit la nouvelle config.
        systemctl reload ssh.service 2>/dev/null || true
    }
    do_ "écrire $SSHD_DROPIN, vérifier (sshd -t) puis recharger sshd" install_sshd
fi

# ---------------------------------------------------------------------------
# Pare-feu (UFW + ports des conteneurs) : délégué à firewall.sh, seule source des règles.
# Sans --egress : les sorties ne sont pas bloquées (option à décider, voir firewall.sh).
title "Pare-feu : voir hardening/firewall.sh"
if [ $APPLY -eq 1 ]; then "$(dirname "$0")/firewall.sh" --apply; else "$(dirname "$0")/firewall.sh"; fi \
    | sed -e '/^MODE /d' -e 's/^/  /'

# ---------------------------------------------------------------------------
title "CUPS (impression, port 631) : arrêté et masqué"
for unit in "${CUPS_UNITS[@]}"; do
    systemctl cat "$unit" >/dev/null 2>&1 || { same "$unit absent"; continue; }
    if [ "$(systemctl is-enabled "$unit" 2>/dev/null || true)" = "masked" ] && ! systemctl is-active -q "$unit"; then
        same "$unit masqué"
    else
        do_ "arrêter et masquer $unit" sh -c "systemctl stop '$unit'; systemctl mask '$unit' >/dev/null"
    fi
done

# ---------------------------------------------------------------------------
title "Rappels hors script (à cocher dans CLAUDE.md)"
echo "  GitHub : révoquer la deploy key de la VM (Settings > Deploy keys), ou la passer en lecture seule."
echo "           Clé locale : /home/$ADMIN/.ssh/github_sentinel (à supprimer une fois révoquée)."
echo "  VirtualBox : presse-papiers partagé et glisser-déposer sur « Désactivé »."
echo "  Windows : pare-feu en refus par défaut, 8883 limité au sous-réseau du point d'accès."

# ---------------------------------------------------------------------------
title "sudo sans mot de passe ($SUDOERS_TMP) : TOUT DERNIER"
if [ ! -e "$SUDOERS_TMP" ]; then
    same "$SUDOERS_TMP déjà supprimé"
elif [ $REMOVE_SUDOERS -eq 1 ]; then
    # Garde-fou : après suppression, $ADMIN doit pouvoir faire sudo avec son mot de passe.
    if ! id -nG "$ADMIN" | tr ' ' '\n' | grep -qx sudo; then
        echo "  ARRÊT : $ADMIN n'est pas dans le groupe sudo, la suppression le priverait de tout accès admin." >&2; exit 1
    fi
    if [ "$(passwd -S "$ADMIN" | awk '{print $2}')" != "P" ]; then
        echo "  ARRÊT : $ADMIN n'a pas de mot de passe utilisable." >&2; exit 1
    fi
    do_ "supprimer $SUDOERS_TMP (sudo demandera désormais le mot de passe)" rm -f "$SUDOERS_TMP"
    visudo -c >/dev/null && same "sudoers valide"
else
    echo "  NON TRAITÉ : à faire en dernier par l'utilisateur, avec --apply --remove-sudoers"
    echo "               (vérifier avant de connaître le mot de passe de $ADMIN)."
fi

echo
if [ $APPLY -eq 1 ]; then
    echo "Terminé : $CHANGES changement(s) appliqué(s). Contrôle : sudo hardening/verify.sh"
else
    echo "Simulation : $CHANGES changement(s) à appliquer."
fi
