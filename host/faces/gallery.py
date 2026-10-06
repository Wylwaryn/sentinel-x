"""Galerie locale des personnes autorisées : plusieurs empreintes par personne (angles, lumière).

Fichier data/gallery.npz (exclu de Git) : ids, noms, rôles, empreintes. Écriture atomique.
La vision recharge le fichier automatiquement quand il change (enrôlement pendant qu'elle tourne).
"""
import os
from pathlib import Path

import numpy as np

# SFace : seuil cosinus recommandé 0,363. On prend un peu plus strict : mieux vaut un « inconnu »
# de trop (alerte normale) qu'un intrus pris pour un membre de l'équipe.
DEFAULT_THRESHOLD = 0.40


class Gallery:
    def __init__(self, path):
        self.path = Path(path)
        self.ids, self.names, self.roles = [], [], []
        self.vectors = np.zeros((0, 128), dtype=np.float32)
        self._mtime = None
        self.reload()

    def __len__(self):
        return len(self.ids)

    def people(self):
        """{id: (nom, rôle, nombre d'empreintes)}"""
        out = {}
        for i, n, r in zip(self.ids, self.names, self.roles):
            out[i] = (n, r, out.get(i, (n, r, 0))[2] + 1)
        return out

    def reload(self):
        if not self.path.exists():
            self.ids, self.names, self.roles = [], [], []
            self.vectors = np.zeros((0, 128), dtype=np.float32)
            self._mtime = None
            return
        with np.load(self.path, allow_pickle=False) as d:
            self.ids = [int(v) for v in d["ids"]]
            self.names = [str(v) for v in d["names"]]
            self.roles = [str(v) for v in d["roles"]]
            self.vectors = d["vectors"].astype(np.float32).reshape(-1, 128)
        self._mtime = self.path.stat().st_mtime

    def reload_if_changed(self):
        mtime = self.path.stat().st_mtime if self.path.exists() else None
        if mtime != self._mtime:
            self.reload()
            return True
        return False

    def add(self, person_id, name, role, vector, save=True):
        self.ids.append(int(person_id))
        self.names.append(name)
        self.roles.append(role)
        self.vectors = np.vstack([self.vectors, np.asarray(vector, dtype=np.float32).reshape(1, 128)])
        if save:
            self.save()

    def remove(self, person_id, save=True):
        keep = [k for k, i in enumerate(self.ids) if i != int(person_id)]
        self.ids = [self.ids[k] for k in keep]
        self.names = [self.names[k] for k in keep]
        self.roles = [self.roles[k] for k in keep]
        self.vectors = self.vectors[keep] if keep else np.zeros((0, 128), dtype=np.float32)
        if save:
            self.save()

    def clear(self):
        self.ids, self.names, self.roles = [], [], []
        self.vectors = np.zeros((0, 128), dtype=np.float32)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.stem + ".tmp.npz")
        np.savez(tmp, ids=np.array(self.ids, dtype=np.int64), names=np.array(self.names, dtype=str),
                 roles=np.array(self.roles, dtype=str), vectors=self.vectors.astype(np.float32))
        os.replace(tmp, self.path)
        self._mtime = self.path.stat().st_mtime

    def match(self, vector, threshold=DEFAULT_THRESHOLD):
        """(id, nom, rôle, score) de la personne la plus proche si score >= seuil, sinon (None, None, None, score)."""
        if len(self.ids) == 0:
            return None, None, None, 0.0
        scores = self.vectors @ np.asarray(vector, dtype=np.float32).reshape(128)
        best = int(np.argmax(scores))
        score = float(scores[best])
        if score < threshold:
            return None, None, None, score
        return self.ids[best], self.names[best], self.roles[best], score
