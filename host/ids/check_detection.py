"""Recette hors ligne de l'IDS : montre, en clair, que l'étage 1 distingue le normal de l'anormal.

Ne touche PAS au réseau : il réutilise le générateur synthétique `simulate.py` (le même qui sert
aux tests et à l'amorçage) et passe ses fenêtres dans le modèle déjà entraîné (`models/`).
C'est une recette du DÉTECTEUR, pas un outil d'attaque : aucun paquet n'est émis.

Usage (depuis host/ids) : python check_detection.py
"""
import random

import numpy as np

from features import FlowAggregator
from model import NetworkAnomalyDetector
from simulate import (SERVER, SERVICE_PORTS, brute_force, normal_window, port_scan, syn_flood)


def window(extra_pkts, seed):
    """Agrège une fenêtre de trafic normal + d'éventuels paquets anormaux en un vecteur par IP."""
    agg = FlowAggregator([SERVER], SERVICE_PORTS)
    for p in normal_window(random.Random(seed)) + extra_pkts:
        agg.add(p)
    return agg.flush(2.0)


def main():
    det = NetworkAnomalyDetector.load("models")
    crit = 0.85  # critical_score de config.json

    # Chaque cas : (libellé, IP à regarder, paquets anormaux ajoutés au trafic normal)
    cases = [
        ("trafic normal seul", None, []),
        ("scan de ports (nmap -sS)", "192.168.137.66", port_scan(n_ports=300)),
        ("déni de service (flood SYN)", "192.168.137.77", syn_flood(n=4000)),
        ("force brute (connexions répétées)", "192.168.137.88", brute_force(attempts=40)),
    ]

    print(f"Modèle sur {det.device} | seuil alerte 0.50 | seuil critique {crit:.2f}\n")
    print(f"{'cas':<38}{'IP':<18}{'score':>7}  {'type':<16}{'confiance':>9}")
    print("-" * 90)
    for label, ip, attack in cases:
        rows = window(attack, seed=0)
        if ip is None:  # pas d'attaque : on regarde la pire IP du trafic normal
            ip = max(rows, key=lambda k: det.score([rows[k]])[0][0])
        vec = rows[ip]
        score = float(det.score([vec])[0][0])
        kind, conf = (c[0] for c in det.classify([vec]))
        flag = "  <-- ALERTE" if score >= 0.5 else ""
        flag += " (CRITIQUE)" if score >= crit else ""
        print(f"{label:<38}{ip:<18}{score:>7.2f}  {kind:<16}{conf:>9.2f}{flag}")

    # Taux de fausses alertes sur du normal jamais vu (doit rester très bas)
    from simulate import normal_dataset
    scores, _ = det.score(normal_dataset(n_windows=500, seed=42))
    print(f"\nFausses alertes sur 500 fenêtres normales jamais vues : {np.mean(scores >= 0.5):.1%} "
          f"(objectif < 2 %)")


if __name__ == "__main__":
    main()
