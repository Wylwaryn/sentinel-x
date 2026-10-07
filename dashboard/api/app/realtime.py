"""Temps réel : PostgreSQL (LISTEN/NOTIFY) et MQTT -> navigateurs (WebSocket). Aucun polling.

Messages envoyés au navigateur :
  texte   {"type": "mesure", "data": {...}}   ligne insérée dans mesure (+ "instant" ISO UTC)
  texte   {"type": "alerte", "data": {...}}   alerte physique créée ou mise à jour (+ "instant")
  texte   {"type": "resync"}                  événements peut-être manqués : recharger les listes
  binaire JPEG brut 640x480                    flux webcam annoté (sentinel/video/#)
"""
import asyncio
import json
import logging
import ssl

import aiomqtt
import psycopg

from .db import instant

log = logging.getLogger("dashboard.realtime")

CANAUX = ("sentinel_mesure", "sentinel_alerte")
VIDEO_TOPIC = "sentinel/video/#"
MAX_JPEG = 1024 * 1024          # même limite que max_packet_size de Mosquitto
FILE_MAX = 256                  # messages en attente par navigateur
VIDEO_MAX_EN_ATTENTE = 4        # au-delà, on saute des images plutôt que d'accumuler du retard


class Client:
    def __init__(self, ws):
        self.ws = ws
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=FILE_MAX)
        self.trop_lent = False

    def pousser(self, item, video: bool = False):
        if video and self.queue.qsize() >= VIDEO_MAX_EN_ATTENTE:
            return  # image sautée : le navigateur aura la suivante
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            # Un navigateur qui ne lit plus ne doit pas faire gonfler la mémoire de l'API.
            self.trop_lent = True


class Hub:
    def __init__(self, max_clients: int = 50):
        self.clients: set[Client] = set()
        self.max_clients = max_clients

    def plein(self) -> bool:
        return len(self.clients) >= self.max_clients

    def texte(self, message: dict):
        data = json.dumps(message, separators=(",", ":"))
        for c in list(self.clients):
            c.pousser(data)

    def video(self, jpeg: bytes):
        for c in list(self.clients):
            c.pousser(jpeg, video=True)

    async def servir(self, client: Client):
        """Envoie au navigateur tout ce que le hub lui destine, jusqu'à la déconnexion."""
        self.clients.add(client)
        try:
            while not client.trop_lent:
                item = await client.queue.get()
                if isinstance(item, bytes):
                    await client.ws.send_bytes(item)
                else:
                    await client.ws.send_text(item)
        finally:
            self.clients.discard(client)


def _avec_instant(data: dict) -> dict:
    if "date_mesure" in data:
        data["instant"] = instant(data["date_mesure"], data["heure_mesure"])
    elif "date_alerte" in data:
        data["instant"] = instant(data["date_alerte"], data["heure_alerte"])
    return data


class PgListener:
    """Connexion dédiée, en LISTEN sur sentinel_mesure et sentinel_alerte."""

    def __init__(self, conninfo: dict, hub: Hub):
        self.conninfo = conninfo
        self.hub = hub
        self.connected = False
        self.sur_alerte = None      # coroutine appelée pour chaque alerte notifiée (LED automatique)
        self._taches: set = set()   # références gardées : une tâche sans référence peut être détruite

    async def run(self):
        premiere = True
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(**self.conninfo, autocommit=True) as conn:
                    for canal in CANAUX:
                        await conn.execute(f"LISTEN {canal}")  # noms fixes, aucune donnée externe
                    self.connected = True
                    log.info("LISTEN %s", ", ".join(CANAUX))
                    if not premiere:
                        self.hub.texte({"type": "resync"})
                    premiere = False
                    async for n in conn.notifies():
                        self._relayer(n.channel, n.payload)
            except (psycopg.Error, OSError) as exc:
                self.connected = False
                log.warning("PostgreSQL indisponible (%s), nouvelle tentative dans 3 s", exc)
                await asyncio.sleep(3)

    def _relayer(self, canal: str, payload: str):
        try:
            data = json.loads(payload)
        except ValueError:
            log.warning("notification %s illisible ignorée", canal)
            return
        type_ = "mesure" if canal == "sentinel_mesure" else "alerte"
        self.hub.texte({"type": type_, "data": _avec_instant(data)})
        if type_ == "alerte" and self.sur_alerte is not None:
            # En tâche séparée : le relais vers les navigateurs ne doit jamais attendre MQTT ou la base.
            tache = asyncio.create_task(self.sur_alerte(data))
            self._taches.add(tache)
            tache.add_done_callback(self._taches.discard)


class MqttLink:
    """Compte MQTT « dashboard » : lit sentinel/video/#, publie sur sentinel/cmd/<numero_serie>."""

    def __init__(self, settings, hub: Hub):
        self.settings = settings
        self.hub = hub
        self.client: aiomqtt.Client | None = None

    @property
    def connected(self) -> bool:
        return self.client is not None

    async def run(self):
        tls = ssl.create_default_context(cafile=self.settings.ca_file)
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self.settings.mqtt_host,
                    port=self.settings.mqtt_port,
                    username=self.settings.mqtt_user,
                    password=self.settings.mqtt_password,
                    identifier="sentinel-dashboard",
                    protocol=aiomqtt.ProtocolVersion.V5,
                    tls_context=tls,
                    keepalive=30,
                ) as client:
                    await client.subscribe(VIDEO_TOPIC, qos=0)
                    self.client = client
                    log.info("connecté à %s:%s, abonné à %s",
                             self.settings.mqtt_host, self.settings.mqtt_port, VIDEO_TOPIC)
                    async for message in client.messages:
                        payload = message.payload
                        # Battement de présence de la vision (JSON) : pilote le verrou du PC hôte.
                        if str(message.topic).endswith("/presence"):
                            try:
                                self.hub.texte({"type": "presence", "data": json.loads(payload)})
                            except (ValueError, TypeError):
                                pass
                            continue
                        # Sinon : seulement des JPEG (octets magiques) de taille raisonnable.
                        if (isinstance(payload, (bytes, bytearray)) and len(payload) <= MAX_JPEG
                                and payload.startswith(b"\xff\xd8\xff")):
                            self.hub.video(bytes(payload))
            except aiomqtt.MqttError as exc:
                self.client = None
                log.warning("MQTT indisponible (%s), nouvelle tentative dans 3 s", exc)
                await asyncio.sleep(3)

    async def commande(self, numero_serie: str, commande: dict) -> bool:
        client = self.client
        if client is None:
            return False
        await client.publish(f"sentinel/cmd/{numero_serie}", json.dumps(commande), qos=1)
        return True
