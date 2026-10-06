#!/usr/bin/env bash
# Rapport de conformité sécurité de la VM (lecture seule, ne modifie rien).
#
#   sudo hardening/verify.sh               # rapport lisible dans le terminal
#   sudo hardening/verify.sh --markdown    # même rapport en tableau Markdown (matrice du dossier)
#
# Code retour = nombre de contrôles NON CONFORMES (0 = tout est conforme).
# Avant jeudi, les points de hardening/apply.sh apparaissent normalement « À FAIRE ».
# Pas de pipefail : plusieurs contrôles lisent la sortie de commandes qui échouent volontairement.
set -u
export LC_ALL=C LANGUAGE=C

cd "$(dirname "$0")/.."
[ "$(id -u)" -eq 0 ] || { echo "À lancer avec sudo." >&2; exit 1; }

MD=0; [ "${1:-}" = "--markdown" ] && MD=1
ADMIN=wyllwaryn
OK=0 KO=0 TODO=0
SECTION=""

section() { SECTION=$1; [ $MD -eq 1 ] && printf '\n### %s\n\n| Contrôle | État | Détail |\n|---|---|---|\n' "$1" || printf '\n== %s\n' "$1"; }
line() {  # état contrôle détail
    case "$1" in OK) OK=$((OK+1)) ;; KO) KO=$((KO+1)) ;; À\ FAIRE) TODO=$((TODO+1)) ;; esac
    if [ $MD -eq 1 ]; then printf '| %s | %s | %s |\n' "$2" "$1" "${3:-}"
    else printf '  %-10s %s%s\n' "$1" "$2" "${3:+  ($3)}"; fi
}
# Contrôle couvert par hardening/apply.sh : non conforme = « À FAIRE » (jeudi), pas une faille imprévue.
planned() { if [ "$1" = 0 ]; then line OK "$2" "${3:-}"; else line "À FAIRE" "$2" "${3:-} -> hardening/apply.sh"; fi; }
must()    { if [ "$1" = 0 ]; then line OK "$2" "${3:-}"; else line KO "$2" "${3:-}"; fi; }
manual()  { line MANUEL "$1" "${2:-}"; }

[ $MD -eq 1 ] && printf '## Rapport de sécurité de la VM sentinel-server (%s UTC)\n' "$(date -u '+%Y-%m-%d %H:%M')" \
              || printf 'Rapport de sécurité VM sentinel-server, %s UTC\n' "$(date -u '+%Y-%m-%d %H:%M')"

# ---------------------------------------------------------------------------
section "Accès SSH"
sshd_cfg=$(sshd -T 2>/dev/null)
v() { awk -v k="$1" '$1==k{ $1=""; sub(/^ /,""); print; exit }' <<<"$sshd_cfg"; }
[ "$(v passwordauthentication)" = no ];       planned $? "Authentification par mot de passe désactivée" "passwordauthentication $(v passwordauthentication)"
[ "$(v permitrootlogin)" = no ];              planned $? "Connexion root interdite" "permitrootlogin $(v permitrootlogin)"
[ "$(v kbdinteractiveauthentication)" = no ]; planned $? "Authentification clavier-interactive désactivée"
[ "$(v allowusers)" = "$ADMIN" ];             planned $? "Seul $ADMIN autorisé" "allowusers $(v allowusers)"
[ "$(v pubkeyauthentication)" = yes ] && grep -qE '^(ssh-|ecdsa-|sk-)' "/home/$ADMIN/.ssh/authorized_keys" 2>/dev/null
must $? "Authentification par clé active, clé autorisée présente"
manual "Port 22 joignable seulement via 127.0.0.1:2222 côté Windows" "redirection NAT VirtualBox"

# ---------------------------------------------------------------------------
section "Privilèges"
[ ! -e /etc/sudoers.d/90-sentinel-setup ]; planned $? "sudo sans mot de passe supprimé" "/etc/sudoers.d/90-sentinel-setup (dernière action, par l'utilisateur)"
id -nG "$ADMIN" | tr ' ' '\n' | grep -qx sudo && [ "$(passwd -S "$ADMIN" | awk '{print $2}')" = P ]
must $? "$ADMIN garde sudo avec mot de passe" "groupe sudo + mot de passe défini"
[ "$(stat -c '%U %a' .env)" = "root 600" ]; must $? "Fichier .env réservé à root" "$(stat -c '%U %a' .env)"
[ "$(stat -c '%U %a' certs/ca)" = "root 700" ] && [ "$(stat -c '%a' certs/ca/ca.key)" = 600 ]
must $? "Clé privée de la CA confinée" "certs/ca root 700, ca.key 600"
bad_keys=$(find certs secrets -type f \( -name '*.key' -o -name passwd \) ! -perm 600 2>/dev/null)
[ -z "$bad_keys" ]; must $? "Clés serveur et passwd MQTT en 600" "${bad_keys:-ok}"
tracked=$(git -C .. ls-files | grep -E '(^|/)(\.env|.*\.key|.*\.pem|passwd)$|/certs/|/secrets/' | grep -v '\.env\.example$')
[ -z "$tracked" ]; must $? "Aucun secret suivi par Git" "${tracked:-ok}"

# ---------------------------------------------------------------------------
section "Pare-feu et services réseau"
ufw_out=$(ufw status verbose 2>/dev/null)
grep -q "^Status: active" <<<"$ufw_out"; planned $? "UFW actif"
grep -q "Default: deny (incoming)" <<<"$ufw_out"; planned $? "UFW : refus par défaut en entrée" "$(grep '^Default' <<<"$ufw_out")"
gw=$(ip -4 route show default | awk '{for(i=1;i<NF;i++) if($i=="via") print $(i+1); exit}')
grep -qE "^22/tcp +ALLOW IN +${gw//./\\.}( |$)" <<<"$ufw_out"
planned $? "UFW : SSH 22 autorisé depuis la passerelle NAT ($gw) uniquement"
grep -qE '^22/tcp +ALLOW IN +Anywhere' <<<"$ufw_out"; [ $? -ne 0 ]
planned $? "UFW : pas de SSH ouvert à toute source"
extra=$(grep ALLOW <<<"$ufw_out" | grep -vE '^22/tcp( \(v6\))? ' | grep -v ' OUT ' | awk '{print $1}' | sort -u | tr '\n' ' ')
[ -z "$extra" ]; must $? "UFW : aucune ouverture en entrée autre que SSH" "${extra:-ok}"
du=$(iptables -S DOCKER-USER 2>/dev/null)
grep -q -- "-i enp0s3 -j DROP" <<<"$du" && grep -q -- "--ctorigdstport 8883 -j RETURN" <<<"$du" \
    && grep -q -- "--ctorigdstport 8443 -j RETURN" <<<"$du" \
    && grep -q -- "-s 192.168.137.0/24 .*--ctorigdstport 8883 -j RETURN" <<<"$du" \
    && ! grep -q -- "-s 192.168.137.0/24 .*--ctorigdstport 8443" <<<"$du"
planned $? "Ports des conteneurs filtrés (DOCKER-USER : 8883/443 depuis $gw + Wi-Fi de la table, 8443 depuis $gw seul)" "Docker contourne UFW"
grep -q "SENTINEL-X DOCKER-USER" /etc/ufw/after.rules /etc/ufw/after6.rules 2>/dev/null
planned $? "Filtrage DOCKER-USER persistant (rechargé par UFW au démarrage, IPv4 et IPv6)"
pub=$(docker ps --format '{{.Ports}}' | grep -oE '0\.0\.0\.0:[0-9]+' | cut -d: -f2 | sort -un | tr '\n' ' ')
[ -z "$(tr ' ' '\n' <<<"$pub" | grep -vxE '8883|8443|443|')" ]
must $? "Ports publiés par Docker dans la liste blanche (8883, 8443, 443)" "publiés : ${pub:-aucun}"
if grep -q "deny (outgoing)" <<<"$ufw_out"; then line INFO "Sorties de la VM bloquées sauf DNS/NTP" "firewall.sh --egress"
else line "À FAIRE" "Sorties de la VM limitées à DNS et NTP" "jeudi après le gel -> firewall.sh --apply --egress"; fi
! systemctl is-active -q cups.service cups.socket 2>/dev/null; planned $? "CUPS arrêté" "port 631"
[ "$(systemctl is-enabled cups.service 2>/dev/null)" = masked ]; planned $? "CUPS masqué (ne redémarre pas)"
listen=$(ss -Htln | awk '{print $4}')
public=$(grep -vE '^(127\.|\[::1\]|\[::ffff:127\.)' <<<"$listen" | sed -E 's/.*:([0-9]+)$/\1/' | sort -un | tr '\n' ' ')
expected="22 443 8443 8883 "
[ "$public" = "$expected" ]; must $? "Ports en écoute hors loopback = 22, 443, 8443, 8883" "trouvés : ${public:-aucun}"
manual "Redirections VirtualBox : 8443 et 2222 liées à 127.0.0.1, pare-feu Windows (windows_firewall.ps1)"
manual "VirtualBox : presse-papiers et glisser-déposer désactivés" "réglage de la VM, côté Windows"
manual "Deploy key GitHub révoquée ou en lecture seule" "github.com > Settings > Deploy keys"

# ---------------------------------------------------------------------------
section "Docker"
daemon=/etc/docker/daemon.json
grep -q '"userns-remap"' $daemon; must $? "userns-remap (root conteneur != root hôte)"
grep -q '"no-new-privileges": true' $daemon; must $? "no-new-privileges par défaut"
grep -q '"max-size"' $daemon; must $? "Rotation des journaux Docker" "$(grep -o '"max-size": "[^"]*"\|"max-file": "[^"]*"' $daemon | tr '\n' ' ')"
for c in $(docker compose ps --format '{{.Service}}' 2>/dev/null); do
    id=$(docker compose ps -q "$c")
    info=$(docker inspect "$id" --format '{{.Config.User}}|{{.HostConfig.CapDrop}}|{{.HostConfig.ReadonlyRootfs}}|{{.HostConfig.Privileged}}|{{.State.Health}}|{{.RestartCount}}|{{range $p, $b := .HostConfig.PortBindings}}{{$p}} {{end}}')
    IFS='|' read -r user capdrop ro priv health restarts ports <<<"$info"
    [[ $capdrop == *ALL* ]] && [ "$priv" = false ]; must $? "$c : cap_drop ALL, non privilégié"
    if [ "$c" = postgres ]; then
        [ -z "$ports" ]; must $? "$c : aucun port publié" "${ports:-réseaux internes uniquement}"
        envs=$(docker inspect "$id" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -cE '^(MQTT_|INGEST_TOKEN)')
        [ "$envs" = 0 ]; must $? "$c : ne reçoit ni mot de passe MQTT ni jeton d'API"
    else
        [ -n "$user" ] && [ "$user" != root ] && [ "$user" != 0 ] && [ "$ro" = true ]
        must $? "$c : utilisateur non root, système de fichiers en lecture seule" "user=$user"
    fi
    [ "$restarts" = 0 ]; must $? "$c : aucun redémarrage inattendu" "restarts=$restarts"
done
for n in net_ingest net_dashboard; do
    [ "$(docker network inspect "sentinel_$n" --format '{{.Internal}}' 2>/dev/null)" = true ]; must $? "Réseau $n interne (pas d'accès extérieur)"
done
for n in net_mqtt_edge net_ingest_edge; do
    [ "$(docker network inspect "sentinel_$n" --format '{{index .Options "com.docker.network.bridge.enable_ip_masquerade"}}' 2>/dev/null)" = false ]
    must $? "Réseau $n sans NAT sortant"
done

# ---------------------------------------------------------------------------
section "Services exposés (tests fonctionnels)"
CA=certs/ca.crt
s_client() { timeout 5 openssl s_client -connect "127.0.0.1:$1" -CAfile $CA "${@:2}" </dev/null 2>&1; }
s_client 8883 -verify_return_error | grep -q "Verify return code: 0"; must $? "MQTTS 8883 : certificat valide (notre CA)"
s_client 8883 -tls1_1 -cipher 'DEFAULT@SECLEVEL=0' | grep -q "alert protocol version"; must $? "MQTTS 8883 : TLS 1.1 refusé"
timeout 3 bash -c 'exec 3<>/dev/tcp/127.0.0.1/1883' 2>/dev/null; [ $? -ne 0 ]; must $? "Aucun MQTT en clair (1883 fermé)"
anon=$(docker run --rm --network sentinel_net_ingest -v "$PWD/$CA:/ca.crt:ro" eclipse-mosquitto:2.0 \
    mosquitto_pub -h mosquitto -p 8883 --cafile /ca.crt -V mqttv5 -t sentinel/telemetry -m x 2>&1)
grep -qi "not authorized" <<<"$anon"; must $? "MQTT anonyme refusé"
code=$(curl -s -o /dev/null -w '%{http_code}' --cacert $CA -X POST https://127.0.0.1:8443/api/v1/alerts)
[ "$code" = 401 ]; must $? "API d'ingestion : 401 sans jeton" "HTTP $code"
code=$(curl -s -o /dev/null -w '%{http_code}' --cacert $CA https://127.0.0.1:8443/docs)
[ "$code" = 404 ]; must $? "API d'ingestion : pas de /docs" "HTTP $code"
curl -s --tlsv1.1 --tls-max 1.1 --cacert $CA https://127.0.0.1:8443/healthz >/dev/null 2>&1; [ $? -ne 0 ]
must $? "API d'ingestion : TLS 1.1 refusé"

# ---------------------------------------------------------------------------
if [ $MD -eq 1 ]; then
    printf '\n**Bilan** : %s conforme(s), %s à faire (durcissement de jeudi), %s non conforme(s).\n' $OK $TODO $KO
else
    printf '\nBilan : %s OK, %s À FAIRE (hardening/apply.sh), %s KO, plus les contrôles MANUEL à cocher.\n' $OK $TODO $KO
fi
exit $KO
