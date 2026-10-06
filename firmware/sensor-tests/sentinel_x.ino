// Pins (shared with the other tabs)
#define PIN_PIR    D6
#define PIN_DHT    D5
#define PIN_BUZZER D7
#define PIN_RED    D0
#define PIN_GREEN  D8
// The MQ-2 uses A0, the only analog pin

bool lastPir = false;
float lastT = NAN, lastH = NAN;
int lastGas = -1;                 // -1 = no valid value
unsigned long lastGasPrint = 0;
unsigned long lastDhtPrint = 0;
unsigned long lastScreen = 0;

void setup() {
  Serial.begin(115200);
  pirBegin();
  dhtBegin();
  screenBegin();
  Serial.println("ESP started");
}

void loop() {
  // Movement: print only when the state changes
  bool pir = pirRead();
  if (pir != lastPir) {
    lastPir = pir;
    Serial.println(pir ? "Movement detected" : "No movement");
  }

  // Gas: once per second
  if (millis() - lastGasPrint >= 1000) {
    lastGasPrint = millis();
    if (!gasReady()) {
      lastGas = -1;
      Serial.println("Gas: warming up...");
    } else {
      int g = gasRead();
      if (gasFault(g)) {
        lastGas = -1;
        Serial.println("Gas: FAULT (check wiring)");
      } else {
        lastGas = g;
        Serial.print("Gas: "); Serial.println(g);
      }
    }
  }

  // Temperature and humidity: every 2 s
  if (millis() - lastDhtPrint >= 2000) {
    lastDhtPrint = millis();
    float t = dhtTemp();
    float h = dhtHumidity();
    if (isnan(t) || isnan(h)) {
      lastT = NAN; lastH = NAN;
      Serial.println("DHT22: FAULT (check wiring)");
    } else {
      lastT = t; lastH = h;
      Serial.print("Temp: "); Serial.print(t, 1);
      Serial.print(" C   Humidity: "); Serial.print(h, 0);
      Serial.println(" %");
    }
  }

  // Screen: once per second
  if (millis() - lastScreen >= 1000) {
    lastScreen = millis();
    screenUpdate(lastT, lastH, lastGas, lastPir, "--", false, false);
  }
}