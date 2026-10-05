"""Formats échangés avec le broker et l'API (sans réseau) : python -m pytest host/vision"""
import json

from alerts import build_payload
from fusion import Alert
from mqtt_link import parse_telemetry


def tele(**kw):
    return json.dumps(kw).encode()


def test_telemetry_from_watched_esp():
    assert parse_telemetry(tele(serie="SX-001", pir=True, temperature_c=22.5), "SX-001") is True
    assert parse_telemetry(tele(serie="SX-001", pir=False), "SX-001") is False


def test_telemetry_from_other_esp_ignored():
    assert parse_telemetry(tele(serie="SX-002", pir=True), "SX-001") is None


def test_telemetry_any_esp_when_no_serie_configured():
    assert parse_telemetry(tele(serie="SX-002", pir=True), None) is True


def test_malformed_telemetry_ignored():
    assert parse_telemetry(b"pas du json", "SX-001") is None
    assert parse_telemetry(tele(serie="SX-001", pir="1"), "SX-001") is None  # type strict
    assert parse_telemetry(tele(serie="SX-001"), "SX-001") is None           # PIR absent / en défaut
    assert parse_telemetry(b"[1, 2]", "SX-001") is None


def test_vision_payload_matches_api_contract():
    alert = Alert(type="intrusion", level="critical", source="fusion", pir_confirmed=True, ts=12.0,
                  track_id=4, zone="zone_interdite", detail={"conf": 0.87})
    p = build_payload(alert, "SX-001", snapshot_jpeg=b"\xff\xd8jpeg")
    # Champs lus par AlertIn (server/ingest/app/models.py)
    assert {k: p[k] for k in ("type", "level", "source", "serie", "pir_confirmed", "track_id", "zone", "score")} == {
        "type": "intrusion", "level": "critical", "source": "fusion", "serie": "SX-001",
        "pir_confirmed": True, "track_id": 4, "zone": "zone_interdite", "score": 0.87}
    assert p["snapshot_jpeg_b64"] == "/9hqcGVn"
    assert "T" in p["ts"]  # horodatage ISO UTC


def test_pir_only_alert_has_no_score():
    alert = Alert(type="pir_blind_spot", level="warning", source="pir", pir_confirmed=True, ts=1.0)
    assert build_payload(alert, "SX-001")["score"] is None
