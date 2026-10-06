#include <ArduinoJson.h>

enum LedMode { LED_OFF, LED_ON, LED_BLINK };
LedMode redMode = LED_OFF, greenMode = LED_OFF;
unsigned long buzzerUntil = 0;

void actuatorsBegin() {
  pinMode(PIN_RED, OUTPUT);
  pinMode(PIN_GREEN, OUTPUT);
  pinMode(PIN_BUZZER, OUTPUT);
  digitalWrite(PIN_BUZZER, LOW);
}

// Same JSON as the dashboard sends, e.g. {"actionneur":"buzzer","etat":"on","duree_ms":2000}
void actuatorsCommand(const char* json) {
  JsonDocument doc;
  if (deserializeJson(doc, json)) { Serial.println("Bad JSON"); return; }
  const char* act  = doc["actionneur"] | "";
  const char* etat = doc["etat"] | "off";

  if (!strcmp(act, "buzzer")) {
    if (!strcmp(etat, "on")) buzzerUntil = millis() + min((unsigned long)(doc["duree_ms"] | 2000), 10000UL);
    else buzzerUntil = 0;
  } else if (!strcmp(act, "led")) {
    const char* c = doc["couleur"] | "";
    LedMode m = !strcmp(etat, "on") ? LED_ON : !strcmp(etat, "clignote") ? LED_BLINK : LED_OFF;
    if (!strcmp(c, "rouge")) redMode = m;
    else if (!strcmp(c, "vert")) greenMode = m;
  }
  Serial.print("Command: "); Serial.println(json);
}

// Call on every pass of loop(). Never waits.
void actuatorsUpdate() {
  bool blinkPhase = (millis() / 250) % 2;
  digitalWrite(PIN_RED,   redMode   == LED_ON || (redMode   == LED_BLINK && blinkPhase));
  digitalWrite(PIN_GREEN, greenMode == LED_ON || (greenMode == LED_BLINK && blinkPhase));
  digitalWrite(PIN_BUZZER, millis() < buzzerUntil ? HIGH : LOW);
}