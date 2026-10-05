"""Générateur de trafic synthétique (normal et attaques) au niveau PacketInfo.

Sert :
- aux tests ;
- à amorcer le détecteur d'anomalies avant d'avoir enregistré du vrai trafic normal ;
- à entraîner le classifieur de TYPE d'attaque (profils d'attaques variés en intensité).
Le détecteur de démo DOIT être ré-entraîné sur le trafic normal réel de la table (mode record).
"""
import random

from features import ACK, RST, SYN, FlowAggregator, PacketInfo

SERVER = "192.168.137.1"
SERVICE_PORTS = [22, 443, 8883, 2222]
ATTACK_TYPES = ["SCAN_PORTS", "DENI_DE_SERVICE", "FORCE_BRUTE"]


def _tcp(src, dst, dport, flags, length):
    return PacketInfo(src=src, dst=dst, proto="tcp", length=length, dport=dport, flags=flags)


def normal_window(rng, window_s=2.0):
    """Une fenêtre de trafic normal : ESP en MQTTS, navigateurs sur le dashboard."""
    pkts = []
    # ESP8266 : publication MQTTS ~toutes les 2 s (+ keepalive), connexion persistante
    esp = f"192.168.137.{rng.randint(10, 13)}"
    for _ in range(rng.randint(2, 5)):
        pkts.append(_tcp(esp, SERVER, 8883, ACK, rng.randint(90, 220)))
        pkts.append(_tcp(SERVER, esp, 50000, ACK, 60))
    # Navigateurs : WebSocket temps réel + requêtes HTTPS, parfois une nouvelle connexion
    for _ in range(rng.randint(1, 3)):
        browser = f"192.168.137.{rng.randint(20, 40)}"
        for _ in range(rng.randint(2, 15)):
            pkts.append(_tcp(browser, SERVER, 443, ACK, rng.randint(60, 900)))
            pkts.append(_tcp(SERVER, browser, 51000, ACK, rng.randint(200, 30000)))
        if rng.random() < 0.3:
            pkts.append(_tcp(browser, SERVER, 443, SYN, 60))
            pkts.append(_tcp(SERVER, browser, 51000, SYN | ACK, 60))
    # DNS / ICMP occasionnels
    if rng.random() < 0.2:
        pkts.append(PacketInfo(src=f"192.168.137.{rng.randint(20, 40)}", dst=SERVER, proto="udp", length=80, dport=53))
    if rng.random() < 0.05:
        pkts.append(PacketInfo(src=f"192.168.137.{rng.randint(20, 40)}", dst=SERVER, proto="icmp", length=84))
    return pkts


def port_scan(attacker="192.168.137.66", n_ports=400, rng=None):
    """Scan SYN (nmap -sS) : la plupart des ports sont fermés -> RST du serveur."""
    rng = rng or random.Random(1)
    pkts = []
    for port in rng.sample(range(1, 10000), n_ports):
        pkts.append(_tcp(attacker, SERVER, port, SYN, 60))
        if port in SERVICE_PORTS:
            pkts.append(_tcp(SERVER, attacker, 40000, SYN | ACK, 60))
        else:
            pkts.append(_tcp(SERVER, attacker, 40000, RST | ACK, 54))
    return pkts


def syn_flood(attacker="192.168.137.77", n=6000, port=443, rng=None, kind="syn"):
    """Déni de service : flood SYN (le serveur répond en partie), UDP ou ICMP."""
    rng = rng or random.Random(2)
    pkts = []
    for _ in range(n):
        if kind == "syn":
            pkts.append(_tcp(attacker, SERVER, port, SYN, 60))
            if rng.random() < 0.2:
                pkts.append(_tcp(SERVER, attacker, 40000, SYN | ACK, 60))
        elif kind == "udp":
            pkts.append(PacketInfo(attacker, SERVER, "udp", rng.randint(60, 1400), rng.randint(1, 65535)))
        else:
            pkts.append(PacketInfo(attacker, SERVER, "icmp", rng.choice([84, 1028, 1500])))
    return pkts


def brute_force(attacker="192.168.137.88", attempts=60, port=8883, rng=None):
    """Force brute : connexions complètes répétées vers un service (handshake + échange + refus)."""
    rng = rng or random.Random(3)
    pkts = []
    for _ in range(attempts):
        pkts.append(_tcp(attacker, SERVER, port, SYN, 60))
        pkts.append(_tcp(SERVER, attacker, 40000, SYN | ACK, 60))
        pkts.extend(_tcp(attacker, SERVER, port, ACK, rng.randint(80, 400)) for _ in range(rng.randint(3, 6)))
        pkts.extend(_tcp(SERVER, attacker, 40000, ACK, rng.randint(60, 300)) for _ in range(rng.randint(2, 4)))
    return pkts


def random_attack(kind, rng, attacker):
    if kind == "SCAN_PORTS":
        return port_scan(attacker, n_ports=rng.choice([15, 30, 60, 150, 400, 1000]), rng=rng)
    if kind == "DENI_DE_SERVICE":
        return syn_flood(attacker, n=rng.choice([400, 1000, 3000, 8000]), port=rng.choice(SERVICE_PORTS + [80]),
                         rng=rng, kind=rng.choice(["syn", "syn", "udp", "icmp"]))
    return brute_force(attacker, attempts=rng.choice([8, 15, 30, 60]), port=rng.choice(SERVICE_PORTS), rng=rng)


def _aggregate(pkts, window_s=2.0):
    agg = FlowAggregator([SERVER], SERVICE_PORTS)
    for p in pkts:
        agg.add(p)
    return agg.flush(window_s)


def normal_dataset(n_windows=1500, seed=0, window_s=2.0):
    """Lignes de caractéristiques normales, prêtes pour l'entraînement."""
    rng = random.Random(seed)
    rows = []
    for _ in range(n_windows):
        rows.extend(_aggregate(normal_window(rng, window_s), window_s).values())
    return rows


def attack_dataset(n_per_type=300, seed=0, window_s=2.0):
    """(lignes, étiquettes) d'attaques variées noyées dans du trafic normal, pour le classifieur."""
    rng = random.Random(seed)
    rows, labels = [], []
    for kind in ATTACK_TYPES:
        for i in range(n_per_type):
            attacker = f"10.66.{i // 250}.{i % 250 + 1}"
            pkts = normal_window(rng, window_s) + random_attack(kind, rng, attacker)
            rows.append(_aggregate(pkts, window_s)[attacker])
            labels.append(kind)
    return rows, labels
