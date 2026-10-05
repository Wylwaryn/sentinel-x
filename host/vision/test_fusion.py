"""Tests de la logique zones + fusion PIR, sans caméra ni GPU : pytest host/vision"""
from behaviour import BehaviourAnalyzer
from fusion import FusionEngine

ZONES = [
    {"name": "perimetre", "kind": "perimeter", "polygon": [[0, 0.25], [1, 0.25], [1, 1], [0, 1]]},
    {"name": "zone_interdite", "kind": "restricted", "polygon": [[0.35, 0.55], [0.65, 0.55], [0.65, 1], [0.35, 1]]},
]
BEHAVIOUR = {"loiter_s": 10.0, "fast_speed": 0.6, "speed_window_s": 0.5, "track_timeout_s": 2.0}
FUSION = {"pir_window_s": 3.0, "blind_spot_delay_s": 2.0, "cooldown_s": 10.0}


def person(tid, cx, feet_y):
    return {"track_id": tid, "box": (cx - 0.05, feet_y - 0.3, cx + 0.05, feet_y)}


def types(events):
    return [e.type for e in events]


def test_presence_then_loitering_then_intrusion():
    a = BehaviourAnalyzer(ZONES, BEHAVIOUR)
    assert types(a.update([person(1, 0.1, 0.9)], 0.0)) == ["presence"]
    assert a.update([person(1, 0.1, 0.9)], 5.0) == []
    assert types(a.update([person(1, 0.1, 0.9)], 10.5)) == ["loitering"]
    assert types(a.update([person(1, 0.5, 0.9)], 21.0)) == ["intrusion"]


def test_intrusion_detected_on_entry():
    a = BehaviourAnalyzer(ZONES, BEHAVIOUR)
    events = a.update([person(1, 0.5, 0.9)], 0.0)
    assert set(types(events)) == {"presence", "intrusion"}


def test_outside_zones_no_event():
    a = BehaviourAnalyzer(ZONES, BEHAVIOUR)
    assert a.update([person(1, 0.5, 0.1)], 0.0) == []


def test_fast_approach():
    a = BehaviourAnalyzer(ZONES, BEHAVIOUR)
    a.update([person(1, 0.1, 0.9)], 0.0)
    events = a.update([person(1, 0.6, 0.9)], 0.5)  # 0.5 largeur en 0.5 s = 1.0/s
    assert "fast_approach" in types(events)


def test_pir_confirms_and_escalates():
    a, f = BehaviourAnalyzer(ZONES, BEHAVIOUR), FusionEngine(FUSION)
    f.on_pir(True, 0.0)
    alerts = f.update(0.5, a.update([person(1, 0.1, 0.9)], 0.5), persons_visible=True)
    assert alerts[0].type == "presence"
    assert alerts[0].pir_confirmed and alerts[0].level == "warning"  # info -> warning


def test_camera_only_not_confirmed():
    a, f = BehaviourAnalyzer(ZONES, BEHAVIOUR), FusionEngine(FUSION)
    alerts = f.update(0.0, a.update([person(1, 0.5, 0.9)], 0.0), persons_visible=True)
    intrusion = next(al for al in alerts if al.type == "intrusion")
    assert not intrusion.pir_confirmed and intrusion.level == "critical"


def test_pir_window_after_falling_edge():
    f = FusionEngine(FUSION)
    f.on_pir(True, 0.0)
    f.on_pir(False, 1.0)
    assert f.pir_recent(3.5)
    assert not f.pir_recent(4.5)


def test_blind_spot_when_pir_without_person():
    f = FusionEngine(FUSION)
    f.on_pir(True, 0.0)
    assert f.update(1.0, [], persons_visible=False) == []
    alerts = f.update(2.5, [], persons_visible=False)
    assert [al.type for al in alerts] == ["pir_blind_spot"]


def test_no_blind_spot_when_person_seen():
    f = FusionEngine(FUSION)
    f.on_pir(True, 0.0)
    f.update(1.0, [], persons_visible=True)
    assert f.update(2.5, [], persons_visible=False) == []


def test_cooldown_prevents_spam():
    f = FusionEngine(FUSION)
    f.on_pir(True, 0.0)
    assert len(f.update(2.5, [], persons_visible=False)) == 1
    assert f.update(5.0, [], persons_visible=False) == []
    assert len(f.update(13.0, [], persons_visible=False)) == 1
