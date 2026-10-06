const unsigned long PIR_WARMUP_MS = 60000;   // ignore the first minute

void pirBegin() {
  pinMode(PIN_PIR, INPUT);
}

bool pirReady() {
  return millis() > PIR_WARMUP_MS;
}

bool pirRead() {
  if (!pirReady()) return false;
  return digitalRead(PIN_PIR) == HIGH;
}