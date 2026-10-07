"""Rehaussement de l'image en faible luminosité, AVANT l'IA (détection, visages) et la vidéo.

But : une image STABLE même quand la lumière change, pour que la détection et surtout la
reconnaissance faciale (verrou du PC hôte) ne vacillent pas. Deux mécanismes contre l'instabilité :

- **Hystérésis** : on entre en rehaussement sous `dark_threshold`, on n'en sort qu'au-dessus de
  `exit_threshold` (> dark_threshold). Entre les deux, on garde l'état courant : plus de bascule
  tout-ou-rien quand la luminosité oscille autour d'un seuil.
- **Lissage temporel** : la luminosité mesurée ET le gamma appliqué sont lissés (moyenne glissante
  exponentielle). Une image bruitée isolée ne fait pas basculer l'état, et la correction ne « pompe »
  pas d'une image à l'autre quand l'éclairage bouge.

Quand c'est actif : CLAHE sur la luminance (espace LAB, éclaircit localement sans brûler les zones
claires) + correction gamma lissée vers `target_mean`. Quand l'image est bien exposée : renvoyée
telle quelle (coût quasi nul). La webcam est déjà en exposition automatique : ceci s'ajoute par-dessus
pour lisser ce que l'auto-exposition laisse passer. ~3 ms en 640x480 sur le CPU quand actif.
"""
import cv2
import numpy as np


class LowLightEnhancer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.clahe = cv2.createCLAHE(clipLimit=cfg["clahe_clip"], tileGridSize=(8, 8))
        self.dark_on = cfg["dark_threshold"]                          # entre en action sous ce seuil
        self.dark_off = cfg.get("exit_threshold", self.dark_on + 20)  # sort au-dessus (hystérésis)
        self.target = cfg["target_mean"]
        self.alpha_lum = cfg.get("lum_smooth", 0.3)                   # lissage de la luminosité mesurée
        self.alpha_gamma = cfg.get("gamma_smooth", 0.25)             # lissage du gamma appliqué
        self.gamma_min = cfg.get("gamma_min", 0.35)
        self.gamma_max = cfg.get("gamma_max", 3.0)
        self.last_mean = None     # luminosité lissée 0..255 (affichée au HUD)
        self.active = False       # rehaussement en cours (avec hystérésis)
        self._gamma = None        # gamma lissé courant

    def __call__(self, frame):
        if not self.cfg.get("enabled", True):
            return frame
        # Luminosité sur une miniature (quasi gratuit), lissée dans le temps : une image sombre
        # isolée (ex. quelqu'un passe devant la caméra) ne déclenche pas une bascule.
        thumb = cv2.resize(frame, (80, 60), interpolation=cv2.INTER_AREA)
        m = float(cv2.cvtColor(thumb, cv2.COLOR_BGR2GRAY).mean())
        self.last_mean = m if self.last_mean is None else self.alpha_lum * m + (1 - self.alpha_lum) * self.last_mean

        # Hystérésis : on n'allume pas / n'éteint pas au même seuil -> pas de clignotement au ras du seuil
        if self.active and self.last_mean > self.dark_off:
            self.active = False
            self._gamma = None
        elif not self.active and self.last_mean < self.dark_on:
            self.active = True
        if not self.active:
            return frame

        lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
        l_chan = self.clahe.apply(lab[:, :, 0])
        mean = max(float(l_chan.mean()), 1.0)
        # gamma qui ramène la luminance moyenne vers target_mean, borné puis LISSÉ dans le temps
        gamma = float(np.clip(np.log(self.target / 255.0) / np.log(mean / 255.0), self.gamma_min, self.gamma_max))
        self._gamma = gamma if self._gamma is None else self.alpha_gamma * gamma + (1 - self.alpha_gamma) * self._gamma
        lut = np.clip(((np.arange(256) / 255.0) ** self._gamma) * 255.0, 0, 255).astype(np.uint8)
        lab[:, :, 0] = cv2.LUT(l_chan, lut)
        return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
