#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

Adafruit_SSD1306 oled(128, 64, &Wire, -1);

void screenBegin() {
  Wire.begin(D2, D1);                 // SDA, SCL
  if (!oled.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    Serial.println("OLED: not found (check wiring)");
  }
  oled.clearDisplay();
  oled.display();
}

// t = NAN, gas = -1 when there is no valid value
void screenUpdate(float t, float h, int gas, bool pir,
                  const char* ip, bool wifi, bool mqtt) {
  oled.clearDisplay();
  oled.setTextSize(1);
  oled.setTextColor(SSD1306_WHITE);
  oled.setCursor(0, 0);
  oled.println("SX-G2-01");
  oled.print("IP: ");   oled.println(ip);
  oled.print("WiFi: "); oled.print(wifi ? "OK" : "--");
  oled.print("  MQTT: "); oled.println(mqtt ? "OK" : "--");

  oled.print("T: ");
  if (isnan(t)) oled.print("--"); else oled.print(t, 1);
  oled.print("  H: ");
  if (isnan(h)) oled.println("--"); else { oled.print(h, 0); oled.println("%"); }

  oled.print("Gaz: ");
  if (gas < 0) oled.print("--"); else oled.print(gas);
  oled.print("  PIR: "); oled.println(pir ? "1" : "0");
  oled.display();
}