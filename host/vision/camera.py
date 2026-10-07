"""Ouverture de la webcam USB et verrouillage de l'exposition / balance des blancs.

Sous Windows (backend DirectShow) :
- CAP_PROP_AUTO_EXPOSURE : 0.25 = manuel, 0.75 = auto (convention héritée de V4L2,
  certains pilotes attendent 0 / 1 : on essaie les deux et on relit la valeur).
- CAP_PROP_EXPOSURE : puissance de 2 en secondes. -6 = 1/64 s (~15.6 ms), -5 = 1/32 s.
  À 30 FPS, ne pas dépasser -5 sinon la cadence chute.
"""
import cv2

AUTO_EXPOSURE_MANUAL = (0.25, 0)
AUTO_EXPOSURE_AUTO = (0.75, 1)


def resolve_index(cam_cfg):
    """Index de la webcam. Si `name` est renseigné, on cherche la caméra dont le nom DirectShow
    contient ce texte (l'ordre DirectShow == l'ordre des index OpenCV CAP_DSHOW) : plus robuste que
    l'index, qui change selon l'ordre de branchement (la webcam USB peut être 0 ou 1 selon le boot).
    Repli sur `index` si `name` est absent, introuvable, ou si pygrabber n'est pas là."""
    name = cam_cfg.get("name")
    if not name:
        return cam_cfg["index"]
    try:
        from pygrabber.dshow_graph import FilterGraph
        devices = FilterGraph().get_input_devices()
    except Exception as exc:
        print(f"[CAMÉRA] énumération par nom indisponible ({exc}), repli sur l'index {cam_cfg['index']}")
        return cam_cfg["index"]
    for i, dev in enumerate(devices):
        if name.lower() in dev.lower():
            print(f"[CAMÉRA] « {dev} » trouvée à l'index {i} (nom « {name} »)")
            return i
    print(f"[CAMÉRA] aucune caméra nommée « {name} » (vues : {devices}), repli sur l'index {cam_cfg['index']}")
    return cam_cfg["index"]


def open_camera(cam_cfg):
    index = resolve_index(cam_cfg)
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        raise RuntimeError(f"Impossible d'ouvrir la webcam (index {index}, nom « {cam_cfg.get('name') or '-'} »)")
    if cam_cfg.get("fourcc"):
        # MJPG : la webcam tient 1280x720 et 1920x1080 à 20 img/s (mesuré avec camera_info.py)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*cam_cfg["fourcc"]))
    # Capture plus grande que l'analyse : la réduction à 640x480 (INTER_AREA) moyenne le bruit du capteur
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, cam_cfg.get("capture_width", cam_cfg["width"]))
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cam_cfg.get("capture_height", cam_cfg["height"]))
    cap.set(cv2.CAP_PROP_FPS, cam_cfg["fps"])
    apply_image_settings(cap, cam_cfg)
    return cap


def apply_image_settings(cap, cam_cfg):
    """Applique exposition / balance des blancs / gain. Retourne un rapport lisible."""
    report = {}

    if cam_cfg.get("auto_exposure", True):
        _set_first_accepted(cap, cv2.CAP_PROP_AUTO_EXPOSURE, AUTO_EXPOSURE_AUTO)
    else:
        _set_first_accepted(cap, cv2.CAP_PROP_AUTO_EXPOSURE, AUTO_EXPOSURE_MANUAL)
        cap.set(cv2.CAP_PROP_EXPOSURE, cam_cfg["exposure"])
    report["auto_exposure"] = cap.get(cv2.CAP_PROP_AUTO_EXPOSURE)
    report["exposure"] = cap.get(cv2.CAP_PROP_EXPOSURE)

    if cam_cfg.get("auto_white_balance", True):
        cap.set(cv2.CAP_PROP_AUTO_WB, 1)
    else:
        cap.set(cv2.CAP_PROP_AUTO_WB, 0)
        cap.set(cv2.CAP_PROP_WB_TEMPERATURE, cam_cfg["white_balance"])
    report["auto_wb"] = cap.get(cv2.CAP_PROP_AUTO_WB)
    report["white_balance"] = cap.get(cv2.CAP_PROP_WB_TEMPERATURE)

    if cam_cfg.get("gain") is not None:
        cap.set(cv2.CAP_PROP_GAIN, cam_cfg["gain"])
    report["gain"] = cap.get(cv2.CAP_PROP_GAIN)
    return report


def open_driver_dialog(cap):
    """Ouvre la fenêtre de réglages du pilote (DirectShow). Les réglages y sont
    souvent persistants côté pilote : utile si le pilote ignore les appels OpenCV."""
    cap.set(cv2.CAP_PROP_SETTINGS, 1)


def _set_first_accepted(cap, prop, candidates):
    for value in candidates:
        if cap.set(prop, value) and abs(cap.get(prop) - value) < 1e-3:
            return True
    return False
