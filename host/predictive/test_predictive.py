"""Tests de la maintenance prédictive (sans MQTT ni API) : python -m pytest host/predictive"""
import json
import random
from pathlib import Path

import numpy as np
import pytest

from bounds import check_bounds, compute_bounds
from features import IDX, compute_features
from model import PredictiveModel
from response import ACTIONS, Responder, to_api_payload
from sentinel_predictive import Pipeline, clean_sessions, parse_telemetry, split_sessions
from simulate import INCIDENTS, generate_series, incident_dataset, normal_dataset, windows

CFG = json.loads((Path(__file__).parent / "config.json").read_text(encoding="utf-8"))
BOUNDS = compute_bounds(CFG["limites"])          # sans modèle : gaz relatif à reference_defaut (67)
BASE_GAZ = CFG["limites"]["gaz"]["reference_defaut"]


@pytest.fixture(scope="module")
def model():
    normal = normal_dataset(CFG, n_series=30, seed=0)
    m = PredictiveModel()
    m.fit(normal)
    m.fit_typer(*incident_dataset(CFG, n_series_per_type=60, seed=0), normal)
    return m


def cfg_tmp(tmp_path):
    return dict(CFG, response=dict(CFG["response"], log_file=str(tmp_path / "e.jsonl")))


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


def test_gas_excluded_from_quality_during_warmup():
    no_gas = [(t, 21.0 + (t % 3) / 10, 60.0 + (t % 5) / 10, None) for t in range(0, 301, 5)]
    v = compute_features(no_gas, 300, 60, 300, quality_cols=(1, 2))
    assert v[IDX["valeurs_manquantes"]] == 0.0   # le gaz absent n'est pas compté comme une panne


# ---------- bornes ----------
def test_gas_bounds_are_relative_to_learned_baseline():
    b = compute_bounds(CFG["limites"], {"temp": 26, "hum": 69, "gaz": 58})
    assert b["gaz_haut"]["avertissement"] == 58 + CFG["limites"]["gaz"]["avertissement_delta"]
    assert b["gaz_haut"]["critique"] == 58 + CFG["limites"]["gaz"]["critique_delta"]
    assert b["temp_haut"]["critique"] == CFG["limites"]["temp"]["critique_haut"]


def test_check_bounds_levels_and_directions():
    ok = {"temp": 26.0, "hum": 69.0, "gaz": 67}
    assert check_bounds(ok, BOUNDS) is None
    assert check_bounds(dict(ok, temp=36.0), BOUNDS) == ("temp_haut", "AVERTISSEMENT")
    assert check_bounds(dict(ok, temp=46.0), BOUNDS) == ("temp_haut", "CRITIQUE")
    assert check_bounds(dict(ok, hum=25.0), BOUNDS) == ("hum_bas", "AVERTISSEMENT")
    assert check_bounds(dict(ok, hum=15.0), BOUNDS) == ("hum_bas", "CRITIQUE")
    assert check_bounds(dict(ok, hum=36.0, temp=46.0), BOUNDS) == ("temp_haut", "CRITIQUE")  # le plus grave
    assert check_bounds(dict(ok, gaz=None, temp=None), BOUNDS) is None


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
    assert detection >= 0.80
    assert accuracy >= 0.85
    assert per_type["HUMIDITE_ELEVEE"] >= 0.8 and per_type["HUMIDITE_BASSE"] >= 0.6


# ---------- prévision : alerter AVANT la borne critique ----------
def test_alerts_long_before_critical_limit_with_accurate_eta(model, tmp_path):
    """Surchauffe +1 °C/min depuis 25 °C : borne critique 45 °C atteinte 20 min après le début.
    L'IA doit alerter bien avant, et le délai annoncé doit être juste (à 25 % près)."""
    cfg = cfg_tmp(tmp_path)
    pipe = Pipeline(cfg, model, Responder(cfg, api=None, base_dir=tmp_path))
    rng = random.Random(5)
    onset, limit = 600, CFG["limites"]["temp"]["critique_haut"]
    crossing = onset + (limit - 25.0) * 60
    first_alert = first_forecast = None
    for t in range(0, int(crossing), 5):
        ramp = max(0, t - onset) / 60
        pipe.add("SX", t, round(25.0 + ramp + rng.gauss(0, 0.1), 1), round(68 - 1.5 * ramp + rng.gauss(0, 0.5), 1),
                 int(BASE_GAZ + rng.gauss(0, 3)))
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
    assert t_alert >= onset                            # rien avant l'incident
    assert t_alert - onset <= 180                      # détectée en moins de 3 min
    assert ev["prevision"]["capteur"] == "temp"
    assert ev["prevision"]["minutes"] == pytest.approx(real_remaining, rel=0.25)
    assert first_alert[1]["action"] == ACTIONS[first_alert[1]["sous_type"]]


def test_no_forecast_when_stable(model):
    v = compute_features([(t, 22.0 + 0.1 * ((t // 2) % 2), 45.0, 200 + (t % 5)) for t in range(0, 301, 2)], 300, 60, 300)
    assert model.forecast(v, {"temp": 22.0, "hum": 45.0, "gaz": 67}, BOUNDS) == (None, None)


def test_forecast_falling_humidity_targets_low_bound(model):
    # -1,5 %/min depuis 60 % : 20 % (borne critique basse) atteints ~26,7 min après la fin de la fenêtre
    samples = [(t, 25.0, 60.0 - 1.5 * t / 60, 67) for t in range(0, 301, 5)]
    v = compute_features(samples, 300, 60, 300)
    key, minutes = model.forecast(v, {"temp": 25.0, "hum": samples[-1][2], "gaz": 67}, BOUNDS)
    assert key == "hum_bas"
    assert minutes == pytest.approx((samples[-1][2] - 20) / 1.5, rel=0.1)


def test_cold_mq2_warmup_does_not_alert(model, tmp_path):
    """Allumage à froid : le MQ-2 part de ~8 et remonte vers la base en ~16 min. Sans la gestion de la
    préchauffe, cette montée ressemblerait à une fuite. Température et humidité normales."""
    cfg = cfg_tmp(tmp_path)
    pipe = Pipeline(cfg, model, Responder(cfg, api=None, base_dir=tmp_path))
    rng, events = random.Random(7), []
    for t in range(0, 30 * 60, 5):
        gaz = BASE_GAZ - (BASE_GAZ - 8) * np.exp(-t / 300)       # remontée exponentielle (~16 min)
        pipe.add("SX", t, round(26 + rng.gauss(0, 0.1), 1), round(69 + rng.gauss(0, 0.5), 1), int(gaz + rng.gauss(0, 2)))
        if t % 10 == 0:
            events += pipe.evaluate(t, verbose=False)
    assert events == [], [e["message"] for e in events]


def test_gas_leak_during_stabilisation_still_alerts(model, tmp_path):
    """Juste après la préchauffe, une vraie fuite (le gaz MONTE au-dessus de la base) doit alerter."""
    cfg = cfg_tmp(tmp_path)
    pipe = Pipeline(cfg, model, Responder(cfg, api=None, base_dir=tmp_path))
    events = []
    for t in range(0, 20 * 60, 5):
        gaz = 10 if t < 600 else (BASE_GAZ if t < 660 else BASE_GAZ + 120)   # froid, puis base, puis fuite
        pipe.add("SX", t, 26.0 + (t % 3) / 10, 69.0 + (t % 5) / 10, gaz)
        if t % 10 == 0:
            events += pipe.evaluate(t, verbose=False)
    assert events and events[0]["sous_type"] == "FUITE_GAZ" and events[0]["niveau"] == "CRITIQUE"


def test_neutralize_sets_sensor_features_to_normal_mean(model):
    v = [9.0] * len(IDX)
    n = model.neutralize(v, "gaz")
    assert n[IDX["gaz_pente_longue"]] == pytest.approx(model.mean[IDX["gaz_pente_longue"]])
    assert n[IDX["corr_temp_gaz"]] == pytest.approx(model.mean[IDX["corr_temp_gaz"]])
    assert n[IDX["temp_dev"]] == 9.0


# ---------- réponse ----------
def test_alert_payload_matches_ingest_contract(tmp_path):
    r = Responder(cfg_tmp(tmp_path), api=None, base_dir=tmp_path)
    v = [0.0] * len(IDX)
    assert r.handle("SX-G2-01", 0.9, "SURCHAUFFE", 0.8, np.ones(len(IDX)), v, ("temp_haut", 2.0), now=0) is None
    ev = r.handle("SX-G2-01", 0.9, "SURCHAUFFE", 0.8, np.ones(len(IDX)), v, ("temp_haut", 2.0), now=10)
    p = to_api_payload(ev)
    assert p["type"] == "ANOMALIE_ENVIRONNEMENTALE" and p["source"] == "CAPTEURS_IA"
    assert p["level"] == "CRITIQUE" and p["serie"] == "SX-G2-01" and p["score"] == 0.9
    assert p["detail"]["sous_type"] == "SURCHAUFFE"
    assert p["detail"]["action"] == ACTIONS["SURCHAUFFE"]
    assert p["detail"]["prevision"] == {"capteur": "temp", "sens": "haut", "minutes": 2.0, "limite": 45}
    assert "Action :" in p["message"] and len(p["message"]) <= 500


def test_crossed_critical_bound_alerts_even_if_model_is_calm(tmp_path):
    """Filet de sécurité : borne critique franchie -> alerte CRITIQUE, même avec un score bas,
    et le type vient de la borne quand le modèle n'en a pas."""
    r = Responder(cfg_tmp(tmp_path), api=None, base_dir=tmp_path)
    v, c = [0.0] * len(IDX), np.zeros(len(IDX))
    crossed = ("hum_haut", "CRITIQUE")
    assert r.handle("SX", 0.05, "DERIVE_INDETERMINEE", 0.3, c, v, (None, None), now=0, crossed=crossed) is None
    ev = r.handle("SX", 0.05, "DERIVE_INDETERMINEE", 0.3, c, v, (None, None), now=10, crossed=crossed)
    assert ev["niveau"] == "CRITIQUE" and ev["sous_type"] == "HUMIDITE_ELEVEE"
    assert ev["borne_franchie"] == {"capteur": "hum", "sens": "haut", "niveau": "CRITIQUE", "limite": 90}


def test_one_alert_per_episode(tmp_path):
    r = Responder(cfg_tmp(tmp_path), api=None, base_dir=tmp_path)
    v, c = [0.0] * len(IDX), np.ones(len(IDX))
    h = lambda s, kind, fc, now: r.handle("SX", s, kind, 0.8, c, v, fc, now=now)  # noqa: E731
    assert h(0.1, "SURCHAUFFE", ("temp_haut", 25.0), 0) is None                # score trop bas
    assert h(0.35, "SURCHAUFFE", ("temp_haut", 25.0), 10) is None              # 1re suspicion
    ev = h(0.35, "SURCHAUFFE", ("temp_haut", 25.0), 20)                        # confirmée : alerte précoce
    assert ev["episode"] == "nouvel_episode" and ev["niveau"] == "AVERTISSEMENT"
    for now in range(30, 200, 10):                                             # même épisode, même niveau
        assert h(0.6, "SURCHAUFFE", ("temp_haut", 20.0), now) is None
    ev = h(0.95, "SURCHAUFFE", ("temp_haut", 0.0), 200)                        # aggravation
    assert ev["episode"] == "aggravation" and ev["niveau"] == "CRITIQUE"
    assert h(0.95, "SURCHAUFFE", ("temp_haut", 0.0), 400) is None              # pas de spam
    assert h(0.95, "SURCHAUFFE", ("temp_haut", 0.0), 800)["episode"] == "rappel"
    assert h(0.1, "SURCHAUFFE", (None, None), 810) is None                     # retour au calme
    assert h(0.1, "SURCHAUFFE", (None, None), 820) is None                     # ... confirmé : épisode clos
    assert h(0.9, "FUITE_GAZ", (None, None), 830) is None
    assert h(0.9, "FUITE_GAZ", (None, None), 840)["episode"] == "nouvel_episode"


def test_isolated_spike_does_not_alert(tmp_path):
    r = Responder(cfg_tmp(tmp_path), api=None, base_dir=tmp_path)
    v, c = [0.0] * len(IDX), np.ones(len(IDX))
    for i, s in enumerate([0.2, 0.95, 0.2, 0.95, 0.2]):  # pics isolés, jamais deux de suite
        assert r.handle("SX", s, "FUITE_GAZ", 0.8, c, v, (None, None), now=10 * i) is None


# ---------- préparation des données réelles ----------
def test_sessions_split_and_cold_start_trimmed():
    cold = [(t, 26.0, 69.0, 8 + t // 20) for t in range(0, 1200, 5)]          # MQ-2 froid qui remonte
    warm = [(t, 26.0, 69.0, 67) for t in range(5000, 6200, 5)]                # session déjà chaude
    sessions = split_sessions(cold + warm)
    assert len(sessions) == 2
    clean = clean_sessions(sessions, CFG, base_gaz=67)
    # Session froide : prête à t = 780 s (gaz >= 0,7 x 67), + 300 s de stabilisation -> trop courte, écartée.
    # Session chaude : seules les 300 premières secondes (fenêtre incomplète) sont retirées, puis recalée à 0.
    assert len(clean) == 1
    assert clean[0][0][0] == 0 and len(clean[0]) == (6200 - 5300) // 5
    assert all(g == 67 for *_, g in clean[0])


def test_parse_telemetry():
    assert parse_telemetry(b'{"serie": "SX-G2-01", "temperature_c": 21.5, "humidite_pct": null, "gaz_brut": 312}') == \
        ("SX-G2-01", 21.5, None, 312.0)
    assert parse_telemetry(b'{"temperature_c": 21.5}') is None
    assert parse_telemetry(b"pas du json") is None
    assert parse_telemetry(b'{"serie": "SX", "temperature_c": true}') == ("SX", None, None, None)


# ---------- IA embarquée (jumeau Python du code ESP8266) ----------
EDGE_P = {"base_temp": 26.2, "base_hum": 68.7, "base_gaz": 67, "temp_warn": 35, "temp_crit": 45,
          "hum_high_warn": 80, "hum_high_crit": 90, "hum_low_warn": 30, "hum_low_crit": 20,
          "gaz_warn": 97, "gaz_crit": 147, "gaz_prechauffe": 47,
          "slope_temp": 0.66, "slope_hum": 1.77, "slope_gaz": 3.71}


def edge_run(fn, minutes=30):
    from edge import replay
    return [e for e in replay(EDGE_P, [(t, *fn(t)) for t in range(0, minutes * 60, 5)]) if e[2]]


def test_edge_quiet_room_and_cold_mq2_do_not_alert():
    rng = random.Random(3)
    cold = lambda t: (round(26 + rng.gauss(0, 0.1), 1), round(69 + rng.gauss(0, 0.5), 1),  # noqa: E731
                      int(67 - 59 * np.exp(-t / 300) + rng.gauss(0, 2)))
    assert edge_run(cold) == []


def test_edge_overheat_detected_before_critical_bound_with_eta():
    ramp = lambda t: (26 + max(0, t - 300) / 60, 69 - 2 * max(0, t - 300) / 60, 67)  # noqa: E731
    evs = edge_run(ramp)
    t, kind, level, minutes = evs[0]
    assert kind == "SURCHAUFFE" and t - 300 <= 240            # vue en moins de 4 min
    assert minutes == pytest.approx((45 - (26 + (t - 300) / 60)) / 1.0, rel=0.25)   # délai juste
    assert any(e[2] == "CRITIQUE" for e in evs)               # puis critique avant 45 °C


def test_edge_temperature_plus_gas_drift_is_fire_risk():
    fire = lambda t: (26 + 0.3 * max(0, t - 300) / 60, 69.0, 67 + 4 * max(0, t - 300) / 60)  # noqa: E731
    assert any(e[1] == "RISQUE_FEU" for e in edge_run(fire))


def test_edge_critical_bound_alerts_from_boot():
    evs = edge_run(lambda t: (26.0, 93.0, 67), minutes=2)   # humidité déjà au-dessus de 90 % à l'allumage
    assert evs and evs[0][1] == "HUMIDITE_HAUTE" and evs[0][2] == "CRITIQUE"
