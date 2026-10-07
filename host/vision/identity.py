"""Identification des personnes suivies (visage) et tri de leurs alertes.

Pour chaque personne suivie (identifiant ByteTrack) :
- on cherche son visage dans le haut de son cadre, au plus toutes les retry_s secondes ;
- il faut confirm_hits correspondances avec la MÊME personne de la galerie pour la déclarer reconnue
  (une seule image floue ne suffit pas) ; une personne reconnue est re-vérifiée toutes les reverify_s.

Tri des alertes (EventGate) :
- pendant grace_s après l'apparition d'une personne, ses alertes sont RETENUES le temps de l'identifier ;
- membre reconnu : ses alertes d'intrusion / rôdeur / approche sont supprimées, une seule information
  « Personne autorisée : <nom> » est émise (traçabilité) ;
- visage inconnu, ou non vu à la fin du délai : les alertes partent normalement, marquées « non identifiée ».
  Par sécurité, on ne laisse JAMAIS passer quelqu'un qu'on n'a pas reconnu.
Limite connue : une photo d'un membre présentée à la caméra pourrait tromper la reconnaissance
(pas de détection de vivacité) ; le PIR et les alertes « non identifiée » restent la défense principale.
"""
from dataclasses import dataclass, field

import numpy as np

from behaviour import VisionEvent

AUTHORIZED, UNKNOWN, PENDING = "autorisee", "inconnue", "en_cours"


@dataclass
class _TrackId:
    first_seen: float
    last_try: float = -1e9
    identity: tuple | None = None   # (id, nom, rôle, score)
    verified_at: float = -1e9
    hits: dict = field(default_factory=dict)
    face_seen: bool = False
    last_score: float | None = None   # dernière similarité avec la galerie (réglage du seuil)


class IdentityTracker:
    def __init__(self, cfg, engine, gallery):
        self.cfg = cfg
        self.engine = engine
        self.gallery = gallery
        self.tracks: dict[int, _TrackId] = {}

    def update(self, frame, persons, ts):
        self.gallery.reload_if_changed()  # enrôlement possible pendant que la vision tourne
        seen = set()
        h, w = frame.shape[:2]
        for p in persons:
            tid = p["track_id"]
            seen.add(tid)
            st = self.tracks.setdefault(tid, _TrackId(first_seen=ts))
            due = st.identity is None or ts - st.verified_at >= self.cfg["reverify_s"]
            if not due or ts - st.last_try < self.cfg["retry_s"] or not len(self.gallery):
                continue
            st.last_try = ts
            x1, y1, x2, y2 = (int(v) for v in (p["box"][0] * w, p["box"][1] * h, p["box"][2] * w, p["box"][3] * h))
            # Le visage est dans le haut du corps : on ne cherche que dans les 60 % supérieurs du cadre
            crop = frame[max(0, y1):max(0, y1) + max(1, int((y2 - y1) * 0.6)), max(0, x1):max(0, x2)]
            faces = self.engine.detect(crop)
            if not faces or faces[0][2] < self.cfg["min_face_px"]:
                continue
            st.face_seen = True
            pid, name, role, score = self.gallery.match(self.engine.embed(crop, faces[0]), self.cfg["threshold"])
            st.last_score = score
            if pid is None:
                st.hits.clear()
                if st.identity is not None:   # n'est plus reconnu à la re-vérification
                    st.identity = None
                continue
            st.hits = {pid: st.hits.get(pid, 0) + 1}
            if st.hits[pid] >= self.cfg["confirm_hits"]:
                st.identity = (pid, name, role, round(score, 3))
                st.verified_at = ts
        for tid in [t for t in self.tracks if t not in seen and ts - self.tracks[t].last_try > 30]:
            del self.tracks[tid]

    def status(self, tid, ts):
        st = self.tracks.get(tid)
        if st is None:
            return PENDING
        if st.identity is not None:
            return AUTHORIZED
        if ts - st.first_seen < self.cfg["grace_s"]:
            return PENDING
        return UNKNOWN

    def identity(self, tid):
        st = self.tracks.get(tid)
        return st.identity if st else None

    def recognized(self, tid, ts, max_age):
        """Identité d'une piste SEULEMENT si elle a été reconfirmée depuis moins de max_age.
        Sert au battement de présence (verrou) : quelqu'un qui prend la place d'un membre n'hérite
        pas de son statut, car l'étiquette expire faute de reconfirmation du visage."""
        st = self.tracks.get(tid)
        if st and st.identity is not None and ts - st.verified_at <= max_age:
            return st.identity
        return None


class EventGate:
    """Retient les alertes des personnes en cours d'identification, puis les transforme ou les libère."""

    def __init__(self, tracker):
        self.tracker = tracker
        self.held: dict[int, list] = {}
        self.announced: set[int] = set()

    def process(self, events, ts):
        for ev in events:
            self.held.setdefault(ev.track_id, []).append(ev)
        out = []
        for tid in list(self.held):
            status = self.tracker.status(tid, ts)
            if status == PENDING:
                continue
            evs = self.held.pop(tid)
            if status == AUTHORIZED:
                if tid not in self.announced:
                    self.announced.add(tid)
                    pid, name, role, score = self.tracker.identity(tid)
                    out.append(VisionEvent(type="presence", level="info", track_id=tid,
                                           zone=evs[0].zone, ts=ts,
                                           detail={"personne": AUTHORIZED, "id_personne": pid,
                                                   "conf": score,
                                                   "message": f"Personne autorisée : {name} ({role})"}))
                # intrusion / rôdeur / approche d'un membre reconnu : supprimées
            else:
                for ev in evs:
                    ev.detail["personne"] = UNKNOWN
                    out.append(ev)
        return out


def label(tracker, tid, ts):
    status = tracker.status(tid, ts)
    if status == AUTHORIZED:
        return tracker.identity(tid)[1]
    if status == PENDING:
        return "?"
    st = tracker.tracks.get(tid)
    if st is None or not st.face_seen:
        return "inconnu (visage non vu)"
    # Score affiché pour régler le seuil : proche du seuil = même personne mal éclairée / autre caméra
    return f"inconnu ({st.last_score:.2f})" if st.last_score is not None else "inconnu"


def unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / max(float(np.linalg.norm(v)), 1e-9)
