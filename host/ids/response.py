"""Réponse aux anomalies : journal JSONL, envoi à l'API d'ingestion, blocage temporaire.

Modes :
- "alerte"  : on journalise et on alerte, sans rien bloquer (par défaut).
- "blocage" : en plus, règle de pare-feu Windows entrante temporaire pour l'IP fautive.
La liste blanche (ESP, PC de l'équipe) et les IP protégées ne sont JAMAIS bloquées,
mais restent alertées : une attaque « sniper » depuis un ESP compromis doit se voir.
"""
import ctypes
import ipaddress
import json
import os
import queue
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from classify import level, top_features
from features import FEATURES

RULE_PREFIX = "SentinelX-IDS-"


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


class FirewallBlocker:
    """Règles netsh temporaires. L'IP est validée avant d'être passée à netsh
    (jamais de chaîne brute venue du réseau dans une ligne de commande)."""

    def __init__(self, ttl_s, runner=subprocess.run, admin=None):
        self.ttl_s = ttl_s
        self.runner = runner
        self.admin = is_admin() if admin is None else admin
        self.blocked: dict[str, float] = {}
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def block(self, ip):
        ip = str(ipaddress.ip_address(ip))
        with self._lock:
            if ip in self.blocked:
                return "deja_bloquee"
            if not self.admin:
                return "blocage_simule"
            self.runner(["netsh", "advfirewall", "firewall", "add", "rule", f"name={RULE_PREFIX}{ip}",
                         "dir=in", "action=block", f"remoteip={ip}"], check=True, capture_output=True)
            self.blocked[ip] = time.monotonic() + self.ttl_s
            # Démon : n'empêche pas l'arrêt du programme ; unblock_all() nettoie les règles
            timer = threading.Timer(self.ttl_s, self.unblock, args=(ip,))
            timer.daemon = True
            self._timers[ip] = timer
        timer.start()
        return "bloquee"

    def unblock(self, ip):
        with self._lock:
            timer = self._timers.pop(ip, None)
            if timer:
                timer.cancel()
            if self.blocked.pop(ip, None) is None:
                return
            self.runner(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={RULE_PREFIX}{ip}"],
                        check=False, capture_output=True)

    def unblock_all(self):
        for ip in list(self.blocked):
            self.unblock(ip)


class ApiSender:
    def __init__(self, api_cfg, base_dir):
        self.cfg = api_cfg
        self.queue = queue.Queue(maxsize=200)
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
                self.session.post(self.cfg["url"], json=payload, timeout=self.cfg["timeout_s"])
            except requests.RequestException as exc:
                print(f"[IDS] alerte non envoyée à l'API : {exc}")


class Responder:
    def __init__(self, response_cfg, protected_ips, devices=None, api=None, blocker=None, base_dir="."):
        self.cfg = response_cfg
        self.never_block = set(response_cfg["whitelist"]) | set(protected_ips)
        self.devices = devices or {}
        self.api = api
        self.blocker = blocker or FirewallBlocker(response_cfg["block_ttl_s"])
        self.log_path = Path(base_dir) / response_cfg["log_file"]
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._last: dict[tuple, float] = {}

    def handle(self, ip, score, kind, type_confidence, contributions, vector, now=None):
        if score < self.cfg["alert_score"]:
            return None
        now = time.monotonic() if now is None else now
        key = (ip, kind)
        if now - self._last.get(key, -1e9) < self.cfg["cooldown_s"]:
            return None
        self._last[key] = now

        action = "alerte"
        if ip in self.never_block:
            action = "liste_blanche"
        elif self.cfg["mode"] == "blocage" and score >= self.cfg["block_score"]:
            action = self.blocker.block(ip)

        top = top_features(contributions)
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "origine": "RESEAU_IA",
            "type_alerte": kind,
            "niveau": level(score, self.cfg["critical_score"]),
            "ip_source": ip,
            "numero_serie": self.devices.get(ip),
            "score_ia": round(float(score), 3),
            "message": f"{kind} depuis {ip} (score {score:.2f}) : {', '.join(top)} ; action {action}",
            "action": action,
            "details": {
                "confiance_type": round(float(type_confidence), 2),
                "principales_deviations": top,
                "caracteristiques": {f: round(float(v), 2) for f, v in zip(FEATURES, vector)},
            },
        }
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        if self.api:
            self.api.send(event)
        return event
