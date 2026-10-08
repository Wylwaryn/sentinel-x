#!/usr/bin/env python3
"""Faux broker MQTT (port 1883, MQTT en clair sans TLS) : l'appât pour les scanners IoT.

Le VRAI broker du projet est en 8883 (MQTTS, chiffre + authentifie) : inattaquable en clair. Les
scanners cherchent alors le 1883 classique, laisse ouvert par negligence. On leur en offre un FAUX :
il parle le protocole MQTT 3.1.1 juste assez pour avoir l'air reel (CONNACK, SUBACK, PINGRESP), LOGGE
tout ce qu'ils envoient (identifiants, topics, publications), et leur sert de la FAUSSE telemetrie.
Aucune vraie donnee, aucune connexion sortante : journal seulement.
(Explications detaillees : voir README.md)

Usage : mqtt_honeypot.py --port 1883
"""
import argparse
import json
import random
import socket
import struct
import threading
import time

from common import record, safe_text

MAX_CONN = 50
READ_TIMEOUT = 90.0        # MQTT garde la connexion ouverte (keepalive) : on patiente plus longtemps

# Types de paquets MQTT (nibble de poids fort du 1er octet).
CONNECT, CONNACK, PUBLISH, PUBACK = 1, 2, 3, 4
SUBSCRIBE, SUBACK, UNSUBSCRIBE, UNSUBACK = 8, 9, 10, 11
PINGREQ, PINGRESP, DISCONNECT = 12, 13, 14

# Topics et appats servis a celui qui s'abonne. Le message "admin" est un LEURRE : de faux identifiants,
# pour deceler un attaquant qui aspire les secrets (il n'y a evidemment aucun vrai secret ici).
FAKE_TOPICS = ["sentinel/telemetry", "capteurs/salle1/temperature", "capteurs/salle1/humidite"]
BAIT_TOPIC = "sentinel/config/broker"
BAIT_PAYLOAD = json.dumps({"admin_user": "mqtt_admin", "admin_pass": "Sup3rS3cret!2026", "note": "ne pas diffuser"})


def read_remaining_length(conn) -> int:
    """Longueur restante MQTT : entier encode sur 1 a 4 octets, bit 7 = "il reste un octet"."""
    value = 0
    mult = 1
    for _ in range(4):
        b = conn.recv(1)
        if not b:
            raise ConnectionError
        value += (b[0] & 0x7F) * mult
        if not b[0] & 0x80:
            return value
        mult *= 128
    return value


def read_string(buf, i):
    """Chaine MQTT : 2 octets de longueur (big-endian) puis les octets UTF-8. Renvoie (texte, i_suivant)."""
    n = struct.unpack(">H", buf[i:i + 2])[0]
    i += 2
    return buf[i:i + n].decode("utf-8", "replace"), i + n


def encode_string(s: str) -> bytes:
    raw = s.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def publish_packet(topic: str, payload: str) -> bytes:
    """Paquet PUBLISH serveur -> client en QoS 0 (pas d'identifiant de paquet) : en-tete + topic + message."""
    body = encode_string(topic) + payload.encode("utf-8")
    return bytes([PUBLISH << 4]) + encode_remaining_length(len(body)) + body


def encode_remaining_length(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n % 128
        n //= 128
        if n:
            byte |= 0x80
        out.append(byte)
        if not n:
            return bytes(out)


def fake_telemetry() -> str:
    """Une mesure credible : memes champs que notre vrai firmware, valeurs plausibles."""
    return json.dumps({
        "serie": "SX-G2-07",
        "temperature_c": round(random.uniform(20.0, 24.5), 1),
        "humidite_pct": round(random.uniform(40.0, 55.0), 1),
        "gaz_brut": random.randint(95, 140),
        "pir": random.random() < 0.1,
    })


def parse_connect(buf, base):
    """Extrait client_id, username et password d'un CONNECT et les journalise (c'est l'or du honeypot)."""
    try:
        proto, i = read_string(buf, 0)       # "MQTT" (ou "MQIsdp" en 3.1)
        level = buf[i]; i += 1               # 4 = 3.1.1
        flags = buf[i]; i += 1
        keepalive = struct.unpack(">H", buf[i:i + 2])[0]; i += 2
        client_id, i = read_string(buf, i)
        if flags & 0x04:                     # will flag : on saute le will topic + will message
            _, i = read_string(buf, i)
            _, i = read_string(buf, i)
        username = password = None
        if flags & 0x80:
            username, i = read_string(buf, i)
        if flags & 0x40:
            password, i = read_string(buf, i)
        record({**base, "event": "mqtt_connect", "proto": proto, "version": level,
                "client_id": client_id, "username": username, "password": password, "keepalive": keepalive})
    except (IndexError, struct.error, UnicodeDecodeError):
        record({**base, "event": "mqtt_connect_malforme", "data_hex": buf[:80].hex()})


def parse_subscribe(buf, base):
    """Topics demandes + identifiant de paquet. Renvoie (packet_id, [qos...]) pour le SUBACK."""
    packet_id = struct.unpack(">H", buf[0:2])[0]
    i = 2
    topics, qos = [], []
    while i < len(buf):
        t, i = read_string(buf, i)
        qos.append(buf[i] & 0x03); i += 1
        topics.append(t)
    record({**base, "event": "mqtt_subscribe", "packet_id": packet_id, "topics": topics})
    return packet_id, qos


def parse_publish(flags, buf, base):
    """Ce que l'attaquant PUBLIE (topic + charge utile) : on le logge en clair."""
    try:
        topic, i = read_string(buf, 0)
        if flags & 0x06:                     # QoS > 0 : un identifiant de paquet suit le topic
            i += 2
        payload = buf[i:]
        record({**base, "event": "mqtt_publish", "topic": topic,
                "payload": safe_text(payload), "bytes": len(payload)})
    except (IndexError, struct.error, UnicodeDecodeError):
        record({**base, "event": "mqtt_publish_malforme", "data_hex": buf[:80].hex()})


def feed_fake_data(conn, base, stop):
    """Apres un abonnement, on deverse de la fausse telemetrie + l'appat, jusqu'a la deconnexion.
    C'est ce qui fait qu'un attaquant 's'installe' et nous laisse le temps de tout journaliser."""
    try:
        conn.sendall(publish_packet(BAIT_TOPIC, BAIT_PAYLOAD))   # le leurre, une fois
        record({**base, "event": "mqtt_bait_servi", "topic": BAIT_TOPIC})
        while not stop.is_set():
            topic = random.choice(FAKE_TOPICS)
            conn.sendall(publish_packet(topic, fake_telemetry()))
            stop.wait(2.0)
    except OSError:
        pass


def handle(conn, addr, dst_port):
    src_ip, src_port = addr
    base = {"service": "mqtt", "src_ip": src_ip, "src_port": src_port, "dst_port": dst_port}
    record({**base, "event": "connect"})
    stop = threading.Event()
    feeder = None
    try:
        conn.settimeout(READ_TIMEOUT)
        while True:
            header = conn.recv(1)
            if not header:
                break
            ptype = header[0] >> 4
            flags = header[0] & 0x0F
            length = read_remaining_length(conn)
            body = b""
            while len(body) < length:
                chunk = conn.recv(length - len(body))
                if not chunk:
                    break
                body += chunk

            if ptype == CONNECT:
                parse_connect(body, base)
                conn.sendall(bytes([CONNACK << 4, 2, 0, 0]))      # connexion acceptee (code 0)
            elif ptype == SUBSCRIBE:
                packet_id, qos = parse_subscribe(body, base)
                suback = struct.pack(">H", packet_id) + bytes(qos or [0])
                conn.sendall(bytes([SUBACK << 4]) + encode_remaining_length(len(suback)) + suback)
                if feeder is None:                                 # on commence a nourrir l'attaquant
                    feeder = threading.Thread(target=feed_fake_data, args=(conn, base, stop), daemon=True)
                    feeder.start()
            elif ptype == PUBLISH:
                parse_publish(flags, body, base)
                if flags & 0x06 == 0x02:                           # QoS 1 : on acquitte (PUBACK)
                    conn.sendall(bytes([PUBACK << 4, 2]) + body[-2:] if len(body) >= 2 else bytes([PUBACK << 4, 2, 0, 0]))
            elif ptype == PINGREQ:
                conn.sendall(bytes([PINGRESP << 4, 0]))
            elif ptype == DISCONNECT:
                break
    except (OSError, ConnectionError, socket.timeout):
        pass
    finally:
        stop.set()
        record({**base, "event": "close"})
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="Faux broker MQTT (journal uniquement)")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--bind", default="0.0.0.0")
    args = ap.parse_args()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.bind, args.port))
    srv.listen(MAX_CONN)
    record({"service": "mqtt", "event": "listen", "dst_port": args.port})

    while True:
        conn, addr = srv.accept()
        if threading.active_count() > MAX_CONN:
            record({"service": "mqtt", "event": "refuse_flood", "src_ip": addr[0], "dst_port": args.port})
            conn.close()
            continue
        threading.Thread(target=handle, args=(conn, addr, args.port), daemon=True).start()


if __name__ == "__main__":
    main()
