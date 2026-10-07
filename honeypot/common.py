"""Fonctions partagees du honeypot : journal JSONL append-only, horodatage UTC.
(Explications detaillees : voir README.md)"""
import json
import os
import threading
from datetime import datetime, timezone

# Un seul verrou process-wide : plusieurs connexions ecrivent dans le meme fichier sans s'entremeler.
_lock = threading.Lock()


def log_dir():
    # LOG_DIR surcharge le defaut ; permet de pointer vers un dossier partage VirtualBox (persistant).
    return os.environ.get("LOG_DIR", "/var/log/honeypot")


def log_path():
    return os.path.join(log_dir(), "honeypot.jsonl")


def now_iso():
    # UTC, a la seconde, suffixe Z : lisible et triable, meme fuseau que le reste du projet.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record(event: dict):
    """Ajoute une ligne JSON au journal. N'echoue jamais bruyamment : un honeypot ne doit pas tomber
    parce que le disque ou le dossier partage a un souci."""
    event.setdefault("ts", now_iso())
    line = json.dumps(event, ensure_ascii=False)
    try:
        with _lock:
            with open(log_path(), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass
    # Copie sur stdout : visible dans `journalctl -u honeypot-*` meme si le fichier est indisponible.
    print(line, flush=True)


def safe_text(data: bytes, limit: int = 300) -> str:
    """Rend lisibles les octets recus : les imprimables tels quels, le reste en '.' (comme tcpdump)."""
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data[:limit])
