"""Caractéristiques de la webcam : résolutions et cadences réellement obtenues, réglages disponibles.

Usage (caméra libre : vision arrêtée) : python camera_info.py [--cam 1]
"""
import argparse
import time

import cv2

PROPS = {
    "luminosité": cv2.CAP_PROP_BRIGHTNESS, "contraste": cv2.CAP_PROP_CONTRAST,
    "saturation": cv2.CAP_PROP_SATURATION, "netteté": cv2.CAP_PROP_SHARPNESS,
    "gamma": cv2.CAP_PROP_GAMMA, "gain": cv2.CAP_PROP_GAIN,
    "exposition": cv2.CAP_PROP_EXPOSURE, "exposition auto": cv2.CAP_PROP_AUTO_EXPOSURE,
    "balance blancs": cv2.CAP_PROP_WB_TEMPERATURE, "balance auto": cv2.CAP_PROP_AUTO_WB,
    "contre-jour": cv2.CAP_PROP_BACKLIGHT, "mise au point": cv2.CAP_PROP_FOCUS,
}
MODES = [(640, 480), (1280, 720), (1920, 1080)]


def measure(cap, n=30):
    for _ in range(5):
        cap.read()
    t0, ok_frames, luma = time.perf_counter(), 0, 0.0
    for _ in range(n):
        ok, f = cap.read()
        if ok:
            ok_frames += 1
            luma += cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).mean()
    dt = time.perf_counter() - t0
    return ok_frames / dt if dt else 0, luma / max(ok_frames, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", type=int, default=1)
    args = ap.parse_args()
    for fourcc in ("YUY2", "MJPG"):
        for w, h in MODES:
            cap = cv2.VideoCapture(args.cam, cv2.CAP_DSHOW)
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
            cap.set(cv2.CAP_PROP_FPS, 30)
            got = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
            fps, luma = measure(cap)
            print(f"{fourcc} demandé {w}x{h} -> obtenu {got[0]}x{got[1]} | {fps:4.1f} img/s | luminosité moyenne {luma:5.1f}/255")
            if fourcc == "MJPG" and (w, h) == MODES[-1]:
                print("\nRéglages exposés par le pilote (-1 ou 0 = non géré ou inconnu) :")
                for name, prop in PROPS.items():
                    print(f"  {name:16} {cap.get(prop)}")
            cap.release()


if __name__ == "__main__":
    main()
