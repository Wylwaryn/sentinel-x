"""Tests identification + tri des alertes (sans caméra ni modèles) : python -m pytest host/vision"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "faces"))
from behaviour import VisionEvent  # noqa: E402
from fusion import FusionEngine  # noqa: E402
from gallery import Gallery  # noqa: E402
from identity import AUTHORIZED, PENDING, UNKNOWN, EventGate, IdentityTracker, unit  # noqa: E402

CFG = {"threshold": 0.40, "confirm_hits": 2, "retry_s": 0.4, "reverify_s": 5.0, "grace_s": 2.0, "min_face_px": 40}
ALICE = unit(np.random.default_rng(1).normal(size=128))
STRANGER = unit(np.random.default_rng(99).normal(size=128))
FRAME = np.zeros((480, 640, 3), dtype=np.uint8)
PERSON = [{"track_id": 7, "box": (0.3, 0.1, 0.6, 0.95), "conf": 0.9}]


class FakeEngine:
    def __init__(self, vector=None):
        self.vector = vector  # None = visage non visible (de dos)

    def detect(self, image):
        return [] if self.vector is None else [np.array([10, 10, 80, 80] + [0] * 10 + [0.95])]

    def embed(self, image, face):
        return self.vector


def make(tmp_path, vector):
    g = Gallery(tmp_path / "g.npz")
    g.add(1, "Alice", "ADMIN", ALICE)
    tracker = IdentityTracker(CFG, FakeEngine(vector), g)
    return tracker, EventGate(tracker)


def intrusion(ts):
    return VisionEvent(type="intrusion", level="critical", track_id=7, zone="zone_interdite", ts=ts,
                       detail={"conf": 0.9})


def run(tracker, gate, events_at, until=3.0, step=0.2):
    out, ts = [], 0.0
    while ts <= until:
        tracker.update(FRAME, PERSON, ts)
        out += gate.process([e for t, e in events_at if abs(t - ts) < 1e-9], ts)
        ts = round(ts + step, 3)
    return out


def test_member_recognized_no_intrusion_alert(tmp_path):
    tracker, gate = make(tmp_path, ALICE)
    out = run(tracker, gate, [(0.0, intrusion(0.0))])
    assert len(out) == 1
    assert out[0].type == "presence" and out[0].level == "info"
    assert out[0].detail["personne"] == AUTHORIZED and out[0].detail["id_personne"] == 1
    assert "Alice" in out[0].detail["message"]


def test_stranger_intrusion_released_after_grace(tmp_path):
    tracker, gate = make(tmp_path, STRANGER)
    out = run(tracker, gate, [(0.0, intrusion(0.0))])
    assert [e.type for e in out] == ["intrusion"]
    assert out[0].detail["personne"] == UNKNOWN and "non identifiée" in out[0].detail["message"]


def test_face_not_visible_is_treated_as_unknown(tmp_path):
    tracker, gate = make(tmp_path, None)   # de dos : sécurité d'abord
    assert gate.process([intrusion(0.0)], 0.0) == []          # retenue pendant l'identification
    assert tracker.status(7, 0.5) == PENDING
    out = run(tracker, gate, [], until=2.2)
    assert [e.type for e in out] == ["intrusion"] and out[0].detail["personne"] == UNKNOWN


def test_one_blurry_match_is_not_enough(tmp_path):
    tracker, _ = make(tmp_path, ALICE)
    tracker.update(FRAME, PERSON, 0.0)   # 1 seule correspondance
    assert tracker.identity(7) is None
    tracker.update(FRAME, PERSON, 0.5)   # 2e : confirmée
    assert tracker.identity(7)[1] == "Alice"


def test_authorized_presence_not_escalated_by_pir(tmp_path):
    tracker, gate = make(tmp_path, ALICE)
    out = run(tracker, gate, [(0.0, intrusion(0.0))])
    f = FusionEngine({"pir_window_s": 3.0, "blind_spot_delay_s": 2.0, "cooldown_s": 10.0})
    f.on_pir(True, 0.0)
    alerts = f.update(1.0, out, persons_visible=True)
    assert alerts[0].level == "info" and alerts[0].pir_confirmed
