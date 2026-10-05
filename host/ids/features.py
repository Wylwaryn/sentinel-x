"""Extraction des caractéristiques réseau par IP distante et par fenêtre de temps.

Chaque paquet est réduit à un PacketInfo (indépendant de scapy, donc testable).
L'IP « distante » est l'extrémité qui n'est PAS protégée (serveur / hôte) : on agrège
le trafic dans les deux sens pour voir aussi les réponses du serveur (RST = port fermé).
"""
import math
from dataclasses import dataclass, field

FEATURES = [
    "pkts_per_s",            # 0  volume entrant
    "bytes_per_s",           # 1
    "mean_pkt_size",         # 2
    "syn_per_s",             # 3  tentatives de connexion
    "syn_ratio",             # 4  part des paquets TCP qui sont des SYN
    "unique_dst_ports",      # 5  ports différents visés (scan)
    "port_spread",           # 6  ports uniques / tentatives de connexion
    "service_syn_per_s",     # 7  connexions vers SSH / HTTPS / MQTTS (force brute)
    "rst_from_server_per_s", # 8  ports fermés touchés (scan)
    "icmp_per_s",            # 9
    "udp_per_s",             # 10
    "unique_dst_ips",        # 11 balayage d'hôtes
    "in_out_bytes_ratio",    # 12 asymétrie du trafic
    "out_pkts_per_s",        # 13 réponses du serveur
]

SYN, ACK, RST = 0x02, 0x10, 0x04


@dataclass
class PacketInfo:
    src: str
    dst: str
    proto: str          # "tcp", "udp", "icmp", "other"
    length: int
    dport: int | None = None
    flags: int = 0      # drapeaux TCP


@dataclass
class _Stats:
    in_pkts: int = 0
    in_bytes: int = 0
    out_pkts: int = 0
    out_bytes: int = 0
    tcp_in: int = 0
    syn: int = 0
    service_syn: int = 0
    rst_from_server: int = 0
    icmp: int = 0
    udp: int = 0
    dst_ports: set = field(default_factory=set)
    dst_ips: set = field(default_factory=set)


class FlowAggregator:
    def __init__(self, protected_ips, service_ports):
        self.protected = set(protected_ips)
        self.service_ports = set(service_ports)
        self.stats: dict[str, _Stats] = {}

    def add(self, p: PacketInfo):
        if p.dst in self.protected and p.src not in self.protected:
            self._add_inbound(p)
        elif p.src in self.protected and p.dst not in self.protected:
            self._add_outbound(p)

    def _add_inbound(self, p):
        s = self.stats.setdefault(p.src, _Stats())
        s.in_pkts += 1
        s.in_bytes += p.length
        s.dst_ips.add(p.dst)
        if p.proto == "tcp":
            s.tcp_in += 1
            if p.dport is not None:
                s.dst_ports.add(p.dport)
            if p.flags & SYN and not p.flags & ACK:
                s.syn += 1
                if p.dport in self.service_ports:
                    s.service_syn += 1
        elif p.proto == "udp":
            s.udp += 1
            if p.dport is not None:
                s.dst_ports.add(p.dport)
        elif p.proto == "icmp":
            s.icmp += 1

    def _add_outbound(self, p):
        s = self.stats.setdefault(p.dst, _Stats())
        s.out_pkts += 1
        s.out_bytes += p.length
        if p.proto == "tcp" and p.flags & RST:
            s.rst_from_server += 1

    def flush(self, window_s):
        """Retourne {ip: vecteur} pour la fenêtre écoulée et remet à zéro."""
        rows = {}
        for ip, s in self.stats.items():
            if s.in_pkts == 0:
                continue  # le serveur parle seul à cette IP : rien à juger
            w = window_s
            rows[ip] = [
                s.in_pkts / w,
                s.in_bytes / w,
                s.in_bytes / s.in_pkts,
                s.syn / w,
                s.syn / s.tcp_in if s.tcp_in else 0.0,
                float(len(s.dst_ports)),
                len(s.dst_ports) / max(s.syn, 1),
                s.service_syn / w,
                s.rst_from_server / w,
                s.icmp / w,
                s.udp / w,
                float(len(s.dst_ips)),
                s.in_bytes / max(s.out_bytes, 1),
                s.out_pkts / w,
            ]
        self.stats.clear()
        return rows


def transform(vector):
    """log1p : écrase les ordres de grandeur (un flood = x1000) pour l'apprentissage."""
    return [math.log1p(max(v, 0.0)) for v in vector]
