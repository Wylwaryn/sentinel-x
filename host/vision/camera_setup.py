"""Procédure de réglage de la webcam : désactive l'exposition automatique, règle
l'exposition à la main et vérifie que l'image est stable (aucun faux mouvement).

Usage : python host/vision/camera_setup.py [--config host/vision/config.json] [--list]

Touches :
  + / -   exposition plus longue / plus courte
  a       bascule exposition auto / manuelle
  w       bascule balance des blancs auto / manuelle
  d       ouvre la fenêtre de réglages du pilote (anti-scintillement 50 Hz, etc.)
  t       test de stabilité 10 s (ne pas bouger devant la caméra)
  s       enregistre les réglages dans config.json
  q       quitter sans enregistrer
"""
import argparse
import json
import time
from pathlib import Path

import cv2

from camera import apply_image_settings, open_camera, open_driver_dialog


def list_cameras(max_index=5):
    for i in range(max_index):
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
        if cap.isOpened():
            ok, frame = cap.read()
            shape = frame.shape if ok else "pas d'image"
            print(f"Caméra {i} : ouverte, {shape}")
            if ok:
                cv2.imshow(f"Camera {i} (touche pour continuer)", frame)
                cv2.waitKey(0)
                cv2.destroyAllWindows()
        cap.release()


def stability_test(cap, seconds=10):
    """Mesure le % d'image vu comme 'mouvement' par MOG2 sur une scène immobile.
    Résultat attendu : < 0.5 % en moyenne. Au-delà : exposition/éclairage instable."""
    mog = cv2.createBackgroundSubtractorMOG2(history=100, varThreshold=25, detectShadows=False)
    ratios, brightness = [], []
    start = time.time()
    while time.time() - start < seconds:
        ok, frame = cap.read()
        if not ok:
            continue
        mask = mog.apply(frame)
        if time.time() - start > 2:  # laisse MOG2 apprendre le fond
            ratios.append((mask > 0).mean() * 100)
            brightness.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean())
        cv2.imshow("Test stabilite (ne bougez pas)", mask)
        cv2.waitKey(1)
    cv2.destroyWindow("Test stabilite (ne bougez pas)")
    mean_ratio = sum(ratios) / len(ratios)
    swing = max(brightness) - min(brightness)
    verdict = "OK" if mean_ratio < 0.5 and swing < 8 else "INSTABLE"
    print(f"[Stabilité] faux mouvement moyen {mean_ratio:.2f} % | "
          f"variation de luminosité {swing:.1f} niveaux -> {verdict}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    parser.add_argument("--list", action="store_true", help="lister les caméras et quitter")
    args = parser.parse_args()

    if args.list:
        list_cameras()
        return

    config_path = Path(args.config)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    cam_cfg = config["camera"]
    cap = open_camera(cam_cfg)
    print("Réglages appliqués :", apply_image_settings(cap, cam_cfg))

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        luma = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean()
        mode = "AUTO" if cam_cfg["auto_exposure"] else f"MANUEL {cam_cfg['exposure']}"
        wb = "AUTO" if cam_cfg["auto_white_balance"] else f"{cam_cfg['white_balance']}K"
        hud = f"Expo {mode} | WB {wb} | luminosite {luma:.0f}/255"
        cv2.putText(frame, hud, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        cv2.putText(frame, "+/- a w d t s q", (10, 470), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
        cv2.imshow("Sentinel-X - reglage camera", frame)

        key = cv2.waitKey(1) & 0xFF
        changed = True
        if key in (ord("+"), ord("=")):
            cam_cfg["auto_exposure"] = False
            cam_cfg["exposure"] = min(cam_cfg["exposure"] + 1, -1)
        elif key == ord("-"):
            cam_cfg["auto_exposure"] = False
            cam_cfg["exposure"] = max(cam_cfg["exposure"] - 1, -13)
        elif key == ord("a"):
            cam_cfg["auto_exposure"] = not cam_cfg["auto_exposure"]
        elif key == ord("w"):
            cam_cfg["auto_white_balance"] = not cam_cfg["auto_white_balance"]
        else:
            changed = False

        if changed:
            print("Réglages appliqués :", apply_image_settings(cap, cam_cfg))
        elif key == ord("d"):
            open_driver_dialog(cap)
        elif key == ord("t"):
            stability_test(cap)
        elif key == ord("s"):
            config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"Enregistré dans {config_path}")
        elif key == ord("q"):
            break

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
