#!/usr/bin/env bash
# PKI Sentinel-X : notre propre CA + certificats serveur (Mosquitto, Caddy...).
#
# Usage (dans la VM, depuis /opt/sentinel-x/server) :
#   sudo pki/pki.sh ca                                  # une seule fois
#   sudo pki/pki.sh server <service> <uid> <SAN,...>    # par service TLS
#   ex : sudo pki/pki.sh server mosquitto 1883 DNS:mosquitto,IP:127.0.0.1,IP:192.168.137.1
#
# Sortie (certs/ est ignoré par Git) :
#   certs/ca/ca.key           root:root 600 : ne quitte JAMAIS la VM, jamais monté
#   certs/ca.crt              public : à copier sur Windows (host/*/certs/) et dans le firmware
#   certs/<service>/          server.crt, server.key, ca.crt, montés en lecture seule
#
# La clé serveur appartient à l'uid du service *après* remappage userns
# (base dockremap + uid), en 600 : lisible par le conteneur et par personne d'autre.
#
# Choix crypto : EC P-256 + SHA-256, que BearSSL (ESP8266) sait vérifier.
# Regénérer un certificat serveur (nouvelle IP...) ne change PAS la CA :
# le firmware et les clients restent valides.
set -euo pipefail
umask 077

cd "$(dirname "$0")/.."
CERTS=certs
CA_DIR=$CERTS/ca

[ "$(id -u)" -eq 0 ] || { echo "À lancer avec sudo." >&2; exit 1; }

cmd_ca() {
    if [ -f "$CA_DIR/ca.key" ]; then
        echo "CA déjà présente ($CA_DIR/ca.key) : rien à faire." >&2
        echo "La supprimer invaliderait le ca.crt embarqué dans l'ESP et sur Windows." >&2
        exit 1
    fi
    mkdir -p "$CA_DIR"
    chmod 700 "$CA_DIR"
    openssl ecparam -name prime256v1 -genkey -noout -out "$CA_DIR/ca.key"
    openssl req -x509 -new -key "$CA_DIR/ca.key" -sha256 -days 1095 \
        -subj "/O=Sentinel-X/CN=Sentinel-X Root CA" \
        -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" \
        -addext "subjectKeyIdentifier=hash" \
        -out "$CA_DIR/ca.crt"
    install -m 644 "$CA_DIR/ca.crt" "$CERTS/ca.crt"
    chmod 755 "$CERTS"
    echo "CA créée. Empreinte :"
    openssl x509 -in "$CERTS/ca.crt" -noout -fingerprint -sha256
}

cmd_server() {
    local service=$1 uid=$2 san=$3
    [[ $service =~ ^[a-z0-9_-]+$ ]] || { echo "Nom de service invalide." >&2; exit 1; }
    [[ $uid =~ ^[0-9]+$ ]] || { echo "uid invalide." >&2; exit 1; }
    [ -f "$CA_DIR/ca.key" ] || { echo "Pas de CA : lancer d'abord « pki.sh ca »." >&2; exit 1; }

    # uid vu par l'hôte = base dockremap + uid dans le conteneur
    local base
    base=$(awk -F: '$1=="dockremap"{print $2}' /etc/subuid)
    [ -n "$base" ] || { echo "dockremap absent de /etc/subuid (userns-remap ?)" >&2; exit 1; }
    local host_uid=$((base + uid))

    local out=$CERTS/$service
    mkdir -p "$out"
    local cn=${san%%,*}; cn=${cn#*:}

    openssl ecparam -name prime256v1 -genkey -noout -out "$out/server.key"
    openssl req -new -key "$out/server.key" -subj "/O=Sentinel-X/CN=$cn" -out "$out/server.csr"
    openssl x509 -req -in "$out/server.csr" -CA "$CA_DIR/ca.crt" -CAkey "$CA_DIR/ca.key" \
        -CAcreateserial -sha256 -days 397 \
        -extfile <(printf '%s\n' \
            "basicConstraints=critical,CA:FALSE" \
            "keyUsage=critical,digitalSignature" \
            "extendedKeyUsage=serverAuth" \
            "subjectKeyIdentifier=hash" \
            "authorityKeyIdentifier=keyid" \
            "subjectAltName=$san") \
        -out "$out/server.crt"
    rm -f "$out/server.csr"
    install -m 644 "$CA_DIR/ca.crt" "$out/ca.crt"

    chmod 755 "$out"
    chmod 644 "$out/server.crt"
    chown "$host_uid:$host_uid" "$out/server.key"
    chmod 600 "$out/server.key"

    openssl verify -CAfile "$CA_DIR/ca.crt" "$out/server.crt"
    openssl x509 -in "$out/server.crt" -noout -ext subjectAltName
}

case "${1:-}" in
    ca) cmd_ca ;;
    server) [ $# -eq 4 ] || { sed -n '4,7p' "$0" >&2; exit 1; }; cmd_server "$2" "$3" "$4" ;;
    *) sed -n '2,20p' "$0" >&2; exit 1 ;;
esac
