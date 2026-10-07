"""Caractéristiques cinétiques des séries temporelles des capteurs (DHT22 + MQ-2).

Pour chaque capteur, sur une fenêtre courte (1 min) et longue (5 min) :
  dev          écart de la dernière valeur à la médiane longue (niveau relatif, pas absolu)
  pente_courte pente des moindres carrés sur la fenêtre courte (unités / minute)
  pente_longue pente sur la fenêtre longue : dérive lente
  volatilite   écart-type court : bruit, instabilité
  acceleration pente_courte - pente_longue : l'évolution s'emballe-t-elle ?
Entre capteurs : corrélations température/gaz et température/humidité (fenêtre longue).
Qualité : part de valeurs manquantes et part de valeurs figées (capteur bloqué).

Tout est relatif à l'historique récent : le modèle juge des ÉVOLUTIONS, pas des seuils fixes.
"""
from collections import defaultdict, deque

import numpy as np

SENSORS = [("temperature_c", "temp"), ("humidite_pct", "hum"), ("gaz_brut", "gaz")]
PER_SENSOR = ["dev", "pente_courte", "pente_longue", "volatilite", "acceleration"]
FEATURES = [f"{short}_{f}" for _, short in SENSORS for f in PER_SENSOR] + [
    "corr_temp_gaz", "corr_temp_hum", "valeurs_manquantes", "valeurs_figees"]
IDX = {name: i for i, name in enumerate(FEATURES)}


def _slope_per_min(t, y):
    if len(y) < 3 or np.ptp(t) == 0:
        return 0.0
    t = t - t.mean()
    return float((t * (y - y.mean())).sum() / (t * t).sum() * 60.0)


def _corr(a, b):
    if len(a) < 5 or a.std() == 0 or b.std() == 0:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def compute_features(samples, now, short_s, long_s, min_short=5, min_long=20, quality_cols=(1, 2, 3)):
    """samples : liste de (t, temperature_c, humidite_pct, gaz_brut), None si capteur en défaut.
    quality_cols : capteurs pris en compte dans valeurs_manquantes / valeurs_figees (1 temp, 2 hum, 3 gaz) ;
    on en retire le gaz pendant la préchauffe du MQ-2 (il est alors absent volontairement).
    Retourne le vecteur FEATURES, ou None si l'historique est insuffisant."""
    long_w = [s for s in samples if now - long_s <= s[0] <= now]
    short_w = [s for s in long_w if s[0] >= now - short_s]
    if len(short_w) < min_short or len(long_w) < min_long:
        return None

    vec = []
    series = {}
    for col, (_, short) in enumerate(SENSORS, start=1):
        lt = np.array([s[0] for s in long_w if s[col] is not None], dtype=float)
        lv = np.array([s[col] for s in long_w if s[col] is not None], dtype=float)
        st = np.array([s[0] for s in short_w if s[col] is not None], dtype=float)
        sv = np.array([s[col] for s in short_w if s[col] is not None], dtype=float)
        if len(sv) < 3 or len(lv) < 5:
            vec += [0.0] * len(PER_SENSOR)  # capteur muet : visible via valeurs_manquantes
            continue
        p_short, p_long = _slope_per_min(st, sv), _slope_per_min(lt, lv)
        vec += [float(sv[-1] - np.median(lv)), p_short, p_long, float(sv.std()), p_short - p_long]
        series[short] = {round(t, 3): v for t, v in zip(lt, lv)}

    def paired(a, b):
        if a not in series or b not in series:
            return np.array([]), np.array([])
        common = sorted(set(series[a]) & set(series[b]))
        return np.array([series[a][t] for t in common]), np.array([series[b][t] for t in common])

    vec.append(_corr(*paired("temp", "gaz")))
    vec.append(_corr(*paired("temp", "hum")))

    cells = [s[c] for s in short_w for c in quality_cols]
    vec.append(sum(v is None for v in cells) / len(cells))
    frozen, pairs = 0, 0
    for c in quality_cols:
        vals = [s[c] for s in short_w if s[c] is not None]
        pairs += max(len(vals) - 1, 0)
        frozen += sum(1 for a, b in zip(vals, vals[1:]) if a == b)
    vec.append(frozen / pairs if pairs else 1.0)
    return vec


class SeriesBuffer:
    """Historique glissant par numéro de série (ESP)."""

    def __init__(self, keep_s):
        self.keep_s = keep_s
        self.data = defaultdict(deque)
        self.first_seen = {}

    def add(self, serie, t, temp, hum, gaz):
        buf = self.data[serie]
        self.first_seen.setdefault(serie, t)
        buf.append((t, temp, hum, gaz))
        while buf and buf[0][0] < t - self.keep_s:
            buf.popleft()

    def samples(self, serie):
        return list(self.data[serie])
