#!/usr/bin/env bash
# Pare-feu de la VM Sentinel-X en « mode pentest » (réversible), pendant de host/hardening/windows_firewall.ps1.
#
#   sudo hardening/firewall.sh                    SIMULATION : affiche ce qui changerait (rien n'est modifié)
#   sudo hardening/firewall.sh --apply            applique (entrées de la VM + ports des conteneurs)
#   sudo hardening/firewall.sh --apply --egress   + bloque les SORTIES de la VM (sauf DNS, NTP)
#   sudo hardening/firewall.sh --restore          remet le pare-feu exactement comme avant le premier --apply
#   sudo hardening/firewall.sh --observe          OBSERVATION : même liste blanche, mais journalise au lieu
#                                                 de bloquer (non persistant). --observe-stop pour arrêter.
#
# 1. Entrées de la VM (UFW) : refus par défaut ; SSH 22 autorisé UNIQUEMENT depuis la passerelle NAT
#    VirtualBox (10.0.2.2), c'est-à-dire via la redirection 127.0.0.1:2222 du PC hôte.
# 2. Ports des conteneurs (chaîne DOCKER-USER) : Docker publie ses ports en contournant UFW.
#    Liste blanche : 8883 (MQTTS), 8443 (ingestion), 443 (Caddy, futur) depuis 10.0.2.2.
#    Toute autre connexion entrante vers un conteneur est journalisée puis bloquée (IPv4 et IPv6),
#    même si un port est publié par erreur (ex. 5432).
#    Règles posées dans /etc/ufw/after*.rules : rechargées par UFW à chaque démarrage.
# 3. --egress (optionnel, à décider) : la VM et les conteneurs ne peuvent plus initier de connexion
#    vers l'extérieur, sauf DNS et NTP. Coupe un reverse shell pendant le pentest, mais aussi
#    git pull, apt, docker pull et pip : à n'activer qu'après le gel du code.
#
# Journal des blocages : journalctl -k | grep SENTINEL
set -euo pipefail
export LC_ALL=C LANGUAGE=C

DOCKER_PORTS=(8883 8443 443)
BACKUP=/var/backups/sentinel-x/pare-feu-avant-durcissement
MARK_BEGIN="# BEGIN SENTINEL-X DOCKER-USER (server/hardening/firewall.sh, ne pas modifier)"
MARK_END="# END SENTINEL-X DOCKER-USER"
UFW_FILES=(/etc/ufw/user.rules /etc/ufw/user6.rules /etc/ufw/after.rules /etc/ufw/after6.rules
           /etc/ufw/ufw.conf /etc/default/ufw)

MODE=dry EGRESS=0
for arg in "$@"; do
    case "$arg" in
        --apply) MODE=apply ;;
        --restore) MODE=restore ;;
        --egress) EGRESS=1 ;;
        --observe) MODE=observe ;;
        --observe-stop) MODE=observe-stop ;;
        --dry-run) ;;
        *) sed -n '2,10p' "$0" >&2; exit 1 ;;
    esac
done
[ "$(id -u)" -eq 0 ] || { echo "À lancer avec sudo." >&2; exit 1; }
command -v ufw >/dev/null || { echo "ufw absent." >&2; exit 1; }

# Interface et passerelle NAT VirtualBox, lues dans la table de routage (enp0s3 / 10.0.2.2).
read -r IFACE GW < <(ip -4 route show default | awk '{for(i=1;i<NF;i++){if($i=="dev")d=$(i+1);if($i=="via")g=$(i+1)} print d, g; exit}')
[ -n "${IFACE:-}" ] && [ -n "${GW:-}" ] || { echo "Route par défaut introuvable." >&2; exit 1; }

CHANGES=0
title() { echo; echo "== $1"; }
same()  { echo "  conforme : $1"; }
todo()  {  # todo "description" commande...
    CHANGES=$((CHANGES+1))
    if [ $MODE = apply ]; then echo "  APPLIQUÉ : $1"; "${@:2}"; else echo "  À FAIRE  : $1"; fi
}

# ---------------------------------------------------------------------------- observation
# Aucun blocage : les règles d'autorisation comptent le trafic légitime, la règle LOG montre ce
# que --apply bloquerait. À lancer pendant l'intégration (mercredi) pour valider la liste blanche.
if [ $MODE = observe ] || [ $MODE = observe-stop ]; then
    if grep -qF "$MARK_BEGIN" /etc/ufw/after.rules; then
        echo "Le mode --apply est déjà en place : l'observation n'a plus de sens." >&2; exit 1
    fi
    iptables -F DOCKER-USER
    if [ $MODE = observe-stop ]; then echo "Observation arrêtée (DOCKER-USER vidée)."; exit 0; fi
    iptables -A DOCKER-USER -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN
    for p in "${DOCKER_PORTS[@]}"; do
        iptables -A DOCKER-USER -i "$IFACE" -s "$GW" -p tcp -m conntrack --ctstate NEW --ctorigdstport "$p" -j RETURN
    done
    iptables -A DOCKER-USER -i "$IFACE" -m limit --limit 30/min -j LOG --log-prefix "[SENTINEL OBSERVATION] "
    iptables -A DOCKER-USER -j RETURN
    echo "Observation active (rien n'est bloqué) :"
    echo "  compteurs      : sudo iptables -L DOCKER-USER -v -n --line-numbers"
    echo "  serait bloqué  : sudo journalctl -k | grep 'SENTINEL OBSERVATION'"
    echo "  arrêter        : sudo hardening/firewall.sh --observe-stop (ou redémarrage de la VM)"
    exit 0
fi

# ---------------------------------------------------------------------------- restauration
if [ $MODE = restore ]; then
    [ -d "$BACKUP" ] || { echo "Aucune sauvegarde ($BACKUP) : rien à restaurer."; exit 0; }
    for f in "${UFW_FILES[@]}"; do
        [ -f "$BACKUP/$(basename "$f")" ] && cp -p "$BACKUP/$(basename "$f")" "$f"
    done
    # Les fichiers restaurés ne déclarent plus DOCKER-USER : UFW ne la viderait pas, on le fait ici.
    iptables -F DOCKER-USER 2>/dev/null || true
    ip6tables -F DOCKER-USER 2>/dev/null || true
    if grep -q '^ENABLED=yes' /etc/ufw/ufw.conf; then ufw reload >/dev/null; else ufw disable >/dev/null; fi
    echo "Pare-feu restauré depuis $BACKUP."
    ufw status verbose | head -4
    exit 0
fi

[ $MODE = apply ] && echo "MODE APPLICATION$([ $EGRESS = 1 ] && echo ' (avec blocage des sorties)')" \
                  || echo "MODE SIMULATION (rien n'est modifié ; --apply pour appliquer)"
echo "Interface externe : $IFACE, passerelle NAT VirtualBox : $GW"

# ---------------------------------------------------------------------------- sauvegarde
if [ $MODE = apply ] && [ ! -d "$BACKUP" ]; then
    install -d -m 700 "$BACKUP"
    for f in "${UFW_FILES[@]}"; do [ -f "$f" ] && cp -p "$f" "$BACKUP/"; done
    iptables-save > "$BACKUP/iptables.rules"
    ip6tables-save > "$BACKUP/ip6tables.rules"
    ufw status verbose > "$BACKUP/ufw-status.txt"
    echo "  Sauvegarde complète : $BACKUP (pour --restore)"
fi

# ---------------------------------------------------------------------------- 1. UFW (entrées de la VM)
title "Entrées de la VM (UFW) : refus par défaut, SSH depuis $GW uniquement"
status=$(ufw status verbose)
added=$(ufw show added)
grep -q "Default: deny (incoming)" <<<"$status" && same "politique d'entrée : deny" \
    || todo "ufw default deny incoming" ufw default deny incoming
grep -q "deny (routed)" <<<"$status" && same "politique de routage : deny" \
    || todo "ufw default deny routed" ufw default deny routed

ssh_rule=(allow from "$GW" to any port 22 proto tcp comment "SSH via NAT VirtualBox (127.0.0.1:2222 du PC hote)")
if grep -qF "ufw allow from $GW to any port 22 proto tcp" <<<"$added"; then
    same "SSH 22 autorisé depuis $GW"
else
    todo "ufw allow from $GW to any port 22 proto tcp" ufw "${ssh_rule[@]}"
fi
# Règle « 22 depuis n'importe où » (ancienne version d'apply.sh) : remplacée par la précédente.
if grep -qxF "ufw allow 22/tcp" <<<"$(sed 's/ comment.*//' <<<"$added")"; then
    todo "supprimer la règle « 22/tcp depuis n'importe où »" ufw delete allow 22/tcp
fi
others=$(grep '^ufw ' <<<"$added" | grep -vE "port 22 proto tcp|^ufw allow 22/tcp|^ufw (allow|deny) out" || true)
[ -z "$others" ] && same "aucune autre ouverture en entrée" \
    || { echo "  ATTENTION : autres règles d'entrée, à revoir à la main :"; sed 's/^/      /' <<<"$others"; }

# ---------------------------------------------------------------------------- 2. DOCKER-USER
title "Ports des conteneurs (DOCKER-USER) : ${DOCKER_PORTS[*]} depuis $GW, tout le reste bloqué"
block4="$MARK_BEGIN
*filter
:DOCKER-USER - [0:0]
-A DOCKER-USER -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN"
for p in "${DOCKER_PORTS[@]}"; do
    block4+="
-A DOCKER-USER -i $IFACE -s $GW -p tcp -m conntrack --ctstate NEW --ctorigdstport $p -j RETURN"
done
block4+="
-A DOCKER-USER -i $IFACE -m limit --limit 6/min --limit-burst 10 -j LOG --log-prefix \"[SENTINEL DOCKER BLOQUE] \"
-A DOCKER-USER -i $IFACE -j DROP"
if [ $EGRESS = 1 ]; then block4+="
-A DOCKER-USER -o $IFACE -m conntrack --ctstate NEW -m limit --limit 6/min -j LOG --log-prefix \"[SENTINEL SORTIE BLOQUEE] \"
-A DOCKER-USER -o $IFACE -m conntrack --ctstate NEW -j DROP"; fi
block4+="
-A DOCKER-USER -j RETURN
COMMIT
$MARK_END"
# IPv6 : VirtualBox ne redirige aucun port en IPv6 ; aucune connexion entrante n'a lieu d'être.
block6="$MARK_BEGIN
*filter
:DOCKER-USER - [0:0]
-A DOCKER-USER -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN
-A DOCKER-USER -i $IFACE -j DROP"
if [ $EGRESS = 1 ]; then block6+="
-A DOCKER-USER -o $IFACE -m conntrack --ctstate NEW -j DROP"; fi
block6+="
-A DOCKER-USER -j RETURN
COMMIT
$MARK_END"

# Validation de la syntaxe par le noyau, sans rien appliquer (transaction annulée).
iptables-restore --noflush --test <<<"$block4" || { echo "  Règles IPv4 invalides." >&2; exit 1; }
ip6tables-restore --noflush --test <<<"$block6" || { echo "  Règles IPv6 invalides." >&2; exit 1; }
echo "  Syntaxe validée (iptables-restore --test)."

write_block() {  # fichier bloc
    local file=$1 block=$2 tmp
    tmp=$(mktemp)
    # Retire l'ancien bloc géré, puis ajoute le nouveau en fin de fichier.
    awk -v b="$MARK_BEGIN" -v e="$MARK_END" '$0==b{skip=1} !skip{print} $0==e{skip=0}' "$file" > "$tmp"
    printf '%s\n' "$block" >> "$tmp"
    cat "$tmp" > "$file"   # conserve propriétaire et droits
    rm -f "$tmp"
}
current_block() { awk -v b="$MARK_BEGIN" -v e="$MARK_END" '$0==b{p=1} p{print} $0==e{p=0}' "$1"; }
for pair in "/etc/ufw/after.rules:block4" "/etc/ufw/after6.rules:block6"; do
    file=${pair%%:*}; var=${pair#*:}
    if [ "$(current_block "$file")" = "${!var}" ]; then
        same "$file"
    else
        todo "écrire les règles DOCKER-USER dans $file" write_block "$file" "${!var}"
    fi
done
[ $MODE = dry ] && { echo "  Règles IPv4 prévues :"; grep '^-A' <<<"$block4" | sed 's/^/      /'; }

# Ports réellement publiés par Docker : tout port hors liste blanche sera bloqué, on le signale.
published=$(docker ps --format '{{.Ports}}' | grep -oE '0\.0\.0\.0:[0-9]+' | cut -d: -f2 | sort -un)
for p in $published; do
    [[ " ${DOCKER_PORTS[*]} " == *" $p "* ]] && same "port publié $p dans la liste blanche" \
        || echo "  ATTENTION : port $p publié par Docker mais hors liste blanche : il sera BLOQUÉ."
done

# ---------------------------------------------------------------------------- 3. sorties (optionnel)
title "Sorties de la VM$([ $EGRESS = 1 ] && echo ' : bloquées sauf DNS et NTP' || echo ' : non modifiées (option --egress)')"
if [ $EGRESS = 1 ]; then
    # Résolveurs DNS réellement utilisés (systemd-resolved), sinon le DNS du NAT VirtualBox.
    dns=$(resolvectl dns "$IFACE" 2>/dev/null | cut -d: -f2- | tr ' ' '\n' | grep -E '^[0-9.]+$' || true)
    [ -n "$dns" ] || dns=10.0.2.3
    out_rules=()
    for d in $dns; do out_rules+=("allow out to $d port 53 comment DNS"); done
    out_rules+=("allow out 123/udp comment NTP"
                "allow out to 172.16.0.0/12 comment Reseaux-Docker-locaux")
    for r in "${out_rules[@]}"; do
        if grep -qF "ufw ${r% comment*}" <<<"$added"; then same "${r% comment*}"
        else
            read -ra words <<<"${r% comment*}"
            todo "ufw ${r% comment*}" ufw "${words[@]}" comment "${r#*comment }"
        fi
    done
    grep -q "deny (outgoing)" <<<"$status" && same "politique de sortie : deny" \
        || todo "ufw default deny outgoing (git, apt, docker pull coupés jusqu'à --restore)" ufw default deny outgoing
else
    echo "  inchangé : politique de sortie actuelle ($(grep -oE '(allow|deny) \(outgoing\)' <<<"$status"))"
fi

# ---------------------------------------------------------------------------- activation
title "Activation"
if grep -q "^Status: active" <<<"$status"; then
    if [ $MODE = apply ] && [ $CHANGES -gt 0 ]; then todo "recharger UFW" ufw reload; else same "UFW actif"; fi
else
    todo "activer UFW (au démarrage aussi)" ufw --force enable
fi

echo
if [ $MODE = apply ]; then
    echo "Terminé : $CHANGES changement(s). Chaîne DOCKER-USER active :"
    iptables -S DOCKER-USER | sed 's/^/  /'
    echo "Contrôle : sudo hardening/verify.sh ; retour arrière : sudo hardening/firewall.sh --restore"
else
    echo "Simulation : $CHANGES changement(s) à appliquer."
fi
