"""IA réseau en deux étages.

Étage 1 — « Est-ce anormal ? » (non supervisé, entraîné UNIQUEMENT sur notre trafic normal)
  * Autoencodeur PyTorch (GPU) : erreur de reconstruction -> repère les volumes hors norme.
  * Isolation Forest : repère les combinaisons inhabituelles DANS les plages normales
    (il sature au-delà : un flood x1000 n'est pas « plus isolé » qu'un extrême normal).
  Chaque score est ramené sur [0, 1] par s = 1 - 0.5 ** (r ** 4), r = score / seuil
  (r = 1 -> 0.5 ; r = 2 -> ~1). Score final = MAX des deux : chaque modèle couvre l'angle mort de l'autre.

Étage 2 — « Quel type d'attaque ? » (supervisé)
  * Random Forest entraînée sur des profils d'attaques synthétiques variés + du trafic normal.
  * Une anomalie que le classifieur ne reconnaît pas (prédit NORMAL ou confiance < 0.5)
    devient TRAFIC_ANORMAL : on ne force jamais une étiquette.
"""
import json
from pathlib import Path

import joblib
import numpy as np
import torch
from sklearn.ensemble import IsolationForest, RandomForestClassifier
from torch import nn

from features import FEATURES, transform

NORMAL = "NORMAL"
MIN_TYPE_CONFIDENCE = 0.5


class _AutoEncoder(nn.Module):
    def __init__(self, n):
        super().__init__()
        self.encoder = nn.Sequential(nn.Linear(n, 10), nn.ReLU(), nn.Linear(10, 5), nn.ReLU())
        self.decoder = nn.Sequential(nn.Linear(5, 10), nn.ReLU(), nn.Linear(10, n))

    def forward(self, x):
        return self.decoder(self.encoder(x))


def _normalized(raw, threshold):
    r = np.maximum(raw, 0) / max(threshold, 1e-9)
    return 1 - 0.5 ** (r ** 4)


class NetworkAnomalyDetector:
    def __init__(self, device=None):
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.mean = self.std = None
        self.ae = None
        self.iforest = None
        self.typer = None
        self.ae_threshold = self.if_threshold = None

    def _prepare(self, rows):
        x = np.array([transform(r) for r in rows], dtype=np.float32)
        return (x - self.mean) / self.std

    # ---------- étage 1 ----------
    def fit(self, normal_rows, epochs=300, percentile=99.5, seed=0):
        torch.manual_seed(seed)
        raw = np.array([transform(r) for r in normal_rows], dtype=np.float32)
        self.mean = raw.mean(axis=0)
        self.std = raw.std(axis=0) + 1e-3  # caractéristiques constantes en trafic normal
        x = (raw - self.mean) / self.std

        self.ae = _AutoEncoder(x.shape[1]).to(self.device)
        opt = torch.optim.Adam(self.ae.parameters(), lr=1e-2)
        xt = torch.tensor(x, device=self.device)
        for _ in range(epochs):
            opt.zero_grad()
            loss = nn.functional.mse_loss(self.ae(xt), xt)
            loss.backward()
            opt.step()

        self.iforest = IsolationForest(n_estimators=200, random_state=seed).fit(x)

        ae_err, _ = self._ae_errors(x)
        self.ae_threshold = float(np.percentile(ae_err, percentile))
        self.if_threshold = float(np.percentile(-self.iforest.score_samples(x), percentile))
        return float(loss.item())

    # ---------- étage 2 ----------
    def fit_typer(self, attack_rows, attack_labels, normal_rows, seed=0):
        rows = list(attack_rows) + list(normal_rows)
        labels = list(attack_labels) + [NORMAL] * len(normal_rows)
        self.typer = RandomForestClassifier(n_estimators=200, class_weight="balanced", random_state=seed)
        self.typer.fit(self._prepare(rows), labels)

    # ---------- inférence ----------
    def _ae_errors(self, x):
        self.ae.eval()
        with torch.no_grad():
            xt = torch.tensor(x, device=self.device)
            per_feature = ((self.ae(xt) - xt) ** 2).cpu().numpy()
        return per_feature.mean(axis=1), per_feature

    def score(self, rows):
        """(scores d'anomalie [0,1], contributions par caractéristique) pour chaque ligne."""
        x = self._prepare(rows)
        ae_err, per_feature = self._ae_errors(x)
        if_raw = -self.iforest.score_samples(x)
        scores = np.maximum(_normalized(ae_err, self.ae_threshold), _normalized(if_raw, self.if_threshold))
        deviation = per_feature + np.abs(x)
        contributions = deviation / deviation.sum(axis=1, keepdims=True)
        return scores, contributions

    def classify(self, rows):
        """(types, confiances) pour chaque ligne ; TRAFIC_ANORMAL si type inconnu ou incertain."""
        proba = self.typer.predict_proba(self._prepare(rows))
        classes = self.typer.classes_
        kinds, confidences = [], []
        for p in proba:
            best = int(np.argmax(p))
            kind = classes[best]
            if kind == NORMAL or p[best] < MIN_TYPE_CONFIDENCE:
                kind = "TRAFIC_ANORMAL"
            kinds.append(kind)
            confidences.append(float(p[best]))
        return kinds, confidences

    # ---------- persistance ----------
    def save(self, directory):
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        torch.save(self.ae.state_dict(), d / "autoencoder.pt")
        joblib.dump(self.iforest, d / "iforest.joblib")
        joblib.dump(self.typer, d / "typer.joblib")
        (d / "meta.json").write_text(json.dumps({
            "features": FEATURES,
            "mean": self.mean.tolist(),
            "std": self.std.tolist(),
            "ae_threshold": self.ae_threshold,
            "if_threshold": self.if_threshold,
        }, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, directory, device=None):
        d = Path(directory)
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        if meta["features"] != FEATURES:
            raise ValueError("Modèle entraîné avec d'autres caractéristiques : ré-entraîner")
        det = cls(device)
        det.mean = np.array(meta["mean"], dtype=np.float32)
        det.std = np.array(meta["std"], dtype=np.float32)
        det.ae_threshold = meta["ae_threshold"]
        det.if_threshold = meta["if_threshold"]
        det.ae = _AutoEncoder(len(FEATURES)).to(det.device)
        det.ae.load_state_dict(torch.load(d / "autoencoder.pt", map_location=det.device, weights_only=True))
        det.iforest = joblib.load(d / "iforest.joblib")
        det.typer = joblib.load(d / "typer.joblib")
        return det
