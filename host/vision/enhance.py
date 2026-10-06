"""Rehaussement de l'image en faible luminosité, AVANT l'IA (détection, visages) et la vidéo.

Ne s'active que si l'image est sombre (luminosité moyenne < dark_threshold) :
- CLAHE sur la luminance (espace LAB) : éclaircit les zones sombres localement (visage à contre-jour)
  sans brûler les zones claires, et sans toucher aux couleurs ;
- correction gamma pour ramener la luminosité moyenne vers target_mean.
~2 ms en 640x480 sur le CPU.
"""
import cv2
import numpy as np


class LowLightEnhancer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.clahe = cv2.createCLAHE(clipLimit=cfg["clahe_clip"], tileGridSize=(8, 8))
        self.last_mean = None
        self.active = False

    def __call__(self, frame):
        if not self.cfg.get("enabled", True):
            return frame
        # Luminosité estimée sur une miniature (quasi gratuit) : la conversion LAB n'a lieu que si c'est sombre
        thumb = cv2.resize(frame, (80, 60), interpolation=cv2.INTER_AREA)
        self.last_mean = float(cv2.cvtColor(thumb, cv2.COLOR_BGR2GRAY).mean())
        self.active = self.last_mean < self.cfg["dark_threshold"]
        if not self.active:
            return frame
        lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
        l_chan = lab[:, :, 0]
        l_chan = self.clahe.apply(l_chan)
        mean = max(float(l_chan.mean()), 1.0)
        if mean < self.cfg["target_mean"]:
            # gamma < 1 éclaircit : on vise target_mean pour la luminance moyenne
            gamma = np.log(self.cfg["target_mean"] / 255.0) / np.log(mean / 255.0)
            lut = np.clip(((np.arange(256) / 255.0) ** gamma) * 255.0, 0, 255).astype(np.uint8)
            l_chan = cv2.LUT(l_chan, lut)
        lab[:, :, 0] = l_chan
        return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
