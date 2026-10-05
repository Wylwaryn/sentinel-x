"""Fusion capteur PIR (ESP8266) + événements caméra.

Règles :
- Événement caméra + PIR actif (fenêtre ±pir_window_s)  -> alerte CONFIRMÉE, niveau élevé d'un cran.
- PIR actif sans personne visible depuis blind_spot_delay_s -> avertissement "angle mort".
- Événement caméra sans PIR                               -> alerte non confirmée, niveau inchangé.
Anti-spam : une même alerte (type, track_id) n'est pas renvoyée avant cooldown_s.

Le HC-SR501 maintient sa sortie HAUTE quelques secondes après détection (potentiomètre
"Time") : la fenêtre pir_window_s couvre le décalage entre les deux capteurs.
"""
import threading
from dataclasses import dataclass, field

ESCALATE = {"info": "warning", "warning": "critical", "critical": "critical"}


@dataclass
class Alert:
    type: str
    level: str
    source: str  # "vision", "fusion" ou "pir"
    pir_confirmed: bool
    ts: float
    track_id: int | None = None
    zone: str | None = None
    detail: dict = field(default_factory=dict)


class FusionEngine:
    def __init__(self, fusion_cfg):
        self.cfg = fusion_cfg
        self._lock = threading.Lock()
        self._pir_state = False
        self._pir_last_high = None
        self._pir_rose_at = None
        self._last_person_seen = None
        self._sent: dict[tuple, float] = {}

    def on_pir(self, active, ts):
        """Appelé par le thread MQTT (ou la simulation clavier)."""
        with self._lock:
            if active:
                if not self._pir_state:
                    self._pir_rose_at = ts
                self._pir_last_high = ts
            elif self._pir_state:
                # Front descendant : le PIR était encore haut jusqu'à maintenant
                self._pir_last_high = ts
            self._pir_state = active

    def pir_recent(self, ts):
        with self._lock:
            return self._pir_recent_unlocked(ts)

    def update(self, ts, vision_events, persons_visible):
        alerts = []
        with self._lock:
            if persons_visible:
                self._last_person_seen = ts
            pir = self._pir_recent_unlocked(ts)

            for ev in vision_events:
                level = ESCALATE[ev.level] if pir else ev.level
                alert = Alert(type=ev.type, level=level, source="fusion" if pir else "vision",
                              pir_confirmed=pir, ts=ts, track_id=ev.track_id, zone=ev.zone,
                              detail=dict(ev.detail))
                if self._should_send((ev.type, ev.track_id), ts):
                    alerts.append(alert)

            blind = self._blind_spot_unlocked(ts)
            if blind and self._should_send(("pir_blind_spot", None), ts):
                alerts.append(Alert(type="pir_blind_spot", level="warning", source="pir",
                                    pir_confirmed=True, ts=ts,
                                    detail={"message": "PIR déclenché sans personne visible à la caméra"}))
        return alerts

    def _pir_recent_unlocked(self, ts):
        if self._pir_state:
            return True
        return self._pir_last_high is not None and ts - self._pir_last_high <= self.cfg["pir_window_s"]

    def _blind_spot_unlocked(self, ts):
        if not self._pir_state or self._pir_rose_at is None:
            return False
        if ts - self._pir_rose_at < self.cfg["blind_spot_delay_s"]:
            return False
        # Une personne vue depuis la montée du PIR explique le déclenchement
        return self._last_person_seen is None or self._last_person_seen < self._pir_rose_at

    def _should_send(self, key, ts):
        last = self._sent.get(key)
        if last is not None and ts - last < self.cfg["cooldown_s"]:
            return False
        self._sent[key] = ts
        return True
