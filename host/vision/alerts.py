"""Envoi des alertes à l'API d'ingestion (POST /api/v1/alerts) en HTTPS, dans un thread dédié
pour ne jamais bloquer la boucle vidéo. Si l'API est désactivée, affichage console.

Format attendu par l'API : server/ingest/app/models.py (AlertIn). La vision garde son
vocabulaire (info/warning/critical, presence, loitering...) : l'API le traduit pour la base.
"""
import base64
import os
import queue
import threading
from dataclasses import asdict
from datetime import datetime, timezone

import requests


LABELS = {"presence": "Présence", "intrusion": "Intrusion", "loitering": "Rôdeur",
          "fast_approach": "Approche rapide", "pir_blind_spot": "Angle mort"}


def message(alert):
    """Message lisible pour le journal du dashboard."""
    d = alert.detail
    if d.get("personne") == "autorisee" and d.get("message"):
        text = d["message"]                                      # « Personne autorisée : <nom> (<rôle>) »
    elif alert.type == "pir_blind_spot":
        text = d.get("message") or LABELS[alert.type]
    else:
        text = LABELS.get(alert.type, alert.type)
        if d.get("personne") == "inconnue":
            text += " : personne non identifiée"
        if alert.zone:
            text += f" ({alert.zone.replace('_', ' ')})"
    if alert.pir_confirmed and alert.type != "pir_blind_spot":
        text += ", confirmée par le PIR"
    return text[:500]


def build_payload(alert, serie, snapshot_jpeg=None):
    payload = asdict(alert)
    payload["ts"] = datetime.now(timezone.utc).isoformat()
    payload["serie"] = serie
    payload["score"] = alert.detail.get("conf")  # confiance YOLO (ou de la reconnaissance), None pour le PIR seul
    payload["message"] = message(alert)
    if alert.detail.get("id_personne") is not None:
        # -> alerte.id_personne_reconnue (ignoré par l'API tant qu'elle ne gère pas ce champ)
        payload["personne_reconnue"] = alert.detail["id_personne"]
    if snapshot_jpeg is not None:
        payload["snapshot_jpeg_b64"] = base64.b64encode(snapshot_jpeg).decode()
    return payload


class AlertSender:
    def __init__(self, api_cfg, base_dir, serie=None):
        self.cfg = api_cfg
        self.serie = serie
        self.enabled = api_cfg["enabled"]
        self.queue = queue.Queue(maxsize=100)
        self.session = requests.Session()
        if self.enabled:
            token = os.environ.get(api_cfg["token_env"])
            if not token:
                raise RuntimeError(f"Variable d'environnement {api_cfg['token_env']} manquante")
            self.session.headers["Authorization"] = f"Bearer {token}"
            self.session.verify = os.path.join(base_dir, api_cfg["ca_cert"])
        threading.Thread(target=self._worker, daemon=True).start()

    def send(self, alert, snapshot_jpeg=None):
        try:
            self.queue.put_nowait(build_payload(alert, self.serie, snapshot_jpeg))
        except queue.Full:
            pass  # on privilégie la fluidité de la vidéo

    def _worker(self):
        while True:
            payload = self.queue.get()
            label = f"[{payload['level'].upper()}] {payload['type']} id={payload['track_id']} " \
                    f"zone={payload['zone']} PIR={'oui' if payload['pir_confirmed'] else 'non'}"
            if not self.enabled:
                print(f"[ALERTE] {label}")
                continue
            try:
                resp = self.session.post(self.cfg["url"], json=payload, timeout=self.cfg["timeout_s"])
                if resp.status_code >= 400:
                    print(f"[ALERTE refusée {resp.status_code}] {label} : {resp.text[:200]}")
            except requests.RequestException as exc:
                print(f"[ALERTE non envoyée] {label} : {exc}")
