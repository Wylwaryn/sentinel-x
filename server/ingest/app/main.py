"""API d'ingestion Sentinel-X : écriture seule (mesures, alertes), aucune lecture exposée.

Entrées :
  - MQTTS sentinel/telemetry (ESP8266)        -> mesure
  - POST /api/v1/alerts (vision, IDS, jeton)  -> alerte
  - détection interne DISPOSITIF_HORS_LIGNE   -> alerte
"""
import asyncio
import base64
import binascii
import logging
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from psycopg import errors as pg_errors
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import config
from .config import CLIENT_ORIGINS
from .db import Database
from .models import AlertIn, translate_niveau, translate_origine, translate_type
from .mqtt_worker import TelemetryWorker
from .security import ApiGuard
from .watchdog import OfflineWatchdog

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s")
log = logging.getLogger("ingest")

settings = config.load()
db = Database(settings)
telemetry = TelemetryWorker(settings, db)
watchdog = OfflineWatchdog(settings, db)

MAX_SNAPSHOT_BYTES = 1024 * 1024


@asynccontextmanager
async def lifespan(_app):
    await db.open()
    tasks = [asyncio.create_task(telemetry.run(), name="mqtt"),
             asyncio.create_task(watchdog.run(), name="watchdog")]
    log.info("API d'ingestion prête (clients : %s)", ", ".join(sorted(settings.tokens)))
    yield
    for task in tasks:
        task.cancel()
    await db.close()


# Pas de /docs ni d'/openapi.json : rien à cartographier pour les attaquants de jeudi.
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(RequestValidationError)
async def validation_error(_request, exc: RequestValidationError):
    # Pas d'écho des valeurs reçues (une image base64 renvoyée en entier, du contenu hostile...).
    errors = [{"champ": ".".join(str(p) for p in e["loc"][1:]), "erreur": e["msg"]} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


def _error(status: int, detail: str):
    return JSONResponse(status_code=status, content={"detail": detail})


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/api/v1/alerts", status_code=201)
async def create_alert(alert: AlertIn, request: Request):
    client = request.state.client

    origine = translate_origine(alert.source)
    if origine is None:
        return _error(422, "source inconnue")
    if origine not in CLIENT_ORIGINS[client]:
        log.warning("client %s : origine %s interdite", client, origine)
        return _error(403, f"origine {origine} interdite pour ce client")
    type_alerte = translate_type(alert.type, origine)
    if type_alerte is None:
        return _error(422, f"type incompatible avec l'origine {origine}")
    niveau = translate_niveau(alert.level)
    if niveau is None:
        return _error(422, "niveau inconnu")

    serie = alert.serie or (settings.default_serie if origine != "RESEAU_IA" else None)
    id_dispositif = None
    if serie:
        id_dispositif = await db.device_id(serie)
        if id_dispositif is None:
            return _error(422, "dispositif inconnu")
    elif origine != "RESEAU_IA":
        return _error(422, "dispositif requis (champ serie ou INGEST_DEFAULT_SERIE)")

    if alert.personne_reconnue is not None and origine not in ("VISION_IA", "FUSION"):
        return _error(422, "personne_reconnue réservée aux alertes caméra")

    chemin_capture = None
    if alert.snapshot_jpeg_b64:
        if origine not in ("VISION_IA", "FUSION"):
            return _error(422, "capture réservée aux alertes caméra")
        try:
            jpeg = base64.b64decode(alert.snapshot_jpeg_b64, validate=True)
        except (binascii.Error, ValueError):
            return _error(422, "capture : base64 invalide")
        if len(jpeg) > MAX_SNAPSHOT_BYTES or not jpeg.startswith(b"\xff\xd8\xff"):
            return _error(422, "capture : JPEG invalide ou trop volumineux")
        # Nom choisi par le serveur : aucun chemin fourni par le client (pas de traversée).
        name = f"{uuid.uuid4().hex}.jpg"
        path = os.path.join(settings.captures_dir, name)
        await asyncio.to_thread(_write_file, path, jpeg)
        chemin_capture = f"captures/{name}"

    try:
        id_alerte = await db.insert_alerte(
            id_dispositif=id_dispositif, type_alerte=type_alerte, origine=origine, niveau=niveau,
            message=alert.build_message(type_alerte),
            ip_source=str(alert.ip_source) if alert.ip_source else None,
            score_ia=alert.score, zone=alert.zone, pir_confirme=alert.pir_confirmed,
            chemin_capture=chemin_capture, id_personne_reconnue=alert.personne_reconnue)
    except Exception as exc:
        if chemin_capture:
            os.unlink(os.path.join(settings.captures_dir, chemin_capture.split("/", 1)[1]))
        if (isinstance(exc, pg_errors.ForeignKeyViolation)
                and exc.diag.constraint_name == "fk_alerte_id_personne_reconnue"):
            return _error(422, "personne_reconnue inconnue")
        log.exception("échec d'écriture de l'alerte")
        return _error(500, "erreur interne")

    log.info("alerte %s : %s/%s/%s (client %s%s%s)", id_alerte, origine, type_alerte, niveau, client,
             f", action {alert.action}" if alert.action else "",
             f", personne reconnue {alert.personne_reconnue}" if alert.personne_reconnue else "")
    return {"id_alerte": id_alerte}


def _write_file(path: str, data: bytes):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


# Garde en dernier : elle enveloppe toute l'application.
app = ApiGuard(app, settings.tokens, settings.max_body_bytes)
