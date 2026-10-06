// Alarme sonore générée par le navigateur (Web Audio) : aucun fichier à charger.
// Les navigateurs n'autorisent le son qu'après une action de l'utilisateur (clic de connexion, par ex.).
let contexte = null;

function bip(debut, frequence) {
  const osc = contexte.createOscillator();
  const gain = contexte.createGain();
  osc.type = "square";
  osc.frequency.value = frequence;
  gain.gain.setValueAtTime(0.15, debut);
  gain.gain.exponentialRampToValueAtTime(0.001, debut + 0.25);
  osc.connect(gain).connect(contexte.destination);
  osc.start(debut);
  osc.stop(debut + 0.25);
}

export function alarme() {
  try {
    contexte ??= new AudioContext();
    contexte.resume();
    const t = contexte.currentTime;
    for (let i = 0; i < 3; i++) {
      bip(t + i * 0.35, 880);
      bip(t + i * 0.35 + 0.15, 660);
    }
  } catch {
    // Son bloqué ou indisponible : le bandeau rouge reste l'alerte principale
  }
}
