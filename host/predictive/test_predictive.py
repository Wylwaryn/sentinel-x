"""Tests de la maintenance prédictive (sans MQTT ni API) : python -m pytest host/predictive"""
import json
import random
from pathlib import Path

import numpy as np
import pytest

from features import IDX, compute_features
from model import PredictiveModel
from response import Responder, to_api_payload
from sentinel_predictive import parse_telemetry
from simulate import INCIDENTS, generate_series, incident_dataset, normal_dataset, windows

CFG = json.loads((Path(__file__).parent / "config.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def model():
    normal = normal_dataset(CFG, n_series=30, seed=0)
    m = PredictiveModel()
    m.fit(normal)
    m.fit_typer(*incident_dataset(CFG, n_series_per_type=60, seed=0), normal)
    return m


# ---------- caractéristiques ----------
def test_slope_and_deviation_of_a_ramp():
    # +1 °C/min pendant 5 min, échantillon toutes les 2 s
    samples = [(t, 20 + t / 60, 50.0, 200) for t in range(0, 301, 2)]
    v = compute_features(samples, 300, 60, 300)
    assert v[IDX["temp_pente_longue"]] == pytest.approx(1.0, abs=1e-6)
    assert v[IDX["temp_dev"]] == pytest.approx(2.5, abs=0.05)  # dernière valeur - médiane
    assert v[IDX["gaz_pente_longue"]] == 0.0


def test_insufficient_history_returns_none():
    assert compute_features([(0, 20, 50, 200), (2, 20, 50, 200)], 2, 60, 300) is None


def test_frozen_and_missing_sensors_are_measured():
    frozen = [(t, 21.0, 40.0, 250) for t in range(0, 301, 2)]
    assert compute_features(frozen, 300, 60, 300)[IDX["valeurs_figees"]] == 1.0
    muted = [(t, None, None, 250 + (t % 7)) for t in range(0, 301, 2)]
    assert compute_features(muted, 300, 60, 300)[IDX["valeurs_manquantes"]] == pytest.approx(2 / 3)


# ---------- détection ----------
def test_low_false_alarm_rate_on_unseen_normal(model):
    """Fausses alertes réellement émises (après persistance) sur du fonctionnement normal jamais vu."""
    rng = random.Random(99)
    persistence = CFG["response"]["persistence"]
    fired = evaluations = 0
    for _ in range(10):
        scores, _ = model.score(windows(generate_series(rng, duration_s=1800), CFG))
        streak = 0
        for s in scores:
            streak = streak + 1 if s >= CFG["response"]["alert_score"] else 0
            fired += streak == persistence
            evaluations += 1
    minutes = evaluations * CFG["windows"]["step_s"] / 60
    print(f"\n  {fired} fausse(s) alerte(s) en {minutes:.0f} min de fonctionnement normal simulé")
    assert fired / minutes < 1 / 60  # moins d'une fausse alerte par heure


def test_unseen_incidents_detected_and_typed(model):
    rows, labels = incident_dataset(CFG, n_series_per_type=25, seed=321, delay_s=180, every=6)
    scores, _ = model.score(rows)
    kinds, _ = model.classify(rows)
    detection = np.mean(scores >= CFG["response"]["alert_score"])
    accuracy = np.mean([k == y for k, y in zip(kinds, labels)])
    per_type = {k: np.mean([kk == k for kk, y in zip(kinds, labels) if y == k]) for k in INCIDENTS}
    print(f"\n  détection {detection:.1%} | typage {accuracy:.1%} | par type "
          + ", ".join(f"{k} {v:.0%}" for k, v in per_type.items()))
    assert detection >= 0.90
    assert accuracy >= 0.85


# ---------- prévision : alerter AVANT de sortir de l'enveloppe ----------
def test_alerts_long_before_critical_limit_with_accurate_eta(model, tmp_path):
    """Surchauffe +1 °C/min depuis 25 °C : limite 45 °C atteinte 20 min après le début.
    L'IA doit alerter bien avant, et le délai annoncé doit être juste (à 25 % près)."""
    from sentinel_predictive import Pipeline
    cfg = dict(CFG, response=dict(CFG["response"], log_file=str(tmp_path / "e.jsonl")))
    pipe = Pipeline(cfg, model, Responder(cfg, api=None, base_dir=tmp_path))
    rng = random.Random(5)
    onset, limit = 600, CFG["forecast"]["limites_exploitation"]["temp"]
    crossing = onset + (limit - 25.0) * 60
    first_alert = first_forecast = None
    for t in range(0, int(crossing), 2):
        ramp = max(0, t - onset) / 60
        pipe.add("SX", t, round(25.0 + ramp + rng.gauss(0, 0.08), 1), round(45 - 1.5 * ramp + rng.gauss(0, 0.3), 1),
                 int(250 + rng.gauss(0, 5)))
        if t % 10 == 0:
            for ev in pipe.evaluate(t, verbose=False):
                first_alert = first_alert or (t, ev)
                if ev["prevision"] and first_forecast is None:
                    first_forecast = (t, ev)
    assert first_alert is not None, "aucune alerte avant la limite"
    assert first_forecast is not None, "aucune prévision avant la limite"
    t_alert, t_fc, ev = first_alert[0], first_forecast[0], first_forecast[1]
    real_remaining = (crossing - t_fc) / 60
    print(f"\n  1re alerte {(t_alert - onset) / 60:.1f} min après le début de la surchauffe "
          f"({(crossing - t_alert) / 60:.1f} min avant les {limit:g} °C) ; prévision : "
          f"{ev['prevision']['minutes']} min annoncées pour {real_remaining:.1f} min réelles")
    assert t_alert - onset <= 180                       # détectée en moins de 3 min
    assert ev["prevision"]["capteur"] == "temp"
    assert ev["prevision"]["minutes"] == pytest.approx(real_remaining, rel=0.25)


def test_no_forecast_when_stable(model):
    v = compute_features([(t, 22.0 + 0.1 * ((t // 2) % 2), 45.0, 200 + (t % 5)) for t in range(0, 301, 2)], 300, 60, 300)
    assert model.forecast(v, {"temp": 22.0, "gaz": 200}, CFG["forecast"]["limites_exploitation"]) == (None, None)


# ---------- réponse ----------
def test_alert_payload_matches_ingest_contract(tmp_path):
    cfg = dict(CFG, response=dict(CFG["response"], log_file=str(tmp_path / "e.jsonl")))
    r = Responder(cfg, api=None, base_dir=tmp_path)
    v = [0.0] * len(IDX)
    assert r.handle("SX-G2-01", 0.9, "SURCHAUFFE", 0.8, np.ones(len(IDX)), v, ("temp", 2.0), now=0) is None
    ev = r.handle("SX-G2-01", 0.9, "SURCHAUFFE", 0.8, np.ones(len(IDX)), v, ("temp", 2.0), now=10)
    p = to_api_payload(ev)
    assert p["type"] == "ANOMALIE_ENVIRONNEMENTALE" and p["source"] == "CAPTEURS_IA"
    assert p["level"] == "CRITIQUE" and p["serie"] == "SX-G2-01" and p["score"] == 0.9
    assert p["detail"]["sous_type"] == "SURCHAUFFE"
    assert p["detail"]["prevision"] == {"capteur": "temp", "minutes": 2.0, "limite": 45.0}
    assert len(p["message"]) <= 500


def test_one_alert_per_episode(tmp_path):
    cfg = dict(CFG, response=dict(CFG["response"], log_file=str(tmp_path / "e.jsonl")))
    r = Responder(cfg, api=None, base_dir=tmp_path)
    v, c = [0.0] * len(IDX), np.ones(len(IDX))
    h = lambda s, kind, fc, now: r.handle("SX", s, kind, 0.8, c, v, fc, now=now)  # noqa: E731
    assert h(0.1, "SURCHAUFFE", ("temp", 25.0), 0) is None                # score trop bas
    assert h(0.35, "SURCHAUFFE", ("temp", 25.0), 10) is None              # 1re suspicion
    ev = h(0.35, "SURCHAUFFE", ("temp", 25.0), 20)                        # confirmée : alerte précoce
    assert ev["episode"] == "nouvel_episode" and ev["niveau"] == "AVERTISSEMENT"
    for now in range(30, 200, 10):                                        # même épisode, même niveau
        assert h(0.6, "SURCHAUFFE", ("temp", 20.0), now) is None
    ev = h(0.95, "SURCHAUFFE", ("temp", 0.0), 200)                        # aggravation
    assert ev["episode"] == "aggravation" and ev["niveau"] == "CRITIQUE"
    assert h(0.95, "SURCHAUFFE", ("temp", 0.0), 400) is None              # pas de spam
    assert h(0.95, "SURCHAUFFE", ("temp", 0.0), 800)["episode"] == "rappel"
    assert h(0.1, "SURCHAUFFE", (None, None), 810) is None                # retour au calme
    assert h(0.1, "SURCHAUFFE", (None, None), 820) is None                # ... confirmé : épisode clos
    assert h(0.9, "FUITE_GAZ", (None, None), 830) is None
    assert h(0.9, "FUITE_GAZ", (None, None), 840)["episode"] == "nouvel_episode"


def test_isolated_spike_does_not_alert(tmp_path):
    cfg = dict(CFG, response=dict(CFG["response"], log_file=str(tmp_path / "e.jsonl")))
    r = Responder(cfg, api=None, base_dir=tmp_path)
    v, c = [0.0] * len(IDX), np.ones(len(IDX))
    for i, s in enumerate([0.2, 0.95, 0.2, 0.95, 0.2]):  # pics isolés, jamais deux de suite
        assert r.handle("SX", s, "FUITE_GAZ", 0.8, c, v, (None, None), now=10 * i) is None


def test_parse_telemetry():
    assert parse_telemetry(b'{"serie": "SX-G2-01", "temperature_c": 21.5, "humidite_pct": null, "gaz_brut": 312}') == \
        ("SX-G2-01", 21.5, None, 312.0)
    assert parse_telemetry(b'{"temperature_c": 21.5}') is None
    assert parse_telemetry(b"pas du json") is None
    assert parse_telemetry(b'{"serie": "SX", "temperature_c": true}') == ("SX", None, None, None)
