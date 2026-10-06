const unsigned long GAS_WARMUP_MS = 120000;

bool gasReady() {
  return millis() > GAS_WARMUP_MS;
}

int gasRead() {
  long sum = 0;
  for (int i = 0; i < 5; i++) {
    sum += analogRead(A0);
    delay(2);
  }
  return sum / 5;
}

bool gasFault(int v) {
  return v <= 2 || v >= 1021;
}