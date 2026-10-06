"""Sentinel-X — service de vision : mouvement (MOG2) + YOLO/ByteTrack + zones + fusion PIR.

Usage : python host/vision/sentinel_vision.py [--config host/vision/config.json] [--headless]

Touches (mode fenêtre) :
  p   simule une impulsion PIR (test sans ESP8266)
  q   quitter
"""
import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import torch
from ultralytics import YOLO

from alerts import AlertSender
from behaviour import BehaviourAnalyzer
from camera import open_camera
from enhance import LowLightEnhancer
from fusion import FusionEngine

LEVEL_COLORS = {"info": (255, 200, 0), "warning": (0, 165, 255), "critical": (0, 0, 255)}
ZONE_COLORS = {"perimeter": (0, 165, 255), "restricted": (0, 0, 255)}


class MotionGate:
    """Décide quand faire tourner YOLO : à pleine cadence s'il y a du mouvement, des
    personnes suivies ou un PIR actif ; sinon toutes les idle_interval_s secondes."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.mog = cv2.createBackgroundSubtractorMOG2(
            history=cfg["history"], varThreshold=cfg["var_threshold"], detectShadows=False)
        self.last_activity = -float("inf")
        self.last_run = -float("inf")
        self.ratio = 0.0

    def should_run(self, frame, ts, has_tracks, pir_active):
        small = cv2.resize(frame, (160, 120))
        mask = self.mog.apply(small)
        self.ratio = (mask > 0).mean()
        if self.ratio >= self.cfg["min_area_ratio"] or has_tracks or pir_active:
            self.last_activity = ts
        active = ts - self.last_activity <= self.cfg["idle_after_s"]
        if active or ts - self.last_run >= self.cfg["idle_interval_s"]:
            self.last_run = ts
            return True, active
        return False, active


def extract_persons(result, width, height):
    boxes = result.boxes
    if boxes is None or boxes.id is None:
        return []
    persons = []
    for (x1, y1, x2, y2), tid, conf in zip(boxes.xyxy.tolist(), boxes.id.int().tolist(), boxes.conf.tolist()):
        persons.append({"track_id": tid, "box": (x1 / width, y1 / height, x2 / width, y2 / height), "conf": conf})
    return persons


def draw(frame, zones, persons, analyzer, ts, hud, recent_alerts, names=None):
    h, w = frame.shape[:2]
    for zone in zones:
        pts = (zone.polygon * [w, h]).astype(int)
        cv2.polylines(frame, [pts], True, ZONE_COLORS.get(zone.kind, (200, 200, 200)), 2)
        cv2.putText(frame, zone.name, tuple(pts[0] + [4, 18]), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    ZONE_COLORS.get(zone.kind, (200, 200, 200)), 1)
    for p in persons:
        x1, y1, x2, y2 = (int(p["box"][0] * w), int(p["box"][1] * h), int(p["box"][2] * w), int(p["box"][3] * h))
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
        dwell = analyzer.dwell_time(p["track_id"], ts)
        who = f" {names(p['track_id'])}" if names else ""
        cv2.putText(frame, f"#{p['track_id']}{who} {dwell:.0f}s", (x1, max(y1 - 6, 12)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2)
        cv2.circle(frame, ((x1 + x2) // 2, y2), 4, (0, 255, 0), -1)
    cv2.putText(frame, hud, (10, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)
    for i, (alert, at) in enumerate(recent_alerts):
        if ts - at > 5:
            continue
        txt = f"{alert.level.upper()} {alert.type} #{alert.track_id}" + (" +PIR" if alert.pir_confirmed else "")
        cv2.putText(frame, txt, (10, h - 12 - 24 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.6, LEVEL_COLORS[alert.level], 2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    parser.add_argument("--headless", action="store_true", help="sans fenêtre (service)")
    args = parser.parse_args()

    base_dir = Path(args.config).resolve().parent
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    model_cfg = config["model"]

    device = 0 if torch.cuda.is_available() else "cpu"
    if device == 0:
        # Laisse de la place sur le GPU pour l'IA réseau
        torch.cuda.set_per_process_memory_fraction(model_cfg["gpu_memory_fraction"], 0)
    print(f"Inférence sur : {torch.cuda.get_device_name(0) if device == 0 else 'CPU'}")

    model = YOLO(model_cfg["weights"])
    cap = open_camera(config["camera"])
    gate = MotionGate(config["motion"])
    enhancer = LowLightEnhancer(config.get("enhance", {"enabled": False, "clahe_clip": 2.0}))
    analyzer = BehaviourAnalyzer(config["zones"], config["behaviour"])
    fusion = FusionEngine(config["fusion"])
    sender = AlertSender(config["api"], base_dir, serie=config["device_serie"])

    tracker = event_gate = None
    faces_cfg = config.get("faces", {})
    if faces_cfg.get("enabled"):
        sys.path.insert(0, str((base_dir / faces_cfg["module_dir"]).resolve()))
        from faces import FaceEngine
        from gallery import Gallery
        from identity import EventGate, IdentityTracker, label
        gallery = Gallery((base_dir / faces_cfg["gallery"]).resolve())
        tracker = IdentityTracker(faces_cfg, FaceEngine(), gallery)
        event_gate = EventGate(tracker)
        print(f"Reconnaissance faciale : {len(gallery.people())} personne(s) autorisée(s) dans la galerie")
        if faces_cfg.get("sync", {}).get("enabled"):
            from sync import BackgroundSync
            # Moteur et galerie dédiés : les modèles OpenCV ne se partagent pas entre threads ;
            # la galerie de la vision recharge le fichier quand la synchronisation l'a modifié.
            bg = BackgroundSync(Gallery(gallery.path), FaceEngine(), faces_cfg["sync"])
            print("Synchronisation des visages avec le dashboard : "
                  + ("active" if bg.start() else "inactive (SENTINEL_FACES_USER / SENTINEL_FACES_PASS absents)"))

    link = None
    if config["mqtt"]["enabled"]:
        from mqtt_link import MqttLink
        link = MqttLink(config["mqtt"], fusion, config["device_serie"], base_dir)
        link.start()

    persons, recent_alerts, infer_ms = [], [], 0.0
    sim_pir_until = 0.0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                # Coupure USB ou caméra prise par un autre programme : on ne s'arrête JAMAIS,
                # on rouvre la caméra toutes les 2 s jusqu'à ce qu'elle revienne.
                print("[CAMÉRA] plus d'image : nouvelle tentative dans 2 s")
                cap.release()
                time.sleep(2)
                try:
                    cap = open_camera(config["camera"])
                    print("[CAMÉRA] rouverte")
                except RuntimeError as exc:
                    print(f"[CAMÉRA] {exc}")
                continue
            frame = cv2.resize(frame, (640, 480), interpolation=cv2.INTER_AREA)
            frame = enhancer(frame)  # faible lumière : image rehaussée pour l'IA, les visages et la vidéo
            ts = time.monotonic()

            if sim_pir_until and ts > sim_pir_until:
                fusion.on_pir(False, ts)
                sim_pir_until = 0.0

            run, active = gate.should_run(frame, ts, bool(analyzer.tracks), fusion.pir_recent(ts))
            vision_events = []
            if run:
                t0 = time.perf_counter()
                result = model.track(frame, persist=True, tracker=model_cfg["tracker"], classes=[0],
                                     conf=model_cfg["conf"], imgsz=model_cfg["imgsz"],
                                     device=device, verbose=False)[0]
                infer_ms = (time.perf_counter() - t0) * 1000
                persons = extract_persons(result, 640, 480)
                vision_events = analyzer.update(persons, ts)
                if tracker:
                    tracker.update(frame, persons, ts)
            if event_gate:
                # Alertes retenues le temps d'identifier la personne, puis supprimées (membre) ou libérées
                vision_events = event_gate.process(vision_events, ts)

            alerts = fusion.update(ts, vision_events, persons_visible=bool(persons))
            for alert in alerts:
                snapshot = None
                if alert.level == "critical":
                    ok_jpg, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                    snapshot = buf.tobytes() if ok_jpg else None
                sender.send(alert, snapshot)
                recent_alerts.insert(0, (alert, ts))
            del recent_alerts[4:]

            pir_txt = "PIR:ON" if fusion.pir_recent(ts) else "PIR:off"
            link_txt = "" if link is None else (" MQTT:ok" if link.connected else " MQTT:--")
            hud = f"{infer_ms:.1f} ms | {'ACTIF' if active else 'veille'} | " \
                  f"mvt {gate.ratio * 100:.1f}% | {pir_txt}{link_txt}"
            if enhancer.last_mean is not None:
                hud += f" | lum {enhancer.last_mean:.0f}" + (" NUIT" if enhancer.active else "")
            names = (lambda tid: label(tracker, tid, ts)) if tracker else None
            draw(frame, analyzer.zones, persons, analyzer, ts, hud, recent_alerts, names)
            if link:
                link.publish_frame(frame)  # flux annoté pour le dashboard

            if not args.headless:
                cv2.imshow("Sentinel-X vision", frame)
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    break
                if key == ord("p"):
                    fusion.on_pir(True, ts)
                    sim_pir_until = ts + 3.0
    finally:
        cap.release()
        cv2.destroyAllWindows()
        if link:
            link.stop()


if __name__ == "__main__":
    main()
