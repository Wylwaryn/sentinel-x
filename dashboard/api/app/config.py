"""Configuration lue dans l'environnement (fourni par docker compose depuis le .env)."""
import os
from dataclasses import dataclass


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"Variable d'environnement {name} manquante")
    return value


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
    jwt_secret: str
    session_hours: float
    cookie_secure: bool
    allowed_origins: frozenset[str]   # vide = Origin non vérifié (dev uniquement)
    service_vision_ips: frozenset[str]  # IP réelles autorisées pour le compte SERVICE_VISION
    captures_dir: str
    references_dir: str
    max_image_bytes: int


def load() -> Settings:
    jwt_secret = _required("DASHBOARD_JWT_SECRET")
    if len(jwt_secret) < 32:
        raise RuntimeError("DASHBOARD_JWT_SECRET trop court (32 caractères minimum)")
    origins = os.environ.get("DASHBOARD_ORIGINS", "")
    # Dans la VM, seul le PC hôte arrive en 10.0.2.2 (passerelle NAT VirtualBox) ; le Wi-Fi garde ses vraies IP.
    service_ips = os.environ.get("SERVICE_VISION_IPS", "10.0.2.2")

    return Settings(
        db_host=os.environ.get("DB_HOST", "postgres"),
        db_name=_required("DB_NAME"),
        db_user=os.environ.get("DB_USER", "sentinel_dashboard"),
        db_password=_required("DB_PASSWORD"),
        mqtt_host=os.environ.get("MQTT_HOST", "mosquitto"),
        mqtt_port=int(os.environ.get("MQTT_PORT", "8883")),
        mqtt_user=os.environ.get("MQTT_USER", "dashboard"),
        mqtt_password=_required("MQTT_PASSWORD"),
        ca_file=os.environ.get("CA_FILE", "/certs/ca.crt"),
        jwt_secret=jwt_secret,
        session_hours=float(os.environ.get("SESSION_HOURS", "8")),
        # Cookie « Secure » : envoyé uniquement en HTTPS (Caddy). Désactivable pour le dev en HTTP.
        cookie_secure=os.environ.get("COOKIE_SECURE", "true").lower() != "false",
        allowed_origins=frozenset(o.strip() for o in origins.split(",") if o.strip()),
        service_vision_ips=frozenset(i.strip() for i in service_ips.split(",") if i.strip()),
        captures_dir=os.environ.get("CAPTURES_DIR", "/data/captures"),
        references_dir=os.environ.get("REFERENCES_DIR", "/data/references"),
        max_image_bytes=int(os.environ.get("MAX_IMAGE_BYTES", str(2 * 1024 * 1024))),
    )
