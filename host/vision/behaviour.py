"""Analyse comportementale : zones polygonales + suivi des personnes (IDs ByteTrack).

Entrée : liste de personnes {track_id, box (x1,y1,x2,y2 normalisés 0-1)} à l'instant t.
Sortie : événements caméra émis une seule fois par transition d'état.
"""
import math
from collections import deque
from dataclasses import dataclass, field

import cv2
import numpy as np

# Niveaux de base des événements caméra (la fusion PIR peut les élever)
EVENT_LEVELS = {
    "presence": "info",
    "loitering": "warning",
    "fast_approach": "warning",
    "intrusion": "critical",
}


@dataclass
class VisionEvent:
    type: str
    level: str
    track_id: int
    zone: str | None
    ts: float
    detail: dict = field(default_factory=dict)


@dataclass
class _Track:
    last_seen: float
    perimeter_since: float | None = None
    in_restricted: bool = False
    loiter_reported: bool = False
    fast_reported_at: float = -math.inf
    feet: deque = field(default_factory=lambda: deque(maxlen=64))


class Zone:
    def __init__(self, name, kind, polygon):
        self.name = name
        self.kind = kind
        self.polygon = np.array(polygon, dtype=np.float32)

    def contains(self, point):
        return cv2.pointPolygonTest(self.polygon, (float(point[0]), float(point[1])), False) >= 0


class BehaviourAnalyzer:
    def __init__(self, zones_cfg, behaviour_cfg):
        self.zones = [Zone(z["name"], z["kind"], z["polygon"]) for z in zones_cfg]
        self.cfg = behaviour_cfg
        self.tracks: dict[int, _Track] = {}

    def update(self, persons, ts):
        events = []
        for p in persons:
            tid = p["track_id"]
            x1, y1, x2, y2 = p["box"]
            # Les pieds (bas-centre de la boîte) disent où la personne se trouve au sol
            feet = ((x1 + x2) / 2, y2)
            track = self.tracks.setdefault(tid, _Track(last_seen=ts))
            track.last_seen = ts
            track.feet.append((ts, feet))
            new_events = self._zone_events(tid, track, feet, ts) + self._speed_events(tid, track, feet, ts)
            for ev in new_events:
                if p.get("conf") is not None:
                    ev.detail["conf"] = round(float(p["conf"]), 3)
            events.extend(new_events)

        self._forget_stale(ts)
        return events

    def dwell_time(self, track_id, ts):
        track = self.tracks.get(track_id)
        if track is None or track.perimeter_since is None:
            return 0.0
        return ts - track.perimeter_since

    def _zone_events(self, tid, track, feet, ts):
        events = []
        perimeter = next((z for z in self.zones if z.kind == "perimeter" and z.contains(feet)), None)
        restricted = next((z for z in self.zones if z.kind == "restricted" and z.contains(feet)), None)
        in_perimeter = perimeter is not None or restricted is not None

        if in_perimeter and track.perimeter_since is None:
            track.perimeter_since = ts
            zone_name = perimeter.name if perimeter else restricted.name
            events.append(self._event("presence", tid, zone_name, ts))
        elif not in_perimeter:
            track.perimeter_since = None
            track.loiter_reported = False

        if restricted and not track.in_restricted:
            events.append(self._event("intrusion", tid, restricted.name, ts))
        track.in_restricted = restricted is not None

        dwell = self.dwell_time(tid, ts)
        if in_perimeter and not track.loiter_reported and dwell >= self.cfg["loiter_s"]:
            track.loiter_reported = True
            zone_name = restricted.name if restricted else perimeter.name
            events.append(self._event("loitering", tid, zone_name, ts, dwell_s=round(dwell, 1)))
        return events

    def _speed_events(self, tid, track, feet, ts):
        window = self.cfg["speed_window_s"]
        old = next(((t, f) for t, f in track.feet if ts - t <= window), None)
        if old is None or ts - old[0] < window * 0.5:
            return []
        dt = ts - old[0]
        speed = math.dist(old[1], feet) / dt  # en "largeurs d'image" par seconde
        if speed >= self.cfg["fast_speed"] and ts - track.fast_reported_at >= self.cfg["loiter_s"]:
            track.fast_reported_at = ts
            return [self._event("fast_approach", tid, None, ts, speed=round(speed, 2))]
        return []

    def _forget_stale(self, ts):
        timeout = self.cfg["track_timeout_s"]
        for tid in [t for t, tr in self.tracks.items() if ts - tr.last_seen > timeout]:
            del self.tracks[tid]

    @staticmethod
    def _event(kind, tid, zone, ts, **detail):
        return VisionEvent(type=kind, level=EVENT_LEVELS[kind], track_id=tid, zone=zone, ts=ts, detail=detail)
