// Sentinel-X — firmware du boîtier ESP8266.
//
// - Lit DHT22 (température, humidité), MQ-2 (gaz, brut 0-1023) et PIR toutes les 2 s.
// - Publie la télémétrie en MQTTS (TLS 1.2, certificat du broker vérifié par notre CA)
//   toutes les 5 s, et immédiatement à chaque changement du PIR.
// - Exécute les commandes du dashboard (buzzer, LED) reçues sur sentinel/cmd/<serie>.
// - Affiche l'état sur l'OLED (IP, liaison, mesures).
// - Mode secours (edge) : sans broker depuis 30 s, alerte locale sur mouvement ou
//   montée brusque du gaz par rapport à SA PROPRE ligne de base (pas de seuil absolu).
#include <Arduino.h>
#include <ArduinoJson.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <DHT.h>
#include <ESP8266WiFi.h>
#include <PubSubClient.h>
#include <WiFiClientSecure.h>
#include <Wire.h>
#include <time.h>

#include "ca_cert.h"
#include "config.h"
#include "secrets.h"

// Heure de compilation, injectée par build_epoch.py : repli si le NTP est injoignable.
// BearSSL vérifie que le certificat du broker est « valide maintenant » : cette date doit être
// postérieure à l'émission des certificats (CA créée le 5 oct. 2026 vers 14 h 42 UTC).
#ifndef BUILD_EPOCH
#error "BUILD_EPOCH absent : compiler avec PlatformIO (extra_scripts = pre:build_epoch.py)"
#endif

DHT dht(PIN_DHT, DHT22);
Adafruit_SSD1306 oled(128, 64, &Wire, -1);
BearSSL::WiFiClientSecure tls;
BearSSL::X509List trustAnchor(CA_CERT);
PubSubClient mqtt(tls);

struct Readings {
  float temp = NAN, hum = NAN;
  int gas = -1;
  bool pir = false;
} r;

bool oledOk = false;
bool timeOk = false;
unsigned long bootMs, lastSensor = 0, lastPublish = 0, lastMqttOk = 0, lastReconnect = 0;
unsigned long buzzerUntil = 0;
enum LedMode { LED_OFF, LED_ON, LED_BLINK };
LedMode redMode = LED_OFF, greenMode = LED_OFF;
float gasBaseline = -1;  // moyenne glissante lente du MQ-2 (mode secours)
String lastCmd = "-";
// Une commande LED du dashboard prend la main pendant LED_MANUAL_HOLD_MS, puis le mode
// automatique (témoin de liaison, mode secours) reprend : la déconnexion reste toujours signalée.
unsigned long greenManualUntil = 0, redManualUntil = 0;

// ---------------------------------------------------------------- actionneurs
void applyLeds() {
  bool blinkPhase = (millis() / 250) % 2;
  digitalWrite(PIN_LED_RED, redMode == LED_ON || (redMode == LED_BLINK && blinkPhase));
  digitalWrite(PIN_LED_GREEN, greenMode == LED_ON || (greenMode == LED_BLINK && blinkPhase));
  digitalWrite(PIN_BUZZER, millis() < buzzerUntil ? HIGH : LOW);
}

void beep(unsigned long ms) { buzzerUntil = millis() + min(ms, 10000UL); }  // 10 s max par commande

LedMode parseLedMode(const char *etat) {
  if (!strcmp(etat, "on")) return LED_ON;
  if (!strcmp(etat, "clignote")) return LED_BLINK;
  return LED_OFF;
}

// Commandes du dashboard : {"actionneur":"buzzer","etat":"on","duree_ms":3000}
//                          {"actionneur":"led","couleur":"rouge"|"vert","etat":"on"|"off"|"clignote"}
void onCommand(char *topic, byte *payload, unsigned int len) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return;  // JSON invalide : ignoré
  const char *act = doc["actionneur"] | "";
  const char *etat = doc["etat"] | "off";
  if (!strcmp(act, "buzzer")) {
    if (!strcmp(etat, "on")) beep(doc["duree_ms"] | 2000);
    else buzzerUntil = 0;
    lastCmd = String("buzzer ") + etat;
  } else if (!strcmp(act, "led")) {
    const char *couleur = doc["couleur"] | "";
    if (!strcmp(couleur, "rouge")) { redMode = parseLedMode(etat); redManualUntil = millis() + LED_MANUAL_HOLD_MS; }
    else if (!strcmp(couleur, "vert")) { greenMode = parseLedMode(etat); greenManualUntil = millis() + LED_MANUAL_HOLD_MS; }
    else return;
    lastCmd = String("led ") + couleur + " " + etat;
  }
  Serial.printf("[CMD] %s\n", lastCmd.c_str());
}

// ---------------------------------------------------------------- capteurs
void readSensors() {
  float t = dht.readTemperature(), h = dht.readHumidity();
  r.temp = (isnan(t) || t < -40 || t > 80) ? NAN : t;   // hors bornes = capteur en défaut -> null
  r.hum = (isnan(h) || h < 0 || h > 100) ? NAN : h;
  long sum = 0;  // moyenne de 5 lectures : l'ADC de l'ESP8266 est bruité
  for (int i = 0; i < 5; i++) { sum += analogRead(PIN_MQ2); delay(2); }
  int gas = sum / 5;
  // -1 = pas de valeur fiable (envoyé en null) : préchauffe, ou valeur bloquée (fil coupé / court-circuit).
  // Sinon la maintenance prédictive prendrait un fil arraché pour une fuite de gaz.
  bool warming = millis() - bootMs < MQ2_WARMUP_MS;
  bool fault = gas <= GAS_FAULT_LOW || gas >= GAS_FAULT_HIGH;
  r.gas = (warming || fault) ? -1 : gas;
  if (r.gas >= 0)
    gasBaseline = gasBaseline < 0 ? r.gas : 0.98f * gasBaseline + 0.02f * r.gas;
}

bool publishTelemetry() {
  JsonDocument doc;
  doc["serie"] = SERIE;
  if (isnan(r.temp)) doc["temperature_c"] = nullptr; else doc["temperature_c"] = roundf(r.temp * 10) / 10;
  if (isnan(r.hum)) doc["humidite_pct"] = nullptr; else doc["humidite_pct"] = roundf(r.hum * 10) / 10;
  if (r.gas < 0) doc["gaz_brut"] = nullptr; else doc["gaz_brut"] = r.gas;
  doc["pir"] = r.pir;
  char buf[192];
  size_t n = serializeJson(doc, buf);
  return mqtt.publish(TOPIC_TELEMETRY, (const uint8_t *)buf, n, false);
}

// ---------------------------------------------------------------- réseau
void syncTime() {
  configTime(0, 0, "pool.ntp.org", "time.google.com");
  unsigned long start = millis();
  while (time(nullptr) < 1700000000 && millis() - start < 15000) delay(200);
  timeOk = time(nullptr) >= 1700000000;
  // Sans NTP, BearSSL vérifie la validité du certificat à la date de compilation.
  tls.setX509Time(timeOk ? time(nullptr) : BUILD_EPOCH);
  Serial.printf("[NTP] %s\n", timeOk ? "heure synchronisée" : "échec : date de compilation utilisée");
}

bool connectMqtt() {
  if (WiFi.status() != WL_CONNECTED) return false;
  if (!timeOk && millis() - lastReconnect > 60000) syncTime();
  Serial.printf("[MQTT] connexion à %s:%d ... ", MQTT_HOST, MQTT_PORT);
  bool ok = mqtt.connect(SERIE, MQTT_USER, MQTT_PASS);
  if (ok) {
    mqtt.subscribe(TOPIC_CMD, 1);
    Serial.println("OK");
  } else {
    char err[80];
    tls.getLastSSLError(err, sizeof(err));
    Serial.printf("échec (état %d, TLS : %s)\n", mqtt.state(), err);
  }
  return ok;
}

// ---------------------------------------------------------------- affichage
void drawOled(bool online) {
  if (!oledOk) return;
  oled.clearDisplay();
  oled.setTextSize(1);
  oled.setTextColor(SSD1306_WHITE);
  oled.setCursor(0, 0);
  oled.printf("SENTINEL-X %s\n", SERIE);
  oled.printf("IP %s\n", WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString().c_str() : "--");
  oled.printf("MQTTS %s\n", online ? "OK" : (millis() - lastMqttOk > LOCAL_FALLBACK_AFTER_MS ? "SECOURS" : "..."));
  if (isnan(r.temp)) oled.print("T --.-C "); else oled.printf("T %.1fC ", r.temp);
  if (isnan(r.hum)) oled.println("H --%"); else oled.printf("H %.0f%%\n", r.hum);
  if (r.gas >= 0) oled.printf("Gaz %d\n", r.gas);
  else oled.println(millis() - bootMs < MQ2_WARMUP_MS ? "Gaz -- (prechauf.)" : "Gaz -- (defaut)");
  oled.printf("PIR %s\n", r.pir ? "MOUVEMENT" : "calme");
  oled.printf("Cmd %s\n", lastCmd.c_str());
  oled.display();
}

// ---------------------------------------------------------------- programme
void setup() {
  Serial.begin(115200);
  delay(200);
  bootMs = millis();
  pinMode(PIN_PIR, INPUT);
  pinMode(PIN_LED_RED, OUTPUT);
  pinMode(PIN_LED_GREEN, OUTPUT);
  pinMode(PIN_BUZZER, OUTPUT);
  digitalWrite(PIN_BUZZER, LOW);

  Serial.printf("\n[SENTINEL-X] %s | Chip ID %08X | MAC %s\n", SERIE, ESP.getChipId(), WiFi.macAddress().c_str());

  Wire.begin(D2, D1);
  oledOk = oled.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR);
  if (!oledOk) Serial.println("[OLED] absent à 0x3C (on continue sans écran)");
  dht.begin();

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  greenMode = LED_BLINK;
  Serial.printf("[WIFI] connexion à %s ...\n", WIFI_SSID);
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) { applyLeds(); delay(100); }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[WIFI] OK, IP %s\n", WiFi.localIP().toString().c_str());
    syncTime();
  } else {
    Serial.println("[WIFI] échec, nouvel essai en arrière-plan");
  }

  tls.setTrustAnchors(&trustAnchor);
  // Petits tampons TLS si le broker accepte la négociation de taille de fragment (économise ~20 Ko de RAM)
  if (tls.probeMaxFragmentLength(MQTT_HOST, MQTT_PORT, 1024)) tls.setBufferSizes(1024, 1024);
  // Connexion PAR IP : BearSSL ne sait pas vérifier une IP dans le SAN du certificat
  // (« Expected server name was not found in the chain »). En passant une IPAddress, la
  // correspondance de nom est sautée, mais la chaîne reste vérifiée : certificat signé par
  // NOTRE CA privée (qui n'a émis que nos serveurs) et valide à la date courante.
  IPAddress brokerIp;
  brokerIp.fromString(MQTT_HOST);
  mqtt.setServer(brokerIp, MQTT_PORT);
  mqtt.setCallback(onCommand);
  mqtt.setBufferSize(512);
  mqtt.setKeepAlive(30);
  lastMqttOk = millis();
}

void loop() {
  unsigned long now = millis();
  bool online = mqtt.connected();

  if (!online && now - lastReconnect > 5000) {
    lastReconnect = now;
    online = connectMqtt();
  }
  if (online) {
    mqtt.loop();
    lastMqttOk = now;
  }

  bool pirChanged = false;
  if (now - lastSensor >= SENSOR_PERIOD_MS) {
    lastSensor = now;
    readSensors();
  }
  // Calibration du HC-SR501 pendant sa première minute : faux mouvements ignorés
  // (sinon fausse alerte FUSION au démarrage du boîtier, devant le jury).
  bool pir = (now - bootMs >= PIR_WARMUP_MS) && digitalRead(PIN_PIR) == HIGH;
  if (pir != r.pir) { r.pir = pir; pirChanged = true; }

  if (online && (pirChanged || now - lastPublish >= TELEMETRY_PERIOD_MS)) {
    if (publishTelemetry()) lastPublish = now;
  }

  // LED verte : fixe si relié au broker, clignotante sinon (sauf si le dashboard la pilote)
  if (now >= greenManualUntil) greenMode = online ? LED_ON : LED_BLINK;

  // Mode secours : broker perdu depuis 30 s -> le boîtier se défend seul
  if (!online && now - lastMqttOk > LOCAL_FALLBACK_AFTER_MS) {
    bool gasSpike = gasBaseline > 0 && r.gas > gasBaseline * 1.5f + 50;
    if (r.pir || gasSpike) { redMode = LED_BLINK; if (pirChanged || gasSpike) beep(800); }
    else if (now >= redManualUntil) redMode = LED_OFF;
  }

  applyLeds();
  static unsigned long lastDraw = 0;
  if (now - lastDraw > 500) { lastDraw = now; drawOled(online); }
  delay(10);
}
