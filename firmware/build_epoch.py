# Injecte l'heure de compilation (BUILD_EPOCH) : repli de l'ESP pour vérifier la validité
# du certificat TLS quand le NTP est injoignable. Doit être postérieure à l'émission des certificats.
import time

Import("env")  # noqa: F821 (fourni par PlatformIO)
env.Append(CPPDEFINES=[("BUILD_EPOCH", f"{int(time.time())}UL")])  # noqa: F821
