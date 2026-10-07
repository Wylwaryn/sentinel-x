#!/usr/bin/env python3
"""Honeypot TCP generique : ecoute un port, envoie une banniere optionnelle, LOGGE tout ce que le
client envoie, puis ferme. Ne renvoie jamais rien d'utile et n'ouvre aucune connexion sortante.
(Explications detaillees : voir README.md)

Usage : tcp_tarpit.py --port 3306 --banner mysql [--max-bytes 4096]
"""
import argparse
import socket
import struct
import threading

from common import record, safe_text

MAX_CONN = 50          # plafond de connexions simultanees : un honeypot ne doit pas s'ecrouler sous un flood
READ_TIMEOUT = 15.0    # on attend un peu les octets du client, puis on ferme


def build_mysql_greeting() -> bytes:
    """Paquet de handshake MySQL v10 credible (pour que `nmap -sV` identifie 'MySQL'). Purement
    decoratif : aucune base derriere. On le CONSTRUIT (plutot qu'un blob fige) pour que le code
    montre la structure du protocole."""
    server_version = b"5.7.40-0ubuntu0.1\x00"        # chaine de version + octet nul
    thread_id = struct.pack("<I", 1234)               # 4 octets, entier little-endian
    auth1 = b"\x11\x22\x33\x44\x55\x66\x77\x88"       # 8 octets de "sel" (faux)
    filler = b"\x00"
    cap_low = b"\xff\xf7"                             # capacites (octets de poids faible)
    charset = b"\x21"                                 # utf8_general_ci
    status = b"\x02\x00"                              # statut serveur
    cap_high = b"\xff\x81"                            # capacites (octets de poids fort)
    auth_len = b"\x15"                                # longueur des donnees d'auth
    reserved = b"\x00" * 10
    auth2 = b"\x99\xaa\xbb\xcc\xdd\xee\xff\x00\x11\x22\x33\x00"
    plugin = b"mysql_native_password\x00"
    body = (b"\x0a" + server_version + thread_id + auth1 + filler + cap_low + charset +
            status + cap_high + auth_len + reserved + auth2 + plugin)
    # En-tete MySQL : 3 octets de longueur (little-endian) + 1 octet de numero de sequence (0).
    header = struct.pack("<I", len(body))[:3] + b"\x00"
    return header + body


BANNERS = {
    "mysql": build_mysql_greeting,
    "none": lambda: b"",
}


def handle(conn: socket.socket, addr, dst_port: int, banner: bytes, max_bytes: int):
    """Traite UNE connexion : log de l'arrivee, envoi de la banniere, lecture + log, fermeture."""
    src_ip, src_port = addr
    base = {"service": "tcp", "src_ip": src_ip, "src_port": src_port, "dst_port": dst_port}
    record({**base, "event": "connect"})
    try:
        conn.settimeout(READ_TIMEOUT)
        if banner:
            conn.sendall(banner)                      # on "repond" pour avoir l'air d'un vrai service
        received = bytearray()
        while len(received) < max_bytes:
            chunk = conn.recv(4096)
            if not chunk:
                break
            received += chunk
            # On logge chaque salve : on voit les tentatives (login MySQL, injection, etc.) en clair.
            record({**base, "event": "data", "bytes": len(chunk),
                    "data_hex": chunk.hex(), "data_txt": safe_text(chunk)})
    except (OSError, socket.timeout):
        pass
    finally:
        record({**base, "event": "close"})
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="Honeypot TCP (journal uniquement)")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--banner", choices=BANNERS, default="none")
    ap.add_argument("--max-bytes", type=int, default=4096)
    ap.add_argument("--bind", default="0.0.0.0")      # le leurre est fait pour etre joignable
    args = ap.parse_args()
    banner = BANNERS[args.banner]()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.bind, args.port))
    srv.listen(MAX_CONN)
    record({"service": "tcp", "event": "listen", "dst_port": args.port, "banner": args.banner})

    while True:
        conn, addr = srv.accept()
        # Au-dela du plafond, on ferme tout de suite (mais on a deja logge l'IP a l'accept) :
        if threading.active_count() > MAX_CONN:
            record({"service": "tcp", "event": "refuse_flood", "src_ip": addr[0], "dst_port": args.port})
            conn.close()
            continue
        t = threading.Thread(target=handle, args=(conn, addr, args.port, banner, args.max_bytes), daemon=True)
        t.start()


if __name__ == "__main__":
    main()
