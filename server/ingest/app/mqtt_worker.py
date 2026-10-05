"""Abonnement MQTTS à sentinel/telemetry -> table mesure.

Un message invalide est journalisé puis ignoré : il ne doit jamais arrêter la boucle.
"""
import asyncio
import json
import logging
import ssl
import time

import aiomqtt
from pydantic import ValidationError

from .models import Telemetry

log = logging.getLogger("ingest.mqtt")

TOPIC = "sentinel/telemetry"
MAX_PAYLOAD = 2048  # un message de télémétrie fait environ 100 octets


class TelemetryWorker:
    def __init__(self, settings, db):
        self.settings = settings
        self.db = db
        self.connected = False
        self._last_seen: dict[int, float] = {}

    async def run(self):
        tls = ssl.create_default_context(cafile=self.settings.ca_file)
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self.settings.mqtt_host,
                    port=self.settings.mqtt_port,
                    username=self.settings.mqtt_user,
                    password=self.settings.mqtt_password,
                    identifier="sentinel-ingest",
                    protocol=aiomqtt.ProtocolVersion.V5,
                    tls_context=tls,
                    keepalive=30,
                ) as client:
                    await client.subscribe(TOPIC, qos=1)
                    self.connected = True
                    log.info("connecté à %s:%s, abonné à %s",
                             self.settings.mqtt_host, self.settings.mqtt_port, TOPIC)
                    async for message in client.messages:
                        await self.handle(message.payload)
            except aiomqtt.MqttError as exc:
                self.connected = False
                log.warning("MQTT indisponible (%s), nouvelle tentative dans 3 s", exc)
                await asyncio.sleep(3)

    async def handle(self, payload: bytes):
        if not isinstance(payload, (bytes, bytearray)) or len(payload) > MAX_PAYLOAD:
            log.warning("télémétrie rejetée : charge absente ou trop grande")
            return
        try:
            data = Telemetry.model_validate(json.loads(payload))
        except (ValueError, ValidationError) as exc:
            log.warning("télémétrie invalide ignorée : %r (%s)", bytes(payload[:120]), type(exc).__name__)
            return
        try:
            device = await self.db.device_id(data.serie)
            if device is None:
                log.warning("numéro de série inconnu ignoré : %r", data.serie)
                return
            # Anti-rafale : un ESP qui boucle ne doit pas remplir la base.
            now = time.monotonic()
            if now - self._last_seen.get(device, 0.0) < self.settings.telemetry_min_interval_s:
                return
            self._last_seen[device] = now
            id_mesure = await self.db.insert_mesure(device, data)
            log.debug("mesure %s enregistrée (dispositif %s)", id_mesure, device)
        except Exception:
            log.exception("échec d'écriture de la mesure")
