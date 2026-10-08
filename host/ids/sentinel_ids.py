"""Sentinel-X — IA de détection d'intrusion réseau (IDS).

Usage (depuis host/ids, Npcap requis pour record/detect) :
  python sentinel_ids.py interfaces                        liste les interfaces de capture
  python sentinel_ids.py record --minutes 20               enregistre le trafic NORMAL (data/normal.csv)
  python sentinel_ids.py train [--synthetic]               entraîne le modèle (models/)
  python sentinel_ids.py detect                            détection en temps réel
Le blocage pare-feu exige mode "blocage" dans config.json ET un terminal administrateur.
"""
import argparse
import csv
import json
import threading
import time
from pathlib import Path

from features import FEATURES, FlowAggregator, PacketInfo

BASE = Path(__file__).resolve().parent


def load_config():
    return json.loads((BASE / "config.json").read_text(encoding="utf-8"))


def to_packet_info(pkt):
    from scapy.layers.inet import ICMP, IP, TCP, UDP
    if IP not in pkt:
        return None
    ip = pkt[IP]
    if TCP in pkt:
        return PacketInfo(ip.src, ip.dst, "tcp", ip.len, pkt[TCP].dport, int(pkt[TCP].flags))
    if UDP in pkt:
        return PacketInfo(ip.src, ip.dst, "udp", ip.len, pkt[UDP].dport)
    if ICMP in pkt:
        return PacketInfo(ip.src, ip.dst, "icmp", ip.len)
    return PacketInfo(ip.src, ip.dst, "other", ip.len)


class LiveCapture:
    """Capture scapy dans un thread ; flush() renvoie les caractéristiques de la fenêtre."""

    def __init__(self, cap_cfg):
        from scapy.all import AsyncSniffer, get_if_addr
        self.window_s = cap_cfg["window_s"]
        protected = set(cap_cfg["protected_ips"]) or {get_if_addr(cap_cfg["iface"])}
        self.protected = protected
        self.agg = FlowAggregator(protected, cap_cfg["service_ports"])
        self.lock = threading.Lock()
        self.ip_mac = {}  # IP -> adresse MAC vue dans les trames (les IP du point d'accès changent)
        # promisc=False par défaut : mettre la carte Wi-Fi en mode promiscuous PENDANT qu'elle sert de
        # point d'accès (Mobile Hotspot) fait planter le pilote Wi-Fi sous Windows (carte perdue jusqu'au
        # redémarrage). Inutile ici : l'hôte est la passerelle du 192.168.137.0/24, tout le trafic des
        # clients (ESP, attaquants) est routé à travers sa pile et donc déjà visible sans promiscuous.
        self.sniffer = AsyncSniffer(iface=cap_cfg["iface"], filter=cap_cfg["bpf"], prn=self._on_packet,
                                    store=False, promisc=cap_cfg.get("promisc", False))

    def _on_packet(self, pkt):
        from scapy.layers.l2 import Ether
        info = to_packet_info(pkt)
        if info:
            with self.lock:
                self.agg.add(info)
                if Ether in pkt and info.src not in self.protected:
                    self.ip_mac[info.src] = pkt[Ether].src.lower()

    def mac_of(self, ip):
        with self.lock:
            return self.ip_mac.get(ip)

    def start(self):
        self.sniffer.start()

    def stop(self):
        self.sniffer.stop()

    def flush(self):
        with self.lock:
            return self.agg.flush(self.window_s)


def cmd_interfaces(_args, _cfg):
    from scapy.all import conf
    for iface in conf.ifaces.values():
        print(f"{iface.name:40} {iface.ip or '-':16} {iface.description}")


def cmd_record(args, cfg):
    out = BASE / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    cap = LiveCapture(cfg["capture"])
    cap.start()
    print(f"Enregistrement du trafic normal sur {cfg['capture']['iface']} (IP protégées : {cap.protected}) "
          f"pendant {args.minutes} min -> {out}")
    end = time.time() + args.minutes * 60
    n = 0
    new_file = not out.exists()
    with out.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["ts", "ip"] + FEATURES)
        try:
            while time.time() < end:
                time.sleep(cfg["capture"]["window_s"])
                for ip, vec in cap.flush().items():
                    w.writerow([round(time.time(), 1), ip] + [round(v, 4) for v in vec])
                    n += 1
                f.flush()
        except KeyboardInterrupt:
            pass
    cap.stop()
    print(f"{n} lignes enregistrées.")


def cmd_train(args, cfg):
    from model import NetworkAnomalyDetector
    rows = []
    data = BASE / args.data
    if data.exists():
        with data.open(encoding="utf-8") as f:
            rows = [[float(r[k]) for k in FEATURES] for r in csv.DictReader(f)]
        print(f"{len(rows)} lignes de trafic normal réel ({data})")
    from simulate import attack_dataset, normal_dataset
    if args.synthetic or not rows:
        synth = normal_dataset()
        print(f"+ {len(synth)} lignes normales synthétiques (amorçage, à remplacer par du trafic réel)")
        rows += synth
    det = NetworkAnomalyDetector()
    loss = det.fit(rows, epochs=cfg["model"]["epochs"], percentile=cfg["model"]["threshold_percentile"])
    print(f"Étage 1 (anomalies) entraîné sur {det.device}, perte finale {loss:.4f}")
    attack_rows, labels = attack_dataset()
    det.fit_typer(attack_rows, labels, rows)
    print(f"Étage 2 (types) entraîné sur {len(attack_rows)} attaques synthétiques + {len(rows)} lignes normales")
    det.save(BASE / cfg["model"]["dir"])
    print(f"Modèle sauvegardé dans {cfg['model']['dir']}/")


def _build_monitor(cap_cfg, resp_cfg, cfg, api, mode):
    """Un moniteur = une capture + un répondeur. Renvoie (nom, LiveCapture, Responder, mode)."""
    from response import Responder
    cap = LiveCapture(cap_cfg)
    rcfg = dict(resp_cfg)
    rcfg["mode"] = mode
    resp = Responder(rcfg, cap.protected, cfg.get("devices"), api=api, base_dir=BASE,
                     devices_mac=cfg.get("devices_mac"), ip_to_mac=cap.mac_of)
    return (cap_cfg["iface"], cap, resp, mode)


def cmd_detect(_args, cfg):
    import torch
    from model import NetworkAnomalyDetector
    from response import ApiSender, is_admin

    if torch.cuda.is_available():
        torch.cuda.set_per_process_memory_fraction(cfg["model"]["gpu_memory_fraction"], 0)
    det = NetworkAnomalyDetector.load(BASE / cfg["model"]["dir"])
    api = ApiSender(cfg["api"], BASE) if cfg["api"]["enabled"] else None

    mode = cfg["response"]["mode"]
    if mode == "blocage" and not is_admin():
        print("[IDS] mode blocage SANS droits administrateur : les blocages seront seulement simulés")

    # 1) Interface principale (hotspot) : mode complet de la config (leurre/blocage/alerte).
    monitors = [_build_monitor(cfg["capture"], cfg["response"], cfg, api, mode)]

    # 2) Interfaces supplémentaires (ex. réseau école). SURVEILLANCE SEULE par défaut : on force
    #    "alerte" (jamais de blocage, pour ne pas couper le trafic Internet légitime de l'hôte) et on
    #    ne capture que ce qui VISE l'hôte (SYN entrants + ICMP = scans/sondes), pas sa navigation.
    from scapy.all import get_if_addr
    for extra in cfg["capture"].get("extra_ifaces", []):
        host_ip = (extra.get("protected_ips") or [None])[0] or get_if_addr(extra["iface"])
        if not host_ip or host_ip in ("0.0.0.0", "127.0.0.1"):
            print(f"[IDS] interface {extra['iface']} ignorée : IP hôte introuvable ({host_ip})")
            continue
        tmpl = extra.get("bpf_template", "dst host {ip} and (tcp[tcpflags] & (tcp-syn|tcp-ack) == tcp-syn or icmp)")
        ecap_cfg = dict(cfg["capture"])
        ecap_cfg.pop("extra_ifaces", None)
        ecap_cfg["iface"] = extra["iface"]
        ecap_cfg["protected_ips"] = [host_ip]
        ecap_cfg["bpf"] = extra.get("bpf", tmpl.format(ip=host_ip))
        emode = extra.get("mode", "alerte")
        if emode != "alerte" and not is_admin():
            emode = "alerte"
        monitors.append(_build_monitor(ecap_cfg, cfg["response"], cfg, api, emode))

    for name, c, _, m in monitors:
        print(f"[IDS] détection sur {name} | IP protégées {c.protected} | mode {m}")
    print(f"[IDS] modèle sur {det.device}")
    for _, c, _, _ in monitors:
        c.start()
    try:
        while True:
            time.sleep(cfg["capture"]["window_s"])
            for name, c, resp, _ in monitors:
                rows = c.flush()
                if not rows:
                    continue
                ips = list(rows)
                t0 = time.perf_counter()
                vectors = [rows[ip] for ip in ips]
                scores, contribs = det.score(vectors)
                kinds, confidences = det.classify(vectors)
                infer_ms = (time.perf_counter() - t0) * 1000
                worst = max(range(len(ips)), key=lambda i: scores[i])
                tag = "" if len(monitors) == 1 else f"[{name}] "
                print(f"[IDS] {tag}{len(ips)} IP | pire {ips[worst]} score {scores[worst]:.2f} | {infer_ms:.1f} ms")
                for i, ip in enumerate(ips):
                    event = resp.handle(ip, scores[i], kinds[i], confidences[i], contribs[i], rows[ip])
                    if event:
                        print(f"  ⚠ [{event['niveau']}] {event['message']}")
    except KeyboardInterrupt:
        pass
    finally:
        for _, c, _, _ in monitors:
            c.stop()
        for _, _, resp, _ in monitors:
            resp.blocker.unblock_all()


def main():
    parser = argparse.ArgumentParser(description="Sentinel-X IDS")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("interfaces")
    p = sub.add_parser("record")
    p.add_argument("--minutes", type=float, default=20)
    p.add_argument("--out", default="data/normal.csv")
    p = sub.add_parser("train")
    p.add_argument("--data", default="data/normal.csv")
    p.add_argument("--synthetic", action="store_true", help="ajoute du trafic normal synthétique")
    sub.add_parser("detect")
    args = parser.parse_args()
    cfg = load_config()
    {"interfaces": cmd_interfaces, "record": cmd_record, "train": cmd_train, "detect": cmd_detect}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
