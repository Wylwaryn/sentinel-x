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
