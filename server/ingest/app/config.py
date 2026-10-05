"""Configuration lue dans l'environnement (fourni par docker compose depuis le .env)."""
import os
from dataclasses import dataclass


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"Variable d'environnement {name} manquante")
    return value


# Client de l'API (identifié par son jeton) -> origines d'alerte qu'il a le droit d'écrire.
CLIENT_ORIGINS = {
    "vision": frozenset({"VISION_IA", "FUSION", "PIR"}),
    "ids": frozenset({"RESEAU_IA"}),
    "capteurs": frozenset({"CAPTEURS_IA"}),
}


@dataclass(frozen=True)
class Settings:
    db_host: str
    db_name: str
    db_user: str
    db_password: str
    mqtt_host: str
    mqtt_port: int
    mqtt_user: str
    mqtt_password: str
    ca_file: str
    tokens: dict[str, str]          # client -> jeton (seuls les clients configurés)
    default_serie: str | None       # dispositif associé à la caméra si la vision ne le précise pas
    offline_after_s: float
    watchdog_interval_s: float
    telemetry_min_interval_s: float
    captures_dir: str
    max_body_bytes: int


def load() -> Settings:
    tokens = {}
    for client in CLIENT_ORIGINS:
        token = os.environ.get(f"INGEST_TOKEN_{client.upper()}", "")
        if token:
            if len(token) < 32:
                raise RuntimeError(f"INGEST_TOKEN_{client.upper()} trop court (32 caractères minimum)")
            tokens[client] = token
    if not tokens:
        raise RuntimeError("Aucun jeton INGEST_TOKEN_* configuré")

    return Settings(
        db_host=os.environ.get("DB_HOST", "postgres"),
        db_name=_required("DB_NAME"),
        db_user=os.environ.get("DB_USER", "sentinel_ingest"),
        db_password=_required("DB_PASSWORD"),
        mqtt_host=os.environ.get("MQTT_HOST", "mosquitto"),
        mqtt_port=int(os.environ.get("MQTT_PORT", "8883")),
        mqtt_user=os.environ.get("MQTT_USER", "ingest"),
        mqtt_password=_required("MQTT_PASSWORD"),
        ca_file=os.environ.get("CA_FILE", "/certs/ca.crt"),
        tokens=tokens,
        default_serie=os.environ.get("INGEST_DEFAULT_SERIE") or None,
        offline_after_s=float(os.environ.get("OFFLINE_AFTER_S", "30")),
        watchdog_interval_s=float(os.environ.get("WATCHDOG_INTERVAL_S", "5")),
        telemetry_min_interval_s=float(os.environ.get("TELEMETRY_MIN_INTERVAL_S", "0.2")),
        captures_dir=os.environ.get("CAPTURES_DIR", "/data/captures"),
        max_body_bytes=int(os.environ.get("MAX_BODY_BYTES", str(2 * 1024 * 1024))),
    )
