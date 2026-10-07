// Sentinel-X — firmware du boîtier ESP8266.
//
// - Lit DHT22 (température, humidité), MQ-2 (gaz, brut 0-1023) et PIR toutes les 2 s.
// - Publie la télémétrie en MQTTS (TLS 1.2, certificat du broker vérifié par notre CA)
//   toutes les 5 s, et immédiatement à chaque changement du PIR.
// - Exécute les commandes du dashboard (buzzer, LED) reçues sur sentinel/cmd/<serie>.
// - Affiche l'état sur l'OLED (IP, liaison, mesures).
// - Mode secours (edge) : sans broker depuis 30 s, alerte locale sur mouvement ou
//   montée brusque du gaz par rapport à SA PROPRE ligne de base (pas de seuil absolu).
// - IA embarquée (edgeAi) : « le PC apprend, l'ESP applique ». Paramètres appris par la maintenance
//   prédictive du PC (ml_params.h) ; toutes les 10 s, tendance sur 5 min et délai avant la borne critique
//   (surchauffe, risque d'incendie, qualité de l'air, humidité haute ou basse). Bandeau OLED en permanence ;
//   buzzer et LED rouge en mode secours. Jumeau Python validé sur la télémétrie réelle : host/predictive/edge.py.
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
#include "ml_params.h"
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
bool gasFault = false, gasRising = false;  // pour le bandeau d'alerte locale de l'OLED
bool timeOk = false;
unsigned long bootMs, lastSensor = 0, lastPublish = 0, lastMqttOk = 0, lastReconnect = 0;
unsigned long lastTimeSync = 0;  // 0 = heure jamais réglée pour BearSSL (Wi-Fi absent au démarrage)
unsigned long buzzerUntil = 0;
enum LedMode { LED_OFF, LED_ON, LED_BLINK };
LedMode redMode = LED_OFF, greenMode = LED_OFF;
float gasBaseline = -1;  // moyenne glissante lente du MQ-2 (mode secours)
String lastCmd = "-";
// Une commande LED du dashboard prend la main pendant LED_MANUAL_HOLD_MS, puis le mode
// automatique (témoin de liaison, mode secours) reprend : la déconnexion reste toujours signalée.
unsigned long greenManualUntil = 0, redManualUntil = 0;

// ---------------------------------------------------------------- IA embarquée
// Même logique que host/predictive/edge.py (validé : 0 fausse alerte sur un jour jamais vu, type juste 46/47).
#define AI_N 30                 // tampon circulaire : 30 x 10 s = 5 min
#define AI_STEP_MS 10000UL
#define AI_MIN_POINTS 12        // 2 min de mesures valides avant de parler de tendance
#define AI_HORIZON_WARN 30.0f   // borne critique prévue dans 30 min -> avertissement
#define AI_HORIZON_CRIT 10.0f   // ... dans 10 min -> critique
#define AI_STAB_MS 300000UL     // stabilisation du MQ-2 après sa préchauffe
#define AI_FIRE_GAS_RATIO 0.25f // gaz qui dérive (fraction de la pente normale max) ...
#define AI_FIRE_GAS_DELTA 15.0f // ... ou au-dessus de la ligne de base de cette marge
#define AI_FIRE_TEMP_RATIO 0.25f// ... avec une température qui monte : risque d'incendie

enum AiKind { AI_NONE, AI_RISQUE_FEU, AI_SURCHAUFFE, AI_GAZ, AI_HUM_HAUTE, AI_AIR_SEC };  // ordre = priorité
enum AiLevel { AI_OK = 0, AI_WARN = 1, AI_CRIT = 2 };
const char *const AI_LABEL[] = {"", "RISQUE FEU", "SURCHAUFFE", "QUALITE AIR", "HUMIDITE+", "AIR SEC"};
const char *const AI_LEVEL_TXT[] = {"OK", "AVERTISSEMENT", "CRITIQUE"};
struct AiCand { AiKind kind; AiLevel level; float minutes; };

float aiT[AI_N], aiH[AI_N], aiG[AI_N];   // NAN = pas de mesure
unsigned long aiTs[AI_N];
int aiCount = 0, aiHead = 0;             // aiHead : prochaine case à écrire
int aiStreak = 0;
unsigned long lastAi = 0, gasLowMs = 0;
bool gasLowSeen = false;
AiKind aiKind = AI_NONE;
AiLevel aiLevel = AI_OK;
float aiMinutes = NAN;

int aiIndex(int k) { return (aiHead - aiCount + k + AI_N) % AI_N; }  // k = 0 : plus ancienne mesure

float aiLastValid(const float *v) {
  for (int k = aiCount - 1; k >= 0; k--) if (!isnan(v[aiIndex(k)])) return v[aiIndex(k)];
  return NAN;
}

// Pente des moindres carrés, en unités par minute, sur les `last` dernières mesures (toutes : 5 min).
// NAN si trop peu de points.
float aiSlope(const float *v, int last = AI_N) {
  unsigned long newest = aiTs[aiIndex(aiCount - 1)];
  float ts[AI_N], vs[AI_N];
  int n = 0;
  for (int k = max(0, aiCount - last); k < aiCount; k++) {
    int i = aiIndex(k);
    if (isnan(v[i])) continue;
    ts[n] = -(float)(newest - aiTs[i]) / 1000.0f;  // secondes, relatives à la mesure la plus récente
    vs[n++] = v[i];
  }
  if (n < AI_MIN_POINTS || ts[n - 1] - ts[0] < (AI_MIN_POINTS - 1) * (AI_STEP_MS / 1000.0f)) return NAN;
  float mt = 0, mv = 0;
  for (int k = 0; k < n; k++) { mt += ts[k]; mv += vs[k]; }
  mt /= n; mv /= n;
  float num = 0, den = 0;
  for (int k = 0; k < n; k++) { num += (ts[k] - mt) * (vs[k] - mv); den += (ts[k] - mt) * (ts[k] - mt); }
  return den > 0 ? num / den * 60.0f : NAN;
}

// Pente qui chiffre le délai : la pente sur 5 min décide s'il y a une tendance ; en début de montée elle
// mélange encore du « plat », donc la pente des 2 dernières minutes, si elle est plus raide, donne le délai.
float aiEtaSlope(float slope, float recent, int sign) {
  return (!isnan(recent) && !isnan(slope) && sign * recent > sign * slope) ? recent : slope;
}

// Minutes avant la borne critique si la tendance va vers elle (sign +1 : borne haute, -1 : basse).
float aiMinutesTo(float last, float crit, float slope, int sign) {
  if (isnan(last) || isnan(slope) || sign * slope <= 0) return NAN;
  return max(0.0f, sign * (crit - last) / slope);
}

// MQ-2 exploitable ? Préchauffe (valeur absente ou sous ML_GAZ_PRECHAUFFE) puis stabilisation ; une vraie
// fuite (au-dessus de la borne d'avertissement) est toujours analysée.
bool aiGasOk(unsigned long now, int gas) {
  if (gas < 0 || gas < ML_GAZ_PRECHAUFFE) { gasLowMs = now; gasLowSeen = true; return false; }
  return !gasLowSeen || now - gasLowMs >= AI_STAB_MS || gas >= ML_GAZ_WARN;
}

void aiJudge(AiCand &best, AiKind kind, float value, float warn, float crit, int sign, bool trend, float slope,
             float recent) {
  float m = trend ? aiMinutesTo(value, crit, aiEtaSlope(slope, recent, sign), sign) : NAN;
  AiLevel lv = AI_OK;
  if ((!isnan(value) && sign * (value - crit) >= 0) || (!isnan(m) && m <= AI_HORIZON_CRIT)) lv = AI_CRIT;
  else if ((!isnan(value) && sign * (value - warn) >= 0) || (!isnan(m) && m <= AI_HORIZON_WARN)) lv = AI_WARN;
  if (lv == AI_OK) return;
  if (lv > best.level || (lv == best.level && kind < best.kind)) best = {kind, lv, m};
}

// Une évaluation toutes les 10 s : mesure dans le tampon, tendances, bornes, type ; persistance sur 2 évaluations.
void edgeAi(unsigned long now) {
  aiTs[aiHead] = now;
  aiT[aiHead] = r.temp;
  aiH[aiHead] = r.hum;
  aiG[aiHead] = r.gas < 0 ? NAN : (float)r.gas;
  aiHead = (aiHead + 1) % AI_N;
  if (aiCount < AI_N) aiCount++;

  float temp = aiLastValid(aiT), hum = aiLastValid(aiH), gaz = aiLastValid(aiG);
  bool gok = aiGasOk(now, r.gas);
  float st = aiSlope(aiT), sh = aiSlope(aiH), sg = gok ? aiSlope(aiG) : NAN;
  float rt = aiSlope(aiT, AI_MIN_POINTS), rh = aiSlope(aiH, AI_MIN_POINTS), rg = gok ? aiSlope(aiG, AI_MIN_POINTS) : NAN;
  bool tUp = !isnan(st) && st > ML_SLOPE_TEMP;
  bool gUp = !isnan(sg) && sg > ML_SLOPE_GAZ;
  bool hUp = !isnan(sh) && sh > ML_SLOPE_HUM;
  bool hDown = !isnan(sh) && sh < -ML_SLOPE_HUM;
  // Échauffement corrélé au gaz (signature apprise par le PC) : requalifié en risque d'incendie
  bool gasDrift = gok && ((!isnan(sg) && sg > AI_FIRE_GAS_RATIO * ML_SLOPE_GAZ)
                          || (!isnan(gaz) && gaz >= ML_BASE_GAZ + AI_FIRE_GAS_DELTA));
  bool fire = gasDrift && !isnan(st) && st > AI_FIRE_TEMP_RATIO * ML_SLOPE_TEMP;

  AiCand best = {AI_NONE, AI_OK, NAN};
  aiJudge(best, gasDrift ? AI_RISQUE_FEU : AI_SURCHAUFFE, temp, ML_TEMP_WARN, ML_TEMP_CRIT, 1, tUp, st, rt);
  if (gok) aiJudge(best, fire ? AI_RISQUE_FEU : AI_GAZ, gaz, ML_GAZ_WARN, ML_GAZ_CRIT, 1, gUp, sg, rg);
  if (!tUp) {  // une surchauffe fait varier l'humidité relative : déjà couverte
    aiJudge(best, AI_HUM_HAUTE, hum, ML_HUM_HIGH_WARN, ML_HUM_HIGH_CRIT, 1, hUp, sh, rh);
    aiJudge(best, AI_AIR_SEC, hum, ML_HUM_LOW_WARN, ML_HUM_LOW_CRIT, -1, hDown, sh, rh);
  }

  AiKind prevKind = aiKind;
  AiLevel prevLevel = aiLevel;
  aiStreak = best.level != AI_OK ? aiStreak + 1 : 0;
  if (best.level == AI_OK) { aiKind = AI_NONE; aiLevel = AI_OK; aiMinutes = NAN; }
  else if (aiStreak >= 2) { aiKind = best.kind; aiLevel = best.level; aiMinutes = best.minutes; }
  if (aiKind != prevKind || aiLevel != prevLevel) {
    if (aiLevel == AI_OK) Serial.println("[IA] retour au calme");
    else {
      char eta[40] = "", gasTxt[8] = "ignore";
      if (!isnan(aiMinutes)) snprintf(eta, sizeof eta, " : borne critique dans ~%d min", (int)(aiMinutes + 0.5f));
      if (gok && !isnan(gaz)) snprintf(gasTxt, sizeof gasTxt, "%d", (int)gaz);
      Serial.printf("[IA] %s %s%s (T %.1f C %+.2f/min, H %.0f%% %+.2f/min, gaz %s)\n", AI_LABEL[aiKind],
                    AI_LEVEL_TXT[aiLevel], eta, temp, isnan(st) ? 0.0f : st, hum, isnan(sh) ? 0.0f : sh, gasTxt);
    }
  }
}

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
    // "duree_ms" facultatif : le dashboard peut tenir la LED (ex. rouge clignotante pendant une alerte
    // critique) avec UNE seule commande, puis envoyer "off" à l'acquittement. Sans durée : 60 s.
    unsigned long hold = min((unsigned long)(doc["duree_ms"] | LED_MANUAL_HOLD_MS), LED_MAX_HOLD_MS);
    if (!strcmp(couleur, "rouge")) { redMode = parseLedMode(etat); redManualUntil = millis() + hold; }
    else if (!strcmp(couleur, "vert")) { greenMode = parseLedMode(etat); greenManualUntil = millis() + hold; }
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
  gasFault = !warming && fault;
  r.gas = (warming || fault) ? -1 : gas;
  // Ligne de base apprise UNIQUEMENT en air propre (idée de la session IoT) : sinon elle « monte avec
  // la fuite » et le mode secours finirait par se taire alors que le gaz est toujours là.
  bool rising = gasBaseline > 0 && r.gas > gasBaseline * 1.5f + 50;
  gasRising = rising;
  if (r.gas >= 0 && !rising)
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
  lastTimeSync = millis();
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
  // Heure jamais réglée (l'ESP a démarré avant le point d'accès) : sans elle, BearSSL refuse le certificat
  // indéfiniment. On la règle dès que le Wi-Fi est là, puis on retente le NTP toutes les 60 s s'il échoue.
  if (lastTimeSync == 0 || (!timeOk && millis() - lastTimeSync > 60000)) syncTime();
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
  // Dernière ligne : bandeau d'alerte locale inversé (lisible de loin), sinon la dernière commande reçue.
  // L'IA embarquée passe en premier ; en CRITIQUE, le bandeau clignote.
  const char *banner = nullptr;
  bool invert = true;
  char aiBanner[24];
  if (aiLevel != AI_OK) {
    if (!isnan(aiMinutes) && aiMinutes >= 1.0f)
      snprintf(aiBanner, sizeof aiBanner, "!! %s ~%dmin", AI_LABEL[aiKind], (int)min(aiMinutes + 0.5f, 99.0f));
    else
      snprintf(aiBanner, sizeof aiBanner, "!! %s", AI_LABEL[aiKind]);
    banner = aiBanner;
    if (aiLevel == AI_CRIT && (millis() / 500) % 2) invert = false;
  } else if (!online && millis() - lastMqttOk > LOCAL_FALLBACK_AFTER_MS) banner = "!! SECOURS (hors ligne)";
  else if (gasRising) banner = "!! GAZ COMBUSTIBLE";
  else if (gasFault || (millis() - bootMs > 10000 && (isnan(r.temp) || isnan(r.hum)))) banner = "!! CAPTEUR HS";
  else if (r.pir) banner = "!! MOUVEMENT";
  if (banner) {
    if (invert) oled.fillRect(0, 56, 128, 8, SSD1306_WHITE);
    oled.setTextColor(invert ? SSD1306_BLACK : SSD1306_WHITE);
    oled.setCursor(0, 56);
    oled.print(banner);
    oled.setTextColor(SSD1306_WHITE);
  } else {
    oled.printf("Cmd %s\n", lastCmd.c_str());
  }
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
  if (now - lastAi >= AI_STEP_MS) {
    lastAi = now;
    edgeAi(now);
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

  // Mode secours : broker perdu depuis 30 s -> le boîtier se défend seul (IA embarquée comprise)
  static AiLevel beepedLevel = AI_OK;
  static unsigned long lastAiBeep = 0;
  if (!online && now - lastMqttOk > LOCAL_FALLBACK_AFTER_MS) {
    bool gasSpike = gasBaseline > 0 && r.gas > gasBaseline * 1.5f + 50;
    if (aiLevel > beepedLevel || (aiLevel == AI_CRIT && now - lastAiBeep > 20000)) {
      beep(aiLevel == AI_CRIT ? 1500 : 400);  // alerte IA nouvelle ou aggravée ; rappel toutes les 20 s si critique
      lastAiBeep = now;
    }
    beepedLevel = aiLevel;
    if (r.pir || gasSpike || aiLevel != AI_OK) { redMode = LED_BLINK; if (pirChanged || gasSpike) beep(800); }
    else if (now >= redManualUntil) redMode = LED_OFF;
  } else if (online && now >= redManualUntil) {
    redMode = LED_OFF;  // liaison revenue : éteindre ce que le mode secours avait allumé
  }

  applyLeds();
  static unsigned long lastDraw = 0;
  if (now - lastDraw > 500) { lastDraw = now; drawOled(online); }
  delay(10);
}
