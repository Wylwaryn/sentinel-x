"""Décision d'alerte et envoi à l'API d'ingestion (origine CAPTEURS_IA, type ANOMALIE_ENVIRONNEMENTALE).

Une évaluation est « suspecte » si :
  * le score d'anomalie dépasse alert_score (comportement anormal maintenant), OU
  * la prévision annonce la borne critique dans horizon_min minutes alors que le score est
    déjà au-dessus de forecast.min_score (alerte précoce), OU
  * une borne d'alerte est franchie (bounds.py) : filet de sécurité, même si le modèle hésite.
On alerte quand `persistence` évaluations consécutives sont suspectes : un pic isolé
(bruit du MQ-2, lecture ratée du DHT22) ne déclenche rien, une vraie dérive est confirmée en +10 s.
CRITIQUE si score >= critical_score, borne critique prévue dans critical_min minutes,
ou borne critique déjà franchie. Chaque alerte porte l'ACTION préventive du type d'incident.
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

from bounds import BOUND_KIND, compute_bounds
from features import FEATURES

LABELS = {
    "SURCHAUFFE": "Surchauffe (risque de départ de feu)",
    "CORRELATION_TEMP_GAZ": "Risque d'incendie : échauffement corrélé au gaz",
    "FUITE_GAZ": "Qualité de l'air dégradée (gaz)",
    "HUMIDITE_ELEVEE": "Humidité élevée (condensation)",
    "HUMIDITE_BASSE": "Air trop sec (électricité statique)",
    "CAPTEUR_DEFAILLANT": "Capteur défaillant",
    "DERIVE_INDETERMINEE": "Dérive environnementale",
}
# Ce qu'il faut faire pour régler le problème AVANT qu'il n'arrive (affiché dans l'alerte du dashboard)
ACTIONS = {
    "SURCHAUFFE": "Vérifier la ventilation et les sources de chaleur ; couper l'appareil en cause avant 45 °C.",
    "CORRELATION_TEMP_GAZ": "Couper l'alimentation de l'équipement suspect, aérer, préparer l'extincteur, prévenir le responsable.",
    "FUITE_GAZ": "Aérer ; aucune flamme ni étincelle (ne pas toucher aux interrupteurs) ; couper l'arrivée de gaz ; évacuer si ça monte.",
    "HUMIDITE_ELEVEE": "Aérer ou déshumidifier ; chercher une fuite d'eau ; éloigner le matériel électronique.",
    "HUMIDITE_BASSE": "Humidifier ; manipuler l'électronique avec précaution (décharges électrostatiques).",
    "CAPTEUR_DEFAILLANT": "Vérifier le câblage et l'alimentation du capteur : ses mesures ne sont plus fiables.",
    "DERIVE_INDETERMINEE": "Dérive inhabituelle sans cause identifiée : inspecter le local.",
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
        "detail": {k: event[k] for k in ("episode", "sous_type", "action", "borne_franchie", "confiance_type",
                                         "prevision", "principales_deviations", "ts")},
    }


class Responder:
    def __init__(self, cfg, api=None, base_dir=".", bounds=None):
        self.resp = cfg["response"]
        self.fc = cfg["forecast"]
        self.bounds = bounds or compute_bounds(cfg["limites"])
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

    def handle(self, serie, score, kind, confidence, contributions, vector, forecast, now=None, ts=None,
               crossed=None):
        """crossed : borne franchie maintenant, (clé, "AVERTISSEMENT"|"CRITIQUE") ou None (bounds.check_bounds)."""
        now = time.monotonic() if now is None else now
        key, minutes = forecast
        early = minutes is not None and minutes <= self.fc["horizon_min"] and score >= self.fc["min_score"]
        suspicious = score >= self.resp["alert_score"] or early or crossed is not None
        persistence = self.resp.get("persistence", 1)
        self._streak[serie] = self._streak.get(serie, 0) + 1 if suspicious else 0
        self._calm[serie] = 0 if suspicious else self._calm.get(serie, 0) + 1
        if self._calm[serie] >= persistence:
            self._episodes.pop(serie, None)  # retour au calme confirmé : l'épisode est clos
        # La persistance filtre le bruit du modèle et des bornes d'avertissement. Une borne CRITIQUE
        # franchie (ex. gaz à 620 pour une limite à 279) n'est pas du bruit : on alerte tout de suite.
        if self._streak[serie] < persistence and not (crossed and crossed[1] == "CRITIQUE"):
            return None

        if crossed and kind == "DERIVE_INDETERMINEE":
            kind = BOUND_KIND[crossed[0]]  # le modèle hésite : la borne franchie donne le type
        critical = (score >= self.resp["critical_score"]
                    or (minutes is not None and minutes <= self.fc["critical_min"])
                    or (crossed is not None and crossed[1] == "CRITIQUE"))
        reason = self._decide(serie, kind, critical, minutes is not None, now)
        if reason is None:
            return None
        top = top_features(np.asarray(contributions))
        if minutes is None:
            prevision, prev_txt = None, ""
        else:
            b = self.bounds[key]
            u, cote = UNITS[b["capteur"]], "haute" if b["sens"] == "haut" else "basse"
            prevision = {"capteur": b["capteur"], "sens": b["sens"], "minutes": round(minutes, 1), "limite": b["critique"]}
            prev_txt = (f" ; borne critique {cote} {b['capteur']} ({b['critique']:g}{u}) "
                        + ("atteinte" if minutes == 0 else f"prévue dans ~{minutes:.0f} min"))
        borne = None
        if crossed:
            b = self.bounds[crossed[0]]
            borne = {"capteur": b["capteur"], "sens": b["sens"], "niveau": crossed[1],
                     "limite": b["critique" if crossed[1] == "CRITIQUE" else "avertissement"]}
        slopes = ", ".join(f"{s} {vector[FEATURES.index(s + '_pente_longue')]:+.2f}{UNITS[s]}/min"
                           for s in ("temp", "hum", "gaz"))
        action = ACTIONS.get(kind, ACTIONS["DERIVE_INDETERMINEE"])
        event = {
            "ts": ts or datetime.now(timezone.utc).isoformat(),
            "episode": reason,
            "serie": serie,
            "niveau": "CRITIQUE" if critical else "AVERTISSEMENT",
            "score_ia": round(float(score), 3),
            "sous_type": kind,
            "action": action,
            "borne_franchie": borne,
            "confiance_type": round(float(confidence), 2),
            "prevision": prevision,
            "principales_deviations": top,
            "message": f"{LABELS.get(kind, kind)} sur {serie} (score {score:.2f}) : {slopes}{prev_txt}. Action : {action}",
            "caracteristiques": {f: round(float(v), 3) for f, v in zip(FEATURES, vector)},
        }
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        if self.api:
            self.api.send(to_api_payload(event))
        return event
