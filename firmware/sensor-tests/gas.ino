const unsigned long GAS_WARMUP_MS = 120000;
float gasBaseline = -1;

bool gasReady() { return millis() > GAS_WARMUP_MS; }

int gasRead() {
  long sum = 0;
  for (int i = 0; i < 5; i++) { sum += analogRead(A0); delay(2); }
  return sum / 5;
}

bool gasFault(int v) { return v <= 2 || v >= 1021; }

// Call once per second after warm-up. true = clear rise above clean air.
bool gasRise(int v) {
  if (gasBaseline < 0) gasBaseline = v;
  bool rise = v > gasBaseline * 1.5f + 50;
  if (!rise) gasBaseline = 0.98f * gasBaseline + 0.02f * v;  // only learn in clean air
  return rise;
}