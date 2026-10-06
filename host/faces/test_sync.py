"""Tests de la synchronisation galerie <-> dashboard (faux client, faux moteur) : python -m pytest host/faces"""
import cv2
import numpy as np

from gallery import Gallery
from sync import sync_once

JPEG = cv2.imencode(".jpg", np.full((64, 64, 3), 128, dtype=np.uint8))[1].tobytes()


def unit(seed):
    v = np.random.default_rng(seed).normal(size=128).astype(np.float32)
    return v / np.linalg.norm(v)


class FakeEngine:
    def detect(self, image):
        return [np.zeros(15)]

    def embed(self, image, face):
        return unit(int(image.mean()))


class FakeClient:
    def __init__(self, images):
        self._images = images
        self.downloads = []

    def images(self):
        return self._images

    def image_file(self, id_image):
        self.downloads.append(id_image)
        return JPEG


def img(id_image, uid, nom, active=True, actif=True):
    return {"id_image_reference": id_image, "id_utilisateur": uid, "utilisateur_nom": nom,
            "utilisateur_role": "ADMIN", "active": active, "utilisateur_actif": actif}


def test_adds_new_active_images_only_once(tmp_path):
    g = Gallery(tmp_path / "g.npz")
    client = FakeClient([img(10, 1, "Alice"), img(11, 1, "Alice"), img(12, 2, "Bob", active=False)])
    assert sync_once(g, FakeEngine(), client) == (2, 0, 0)
    assert sorted(g.image_ids) == [10, 11] and client.downloads == [10, 11]
    assert sync_once(g, FakeEngine(), client) == (0, 0, 0)          # rien de nouveau : aucun téléchargement
    assert client.downloads == [10, 11]


def test_deactivated_image_or_account_is_removed(tmp_path):
    g = Gallery(tmp_path / "g.npz")
    sync_once(g, FakeEngine(), FakeClient([img(10, 1, "Alice"), img(20, 2, "Bob")]))
    assert sync_once(g, FakeEngine(), FakeClient([img(10, 1, "Alice", active=False), img(20, 2, "Bob")])) == (0, 1, 0)
    assert g.image_ids == [20]
    assert sync_once(g, FakeEngine(), FakeClient([img(20, 2, "Bob", actif=False)])) == (0, 1, 0)
    assert len(g) == 0


def test_image_without_face_is_skipped(tmp_path):
    class NoFace(FakeEngine):
        def detect(self, image):
            return []
    g = Gallery(tmp_path / "g.npz")
    assert sync_once(g, NoFace(), FakeClient([img(30, 3, "Chloé")])) == (0, 0, 1)
    assert len(g) == 0


def test_locally_enrolled_embeddings_survive_sync(tmp_path):
    g = Gallery(tmp_path / "g.npz")
    g.add(1, "Alice", "ADMIN", unit(1), image_id=10)
    sync_once(g, FakeEngine(), FakeClient([img(10, 1, "Alice")]))
    assert g.image_ids == [10] and len(g) == 1                       # pas de doublon
