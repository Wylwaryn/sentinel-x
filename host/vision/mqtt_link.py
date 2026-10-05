"""Liaison MQTTS unique de la vision (client_id sentinel-vision) :
- reçoit la télémétrie de l'ESP surveillé (sentinel/telemetry) pour la fusion PIR ;
- publie le flux annoté pour le dashboard (sentinel/video/cam1, JPEG brut, ~5 images/s).

Une seule connexion : deux connexions avec le même client_id se déconnecteraient mutuellement.
Connexion SORTANTE uniquement (Windows -> VM) : aucun port ouvert sur l'hôte.
Le certificat du broker est vérifié avec notre CA ; identifiants lus dans l'environnement.
"""
import json
import os
import time

import cv2
import paho.mqtt.client as mqtt


def parse_telemetry(payload, watched_serie):
    """Retourne l'état PIR (bool) si le message vient de l'ESP surveillé, sinon None.
    Format (cf. server/ingest/app/models.py) : {"serie": "...", "pir": true, ...}."""
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("pir"), bool):
        return None
    if watched_serie and data.get("serie") != watched_serie:
        return None
    return data["pir"]


class MqttLink:
    def __init__(self, mqtt_cfg, fusion, watched_serie, base_dir):
        self.cfg = mqtt_cfg
        self.fusion = fusion
        self.watched_serie = watched_serie
        self.connected = False
        self.last_pir = None
        self._last_frame = 0.0
        self._frame_interval = 1.0 / mqtt_cfg["video_fps"]
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=mqtt_cfg["client_id"])
        self.client.tls_set(ca_certs=os.path.join(base_dir, mqtt_cfg["ca_cert"]))
        user = os.environ.get(mqtt_cfg["username_env"])
        password = os.environ.get(mqtt_cfg["password_env"])
        if not user or not password:
            raise RuntimeError(f"Variables {mqtt_cfg['username_env']} / {mqtt_cfg['password_env']} manquantes")
        self.client.username_pw_set(user, password)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_subscribe = self._on_subscribe
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=10)

    def start(self):
        self.client.connect_async(self.cfg["host"], self.cfg["port"], keepalive=30)
        self.client.loop_start()

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()

    def publish_frame(self, frame):
        """Publie au plus video_fps images/s ; ne bloque jamais la boucle vidéo."""
        now = time.monotonic()
        if not self.connected or now - self._last_frame < self._frame_interval:
            return False
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.cfg["video_quality"]])
        if not ok:
            return False
        self._last_frame = now
        # QoS 0, non retenu : une image perdue est remplacée 200 ms plus tard
        self.client.publish(self.cfg["video_topic"], buf.tobytes(), qos=0, retain=False)
        return True

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        self.connected = not reason_code.is_failure
        if self.connected:
            client.subscribe(self.cfg["telemetry_topic"], qos=1)
        else:
            print(f"[MQTT] connexion refusée : {reason_code}")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected = False

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties):
        for rc in reason_codes:
            if rc.is_failure:
                print(f"[MQTT] abonnement refusé par le broker (ACL ?) : {rc}")

    def _on_message(self, client, userdata, msg):
        pir = parse_telemetry(msg.payload, self.watched_serie)
        if pir is None:
            return
        self.last_pir = pir
        self.fusion.on_pir(pir, time.monotonic())
