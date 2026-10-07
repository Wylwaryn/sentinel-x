"""Séries réalistes (DHT22 + MQ-2) : fonctionnement normal et incidents, CALÉES sur la télémétrie réelle.

Calage sur 5 039 mesures réelles de SX-G2-01 (6-7 oct.) : une mesure toutes les 5 s ; température
24,5-27,5 °C (bruit ~0,3) ; humidité 65-73 % (bruit ~1,1) ; gaz brut MQ-2 60-76 (bruit ~3,7).
Le MQ-2 réagit avec des amplitudes modestes (un test réel : +50 sur une base de 67).

Usages :
  * entraîner le classifieur de TYPE : incidents SUPERPOSÉS à de vrais tronçons de télémétrie
    (incident_dataset(..., base=sessions_réelles)), complétés par des séries synthétiques ;
  * amorcer le modèle sans données réelles (normal_dataset) ; tests.

Incidents (l'action préventive associée est dans response.py) :
  SURCHAUFFE            hausse de température (ventilation HS, appareil qui chauffe) ; l'humidité
                        relative baisse mécaniquement quand l'air chauffe
  CORRELATION_TEMP_GAZ  hausse lente de température + dérive du gaz : risque d'incendie (exemple du sujet)
  FUITE_GAZ             qualité de l'air dégradée : montée du MQ-2 (fuite progressive, bouffée, fumée)
  HUMIDITE_ELEVEE       montée de l'humidité sans échauffement (fuite d'eau, vapeur)
  HUMIDITE_BASSE        chute de l'humidité sans échauffement (air sec, climatisation)
  CAPTEUR_DEFAILLANT    valeurs figées, aberrantes ou manquantes
"""
import math
import random

from features import compute_features

INCIDENTS = ["SURCHAUFFE", "CORRELATION_TEMP_GAZ", "FUITE_GAZ", "HUMIDITE_ELEVEE", "HUMIDITE_BASSE",
             "CAPTEUR_DEFAILLANT"]
DT = 5.0           # cadence réelle du firmware
REAL_SHARE = 0.75  # part des incidents superposés à de la télémétrie réelle (si fournie)


def base_series(rng, duration_s=1800, dt=DT):
    """Fonctionnement normal synthétique, aux niveaux et bruits mesurés sur la pièce."""
    temp0, hum0, gaz0 = rng.uniform(24.5, 27.5), rng.uniform(65, 72), rng.uniform(60, 75)
    t_amp, t_per = rng.uniform(0.3, 1.2), rng.uniform(2400, 7200)   # dérive lente de la pièce
    g_amp, g_per = rng.uniform(3, 8), rng.uniform(900, 2400)
    t_noise, h_noise, g_noise = rng.uniform(0.1, 0.3), rng.uniform(0.4, 1.2), rng.uniform(2, 5)
    phase = rng.uniform(0, 2 * math.pi)
    out, t = [], 0.0
    while t < duration_s:
        drift = math.sin(2 * math.pi * t / t_per + phase)
        temp = temp0 + t_amp * drift + rng.gauss(0, t_noise)
        hum = hum0 - 2.0 * t_amp * drift + rng.gauss(0, h_noise)
        gaz = gaz0 + g_amp * math.sin(2 * math.pi * t / g_per + phase) + rng.gauss(0, g_noise)
        if rng.random() < 0.005:  # échec de lecture ponctuel du DHT22, normal
            temp = hum = None
        out.append((t, temp, hum, gaz))
        t += dt
    return out


def _plus(v, d):
    return None if v is None else v + d


def _finalize(t, temp, hum, gaz):
    temp = None if temp is None else round(min(max(temp, -40), 80), 1)
    hum = None if hum is None else round(min(max(hum, 0), 100), 1)
    gaz = None if gaz is None else int(min(max(gaz, 0), 1023))
    return t, temp, hum, gaz


def apply_incident(samples, kind, onset_s, rng):
    """Superpose un incident (à partir de onset_s) à une série normale, réelle ou synthétique."""
    p = {"rate_t": rng.uniform(0.4, 2.5), "rh_k": rng.uniform(1.5, 3.0),
         "slow_t": rng.uniform(0.12, 0.4), "slow_g": rng.uniform(0.8, 4.0),
         "puff": rng.random() < 0.4, "amp_g": rng.uniform(25, 150), "tau": rng.uniform(10, 60),
         "rate_g": rng.uniform(3, 40), "rate_h": rng.uniform(0.5, 4.0), "steam": rng.random() < 0.3,
         "amp_h": rng.uniform(12, 28), "rate_hb": rng.uniform(0.3, 2.0)}
    mode = rng.choice(["fige", "aberrant", "muet"])
    frozen, out = None, []
    for t, temp, hum, gaz in samples:
        if kind and t >= onset_s:
            m = (t - onset_s) / 60.0
            if kind == "SURCHAUFFE":
                temp, hum, gaz = _plus(temp, p["rate_t"] * m), _plus(hum, -p["rh_k"] * p["rate_t"] * m), _plus(gaz, 0.5 * m)
            elif kind == "CORRELATION_TEMP_GAZ":
                temp, hum, gaz = _plus(temp, p["slow_t"] * m), _plus(hum, -p["rh_k"] * p["slow_t"] * m), _plus(gaz, p["slow_g"] * m)
            elif kind == "FUITE_GAZ":
                d = p["amp_g"] * (1 - math.exp(-(t - onset_s) / p["tau"])) if p["puff"] else p["rate_g"] * m
                gaz = _plus(gaz, d)
            elif kind == "HUMIDITE_ELEVEE":
                d = p["amp_h"] * (1 - math.exp(-(t - onset_s) / 90)) if p["steam"] else p["rate_h"] * m
                hum, temp = _plus(hum, d), _plus(temp, 0.02 * m)
            elif kind == "HUMIDITE_BASSE":
                hum = _plus(hum, -p["rate_hb"] * m)
            elif kind == "CAPTEUR_DEFAILLANT":
                if mode == "fige":
                    frozen = frozen or _finalize(0, temp, hum, gaz)[1:]
                    out.append((t, *frozen))
                    continue
                if mode == "aberrant" and rng.random() < 0.35:
                    temp = _plus(temp, rng.choice([-1, 1]) * rng.uniform(4, 12))
                    hum = _plus(hum, rng.choice([-1, 1]) * rng.uniform(10, 30))
                elif mode == "muet" and rng.random() < 0.5:
                    temp = hum = None
        out.append(_finalize(t, temp, hum, gaz))
    return out


def generate_series(rng, duration_s=1800, dt=DT, incident=None, onset_s=None):
    return apply_incident(base_series(rng, duration_s, dt), incident, onset_s, rng)


def real_segment(rng, base, duration_s):
    """Tronçon continu de télémétrie réelle de duration_s, recalé à t = 0 (None si aucun assez long)."""
    long_enough = [s for s in base if s[-1][0] - s[0][0] >= duration_s]
    if not long_enough:
        return None
    s = rng.choice(long_enough)
    start = rng.uniform(s[0][0], s[-1][0] - duration_s)
    seg = [x for x in s if start <= x[0] < start + duration_s]
    return [(t - seg[0][0], *v) for t, *v in seg]


def windows(samples, cfg, start_s=0.0, every=1):
    w = cfg["windows"]
    out = []
    t = max(start_s, w["long_s"] * 0.5)
    end = samples[-1][0]
    k = 0
    while t <= end:
        if k % every == 0:
            v = compute_features(samples, t, w["short_s"], w["long_s"], w["min_samples_short"], w["min_samples_long"])
            if v is not None:
                out.append(v)
        t += w["step_s"]
        k += 1
    return out


def normal_dataset(cfg, n_series=30, seed=0):
    rng = random.Random(seed)
    rows = []
    for _ in range(n_series):
        rows += windows(generate_series(rng, duration_s=1800), cfg)
    return rows


def incident_dataset(cfg, n_series_per_type=60, seed=0, delay_s=60, every=3, base=None,
                     real_share=REAL_SHARE):
    """(fenêtres, étiquettes) prises au moins delay_s après le début de l'incident.
    base : sessions de télémétrie réelle [[(t, temp, hum, gaz), ...], ...] sur lesquelles superposer
    les incidents (REAL_SHARE d'entre eux) ; sinon fond synthétique."""
    rng = random.Random(seed)
    rows, labels = [], []
    for kind in INCIDENTS:
        for _ in range(n_series_per_type):
            onset = rng.uniform(400, 600)
            duration = onset + 420
            seg = real_segment(rng, base, duration) if base and rng.random() < real_share else None
            s = apply_incident(seg, kind, onset, rng) if seg else generate_series(
                rng, duration_s=duration, incident=kind, onset_s=onset)
            w = windows(s, cfg, start_s=onset + delay_s, every=every)
            rows += w
            labels += [kind] * len(w)
    return rows, labels
