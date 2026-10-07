"""Rehaussement faible lumière : python -m pytest host/vision"""
import numpy as np

from enhance import LowLightEnhancer

CFG = {"enabled": True, "dark_threshold": 100, "clahe_clip": 2.0, "target_mean": 115}


def test_dark_image_is_brightened():
    rng = np.random.default_rng(0)
    dark = rng.integers(5, 40, size=(480, 640, 3), dtype=np.uint8)
    e = LowLightEnhancer(CFG)
    out = e(dark)
    assert e.active and out.mean() > dark.mean() * 2


def test_bright_image_untouched():
    bright = np.full((480, 640, 3), 170, dtype=np.uint8)
    e = LowLightEnhancer(CFG)
    assert e(bright) is bright and not e.active


def test_disabled():
    dark = np.zeros((10, 10, 3), dtype=np.uint8)
    assert LowLightEnhancer(dict(CFG, enabled=False))(dark) is dark


def uni(v):
    return np.full((60, 80, 3), v, dtype=np.uint8)


def test_hysteresis_no_toggle_at_threshold():
    # lum_smooth=1 pour isoler l'hystérésis (pas de lissage temporel ici)
    e = LowLightEnhancer(dict(CFG, exit_threshold=120, lum_smooth=1.0))
    e(uni(90))                      # sous dark_threshold -> actif
    assert e.active
    e(uni(110))                     # dans la bande [100, 120] -> RESTE actif (pas de clignotement)
    assert e.active
    e(uni(130))                     # au-dessus d'exit_threshold -> inactif
    assert not e.active
    e(uni(110))                     # de nouveau dans la bande -> RESTE inactif
    assert not e.active


def test_single_dark_frame_does_not_flip():
    # Lissage : une seule image sombre isolée (quelqu'un passe devant) ne déclenche pas le mode nuit
    e = LowLightEnhancer(CFG)
    e(uni(150))                     # bien éclairé -> inactif
    assert not e.active
    e(uni(10))                      # une image très sombre isolée
    assert not e.active             # la luminosité lissée reste au-dessus du seuil
