// L'API envoie tout en UTC ("2026-10-07T14:02:11.482913Z") : conversion en heure de Paris ici seulement.
const FUSEAU = "Europe/Paris";

const fmtHeure = new Intl.DateTimeFormat("fr-FR", { timeZone: FUSEAU, hour: "2-digit", minute: "2-digit", second: "2-digit" });
const fmtDateHeure = new Intl.DateTimeFormat("fr-FR", {
  timeZone: FUSEAU, day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
});

export function heure(instant) {
  return instant ? fmtHeure.format(new Date(instant)) : "—";
}

export function dateHeure(instant) {
  return instant ? fmtDateHeure.format(new Date(instant)) : "—";
}

// Valeur numérique affichable (null si le capteur est en défaut)
export function valeur(v, unite = "", decimales = 1) {
  return v === null || v === undefined ? "—" : `${Number(v).toFixed(decimales)}${unite ? " " + unite : ""}`;
}

// Libellés lisibles du catalogue des alertes (fiche API dashboard, §5)
export const TYPES = {
  PRESENCE: "Présence",
  RODEUR: "Rôdeur",
  INTRUSION: "Intrusion",
  APPROCHE_RAPIDE: "Approche rapide",
  ANGLE_MORT: "Angle mort (PIR seul)",
  ANOMALIE_ENVIRONNEMENTALE: "Anomalie environnementale",
  DISPOSITIF_HORS_LIGNE: "Dispositif hors ligne",
};

export const ORIGINES = {
  VISION_IA: "caméra",
  FUSION: "caméra + PIR",
  PIR: "PIR",
  CAPTEURS_IA: "IA capteurs",
  SYSTEME: "système",
};
