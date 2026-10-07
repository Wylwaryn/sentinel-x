"""Maintenance prédictive Sentinel-X : détecter, prévoir, nommer.

Étage 1 — Détection (non supervisée, entraînée sur le fonctionnement NORMAL uniquement)
  * Isolation Forest (Scikit-Learn) : combinaisons inhabituelles de pentes / corrélations.
  * Écart statistique : max |z| des caractéristiques, rapporté à son percentile normal.
    L'Isolation Forest sature sur les valeurs très au-delà de l'entraînement (constaté sur l'IDS) :
    l'écart statistique couvre ces cas. Score final = max des deux, sur [0, 1].
Prévision — Quand une tendance est significative (pente au-delà de celles du fonctionnement normal,
  seuil APPRIS), projection linéaire jusqu'à la borne CRITIQUE (bounds.py), vers le haut OU vers le bas :
  « au rythme actuel, 45 °C atteints dans ~18 min », « humidité à 20 % dans ~25 min ».
  La prévision chiffre l'urgence AVANT le seuil critique ; les bornes franchies sont un filet de sécurité.
Étage 2 — Type d'incident (supervisé) : Random Forest entraînée sur des incidents synthétiques.
"""
import json
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest, RandomForestClassifier

from features import FEATURES, IDX, PER_SENSOR

NORMAL = "NORMAL"
UNKNOWN = "DERIVE_INDETERMINEE"
MIN_TYPE_CONFIDENCE = 0.5
FORECAST_SENSORS = ["temp", "hum", "gaz"]

# Échelle minimale des caractéristiques « rares » : presque toujours ~0 en fonctionnement normal,
# leur écart-type appris est minuscule et une seule lecture ratée du DHT22 (banal) paraîtrait énorme.
# Un écart ne compte qu'au-delà d'une variation physiquement significative.
SCALE_FLOOR = {"valeurs_manquantes": 0.1, "valeurs_figees": 0.15, "corr_temp_gaz": 0.25, "corr_temp_hum": 0.25}


def _normalized(raw, threshold):
    r = np.maximum(raw, 0) / max(threshold, 1e-9)
    return 1 - 0.5 ** (r ** 4)


class PredictiveModel:
    def __init__(self):
        self.mean = self.std = None
        self.iforest = None
        self.typer = None
        self.if_threshold = self.z_threshold = None
        self.envelope = {}
        self.slope_floor = {}
        self.slope_floor_short = {}
        self.baseline = None      # médianes du fonctionnement normal réel {"temp", "hum", "gaz"}

    def _z(self, rows):
        return (np.asarray(rows, dtype=float) - self.mean) / self.std

    # ---------- étage 1 + enveloppe ----------
    def fit(self, normal_rows, percentile=99.5, trees=200, seed=0, purge=False):
        """purge : le « normal » réel contient des épisodes de test (boîtier manipulé, souffle, briquet).
        On ajuste une première fois, on retire les fenêtres que le modèle juge lui-même anormales
        (score >= 0,5), puis on réajuste sur le normal propre : enveloppe plus serrée, plus sensible."""
        self.purged = 0
        if purge:
            self.fit(normal_rows, percentile, trees, seed)
            scores, _ = self.score(normal_rows)
            keep = [r for r, sc in zip(normal_rows, scores) if sc < 0.5]
            self.purged = len(normal_rows) - len(keep)
            normal_rows = keep
        x_raw = np.asarray(normal_rows, dtype=float)
        self.mean = x_raw.mean(axis=0)
        self.std = x_raw.std(axis=0) + 1e-3
        for name, floor in SCALE_FLOOR.items():
            self.std[IDX[name]] = max(self.std[IDX[name]], floor)
        z = self._z(x_raw)
        self.iforest = IsolationForest(n_estimators=trees, random_state=seed).fit(z)
        self.if_threshold = float(np.percentile(-self.iforest.score_samples(z), percentile))
        self.z_threshold = float(np.percentile(np.abs(z).max(axis=1), percentile))
        self.envelope = {s: float(np.percentile(np.abs(x_raw[:, IDX[f"{s}_dev"]]), percentile))
                         for s in FORECAST_SENSORS}
        # Pente « significative » : au-delà de ce que le fonctionnement normal produit (tendance, pas bruit)
        self.slope_floor = {s: float(np.percentile(np.abs(x_raw[:, IDX[f"{s}_pente_longue"]]), percentile))
                            for s in FORECAST_SENSORS}
        self.slope_floor_short = {s: float(np.percentile(np.abs(x_raw[:, IDX[f"{s}_pente_courte"]]), percentile))
                                  for s in FORECAST_SENSORS}

    # ---------- étage 2 ----------
    def fit_typer(self, incident_rows, incident_labels, normal_rows, seed=0):
        rows = list(incident_rows) + list(normal_rows)
        labels = list(incident_labels) + [NORMAL] * len(normal_rows)
        self.typer = RandomForestClassifier(n_estimators=200, class_weight="balanced", random_state=seed)
        self.typer.fit(self._z(rows), labels)

    # ---------- inférence ----------
    def score(self, rows):
        z = self._z(rows)
        s_if = _normalized(-self.iforest.score_samples(z), self.if_threshold)
        s_z = _normalized(np.abs(z).max(axis=1), self.z_threshold)
        contributions = np.abs(z) / np.maximum(np.abs(z).sum(axis=1, keepdims=True), 1e-9)
        return np.maximum(s_if, s_z), contributions

    def classify(self, rows):
        proba = self.typer.predict_proba(self._z(rows))
        kinds, confidences = [], []
        for p in proba:
            best = int(np.argmax(p))
            kind = self.typer.classes_[best]
            if kind == NORMAL or p[best] < MIN_TYPE_CONFIDENCE:
                kind = UNKNOWN
            kinds.append(kind)
            confidences.append(float(p[best]))
        return kinds, confidences

    def forecast(self, row, last_values, bounds):
        """Minutes avant la borne CRITIQUE de chaque borne (bounds.py), au rythme de la tendance actuelle :
        borne haute -> tendance montante, borne basse -> tendance descendante. Seules les tendances
        SIGNIFICATIVES comptent (|pente| au-delà du fonctionnement normal, seuil appris) : le bruit ne
        prévoit rien. Retourne (clé de borne, minutes) ; minutes = 0 si déjà atteinte ; (None, None) sinon."""
        best = (None, None)
        for key, b in bounds.items():
            s, sign = b["capteur"], (1 if b["sens"] == "haut" else -1)
            last = last_values.get(s)
            # La pente longue (5 min) décide s'il y a une vraie tendance (robuste au bruit) ;
            # la pente courte (1 min) chiffre le délai dès qu'elle est significative : en début de montée,
            # la pente longue mélange encore du « plat » et surestime fortement le délai.
            slope_long = sign * row[IDX[f"{s}_pente_longue"]]
            slope_short = sign * row[IDX[f"{s}_pente_courte"]]
            if last is None or slope_long <= self.slope_floor[s]:
                continue
            slope = slope_short if slope_short > self.slope_floor_short[s] else slope_long
            minutes = max(0.0, sign * (b["critique"] - last) / slope)
            if best[1] is None or minutes < best[1]:
                best = (key, float(minutes))
        return best

    def neutralize(self, row, sensor):
        """Remplace les caractéristiques d'un capteur (et ses corrélations) par leur moyenne normale apprise :
        le capteur ne pèse plus ni sur le score ni sur le type (ex. MQ-2 en préchauffe)."""
        row = list(row)
        names = [f"{sensor}_{f}" for f in PER_SENSOR] + (["corr_temp_gaz"] if sensor == "gaz" else [])
        for name in names:
            row[IDX[name]] = float(self.mean[IDX[name]])
        return row

    # ---------- persistance ----------
    def save(self, directory):
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        joblib.dump(self.iforest, d / "iforest.joblib")
        joblib.dump(self.typer, d / "typer.joblib")
        (d / "meta.json").write_text(json.dumps({
            "features": FEATURES, "mean": self.mean.tolist(), "std": self.std.tolist(),
            "if_threshold": self.if_threshold, "z_threshold": self.z_threshold, "envelope": self.envelope,
            "slope_floor": self.slope_floor, "slope_floor_short": self.slope_floor_short,
            "baseline": self.baseline,
        }, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, directory):
        d = Path(directory)
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        if meta["features"] != FEATURES:
            raise ValueError("Modèle entraîné avec d'autres caractéristiques : ré-entraîner")
        m = cls()
        m.mean, m.std = np.array(meta["mean"]), np.array(meta["std"])
        m.if_threshold, m.z_threshold, m.envelope = meta["if_threshold"], meta["z_threshold"], meta["envelope"]
        m.slope_floor, m.slope_floor_short = meta["slope_floor"], meta["slope_floor_short"]
        m.baseline = meta.get("baseline")
        m.iforest = joblib.load(d / "iforest.joblib")
        m.typer = joblib.load(d / "typer.joblib")
        return m
