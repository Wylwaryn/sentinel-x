"""Synchronisation de la galerie locale avec les images de référence du dashboard.

Incrémentale : ne télécharge que les NOUVELLES images actives, retire les empreintes des images
désactivées (ou des comptes désactivés). Utilisée par la vision en tâche de fond (compte de service
SERVICE_VISION, lecture seule des images) et par `enroll.py sync` (compte ADMIN au clavier).
"""
import os
import threading
import time

import cv2
import numpy as np

from dashboard_client import DashboardClient, DashboardError


def sync_once(gallery, engine, client):
    """Retourne (ajoutées, retirées, ignorées sans visage)."""
    gallery.reload_if_changed()
    wanted = {}
    for img in client.images():
        if img.get("active") and img.get("utilisateur_actif", True):
            wanted[int(img["id_image_reference"])] = img
    have = {i for i in gallery.image_ids if i >= 0}
    removed = have - set(wanted)
    if removed:
        gallery.remove_images(removed, save=False)
    added = skipped = 0
    for id_image in sorted(set(wanted) - have):
        img = wanted[id_image]
        data = np.frombuffer(client.image_file(id_image), dtype=np.uint8)
        image = cv2.imdecode(data, cv2.IMREAD_COLOR)
        faces = engine.detect(image) if image is not None else []
        if not faces:
            skipped += 1
            continue
        gallery.add(img["id_utilisateur"], img["utilisateur_nom"], img.get("utilisateur_role", "?"),
                    engine.embed(image, faces[0]), save=False, image_id=id_image)
        added += 1
    if added or removed:
        gallery.save()
    return added, len(removed), skipped


class BackgroundSync:
    """Thread de la vision : se connecte avec le compte de service et synchronise toutes les interval_s."""

    def __init__(self, gallery, engine, cfg):
        self.gallery, self.engine, self.cfg = gallery, engine, cfg
        self.user = os.environ.get(cfg["username_env"])
        self.password = os.environ.get(cfg["password_env"])
        self.last_error = None

    @property
    def configured(self):
        return bool(self.user and self.password)

    def start(self):
        if self.configured:
            threading.Thread(target=self._run, daemon=True).start()
        return self.configured

    def _run(self):
        while True:
            client = DashboardClient(base=self.cfg["dashboard_url"], origin=self.cfg["origin"])
            try:
                client.login(self.user, self.password, allowed_roles=("SERVICE_VISION", "ADMIN"))
                added, removed, skipped = sync_once(self.gallery, self.engine, client)
                if added or removed or skipped:
                    print(f"[VISAGES] synchronisation : +{added} empreinte(s), -{removed}"
                          + (f", {skipped} image(s) sans visage" if skipped else ""))
                self.last_error = None
            except (DashboardError, OSError, ValueError) as exc:
                if str(exc) != self.last_error:
                    print(f"[VISAGES] synchronisation impossible : {exc}")
                self.last_error = str(exc)
            finally:
                client.logout()
            time.sleep(self.cfg["interval_s"])
