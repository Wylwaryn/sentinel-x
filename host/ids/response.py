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

    def block(self, ip, ports=None):
        """Bloque l'IP au pare-feu Windows (règle entrante temporaire).
        ports=None : blocage TOTAL (mode "blocage").
        ports=[...] : on ne bloque QUE ces ports (mode "leurre" : on coupe les vrais services, mais on
        laisse ouverts les ports du honeypot -> l'attaquant est aspiré vers le leurre sans le savoir)."""
        ip = str(ipaddress.ip_address(ip))
        with self._lock:
            if ip in self.blocked:
                return "deja_traitee"
            if not self.admin:
                return "leurre_simule" if ports else "blocage_simule"
            cmd = ["netsh", "advfirewall", "firewall", "add", "rule", f"name={RULE_PREFIX}{ip}",
                   "dir=in", "action=block", f"remoteip={ip}"]
            if ports:  # ne couper que les vrais services ; le honeypot (autres ports) reste joignable
                cmd += ["protocol=TCP", "localport=" + ",".join(str(p) for p in ports)]
            try:
                self.runner(cmd, check=True, capture_output=True)
            except Exception as exc:  # une regle refusee (droits, syntaxe) ne doit PAS tuer l'IDS
                print(f"[IDS] regle pare-feu non appliquee pour {ip} : {exc}")
                return "regle_echec"
            self.blocked[ip] = time.monotonic() + self.ttl_s
            # Démon : n'empêche pas l'arrêt du programme ; unblock_all() nettoie les règles
            timer = threading.Timer(self.ttl_s, self.unblock, args=(ip,))
            timer.daemon = True
            self._timers[ip] = timer
        timer.start()
        return "leurre" if ports else "bloquee"

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
                resp = self.session.post(self.cfg["url"], json=payload, timeout=self.cfg["timeout_s"])
                if resp.status_code >= 400:
                    print(f"[IDS] alerte refusée par l'API ({resp.status_code}) : {resp.text[:200]}")
            except requests.RequestException as exc:
                print(f"[IDS] alerte non envoyée à l'API : {exc}")


def to_api_payload(event):
    """Événement complet -> format AlertIn de l'API d'ingestion (server/ingest/app/models.py)."""
    return {
        "type": event["type_alerte"],
        "level": event["niveau"],
        "source": event["origine"],
        "serie": event["numero_serie"],
        "ip_source": event["ip_source"],
        "score": event["score_ia"],
        "message": event["message"][:500],
        "detail": {"action": event["action"], "ts": event["ts"], **event["details"]},
    }


class Responder:
    """devices / whitelist par IP (statique) ou par adresse MAC (stable : le DHCP du point
    d'accès change les IP à chaque reconnexion). ip_to_mac : résolution fournie par la capture.
    Une MAC peut être usurpée : une machine en liste blanche est toujours ALERTÉE, jamais muette."""

    def __init__(self, response_cfg, protected_ips, devices=None, api=None, blocker=None, base_dir=".",
                 devices_mac=None, ip_to_mac=None):
        self.cfg = response_cfg
        self.never_block = set(response_cfg["whitelist"]) | set(protected_ips)
        self.never_block_mac = {m.lower() for m in response_cfg.get("whitelist_mac", [])}
        self.devices = devices or {}
        self.devices_mac = {m.lower(): s for m, s in (devices_mac or {}).items()}
        self.ip_to_mac = ip_to_mac or (lambda ip: None)
        self.api = api
        self.blocker = blocker or FirewallBlocker(response_cfg["block_ttl_s"])
        self.log_path = Path(base_dir) / response_cfg["log_file"]
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._last: dict[tuple, float] = {}
        # IP ayant déjà prouvé leur hostilité (passées >= block_score au moins une fois). Une attaque
        # a un score qui fluctue : sans ça, dès qu'elle retombe sous block_score, elle n'est plus
        # re-leurrée et récupère l'accès aux vrais services à l'expiration de la règle. "leurre_persistant"
        # (défaut vrai) : on garde une IP connue hostile au leurre tant qu'elle est encore signalée.
        self._hostile: set = set()
        self.sticky = response_cfg.get("leurre_persistant", True)

    def handle(self, ip, score, kind, type_confidence, contributions, vector, now=None):
        if score < self.cfg["alert_score"]:
            return None
        now = time.monotonic() if now is None else now
        key = (ip, kind)
        if now - self._last.get(key, -1e9) < self.cfg["cooldown_s"]:
            return None
        self._last[key] = now

        mac = (self.ip_to_mac(ip) or "").lower() or None
        serie = self.devices.get(ip) or self.devices_mac.get(mac)
        action = "alerte"
        if ip in self.never_block or (mac and mac in self.never_block_mac):
            action = "liste_blanche"
        elif self.cfg["mode"] in ("blocage", "leurre") and (
                score >= self.cfg["block_score"] or (self.sticky and ip in self._hostile)):
            # Hostile si le score franchit le seuil MAINTENANT, ou si l'IP est déjà connue hostile et
            # encore signalée (ici score >= alert_score, garanti plus haut). On la mémorise au premier
            # franchissement : elle reste au leurre même quand son score redescend en "alerte".
            if score >= self.cfg["block_score"]:
                self._hostile.add(ip)
            # "leurre" : on ne coupe que les vrais services, le honeypot reste ouvert (déception).
            ports = self.cfg.get("real_ports") if self.cfg["mode"] == "leurre" else None
            action = self.blocker.block(ip, ports=ports)

        top = top_features(contributions)
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "origine": "RESEAU_IA",
            "type_alerte": kind,
            "niveau": level(score, self.cfg["critical_score"]),
            "ip_source": ip,
            "mac_source": mac,
            "numero_serie": serie,
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
            self.api.send(to_api_payload(event))
        return event
