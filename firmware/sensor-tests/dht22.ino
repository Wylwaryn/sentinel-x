#include <DHT.h>

DHT dht(PIN_DHT, DHT22);

void dhtBegin() {
  dht.begin();
}

float dhtTemp() {
  return dht.readTemperature();
}

float dhtHumidity() {
  return dht.readHumidity();
}