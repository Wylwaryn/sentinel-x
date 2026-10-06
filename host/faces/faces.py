"""Moteur de reconnaissance faciale : YuNet (détection) + SFace (empreinte 128 valeurs), modèles officiels
OpenCV (opencv_zoo), exécutés par OpenCV seul. Les modèles sont dans models/ (téléchargés, exclus de Git).

Données biométriques : seules les EMPREINTES (vecteurs) sont gardées sur le PC hôte, jamais les photos.
Une empreinte ne permet pas de reconstruire le visage. Les photos restent dans le dashboard (ADMIN).
"""
from pathlib import Path

import cv2
import numpy as np

MODELS = Path(__file__).resolve().parent / "models"
DETECTOR = MODELS / "face_detection_yunet_2023mar.onnx"
RECOGNIZER = MODELS / "face_recognition_sface_2021dec.onnx"
MODEL_URL = "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"


class FaceEngine:
    def __init__(self, score_threshold=0.85):
        for m in (DETECTOR, RECOGNIZER):
            if not m.exists():
                raise FileNotFoundError(f"Modèle absent : {m}. Le télécharger depuis {MODEL_URL}")
        self.detector = cv2.FaceDetectorYN.create(str(DETECTOR), "", (320, 320), score_threshold, 0.3, 50)
        self.recognizer = cv2.FaceRecognizerSF.create(str(RECOGNIZER), "")

    def detect(self, image):
        """Visages trouvés, du plus grand au plus petit. Chaque visage : [x, y, w, h, 5 points (10 valeurs), score]."""
        if image is None or image.size == 0:
            return []
        h, w = image.shape[:2]
        self.detector.setInputSize((w, h))
        _, faces = self.detector.detect(image)
        if faces is None:
            return []
        return sorted(faces, key=lambda f: f[2] * f[3], reverse=True)

    def embed(self, image, face):
        """Empreinte normalisée (norme 1) : la similarité cosinus devient un simple produit scalaire."""
        feat = self.recognizer.feature(self.recognizer.alignCrop(image, face)).flatten().astype(np.float32)
        return feat / max(float(np.linalg.norm(feat)), 1e-9)


def face_crop(image, face, margin=0.4, max_side=320):
    """Portrait autour du visage (pour l'enregistrement dans le dashboard), en JPEG."""
    x, y, w, h = (int(v) for v in face[:4])
    mx, my = int(w * margin), int(h * margin)
    H, W = image.shape[:2]
    crop = image[max(0, y - my):min(H, y + h + my), max(0, x - mx):min(W, x + w + mx)]
    scale = max_side / max(crop.shape[:2])
    if scale < 1:
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return buf.tobytes() if ok else None
