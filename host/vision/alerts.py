"""Envoi des alertes à l'API (POST /api/v1/alerts) en HTTPS, dans un thread dédié
pour ne jamais bloquer la boucle vidéo. Si l'API est désactivée, affichage console."""
import base64
import os
import queue
import threading
from dataclasses import asdict
from datetime import datetime, timezone

import requests


class AlertSender:
    def __init__(self, api_cfg, base_dir):
        self.cfg = api_cfg
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
        payload = asdict(alert)
        payload["ts"] = datetime.now(timezone.utc).isoformat()
        if snapshot_jpeg is not None:
            payload["snapshot_jpeg_b64"] = base64.b64encode(snapshot_jpeg).decode()
        try:
            self.queue.put_nowait(payload)
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
                self.session.post(self.cfg["url"], json=payload, timeout=self.cfg["timeout_s"])
            except requests.RequestException as exc:
                print(f"[ALERTE non envoyée] {label} : {exc}")
