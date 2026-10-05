#!/usr/bin/env python3
"""Supervision et maintien en condition opérationnelle de la VM Sentinel-X.

    sentinel-monitor collect           relevé ponctuel ajouté au journal (lancé chaque minute par systemd)
    sentinel-monitor report [--hours N] résumé lisible : dernier relevé + tendances sur N heures (défaut 1)
    sentinel-monitor report --markdown  idem, en Markdown (dossier, démo)

Relevé : CPU, RAM et disque de la VM ; CPU, RAM, PIDs, réseau, redémarrages et taille des
journaux de chaque conteneur ; compteurs Mosquitto ($SYS, compte « monitor ») ; volumétrie
PostgreSQL. Aucun port exposé : tout est lu localement (procfs, Docker, réseau Docker interne).

Bibliothèque standard uniquement. Lancé en root (Docker, journaux des conteneurs).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

PROJECT = "sentinel"
LOG_FILE = os.environ.get("SENTINEL_MONITOR_LOG", "/var/log/sentinel-x/metrics.jsonl")
CONF_FILE = os.environ.get("SENTINEL_MONITOR_CONF", "/etc/sentinel-x/monitor.conf")
MQTT_IMAGE = "eclipse-mosquitto:2.0"
SYS_TOPICS = {
    "clients_connectes": "$SYS/broker/clients/connected",
    "messages_recus": "$SYS/broker/messages/received",
    "messages_envoyes": "$SYS/broker/messages/sent",
    "octets_recus": "$SYS/broker/bytes/received",
    "octets_envoyes": "$SYS/broker/bytes/sent",
    "messages_recus_1min": "$SYS/broker/load/messages/received/1min",
    "messages_envoyes_1min": "$SYS/broker/load/messages/sent/1min",
    "uptime": "$SYS/broker/uptime",
}

# Seuils d'alerte du rapport
SEUILS = {"cpu_pct": 85.0, "ram_pct": 85.0, "disque_pct": 80.0, "journal_conteneur_mo": 25.0}


def run(cmd, timeout=20, input_=None):
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input_)
    return res.stdout


def read_conf():
    conf = {}
    try:
        with open(CONF_FILE) as f:
            for line in f:
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.rstrip("\n").split("=", 1)
                    conf[k.strip()] = v.strip()
    except OSError:
        pass
    return conf


# --------------------------------------------------------------------------- VM
def cpu_pct(interval=1.0):
    def sample():
        with open("/proc/stat") as f:
            vals = list(map(int, f.readline().split()[1:]))
        idle = vals[3] + vals[4]  # idle + iowait
        return idle, sum(vals)
    i1, t1 = sample()
    time.sleep(interval)
    i2, t2 = sample()
    return round(100.0 * (1 - (i2 - i1) / max(t2 - t1, 1)), 1)


def vm_metrics():
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":")
            mem[k] = int(v.split()[0]) * 1024
    with open("/proc/loadavg") as f:
        load = list(map(float, f.read().split()[:3]))
    disk = shutil.disk_usage("/")
    docker_disk = shutil.disk_usage("/var/lib/docker")
    used = mem["MemTotal"] - mem["MemAvailable"]
    return {
        "cpu_pct": cpu_pct(),
        "charge": load,
        "ram_totale": mem["MemTotal"],
        "ram_utilisee": used,
        "ram_pct": round(100.0 * used / mem["MemTotal"], 1),
        "swap_utilise": mem["SwapTotal"] - mem["SwapFree"],
        "disque_pct": round(100.0 * disk.used / disk.total, 1),
        "disque_libre": disk.free,
        "docker_disque_libre": docker_disk.free,
    }


# --------------------------------------------------------------------------- conteneurs
UNITS = {"B": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12,
         "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}


def to_bytes(text):
    m = re.match(r"([\d.]+)\s*([A-Za-z]+)", text.strip())
    return int(float(m.group(1)) * UNITS.get(m.group(2), 1)) if m else 0


def container_metrics():
    ids = run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}"]).split()
    if not ids:
        return {}
    stats = {}
    for line in run(["docker", "stats", "--no-stream", "--format", "{{json .}}", *ids]).splitlines():
        s = json.loads(line)
        stats[s["ID"][:12]] = s
    out = {}
    for c in json.loads(run(["docker", "inspect", *ids])):
        service = c["Config"]["Labels"].get("com.docker.compose.service", c["Name"].lstrip("/"))
        log_path = c.get("LogPath") or ""
        log_bytes = 0
        if log_path:
            d, base = os.path.split(log_path)
            try:
                log_bytes = sum(os.path.getsize(os.path.join(d, f)) for f in os.listdir(d) if f.startswith(base))
            except OSError:
                pass
        s = stats.get(c["Id"][:12], {})
        rx, _, tx = s.get("NetIO", "0B / 0B").partition("/")
        health = (c["State"].get("Health") or {}).get("Status")
        out[service] = {
            "etat": c["State"]["Status"],
            "sante": health,
            "redemarrages": c["RestartCount"],
            "cpu_pct": float(s.get("CPUPerc", "0%").rstrip("%") or 0),
            "ram": to_bytes(s.get("MemUsage", "0B").split("/")[0]),
            "ram_pct": float(s.get("MemPerc", "0%").rstrip("%") or 0),
            "pids": int(s.get("PIDs", "0") or 0),
            "reseau_recu": to_bytes(rx),
            "reseau_envoye": to_bytes(tx),
            "journal_octets": log_bytes,
        }
    return out


# --------------------------------------------------------------------------- Mosquitto
def mosquitto_metrics(conf):
    password = conf.get("MQTT_MONITOR_PASSWORD")
    if not password:
        return {"erreur": f"MQTT_MONITOR_PASSWORD absent de {CONF_FILE}"}
    ca = conf.get("CA_FILE", "/opt/sentinel-x/server/certs/ca.crt")
    # Mot de passe transmis par stdin dans un fichier de config du client : jamais dans argv.
    script = ('mkdir -p /tmp/cfg && cat > /tmp/cfg/mosquitto_sub && '
              'XDG_CONFIG_HOME=/tmp/cfg mosquitto_sub -v -C %d -W 4 %s' %
              (len(SYS_TOPICS), " ".join(f"-t '{t}'" for t in SYS_TOPICS.values())))
    opts = f"-h mosquitto\n-p 8883\n--cafile /ca.crt\n-u monitor\n-P {password}\n"
    raw = run(["docker", "run", "--rm", "-i", "--network", f"{PROJECT}_net_ingest", "--user", "1883",
               "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL", "-v", f"{ca}:/ca.crt:ro",
               "--entrypoint", "sh", MQTT_IMAGE, "-c", script], timeout=30, input_=opts)
    values = dict(line.split(" ", 1) for line in raw.splitlines() if " " in line)
    out = {}
    for key, topic in SYS_TOPICS.items():
        v = values.get(topic)
        if v is None:
            continue
        num = re.match(r"[\d.]+", v)
        out[key] = float(num.group()) if num else v
    return out or {"erreur": "aucun compteur $SYS reçu"}


# --------------------------------------------------------------------------- PostgreSQL
def db_metrics():
    sql = ("SELECT pg_database_size(current_database()), "
           "(SELECT count(*) FROM mesure), (SELECT count(*) FROM alerte), "
           "(SELECT count(*) FROM alerte WHERE statut = 'NOUVELLE'), "
           "(SELECT count(*) FROM pg_stat_activity WHERE datname = current_database())")
    raw = run(["docker", "exec", f"{PROJECT}-postgres-1", "sh", "-c",
               'PGPASSWORD="$POSTGRES_PASSWORD" psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "$0"', sql])
    try:
        size, mesures, alertes, nouvelles, conns = map(int, raw.strip().split("|"))
    except ValueError:
        return {"erreur": "base injoignable"}
    return {"taille": size, "mesures": mesures, "alertes": alertes,
            "alertes_nouvelles": nouvelles, "connexions": conns}


def collect():
    conf = read_conf()
    sample = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    for key, fn in (("vm", vm_metrics), ("conteneurs", container_metrics),
                    ("mosquitto", lambda: mosquitto_metrics(conf)), ("bdd", db_metrics)):
        try:
            sample[key] = fn()
        except Exception as exc:  # un relevé partiel vaut mieux que pas de relevé
            sample[key] = {"erreur": f"{type(exc).__name__}: {exc}"[:200]}
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(sample, separators=(",", ":")) + "\n")
    return sample


# --------------------------------------------------------------------------- rapport
def load_samples(hours):
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    samples = []
    for path in (LOG_FILE + ".1", LOG_FILE):  # fichier courant + dernière rotation (non compressée)
        try:
            with open(path) as f:
                for line in f:
                    try:
                        s = json.loads(line)
                        if datetime.fromisoformat(s["ts"]) >= since:
                            samples.append(s)
                    except (ValueError, KeyError):
                        continue
        except OSError:
            continue
    return samples


def mo(n):
    return f"{n / 1e6:.1f} Mo"


def report(hours, markdown):
    samples = load_samples(hours)
    if not samples:
        print(f"Aucun relevé sur les {hours} dernière(s) heure(s) dans {LOG_FILE}.")
        print("Le timer tourne-t-il ? systemctl status sentinel-monitor.timer")
        return 1
    last, first = samples[-1], samples[0]
    vm, cont = last.get("vm", {}), last.get("conteneurs", {})
    mq, db = last.get("mosquitto", {}), last.get("bdd", {})
    alerts = []

    lines = []
    h = (lambda t: lines.append(f"\n### {t}\n")) if markdown else (lambda t: lines.append(f"\n== {t}"))
    kv = (lambda k, v: lines.append(f"- **{k}** : {v}")) if markdown else (lambda k, v: lines.append(f"  {k:<28} {v}"))

    title = f"Supervision Sentinel-X : dernier relevé {last['ts']}, {len(samples)} relevés sur {hours} h"
    lines.append(f"## {title}" if markdown else title)

    h("VM")
    cpus = [s["vm"]["cpu_pct"] for s in samples if "cpu_pct" in s.get("vm", {})]
    rams = [s["vm"]["ram_pct"] for s in samples if "ram_pct" in s.get("vm", {})]
    if "cpu_pct" in vm:
        kv("CPU", f"{vm['cpu_pct']} % (moyenne {sum(cpus) / len(cpus):.1f} %, max {max(cpus)} %)")
        kv("Charge 1/5/15 min", " / ".join(map(str, vm["charge"])))
        kv("RAM", f"{vm['ram_pct']} % de {mo(vm['ram_totale'])} (max {max(rams)} %)")
        kv("Swap utilisé", mo(vm["swap_utilise"]))
        kv("Disque /", f"{vm['disque_pct']} % utilisé, {mo(vm['disque_libre'])} libres")
        if max(cpus) > SEUILS["cpu_pct"]:
            alerts.append(f"CPU de la VM au-dessus de {SEUILS['cpu_pct']} %")
        if vm["ram_pct"] > SEUILS["ram_pct"]:
            alerts.append(f"RAM de la VM au-dessus de {SEUILS['ram_pct']} %")
        if vm["disque_pct"] > SEUILS["disque_pct"]:
            alerts.append(f"disque au-dessus de {SEUILS['disque_pct']} %")
    else:
        kv("Erreur", vm.get("erreur"))

    h("Conteneurs")
    if markdown:
        lines.append("| Service | État | CPU | RAM | PIDs | Journal | Redémarrages |\n|---|---|---|---|---|---|---|")
    for name, c in sorted(cont.items()):
        if "etat" not in c:
            continue
        etat = c["etat"] + (f" ({c['sante']})" if c["sante"] else "")
        cells = (name, etat, f"{c['cpu_pct']:.1f} %", f"{mo(c['ram'])} ({c['ram_pct']:.1f} %)",
                 str(c["pids"]), mo(c["journal_octets"]), str(c["redemarrages"]))
        lines.append("| " + " | ".join(cells) + " |" if markdown else
                     f"  {cells[0]:<10} {cells[1]:<18} CPU {cells[2]:>7}  RAM {cells[3]:<18} PIDs {cells[4]:>3}"
                     f"  journal {cells[5]:>8}  redémarrages {cells[6]}")
        if c["etat"] != "running" or c["sante"] == "unhealthy":
            alerts.append(f"conteneur {name} : {etat}")
        if c["redemarrages"]:
            alerts.append(f"conteneur {name} : {c['redemarrages']} redémarrage(s)")
        if c["journal_octets"] > SEUILS["journal_conteneur_mo"] * 1e6:
            alerts.append(f"journal de {name} > {SEUILS['journal_conteneur_mo']} Mo")
    kv("Rotation des journaux", "Docker json-file 10 Mo x 3 par conteneur (plafond ~30 Mo chacun)")

    h("Mosquitto (MQTT)")
    if "messages_recus" in mq:
        kv("Clients connectés", int(mq.get("clients_connectes", 0)))
        kv("Débit (1 min)", f"{mq.get('messages_recus_1min', 0):.1f} msg/min reçus, "
                            f"{mq.get('messages_envoyes_1min', 0):.1f} msg/min envoyés")
        kv("Depuis le démarrage", f"{int(mq['messages_recus'])} messages reçus, "
                                  f"{mo(mq.get('octets_recus', 0))} reçus, {mo(mq.get('octets_envoyes', 0))} envoyés")
        if mq.get("clients_connectes", 0) < 1:
            alerts.append("aucun client MQTT connecté (l'API d'ingestion devrait l'être)")
    else:
        kv("Erreur", mq.get("erreur"))
        alerts.append("compteurs Mosquitto indisponibles")

    h("PostgreSQL")
    if "taille" in db:
        kv("Taille de la base", mo(db["taille"]))
        kv("Mesures / alertes", f"{db['mesures']} / {db['alertes']} (dont {db['alertes_nouvelles']} non traitées)")
        old = first.get("bdd", {})
        if "mesures" in old and first is not last:
            dt_h = (datetime.fromisoformat(last["ts"]) - datetime.fromisoformat(first["ts"])).total_seconds() / 3600
            if dt_h > 0:
                rate = (db["mesures"] - old["mesures"]) / dt_h
                kv("Croissance", f"{rate:.0f} mesures/h, soit ~{rate * 24:.0f}/jour")
        kv("Connexions", db["connexions"])
    else:
        kv("Erreur", db.get("erreur"))
        alerts.append("base de données injoignable")

    h("Alertes de supervision")
    if alerts:
        lines.extend(f"- {a}" if markdown else f"  ! {a}" for a in alerts)
    else:
        lines.append("- aucune" if markdown else "  aucune : tout est dans les seuils")
    print("\n".join(lines))
    return 1 if alerts else 0


def main():
    p = argparse.ArgumentParser(description="Supervision de la VM Sentinel-X")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("collect", help="ajouter un relevé au journal")
    r = sub.add_parser("report", help="résumé lisible")
    r.add_argument("--hours", type=float, default=1.0)
    r.add_argument("--markdown", action="store_true")
    args = p.parse_args()
    if os.geteuid() != 0:
        sys.exit("À lancer en root (sudo).")
    if args.cmd == "collect":
        collect()
        return 0
    return report(args.hours, args.markdown)


if __name__ == "__main__":
    sys.exit(main())
