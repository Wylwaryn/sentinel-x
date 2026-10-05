"""Décision d'alerte et envoi à l'API d'ingestion (origine CAPTEURS_IA, type ANOMALIE_ENVIRONNEMENTALE).

Une évaluation est « suspecte » si :
  * le score d'anomalie dépasse alert_score (comportement anormal maintenant), OU
  * la prévision annonce une sortie de l'enveloppe normale dans horizon_min minutes
    alors que le score est déjà au-dessus de forecast.min_score (alerte précoce).
On alerte quand `persistence` évaluations consécutives sont suspectes : un pic isolé
(bruit du MQ-2, lecture ratée du DHT22) ne déclenche rien, une vraie dérive est confirmée en +10 s.
CRITIQUE si score >= critical_score ou sortie prévue dans critical_min minutes.
"""
import json
import os
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests

from features import FEATURES

LABELS = {
    "SURCHAUFFE": "Surchauffe",
    "FUITE_GAZ": "Fuite de gaz",
    "CORRELATION_TEMP_GAZ": "Hausse de température corrélée au gaz",
    "CAPTEUR_DEFAILLANT": "Capteur défaillant",
    "DERIVE_INDETERMINEE": "Dérive environnementale",
}
UNITS = {"temp": "°C", "hum": "%", "gaz": ""}


def top_features(contributions, n=3):
    return [FEATURES[i] for i in np.argsort(contributions)[::-1][:n]]


class ApiSender:
    def __init__(self, api_cfg, base_dir):
        self.cfg = api_cfg
        self.queue = queue.Queue(maxsize=100)
        self.session = requests.Session()
        token = os.environ.get(api_cfg["token_env"])
        if not token:
            raise RuntimeError(f"Variable d'environnement {api_cfg['token_env']} manquante")
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.session.verify = str((Path(base_dir) / api_cfg["ca_cert"]).resolve())
        threading.Thread(target=self._worker, daemon=True).start()

    def send(self, payload):
        try:
            self.queue.put_nowait(payload)
        except queue.Full:
            pass

    def _worker(self):
        while True:
            payload = self.queue.get()
            try:
                r = self.session.post(self.cfg["url"], json=payload, timeout=self.cfg["timeout_s"])
                if r.status_code >= 400:
                    print(f"[PRÉDICTIF] alerte refusée par l'API ({r.status_code}) : {r.text[:200]}")
            except requests.RequestException as exc:
                print(f"[PRÉDICTIF] alerte non envoyée : {exc}")


def to_api_payload(event):
    """Format AlertIn de l'API d'ingestion (server/ingest/app/models.py)."""
    return {
        "type": "ANOMALIE_ENVIRONNEMENTALE",
        "level": event["niveau"],
        "source": "CAPTEURS_IA",
        "serie": event["serie"],
        "score": event["score_ia"],
        "message": event["message"][:500],
        "detail": {k: event[k] for k in ("episode", "sous_type", "confiance_type", "prevision",
                                         "principales_deviations", "ts")},
    }


class Responder:
    def __init__(self, cfg, api=None, base_dir="."):
        self.resp = cfg["response"]
        self.fc = cfg["forecast"]
        self.api = api
        self.log_path = Path(base_dir) / self.resp["log_file"]
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._streak = {}
        self._calm = {}
        self._episodes = {}  # serie -> {"kind", "critical", "last_sent"}

    def _decide(self, serie, kind, critical, has_forecast, now):
        """Une alerte par épisode : début, aggravation, type précisé, délai estimé disponible,
        puis rappel au plus toutes les reminder_s."""
        ep = self._episodes.get(serie)
        if ep is None:
            reason = "nouvel_episode"
        elif now - ep["last_sent"] < self.resp["cooldown_s"]:
            return None
        elif critical and not ep["critical"]:
            reason = "aggravation"
        elif kind != ep["kind"] and kind != "DERIVE_INDETERMINEE":
            reason = "type_precise"
        elif has_forecast and not ep["forecast"]:
            reason = "prevision_disponible"
        elif now - ep["last_sent"] >= self.resp["reminder_s"]:
            reason = "rappel"
        else:
            return None
        self._episodes[serie] = {"kind": kind, "critical": critical or bool(ep and ep["critical"]),
                                 "forecast": has_forecast or bool(ep and ep["forecast"]), "last_sent": now}
        return reason

    def handle(self, serie, score, kind, confidence, contributions, vector, forecast, now=None, ts=None):
        now = time.monotonic() if now is None else now
        sensor, minutes = forecast
        early = minutes is not None and minutes <= self.fc["horizon_min"] and score >= self.fc["min_score"]
        suspicious = score >= self.resp["alert_score"] or early
        persistence = self.resp.get("persistence", 1)
        self._streak[serie] = self._streak.get(serie, 0) + 1 if suspicious else 0
        self._calm[serie] = 0 if suspicious else self._calm.get(serie, 0) + 1
        if self._calm[serie] >= persistence:
            self._episodes.pop(serie, None)  # retour au calme confirmé : l'épisode est clos
        if self._streak[serie] < persistence:
            return None

        critical = score >= self.resp["critical_score"] or (minutes is not None and minutes <= self.fc["critical_min"])
        reason = self._decide(serie, kind, critical, minutes is not None, now)
        if reason is None:
            return None
        top = top_features(np.asarray(contributions))
        if minutes is None:
            prevision = None
            prev_txt = ""
        else:
            limit = self.fc["limites_exploitation"][sensor]
            prevision = {"capteur": sensor, "minutes": round(minutes, 1), "limite": limit}
            prev_txt = (f" ; limite d'exploitation {sensor} ({limit:g}{UNITS[sensor]}) atteinte" if minutes == 0 else
                        f" ; limite d'exploitation {sensor} ({limit:g}{UNITS[sensor]}) prévue dans ~{minutes:.0f} min")
        slopes = ", ".join(f"{s} {vector[FEATURES.index(s + '_pente_longue')]:+.2f}{UNITS[s]}/min"
                           for s in ("temp", "gaz"))
        event = {
            "ts": ts or datetime.now(timezone.utc).isoformat(),
            "episode": reason,
            "serie": serie,
            "niveau": "CRITIQUE" if critical else "AVERTISSEMENT",
            "score_ia": round(float(score), 3),
            "sous_type": kind,
            "confiance_type": round(float(confidence), 2),
            "prevision": prevision,
            "principales_deviations": top,
            "message": f"{LABELS.get(kind, kind)} sur {serie} (score {score:.2f}) : {slopes}{prev_txt}",
            "caracteristiques": {f: round(float(v), 3) for f, v in zip(FEATURES, vector)},
        }
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        if self.api:
            self.api.send(to_api_payload(event))
        return event
