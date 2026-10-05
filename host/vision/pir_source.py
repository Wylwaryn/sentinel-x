"""Abonnement MQTTS au flux de l'ESP8266 pour récupérer l'état du PIR.

Connexion SORTANTE uniquement (Windows -> VM via la redirection de port 8883) :
aucun port n'est ouvert sur l'hôte. Le certificat du broker est vérifié avec notre CA.
Identifiants lus dans les variables d'environnement, jamais dans le code.
"""
import json
import os
import time

import paho.mqtt.client as mqtt


class PirSource:
    def __init__(self, pir_cfg, fusion, base_dir):
        self.cfg = pir_cfg
        self.fusion = fusion
        self.connected = False
        self.last_value = None
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sentinel-vision")
        self.client.tls_set(ca_certs=os.path.join(base_dir, pir_cfg["ca_cert"]))
        user = os.environ.get(pir_cfg["username_env"])
        if user:
            self.client.username_pw_set(user, os.environ.get(pir_cfg["password_env"]))
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=10)

    def start(self):
        self.client.connect_async(self.cfg["host"], self.cfg["port"], keepalive=30)
        self.client.loop_start()

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        self.connected = not reason_code.is_failure
        if self.connected:
            client.subscribe(self.cfg["topic"], qos=1)
        else:
            print(f"[PIR] connexion refusée : {reason_code}")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected = False

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload)
            value = bool(payload[self.cfg["field"]])
        except (ValueError, KeyError, TypeError):
            return  # message mal formé : ignoré (et visible côté IDS/API)
        self.last_value = value
        self.fusion.on_pir(value, time.monotonic())
