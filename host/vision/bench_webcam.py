"""Benchmark YOLO sur la webcam USB : vérifie CUDA et mesure la latence par trame.

Usage : python host/vision/bench_webcam.py [--model yolov8n.pt] [--cam 0] [--imgsz 640]
Touche 'q' pour quitter.
"""
import argparse
import time

import cv2
import torch
from ultralytics import YOLO


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="yolov8n.pt")
    parser.add_argument("--cam", type=int, default=0)
    parser.add_argument("--imgsz", type=int, default=640)
    args = parser.parse_args()

    print(f"torch {torch.__version__} | CUDA dispo : {torch.cuda.is_available()}")
    if torch.cuda.is_available():
        print(f"GPU : {torch.cuda.get_device_name(0)}")
    device = 0 if torch.cuda.is_available() else "cpu"

    model = YOLO(args.model)

    # CAP_DSHOW : ouverture plus rapide et fiable des webcams USB sous Windows
    cap = cv2.VideoCapture(args.cam, cv2.CAP_DSHOW)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    if not cap.isOpened():
        raise SystemExit(f"Impossible d'ouvrir la webcam {args.cam}")

    latencies = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame = cv2.resize(frame, (640, 480))

        t0 = time.perf_counter()
        # classes=[0] : on ne garde que les personnes
        result = model.predict(frame, imgsz=args.imgsz, device=device, classes=[0], verbose=False)[0]
        dt = (time.perf_counter() - t0) * 1000
        latencies.append(dt)

        annotated = result.plot()
        cv2.putText(annotated, f"{dt:.1f} ms | personnes : {len(result.boxes)}", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
        cv2.imshow("Sentinel-X vision (q pour quitter)", annotated)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    cap.release()
    cv2.destroyAllWindows()

    # On ignore les 10 premières trames (chauffe du GPU)
    steady = sorted(latencies[10:]) or sorted(latencies)
    if steady:
        print(f"Trames : {len(steady)} | moyenne {sum(steady) / len(steady):.1f} ms | "
              f"p95 {steady[int(len(steady) * 0.95) - 1]:.1f} ms")


if __name__ == "__main__":
    main()
