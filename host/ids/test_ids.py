"""Tests de l'IDS sans capture réelle : python -m pytest host/ids"""
import random

import numpy as np
import pytest

from features import FEATURES, FlowAggregator, PacketInfo
from model import NetworkAnomalyDetector
from response import FirewallBlocker, Responder
from simulate import (SERVER, attack_dataset, brute_force, normal_dataset, normal_window, port_scan,
                      syn_flood)

IDX = {f: i for i, f in enumerate(FEATURES)}
RESPONSE_CFG = {
    "mode": "blocage", "alert_score": 0.5, "critical_score": 0.85, "block_score": 0.85,
    "block_ttl_s": 300, "cooldown_s": 30, "whitelist": ["192.168.137.10"], "log_file": "logs/test.jsonl",
}
ZERO = [0.0] * len(FEATURES)


def window_with(attack_pkts, seed=0):
    agg = FlowAggregator([SERVER], [22, 443, 8883, 2222])
    for p in normal_window(random.Random(seed)) + attack_pkts:
        agg.add(p)
    return agg.flush(2.0)


@pytest.fixture(scope="module")
def detector():
    normal = normal_dataset(n_windows=1500, seed=0)
    det = NetworkAnomalyDetector()
    det.fit(normal, epochs=300)
    det.fit_typer(*attack_dataset(n_per_type=300, seed=0), normal)
    return det


# ---------- caractéristiques ----------
def test_scan_features():
    v = window_with(port_scan(n_ports=200))["192.168.137.66"]
    assert v[IDX["unique_dst_ports"]] == 200
    assert v[IDX["rst_from_server_per_s"]] > 90  # ~200 RST en 2 s (sauf ports de service ouverts)


def test_server_only_traffic_is_ignored():
    agg = FlowAggregator([SERVER], [443])
    agg.add(PacketInfo(SERVER, "8.8.8.8", "udp", 80, 53))
    assert agg.flush(2.0) == {}


# ---------- étage 1 : anomalies ----------
def test_low_false_positive_rate_on_unseen_normal(detector):
    scores, _ = detector.score(normal_dataset(n_windows=500, seed=42))
    assert np.mean(scores >= 0.5) < 0.02


@pytest.mark.parametrize("attack, ip, expected", [
    (port_scan(), "192.168.137.66", "SCAN_PORTS"),
    (syn_flood(), "192.168.137.77", "DENI_DE_SERVICE"),
    (brute_force(), "192.168.137.88", "FORCE_BRUTE"),
])
def test_reference_attacks_detected_and_typed(detector, attack, ip, expected):
    row = window_with(attack)[ip]
    scores, _ = detector.score([row])
    kinds, _ = detector.classify([row])
    assert scores[0] >= 0.85, f"score {scores[0]:.2f}"
    assert kinds[0] == expected


def test_unseen_attacks_detection_and_typing(detector):
    rows, labels = attack_dataset(n_per_type=100, seed=123)  # profils jamais vus à l'entraînement
    scores, _ = detector.score(rows)
    kinds, _ = detector.classify(rows)
    detection = np.mean(scores >= 0.5)
    accuracy = np.mean([k == y for k, y in zip(kinds, labels)])
    print(f"\n  détection {detection:.1%} | typage {accuracy:.1%}")
    assert detection >= 0.95
    assert accuracy >= 0.90


def test_save_load_roundtrip(detector, tmp_path):
    detector.save(tmp_path)
    loaded = NetworkAnomalyDetector.load(tmp_path)
    rows = list(window_with(port_scan()).values())
    np.testing.assert_allclose(detector.score(rows)[0], loaded.score(rows)[0], rtol=1e-5)
    assert detector.classify(rows)[0] == loaded.classify(rows)[0]


# ---------- réponse ----------
class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)


def make_responder(tmp_path, admin=True):
    runner = FakeRunner()
    blocker = FirewallBlocker(ttl_s=300, runner=runner, admin=admin)
    return Responder(RESPONSE_CFG, [SERVER], devices={"192.168.137.10": "SX-001"},
                     blocker=blocker, base_dir=tmp_path), runner


def contrib():
    c = np.zeros(len(FEATURES))
    c[IDX["unique_dst_ports"]] = 1.0
    return c


def test_attacker_blocked_with_validated_ip(tmp_path):
    responder, runner = make_responder(tmp_path)
    event = responder.handle("192.168.137.66", 0.97, "SCAN_PORTS", 0.9, contrib(), ZERO, now=0)
    assert event["action"] == "bloquee" and event["niveau"] == "CRITIQUE"
    assert runner.calls[0][-1] == "remoteip=192.168.137.66"
    responder.blocker.unblock_all()
    assert runner.calls[-1][:5] == ["netsh", "advfirewall", "firewall", "delete", "rule"]


def test_whitelisted_esp_alerted_but_never_blocked(tmp_path):
    responder, runner = make_responder(tmp_path)
    event = responder.handle("192.168.137.10", 0.99, "DENI_DE_SERVICE", 0.9, contrib(), ZERO, now=0)
    assert event["action"] == "liste_blanche"
    assert event["numero_serie"] == "SX-001"  # attaque « sniper » rattachée à l'ESP
    assert runner.calls == []


def test_esp_recognised_by_mac_whatever_its_ip(tmp_path):
    """Le DHCP du point d'accès change l'IP de l'ESP : on le reconnaît par sa MAC."""
    cfg = dict(RESPONSE_CFG, whitelist=[], whitelist_mac=["40:F5:20:0D:5F:21"])
    runner = FakeRunner()
    macs = {"192.168.137.6": "40:f5:20:0d:5f:21", "192.168.137.66": "aa:bb:cc:dd:ee:ff"}
    r = Responder(cfg, [SERVER], devices_mac={"40:f5:20:0d:5f:21": "SX-G2-01"}, ip_to_mac=macs.get,
                  blocker=FirewallBlocker(ttl_s=300, runner=runner, admin=True), base_dir=tmp_path)
    esp = r.handle("192.168.137.6", 0.99, "DENI_DE_SERVICE", 0.9, contrib(), ZERO, now=0)
    assert esp["action"] == "liste_blanche" and esp["numero_serie"] == "SX-G2-01"   # alerté, pas bloqué
    other = r.handle("192.168.137.66", 0.99, "SCAN_PORTS", 0.9, contrib(), ZERO, now=0)
    assert other["action"] == "bloquee" and other["numero_serie"] is None
    r.blocker.unblock_all()


def test_block_simulated_without_admin(tmp_path):
    responder, runner = make_responder(tmp_path, admin=False)
    event = responder.handle("192.168.137.66", 0.97, "SCAN_PORTS", 0.9, contrib(), ZERO, now=0)
    assert event["action"] == "blocage_simule" and runner.calls == []


def test_cooldown_and_low_score(tmp_path):
    responder, _ = make_responder(tmp_path, admin=False)
    ip = "192.168.137.66"
    assert responder.handle(ip, 0.3, "SCAN_PORTS", 0.9, contrib(), ZERO, now=0) is None
    assert responder.handle(ip, 0.9, "SCAN_PORTS", 0.9, contrib(), ZERO, now=0)
    assert responder.handle(ip, 0.9, "SCAN_PORTS", 0.9, contrib(), ZERO, now=10) is None
    assert responder.handle(ip, 0.9, "SCAN_PORTS", 0.9, contrib(), ZERO, now=31)


def test_api_payload_matches_ingest_contract(tmp_path):
    from response import to_api_payload
    responder, _ = make_responder(tmp_path, admin=False)
    event = responder.handle("192.168.137.10", 0.99, "DENI_DE_SERVICE", 0.93, contrib(), ZERO, now=0)
    p = to_api_payload(event)
    # Champs lus par AlertIn (server/ingest/app/models.py)
    assert p["type"] == "DENI_DE_SERVICE" and p["level"] == "CRITIQUE" and p["source"] == "RESEAU_IA"
    assert p["serie"] == "SX-001" and p["ip_source"] == "192.168.137.10" and p["score"] == 0.99
    assert len(p["message"]) <= 500
    assert p["detail"]["action"] == "liste_blanche" and p["detail"]["confiance_type"] == 0.93


def test_malformed_ip_rejected():
    blocker = FirewallBlocker(ttl_s=1, runner=FakeRunner(), admin=True)
    with pytest.raises(ValueError):
        blocker.block("1.2.3.4 & del C:\\")


def test_leurre_mode_blocks_only_real_services(tmp_path):
    """Mode leurre : on coupe les vrais services (443, 8883), mais le honeypot (autres ports) reste
    ouvert -> l'attaquant est aspiré vers le leurre au lieu d'être bloqué net."""
    cfg = dict(RESPONSE_CFG, mode="leurre", real_ports=[443, 8883])
    runner = FakeRunner()
    r = Responder(cfg, [SERVER], blocker=FirewallBlocker(ttl_s=300, runner=runner, admin=True), base_dir=tmp_path)
    ev = r.handle("192.168.137.66", 0.97, "SCAN_PORTS", 0.9, contrib(), ZERO, now=0)
    assert ev["action"] == "leurre" and ev["niveau"] == "CRITIQUE"
    cmd = runner.calls[0]
    assert "action=block" in cmd and "remoteip=192.168.137.66" in cmd
    assert "localport=443,8883" in cmd           # seuls les vrais services sont coupés
    r.blocker.unblock_all()
    assert runner.calls[-1][:5] == ["netsh", "advfirewall", "firewall", "delete", "rule"]


def test_leurre_simule_sans_admin(tmp_path):
    cfg = dict(RESPONSE_CFG, mode="leurre", real_ports=[443, 8883])
    runner = FakeRunner()
    r = Responder(cfg, [SERVER], blocker=FirewallBlocker(ttl_s=300, runner=runner, admin=False), base_dir=tmp_path)
    ev = r.handle("192.168.137.66", 0.97, "SCAN_PORTS", 0.9, contrib(), ZERO, now=0)
    assert ev["action"] == "leurre_simule" and runner.calls == []


def test_leurre_persistant_garde_une_ip_connue_hostile(tmp_path):
    """Une IP passée >= block_score reste au leurre même quand son score retombe en 'alerte' (sinon
    l'attaquant récupère les vrais services entre deux rafales, à l'expiration de la règle)."""
    cfg = dict(RESPONSE_CFG, mode="leurre", real_ports=[443, 8883], leurre_persistant=True)
    r = Responder(cfg, [SERVER], blocker=FirewallBlocker(ttl_s=300, runner=FakeRunner(), admin=True), base_dir=tmp_path)
    assert r.handle("192.168.137.66", 0.97, "SCAN_PORTS", 0.9, contrib(), ZERO, now=0)["action"] == "leurre"
    r.blocker.unblock("192.168.137.66")                                   # la règle expire (TTL)
    # score retombé sous block_score mais toujours signalé : re-leurrée car déjà connue hostile
    assert r.handle("192.168.137.66", 0.60, "SCAN_PORTS", 0.5, contrib(), ZERO, now=100)["action"] == "leurre"
    # une IP jamais hostile au même score reste en simple alerte
    assert r.handle("192.168.137.99", 0.60, "SCAN_PORTS", 0.5, contrib(), ZERO, now=100)["action"] == "alerte"


def test_leurre_persistant_desactivable(tmp_path):
    """leurre_persistant=false : ancien comportement, une IP retombée sous block_score n'est plus leurrée."""
    cfg = dict(RESPONSE_CFG, mode="leurre", real_ports=[443, 8883], leurre_persistant=False)
    r = Responder(cfg, [SERVER], blocker=FirewallBlocker(ttl_s=300, runner=FakeRunner(), admin=True), base_dir=tmp_path)
    assert r.handle("192.168.137.66", 0.97, "SCAN_PORTS", 0.9, contrib(), ZERO, now=0)["action"] == "leurre"
    r.blocker.unblock("192.168.137.66")
    assert r.handle("192.168.137.66", 0.60, "SCAN_PORTS", 0.5, contrib(), ZERO, now=100)["action"] == "alerte"
