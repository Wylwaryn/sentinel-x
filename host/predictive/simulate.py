"""Séries synthétiques réalistes (DHT22 + MQ-2) : fonctionnement normal et incidents.

Sert aux tests, à amorcer le modèle et à entraîner le classifieur de type d'incident.
Le détecteur de démo DOIT être ré-entraîné sur la télémétrie réelle (mode record).
Incidents :
  SURCHAUFFE            hausse de température (lente : panne de ventilation ; rapide : sèche-cheveux)
  FUITE_GAZ             montée du MQ-2 (fuite progressive ou bouffée de briquet)
  CORRELATION_TEMP_GAZ  hausse lente de température + micro-dérive du gaz (exemple du sujet)
  CAPTEUR_DEFAILLANT    valeurs figées, aberrantes ou manquantes
"""
import math
import random

from features import compute_features

INCIDENTS = ["SURCHAUFFE", "FUITE_GAZ", "CORRELATION_TEMP_GAZ", "CAPTEUR_DEFAILLANT"]


def generate_series(rng, duration_s=1800, dt=2.0, incident=None, onset_s=None):
    temp0, hum0, gaz0 = rng.uniform(18, 27), rng.uniform(30, 60), rng.uniform(120, 400)
    # Pièce réelle : ~1 °C/heure au plus en fonctionnement normal (pente max ~0,05 °C/min)
    t_amp, t_per = rng.uniform(0.1, 0.3), rng.uniform(2400, 5400)
    g_amp, g_per = rng.uniform(4, 20), rng.uniform(900, 2400)
    t_noise, g_noise = rng.uniform(0.04, 0.12), rng.uniform(2, 8)
    phase = rng.uniform(0, 2 * math.pi)
    mode = rng.choice(["fige", "aberrant", "muet"])
    p = {"rate_t": rng.uniform(0.4, 2.5), "rate_g": rng.uniform(15, 200), "puff": rng.random() < 0.5,
         "amp_g": rng.uniform(100, 500), "tau": rng.uniform(10, 60),
         "slow_t": rng.uniform(0.12, 0.3), "slow_g": rng.uniform(2.0, 8.0)}
    frozen = None
    samples = []
    t = 0.0
    while t < duration_s:
        drift = math.sin(2 * math.pi * t / t_per + phase)
        temp = temp0 + t_amp * drift + rng.gauss(0, t_noise)
        hum = hum0 - 1.2 * t_amp * drift + rng.gauss(0, 0.3)
        gaz = gaz0 + g_amp * math.sin(2 * math.pi * t / g_per + phase) + rng.gauss(0, g_noise)

        if incident and t >= onset_s:
            m = (t - onset_s) / 60.0
            if incident == "SURCHAUFFE":
                temp += p["rate_t"] * m
                hum -= 1.5 * p["rate_t"] * m
                gaz += 1.0 * m
            elif incident == "FUITE_GAZ":
                gaz += p["amp_g"] * (1 - math.exp(-(t - onset_s) / p["tau"])) if p["puff"] else p["rate_g"] * m
            elif incident == "CORRELATION_TEMP_GAZ":
                temp += p["slow_t"] * m
                hum -= 1.2 * p["slow_t"] * m
                gaz += p["slow_g"] * m
            elif incident == "CAPTEUR_DEFAILLANT":
                if mode == "fige":
                    frozen = frozen or (round(temp, 1), round(hum, 1), int(gaz))
                    temp, hum, gaz = frozen
                elif mode == "aberrant" and rng.random() < 0.35:
                    temp += rng.choice([-1, 1]) * rng.uniform(4, 12)
                    hum += rng.choice([-1, 1]) * rng.uniform(10, 30)
                elif mode == "muet" and rng.random() < 0.5:
                    temp = hum = None

        if temp is not None and not (incident == "CAPTEUR_DEFAILLANT" and mode == "fige" and t >= onset_s):
            temp = round(min(max(temp, -40), 80), 1)
            hum = round(min(max(hum, 0), 100), 1)
        if rng.random() < 0.005:  # échec de lecture ponctuel du DHT22, normal
            temp = hum = None
        gaz = int(min(max(gaz, 0), 1023))
        samples.append((t, temp, hum, gaz))
        t += dt
    return samples


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


def incident_dataset(cfg, n_series_per_type=60, seed=0, delay_s=60, every=3):
    """Fenêtres prises au moins delay_s après le début de l'incident."""
    rng = random.Random(seed)
    rows, labels = [], []
    for kind in INCIDENTS:
        for _ in range(n_series_per_type):
            onset = rng.uniform(400, 600)
            s = generate_series(rng, duration_s=onset + 420, incident=kind, onset_s=onset)
            w = windows(s, cfg, start_s=onset + delay_s, every=every)
            rows += w
            labels += [kind] * len(w)
    return rows, labels
