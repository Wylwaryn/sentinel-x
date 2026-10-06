// Sentinel-X — paramètres du boîtier (aucun secret ici : voir secrets.h).
#pragma once

// Identité : doit exister dans dispositif.numero_serie (sinon l'API ignore les mesures).
#define SERIE "SX-G2-01"

// Broker MQTTS : point d'accès Windows de la table (SAN du certificat Mosquitto).
#define MQTT_HOST "192.168.137.1"
#define MQTT_PORT 8883
#define MQTT_USER "esp"
#define TOPIC_TELEMETRY "sentinel/telemetry"
#define TOPIC_CMD "sentinel/cmd/" SERIE

// Cadences
#define TELEMETRY_PERIOD_MS 5000UL   // l'API passe l'ESP hors ligne après 30 s sans mesure
#define SENSOR_PERIOD_MS 2000UL      // le DHT22 ne supporte pas plus d'une lecture toutes les 2 s
#define MQ2_WARMUP_MS 120000UL       // préchauffe du capteur de gaz : "gaz_brut": null pendant ce temps
#define PIR_WARMUP_MS 60000UL        // calibration du HC-SR501 : faux mouvements ignorés (proposition IoT)
#define GAS_FAULT_LOW 2              // valeur bloquée en bas (fil coupé) -> null, pas une « fuite »
#define GAS_FAULT_HIGH 1021          // valeur bloquée en haut (court-circuit) -> null
#define LED_MANUAL_HOLD_MS 60000UL   // après une commande LED du dashboard, retour au mode auto
#define LED_MAX_HOLD_MS 1800000UL    // durée max d'une commande LED avec "duree_ms" (30 min)

// Mode secours : sans broker depuis ce délai, l'ESP alerte localement (buzzer, LED rouge).
#define LOCAL_FALLBACK_AFTER_MS 30000UL

// Brochage (NodeMCU v3). D3, D4 et D8 conditionnent le démarrage : seul D8 est utilisé (buzzer,
// niveau bas au repos, compatible). Ne PAS tirer D8 au +3V3.
#define PIN_DHT D5          // DHT22 (data, résistance de tirage 10k si module nu)
#define PIN_PIR D6          // HC-SR501 OUT (sortie 3,3 V, alimentation 5 V)
#define PIN_LED_RED D7      // LED bicolore, cathode commune, résistance 220 ohms
#define PIN_LED_GREEN D0    // idem
#define PIN_BUZZER D8       // buzzer actif (via transistor si > 12 mA)
#define PIN_MQ2 A0          // MQ-2 AO via pont diviseur (5 V -> 3,3 V max)
// OLED I2C 0x3C : SDA = D2, SCL = D1 (bus I2C par défaut du NodeMCU)
#define OLED_ADDR 0x3C
