"""Sentinel-X — maintenance prédictive (IA sur les séries temporelles des capteurs).

Usage (depuis host/predictive) :
  python sentinel_predictive.py export-db               exporte toute la télémétrie de la base -> data/telemetry.csv
  python sentinel_predictive.py episode "souffle"       note le DÉBUT d'un test (retiré de l'apprentissage)
  python sentinel_predictive.py episode --fin           ... et sa fin
  python sentinel_predictive.py record --minutes 30     enregistre la télémétrie réelle (data/telemetry.csv)
  python sentinel_predictive.py train [--synthetic]     entraîne le modèle (models/) sur data/telemetry.csv
  python sentinel_predictive.py evaluate                entraîné sur un jour, testé sur un autre (jamais vu)
  python sentinel_predictive.py export-esp              paramètres appris -> firmware/include/ml_params.h
  python sentinel_predictive.py edge-eval               valide le détecteur EMBARQUÉ (jumeau edge.py) pareil
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

from bounds import check_bounds, compute_bounds
from features import FEATURES, SeriesBuffer, compute_features

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
    """Buffer -> caractéristiques -> score + prévision + type + bornes -> réponse."""

    def __init__(self, cfg, model, responder):
        self.cfg, self.model, self.responder = cfg, model, responder
        self.buffer = SeriesBuffer(keep_s=cfg["windows"]["long_s"] * 1.2)
        self.lock = threading.Lock()
        # Bornes absolues : le gaz est relatif à la ligne de base apprise sur la pièce
        self.bounds = compute_bounds(cfg["limites"], model.baseline)
        responder.bounds = self.bounds
        self.base_gaz = (model.baseline or {}).get("gaz") or cfg["limites"]["gaz"]["reference_defaut"]
        self._gas_low = {}  # serie -> dernier instant où le MQ-2 était sous sa ligne de base (préchauffe)

    def add(self, serie, t, temp, hum, gaz):
        with self.lock:
            self.buffer.add(serie, t, temp, hum, gaz)

    def gas_warming(self, serie, gaz, now):
        """MQ-2 en préchauffe (allumage à froid : ~6-10 puis remontée en ~16 min) : aucune valeur de gaz
        dans l'historique, ou valeur sous ratio_base x base, puis pendant stabilisation_s. Une vraie fuite fait
        MONTER le gaz au-dessus de la base : dès la borne d'avertissement, le gaz est de nouveau analysé.
        gaz : dernière valeur NON nulle (un null isolé ne relance pas la préchauffe)."""
        pc = self.cfg["prechauffe_gaz"]
        if gaz is None or gaz < pc["ratio_base"] * self.base_gaz:
            self._gas_low[serie] = now
            return True
        t_low = self._gas_low.get(serie)
        return (t_low is not None and now - t_low < pc["stabilisation_s"]
                and gaz < self.bounds["gaz_haut"]["avertissement"])

    def evaluate(self, now, verbose=True, ts=None):
        w = self.cfg["windows"]
        events = []
        with self.lock:
            snapshot = {s: self.buffer.samples(s) for s in self.buffer.data}
            first_seen = dict(self.buffer.first_seen)
        for serie, samples in snapshot.items():
            last = {name: next((smp[col] for smp in reversed(samples) if smp[col] is not None), None)
                    for name, col in (("temp", 1), ("hum", 2), ("gaz", 3))}
            crossed = check_bounds(last, self.bounds)
            warming = self.gas_warming(serie, last["gaz"], now)
            if now - first_seen[serie] < w["warmup_s"]:
                # Fenêtre de 5 min encore incomplète : l'IA ne juge pas encore, mais les BORNES veillent
                # dès l'allumage (un boîtier allumé dans une pièce déjà à 50 °C doit alerter tout de suite).
                if crossed:
                    ev = self.responder.handle(serie, 0.0, "DERIVE_INDETERMINEE", 0.0, [0.0] * len(FEATURES),
                                               [0.0] * len(FEATURES), (None, None), now=now, ts=ts, crossed=crossed)
                    if ev:
                        events.append(ev)
                continue
            if warming:
                # Gaz exclu de l'analyse : retiré des échantillons et du contrôle qualité, puis neutralisé
                samples = [(t, temp, hum, None) for t, temp, hum, _ in samples]
            v = compute_features(samples, now, w["short_s"], w["long_s"], w["min_samples_short"], w["min_samples_long"],
                                 quality_cols=(1, 2) if warming else (1, 2, 3))
            if v is None:
                continue
            if warming:
                v = self.model.neutralize(v, "gaz")
            scores, contribs = self.model.score([v])
            kinds, confs = self.model.classify([v])
            fc = self.model.forecast(v, last, self.bounds)
            if verbose:
                fc_txt = "-" if fc[1] is None else f"{fc[0]} {fc[1]:.0f} min"
                print(f"[PRÉDICTIF] {serie} score {scores[0]:.2f} | {kinds[0]} | borne critique : {fc_txt}"
                      + (f" | borne franchie {crossed[0]} {crossed[1]}" if crossed else "")
                      + (" | MQ-2 en préchauffe" if warming else ""))
            ev = self.responder.handle(serie, scores[0], kinds[0], confs[0], contribs[0], v, fc,
                                       now=now, ts=ts, crossed=crossed)
            if ev:
                if verbose:
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


def split_sessions(samples, gap_s=120):
    """Coupe une série aux trous de plus de gap_s (ESP éteint, nuit) : liste de sessions continues."""
    sessions, cur = [], []
    for smp in samples:
        if cur and smp[0] - cur[-1][0] > gap_s:
            sessions.append(cur)
            cur = []
        cur.append(smp)
    if cur:
        sessions.append(cur)
    return sessions


EPISODES = BASE / "data" / "episodes.csv"
EPISODE_MARGIN_S = 300  # la fenêtre de 5 min qui suit un test le contient encore


def load_episodes():
    """Épisodes de test notés avec `episode` : [(début, fin)] en secondes UTC (fin = maintenant si en cours)."""
    from datetime import datetime, timezone
    if not EPISODES.exists():
        return []
    out = []
    with EPISODES.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            a = datetime.fromisoformat(r["debut_utc"]).timestamp()
            b = datetime.fromisoformat(r["fin_utc"]).timestamp() if r["fin_utc"] else datetime.now(timezone.utc).timestamp()
            out.append((a, b))
    return out


def real_sessions(path, exclude_episodes=True):
    """Sessions continues de télémétrie réelle, épisodes de test retirés (+ 5 min après chacun)."""
    episodes = load_episodes() if exclude_episodes else []
    sessions = []
    for samples in load_csv(path).values():
        kept = [smp for smp in sorted(samples)
                if not any(a <= smp[0] <= b + EPISODE_MARGIN_S for a, b in episodes)]
        sessions += split_sessions(kept)
    return sessions


def baseline_of(sessions):
    """Ligne de base de la pièce : médianes du fonctionnement normal réel (robustes aux préchauffes)."""
    import statistics
    return {k: round(statistics.median(smp[c] for sess in sessions for smp in sess if smp[c] is not None), 1)
            for k, c in (("temp", 1), ("hum", 2), ("gaz", 3))}


def clean_sessions(sessions, cfg, base_gaz):
    """Retire de chaque session sa préchauffe INITIALE du MQ-2 (gaz absent ou sous ratio_base x base au
    démarrage) + la stabilisation, recale t à 0, et garde les sessions assez longues pour des fenêtres de 5 min.
    Les null isolés plus tard sont gardés : ils font partie du fonctionnement réel."""
    pc, w = cfg["prechauffe_gaz"], cfg["windows"]
    out = []
    for sess in sessions:
        t0 = sess[0][0]
        ready = next((t - t0 for t, _, _, g in sess if g is not None and g >= pc["ratio_base"] * base_gaz), None)
        if ready is None:
            continue  # MQ-2 jamais chaud sur cette session : rien d'exploitable comme « normal »
        start = max(w["warmup_s"], ready + pc["stabilisation_s"] if ready > 0 else 0)
        trimmed = [(t - t0 - start, *rest) for t, *rest in sess if t - t0 >= start]
        if trimmed and trimmed[-1][0] >= 2 * w["long_s"]:
            out.append(trimmed)
    return out


def train_model(cfg, raw_sessions, synthetic=False, seed=0):
    """Étage 1 sur le normal RÉEL nettoyé ; étage 2 sur des incidents superposés à ce même réel."""
    from model import PredictiveModel
    from simulate import incident_dataset, normal_dataset, windows
    model = PredictiveModel()
    clean, rows = [], []
    if raw_sessions:
        model.baseline = baseline_of(raw_sessions)
        clean = clean_sessions(raw_sessions, cfg, model.baseline["gaz"])
        rows = [r for sess in clean for r in windows(sess, cfg)]
    n_real = len(rows)
    if synthetic or not rows:
        rows += normal_dataset(cfg)
    model.fit(rows, percentile=cfg["model"]["threshold_percentile"], trees=cfg["model"]["iforest_trees"],
              purge=cfg["model"].get("purge", False))
    inc_rows, labels = incident_dataset(cfg, seed=seed, base=clean or None)
    model.fit_typer(inc_rows, labels, rows)
    return model, clean, n_real, len(rows) - n_real, len(inc_rows)


def print_model_summary(model, cfg):
    if getattr(model, "purged", 0):
        print(f"Purge : {model.purged} fenêtre(s) d'épisodes de test retirée(s) du normal d'entraînement")
    if model.baseline:
        print("Ligne de base apprise (médianes) : " + ", ".join(f"{k} {v:g}" for k, v in model.baseline.items()))
    print("Pente normale max apprise (5 min, p99.5) : "
          + ", ".join(f"{k} ±{v:.2f}/min" for k, v in model.slope_floor.items()))
    print("Bornes d'alerte (avertissement / critique) :")
    for key, b in compute_bounds(cfg["limites"], model.baseline).items():
        print(f"  {key:10} {b['avertissement']:>6g} / {b['critique']:<6g}")


def cmd_train(args, cfg):
    data = BASE / args.data
    sessions = real_sessions(data) if data.exists() else []
    model, clean, n_real, n_synth, n_inc = train_model(cfg, sessions, synthetic=args.synthetic)
    model.save(BASE / cfg["model"]["dir"])
    print(f"{len(sessions)} session(s) réelle(s), {len(clean)} gardée(s) après retrait des préchauffes : "
          f"{n_real} fenêtres de fonctionnement normal réel" + (f" + {n_synth} synthétiques" if n_synth else ""))
    print(f"Classifieur de type : {n_inc} fenêtres d'incidents superposés à la télémétrie réelle (et synthétique)")
    print_model_summary(model, cfg)


def run_pipeline(cfg, model, samples, base_dir, serie="SX", step=None):
    """Rejoue des échantillons (t, temp, hum, gaz) dans le pipeline complet ; renvoie [(t, événement)]."""
    from response import Responder
    pipe = Pipeline(cfg, model, Responder(cfg, api=None, base_dir=base_dir))
    step = step or cfg["windows"]["step_s"]
    events, next_eval = [], samples[0][0] + step
    for t, *vals in samples:
        while t >= next_eval:
            events += [(next_eval, ev) for ev in pipe.evaluate(next_eval, verbose=False)]
            next_eval += step
        pipe.add(serie, t, *vals)
    return events


def cmd_evaluate(args, cfg):
    """Évaluation honnête : entraîné sur le PREMIER jour de données, testé sur les jours suivants (jamais vus)."""
    if args.purge:
        cfg = dict(cfg, model=dict(cfg["model"], purge=True))
    import tempfile
    from datetime import datetime, timezone

    import numpy as np

    from simulate import INCIDENTS, incident_dataset

    def day(sess):
        return datetime.fromtimestamp(sess[0][0], timezone.utc).date()

    sessions = real_sessions(BASE / args.data)
    d0 = day(sessions[0])
    train = [x for x in sessions if day(x) == d0]
    test = [x for x in sessions if day(x) != d0]
    if not test:
        raise SystemExit("Il faut des données sur au moins deux jours.")
    model, _, n_real, _, n_inc = train_model(cfg, train)
    print(f"Entraîné sur le {d0} ({len(train)} session(s), {n_real} fenêtres normales réelles, "
          f"{n_inc} fenêtres d'incidents) ; testé sur {len(test)} session(s) d'un autre jour.")
    print_model_summary(model, cfg)
    log = Path(tempfile.mkdtemp())
    cfg_eval = dict(cfg, response=dict(cfg["response"], log_file=str(log / "eval.jsonl")))

    # 1) Fausses alertes sur le normal réel jamais vu, préchauffes comprises (allumages à froid)
    hours, false_alerts = 0.0, []
    for sess in test:
        hours += (sess[-1][0] - sess[0][0]) / 3600
        false_alerts += run_pipeline(cfg_eval, model, sess, log)
    print(f"\n1) Fonctionnement normal jamais vu : {len(false_alerts)} alerte(s) en {hours:.1f} h "
          f"({len(false_alerts) / hours:.2f} / h), allumages à froid du MQ-2 compris")
    for t, ev in false_alerts:
        print(f"     {datetime.fromtimestamp(t, timezone.utc):%d %H:%M} {ev['niveau']} {ev['sous_type']} "
              f"{ev['borne_franchie'] or ''}")

    # 2) Incidents superposés à la télémétrie réelle du jour de test, rejoués dans le PIPELINE COMPLET
    #    (persistance, prévision, bornes) : délai de détection et type annoncé.
    import random

    from simulate import apply_incident, real_segment
    test_clean = clean_sessions(test, cfg, model.baseline["gaz"])
    print(f"\n2) Incidents superposés à {len(test_clean)} tronçon(s) réel(s) jamais vu(s), rejoués dans le pipeline :")
    rng = random.Random(123)
    n, onset, horizon = args.series, 400.0, args.horizon * 60.0
    tot_ok = tot_type = tot_fp = 0
    for kind in INCIDENTS:
        delays, types, fp = [], [], 0
        for _ in range(n):
            seg = real_segment(rng, test_clean, onset + horizon)
            if seg is None:
                break
            evs = run_pipeline(cfg_eval, model, apply_incident(seg, kind, onset, rng), log)
            fp += sum(1 for t, _ in evs if t < onset)
            after = [(t, ev) for t, ev in evs if t >= onset]
            if after:
                delays.append((after[0][0] - onset) / 60)
                types.append(after[-1][1]["sous_type"])  # type après précision éventuelle
        ok = len(delays)
        good = sum(1 for k in types if k == kind)
        tot_ok, tot_type, tot_fp = tot_ok + ok, tot_type + good, tot_fp + fp
        med = sorted(delays)[len(delays) // 2] if delays else float("nan")
        others = {k: types.count(k) for k in set(types) if k != kind}
        print(f"     {kind:22} détecté {ok:2}/{n} en {med:4.1f} min (médiane) | type juste {good:2}/{max(ok, 1)}"
              + (f" | sinon {others}" if others else ""))
    print(f"     TOTAL : détecté {tot_ok}/{n * len(INCIDENTS)}, type juste {tot_type}/{max(tot_ok, 1)}, "
          f"{tot_fp} fausse(s) alerte(s) avant le début des incidents")

    # 3) Anticipation : surchauffe +1 °C/min superposée au plus long tronçon réel jamais vu
    sess = max(test_clean, key=lambda x: x[-1][0])
    onset, crit = 300.0, cfg["limites"]["temp"]["critique_haut"]
    ramp = [(t, None if T is None else T + max(0.0, t - onset) / 60,
             None if H is None else H - 2 * max(0.0, t - onset) / 60, G) for t, T, H, G in sess]
    t_cross = next((t for t, T, _, _ in ramp if T is not None and T >= crit), None)
    evs = run_pipeline(cfg_eval, model, ramp, log)
    first = next(((t, ev) for t, ev in evs if t >= onset), None)
    fc = next(((t, ev) for t, ev in evs if t >= onset and ev["prevision"]), None)
    print("\n3) Surchauffe +1 °C/min superposée à un tronçon réel jamais vu :")
    if first and t_cross:
        print(f"     1re alerte {(first[0] - onset) / 60:.1f} min après le début ({first[1]['sous_type']}), "
              f"soit {(t_cross - first[0]) / 60:.1f} min AVANT {crit:g} °C")
    if fc and t_cross:
        print(f"     prévision : « {fc[1]['prevision']['minutes']} min » annoncées pour "
              f"{(t_cross - fc[0]) / 60:.1f} min réelles")


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


def cmd_export_esp(args, cfg):
    """Écrit les paramètres appris par le PC dans firmware/include/ml_params.h (détecteur embarqué)."""
    from datetime import datetime, timezone

    from edge import params_from_model
    from model import PredictiveModel
    model = PredictiveModel.load(BASE / cfg["model"]["dir"])
    if not model.baseline:
        raise SystemExit("Modèle sans ligne de base réelle : lancer d'abord `train` sur data/telemetry.csv")
    p = params_from_model(model, cfg)
    out = (BASE / args.out).resolve()
    f = lambda v: f"{float(v):.3f}f"  # noqa: E731
    lines = [
        "// GÉNÉRÉ par host/predictive/sentinel_predictive.py export-esp : NE PAS MODIFIER À LA MAIN.",
        f"// {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. Paramètres APPRIS par le PC sur la télémétrie réelle :",
        "// le PC apprend, l'ESP applique (détecteur embarqué edgeAi* de main.cpp, jumeau Python : edge.py).",
        "#pragma once",
        "",
        "// Ligne de base de la pièce (médianes du fonctionnement normal réel)",
        f"#define ML_BASE_TEMP {f(p['base_temp'])}",
        f"#define ML_BASE_HUM {f(p['base_hum'])}",
        f"#define ML_BASE_GAZ {round(p['base_gaz'])}",
        "",
        "// Bornes d'alerte : avertissement (agir avant) / critique (danger)",
        f"#define ML_TEMP_WARN {f(p['temp_warn'])}",
        f"#define ML_TEMP_CRIT {f(p['temp_crit'])}",
        f"#define ML_HUM_HIGH_WARN {f(p['hum_high_warn'])}",
        f"#define ML_HUM_HIGH_CRIT {f(p['hum_high_crit'])}",
        f"#define ML_HUM_LOW_WARN {f(p['hum_low_warn'])}",
        f"#define ML_HUM_LOW_CRIT {f(p['hum_low_crit'])}",
        f"#define ML_GAZ_WARN {f(p['gaz_warn'])}   // relatif à la ligne de base du MQ-2 (non étalonné)",
        f"#define ML_GAZ_CRIT {f(p['gaz_crit'])}",
        f"#define ML_GAZ_PRECHAUFFE {f(p['gaz_prechauffe'])}   // en dessous : MQ-2 en préchauffe, gaz ignoré",
        "",
        "// Pentes normales maximales apprises (5 min, p99.5, par minute) : au-delà, tendance significative",
        f"#define ML_SLOPE_TEMP {f(p['slope_temp'])}",
        f"#define ML_SLOPE_HUM {f(p['slope_hum'])}",
        f"#define ML_SLOPE_GAZ {f(p['slope_gaz'])}",
        "",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Paramètres embarqués écrits dans {out}")
    for k, v in p.items():
        print(f"  {k:15} {v}")


def cmd_edge_eval(args, cfg):
    """Valide le détecteur EMBARQUÉ (jumeau Python) comme le modèle PC : appris le 1er jour, testé sur un autre."""
    import random
    from datetime import datetime, timezone

    from edge import PC_KIND, params_from_model, replay
    from simulate import apply_incident, real_segment

    def day(sess):
        return datetime.fromtimestamp(sess[0][0], timezone.utc).date()

    sessions = real_sessions(BASE / args.data)
    d0 = day(sessions[0])
    train = [x for x in sessions if day(x) == d0]
    test = [x for x in sessions if day(x) != d0]
    model, *_ = train_model(cfg, train)
    params = params_from_model(model, cfg)
    print(f"Paramètres appris le {d0} : " + ", ".join(f"{k} {v}" for k, v in params.items()))

    hours = sum((x[-1][0] - x[0][0]) / 3600 for x in test)
    alerts = [a for sess in test for a in replay(params, sess) if a[2]]
    print(f"\n1) Normal jamais vu ({hours:.1f} h, allumages à froid compris) : {len(alerts)} alerte(s) embarquée(s)")
    for t, kind, level, m in alerts:
        print(f"     {datetime.fromtimestamp(t, timezone.utc):%d %H:%M} {level} {kind}")

    test_clean = clean_sessions(test, cfg, model.baseline["gaz"])
    rng, onset, horizon = random.Random(123), 400.0, args.horizon * 60.0
    print(f"\n2) Incidents superposés au réel jamais vu ({args.horizon:g} min), vus par l'ESP seul :")
    expected = {v: k for k, v in PC_KIND.items()}
    tot = ok_tot = good_tot = 0
    for kind in [k for k in PC_KIND.values()]:
        delays, good, fp = [], 0, 0
        for _ in range(args.series):
            seg = real_segment(rng, test_clean, onset + horizon)
            if seg is None:
                break
            evs = [e for e in replay(params, apply_incident(seg, kind, onset, rng)) if e[2]]
            fp += sum(1 for e in evs if e[0] < onset)
            after = [e for e in evs if e[0] >= onset]
            if after:
                delays.append((after[0][0] - onset) / 60)
                # type juste si l'une des alertes de l'incident a le bon type (le 1er signe peut être plus général)
                good += any(e[1] == expected[kind] for e in after)
        n = args.series
        tot, ok_tot, good_tot = tot + n, ok_tot + len(delays), good_tot + good
        med = sorted(delays)[len(delays) // 2] if delays else float("nan")
        print(f"     {kind:22} détecté {len(delays):2}/{n} en {med:4.1f} min (médiane) | type juste {good:2}/{max(len(delays), 1)}"
              + (f" | {fp} alerte(s) avant l'incident" if fp else ""))
    print(f"     TOTAL : détecté {ok_tot}/{tot}, type juste {good_tot}/{max(ok_tot, 1)}")


def cmd_episode(args, _cfg):
    """Note un épisode de TEST (souffle, chaleur, briquet...) : retiré de l'apprentissage du « normal »."""
    from datetime import datetime, timezone
    EPISODES.parent.mkdir(parents=True, exist_ok=True)
    rows = list(csv.DictReader(EPISODES.open(encoding="utf-8"))) if EPISODES.exists() else []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if args.fin:
        open_rows = [r for r in rows if not r["fin_utc"]]
        if not open_rows:
            raise SystemExit("Aucun épisode en cours.")
        open_rows[-1]["fin_utc"] = now
        print(f"Épisode « {open_rows[-1]['description']} » terminé à {now}")
    else:
        if any(not r["fin_utc"] for r in rows):
            raise SystemExit("Un épisode est déjà en cours : `episode --fin` d'abord.")
        rows.append({"debut_utc": now, "fin_utc": "", "description": args.description or "test"})
        print(f"Épisode « {rows[-1]['description']} » commencé à {now} (terminer avec : episode --fin)")
    with EPISODES.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["debut_utc", "fin_utc", "description"])
        w.writeheader()
        w.writerows(rows)


EXPORT_SQL = ("SELECT extract(epoch from (m.date_mesure + m.heure_mesure)), d.numero_serie, m.temperature_c, "
              "m.humidite_pct, m.gaz_brut, m.mouvement_detecte FROM mesure m JOIN dispositif d USING (id_dispositif) "
              "ORDER BY 1;")


def cmd_export_db(args, _cfg):
    """Exporte TOUTE la télémétrie de la base (VM) vers data/telemetry.csv, par SSH (sentinel-vm)."""
    import subprocess
    remote = ("sudo docker exec -i sentinel-postgres-1 sh -c "
              "'PGPASSWORD=$POSTGRES_PASSWORD psql -q -U $POSTGRES_USER -d $POSTGRES_DB -At -F,'")
    res = subprocess.run(["ssh", "sentinel-vm", remote], input=EXPORT_SQL, capture_output=True, text=True,
                         encoding="utf-8", timeout=300)
    if res.returncode != 0:
        raise SystemExit(f"Export impossible : {res.stderr.strip()[:300]}")
    out = BASE / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for line in res.stdout.splitlines():
            parts = line.split(",")
            if len(parts) != 6:
                continue
            ts, serie, t, h, g, pir = parts
            w.writerow([ts, serie, t, h, g, pir == "t"])
            n += 1
    sessions = real_sessions(out)
    hours = sum((x[-1][0] - x[0][0]) / 3600 for x in sessions)
    print(f"{n} mesures exportées -> {out} | {len(sessions)} session(s), {hours:.1f} h utiles "
          f"après retrait des {len(load_episodes())} épisode(s) de test notés")


def main():
    parser = argparse.ArgumentParser(description="Sentinel-X maintenance prédictive")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("record")
    p.add_argument("--minutes", type=float, default=30)
    p.add_argument("--out", default="data/telemetry.csv")
    p = sub.add_parser("train")
    p.add_argument("--data", default="data/telemetry.csv")
    p.add_argument("--synthetic", action="store_true")
    p = sub.add_parser("evaluate")
    p.add_argument("--data", default="data/telemetry.csv")
    p.add_argument("--series", type=int, default=12, help="incidents rejoués par type")
    p.add_argument("--horizon", type=float, default=20, help="minutes d'incident rejouées")
    p.add_argument("--purge", action="store_true", help="retire les épisodes de test du normal d'entraînement")
    p = sub.add_parser("episode", help="noter un test (retiré de l'apprentissage)")
    p.add_argument("description", nargs="?")
    p.add_argument("--fin", action="store_true")
    p = sub.add_parser("export-db")
    p.add_argument("--out", default="data/telemetry.csv")
    p = sub.add_parser("export-esp")
    p.add_argument("--out", default="../../firmware/include/ml_params.h")
    p = sub.add_parser("edge-eval")
    p.add_argument("--data", default="data/telemetry.csv")
    p.add_argument("--series", type=int, default=12)
    p.add_argument("--horizon", type=float, default=20)
    sub.add_parser("detect")
    p = sub.add_parser("replay")
    p.add_argument("--csv", required=True)
    args = parser.parse_args()
    cfg = load_config()
    {"record": cmd_record, "train": cmd_train, "evaluate": cmd_evaluate, "detect": cmd_detect,
     "replay": cmd_replay, "export-esp": cmd_export_esp, "edge-eval": cmd_edge_eval,
     "episode": cmd_episode, "export-db": cmd_export_db}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
