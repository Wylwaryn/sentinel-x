"""Sentinel-X — maintenance prédictive (IA sur les séries temporelles des capteurs).

Usage (depuis host/predictive) :
  python sentinel_predictive.py record --minutes 30     enregistre la télémétrie réelle (data/telemetry.csv)
  python sentinel_predictive.py train [--synthetic]     entraîne le modèle (models/)
  python sentinel_predictive.py detect                  détection temps réel (MQTT -> API)
  python sentinel_predictive.py replay --csv FICHIER    rejoue un enregistrement hors ligne (évaluation)
Compte MQTT : variables SENTINEL_CAPTEURS_USER / SENTINEL_CAPTEURS_PASS ; jeton API : SENTINEL_CAPTEURS_TOKEN.
"""
import argparse
import csv
import json
import os
import threading
import time
from collections import defaultdict
from pathlib import Path

from features import SeriesBuffer, compute_features

BASE = Path(__file__).resolve().parent
COLUMNS = ["ts", "serie", "temperature_c", "humidite_pct", "gaz_brut", "pir"]


def load_config():
    return json.loads((BASE / "config.json").read_text(encoding="utf-8"))


def parse_telemetry(payload):
    """(serie, temp, hum, gaz) ou None. Format : server/ingest/app/models.py (Telemetry)."""
    try:
        d = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict) or not isinstance(d.get("serie"), str):
        return None

    def num(key):
        v = d.get(key)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    return d["serie"], num("temperature_c"), num("humidite_pct"), num("gaz_brut")


def mqtt_client(cfg, on_telemetry):
    import paho.mqtt.client as mqtt
    m = cfg["mqtt"]
    user = os.environ.get(m["username_env"], "capteurs")
    password = os.environ.get(m["password_env"])
    if not password:
        raise RuntimeError(f"Variable d'environnement {m['password_env']} manquante")
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=m["client_id"])
    c.tls_set(ca_certs=str((BASE / m["ca_cert"]).resolve()))
    c.username_pw_set(user, password)
    c.on_connect = lambda cl, u, f, rc, p: cl.subscribe(m["topic"], qos=1) if not rc.is_failure \
        else print(f"[PRÉDICTIF] connexion MQTT refusée : {rc}")
    c.on_message = lambda cl, u, msg: on_telemetry(msg.payload)
    c.reconnect_delay_set(1, 10)
    c.connect_async(m["host"], m["port"], keepalive=30)
    return c


class Pipeline:
    """Buffer -> caractéristiques -> score + prévision + type -> réponse."""

    def __init__(self, cfg, model, responder):
        self.cfg, self.model, self.responder = cfg, model, responder
        self.buffer = SeriesBuffer(keep_s=cfg["windows"]["long_s"] * 1.2)
        self.lock = threading.Lock()

    def add(self, serie, t, temp, hum, gaz):
        with self.lock:
            self.buffer.add(serie, t, temp, hum, gaz)

    def evaluate(self, now, verbose=True, ts=None):
        w = self.cfg["windows"]
        events = []
        with self.lock:
            snapshot = {s: self.buffer.samples(s) for s in self.buffer.data}
            first_seen = dict(self.buffer.first_seen)
        for serie, samples in snapshot.items():
            if now - first_seen[serie] < w["warmup_s"]:
                continue  # MQ-2 en préchauffe, historique trop court
            v = compute_features(samples, now, w["short_s"], w["long_s"], w["min_samples_short"], w["min_samples_long"])
            if v is None:
                continue
            scores, contribs = self.model.score([v])
            kinds, confs = self.model.classify([v])
            last = {name: next((smp[col] for smp in reversed(samples) if smp[col] is not None), None)
                    for name, col in (("temp", 1), ("hum", 2), ("gaz", 3))}
            fc = self.model.forecast(v, last, self.cfg["forecast"]["limites_exploitation"])
            if verbose:
                fc_txt = "-" if fc[1] is None else f"{fc[0]} {fc[1]:.0f} min"
                print(f"[PRÉDICTIF] {serie} score {scores[0]:.2f} | {kinds[0]} | sortie enveloppe : {fc_txt}")
            ev = self.responder.handle(serie, scores[0], kinds[0], confs[0], contribs[0], v, fc, now=now, ts=ts)
            if ev:
                print(f"  ⚠ [{ev['niveau']}] {ev['message']}")
                events.append(ev)
        return events


def cmd_record(args, cfg):
    out = BASE / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    new = not out.exists()
    f = out.open("a", newline="", encoding="utf-8")
    w = csv.writer(f)
    if new:
        w.writerow(COLUMNS)
    count = [0]

    def on_tel(payload):
        p = parse_telemetry(payload)
        if p:
            raw = json.loads(payload)
            w.writerow([round(time.time(), 2), *p, raw.get("pir")])
            f.flush()
            count[0] += 1

    c = mqtt_client(cfg, on_tel)
    c.loop_start()
    print(f"Enregistrement de la télémétrie pendant {args.minutes} min -> {out}")
    try:
        time.sleep(args.minutes * 60)
    except KeyboardInterrupt:
        pass
    c.loop_stop()
    f.close()
    print(f"{count[0]} mesures enregistrées.")


def load_csv(path):
    series = defaultdict(list)
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            val = lambda k: float(r[k]) if r[k] not in ("", "None") else None  # noqa: E731
            series[r["serie"]].append((float(r["ts"]), val("temperature_c"), val("humidite_pct"), val("gaz_brut")))
    return series


def cmd_train(args, cfg):
    from model import PredictiveModel
    from simulate import incident_dataset, normal_dataset, windows
    rows = []
    data = BASE / args.data
    if data.exists():
        for serie, samples in load_csv(data).items():
            t0 = samples[0][0]
            rel = [(t - t0, *rest) for t, *rest in samples]
            rows += windows(rel, cfg, start_s=cfg["windows"]["warmup_s"])
        print(f"{len(rows)} fenêtres de fonctionnement normal réel ({data})")
    if args.synthetic or not rows:
        synth = normal_dataset(cfg)
        print(f"+ {len(synth)} fenêtres normales synthétiques (amorçage, à remplacer par des données réelles)")
        rows += synth
    model = PredictiveModel()
    model.fit(rows, percentile=cfg["model"]["threshold_percentile"], trees=cfg["model"]["iforest_trees"])
    inc_rows, labels = incident_dataset(cfg)
    model.fit_typer(inc_rows, labels, rows)
    model.save(BASE / cfg["model"]["dir"])
    env = ", ".join(f"{k} ±{v:.2f}" for k, v in model.envelope.items())
    print(f"Modèle entraîné ({len(inc_rows)} fenêtres d'incidents synthétiques) | enveloppe normale apprise : {env}")


def cmd_detect(_args, cfg):
    from model import PredictiveModel
    from response import ApiSender, Responder
    model = PredictiveModel.load(BASE / cfg["model"]["dir"])
    api = ApiSender(cfg["api"], BASE) if cfg["api"]["enabled"] else None
    pipe = Pipeline(cfg, model, Responder(cfg, api=api, base_dir=BASE))

    def on_tel(payload):
        p = parse_telemetry(payload)
        if p:
            pipe.add(p[0], time.monotonic(), *p[1:])

    c = mqtt_client(cfg, on_tel)
    c.loop_start()
    print("[PRÉDICTIF] détection en cours (Ctrl+C pour arrêter)")
    try:
        while True:
            time.sleep(cfg["windows"]["step_s"])
            pipe.evaluate(time.monotonic())
    except KeyboardInterrupt:
        pass
    finally:
        c.loop_stop()


def cmd_replay(args, cfg):
    from model import PredictiveModel
    from response import Responder
    model = PredictiveModel.load(BASE / cfg["model"]["dir"])
    pipe = Pipeline(cfg, model, Responder(cfg, api=None, base_dir=BASE))
    from datetime import datetime, timezone
    samples = sorted((t, s, *rest) for s, rows in load_csv(args.csv).items() for t, *rest in rows)
    t0 = samples[0][0]
    next_eval = t0 + cfg["windows"]["step_s"]
    events = []
    for t, serie, *vals in samples:
        while t >= next_eval:
            iso = datetime.fromtimestamp(next_eval, timezone.utc).isoformat()
            for ev in pipe.evaluate(next_eval, verbose=False, ts=iso):
                events.append((next_eval - t0, ev))
            next_eval += cfg["windows"]["step_s"]
        pipe.add(serie, t, *vals)
    print(f"Rejeu terminé : {len(events)} alerte(s).")
    for rel, ev in events:
        fc = ev["prevision"]
        fc_txt = "" if not fc else f" | prévision {fc['capteur']} {fc['minutes']} min"
        print(f"  t = {rel / 60:5.1f} min | {ev['episode']:15} | {ev['niveau']:13} | {ev['sous_type']}{fc_txt}")


def main():
    parser = argparse.ArgumentParser(description="Sentinel-X maintenance prédictive")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("record")
    p.add_argument("--minutes", type=float, default=30)
    p.add_argument("--out", default="data/telemetry.csv")
    p = sub.add_parser("train")
    p.add_argument("--data", default="data/telemetry.csv")
    p.add_argument("--synthetic", action="store_true")
    sub.add_parser("detect")
    p = sub.add_parser("replay")
    p.add_argument("--csv", required=True)
    args = parser.parse_args()
    cfg = load_config()
    {"record": cmd_record, "train": cmd_train, "detect": cmd_detect, "replay": cmd_replay}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
