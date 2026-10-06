// Panneau de commandes : format de la fiche API dashboard, déjà géré par le firmware
// (topic sentinel/cmd/<numero_serie>). L'API vérifie le rôle (OPERATEUR ou ADMIN).
const BOUTONS = [
  { nom: "Buzzer 3 s", commande: { actionneur: "buzzer", etat: "on", duree_ms: 3000 }, alerte: true },
  { nom: "Couper le buzzer", commande: { actionneur: "buzzer", etat: "off" } },
  { nom: "LED rouge clignote", commande: { actionneur: "led", couleur: "rouge", etat: "clignote" }, alerte: true },
  { nom: "LED rouge éteinte", commande: { actionneur: "led", couleur: "rouge", etat: "off" } },
  { nom: "LED verte allumée", commande: { actionneur: "led", couleur: "vert", etat: "on" } },
  { nom: "LED verte éteinte", commande: { actionneur: "led", couleur: "vert", etat: "off" } },
];

export default function Commandes({ envoyer, actif, raison, enLigne }) {
  return (
    <section className="panneau">
      <h2>Commandes</h2>
      <div className="commandes">
        {BOUTONS.map((b) => (
          <button key={b.nom} className={`bouton ${b.alerte ? "bouton-on" : ""}`} disabled={!actif}
            onClick={() => envoyer(b.commande, b.nom)}>
            {b.nom}
          </button>
        ))}
      </div>
      {raison && <p className="vide">{raison}</p>}
      {!raison && !enLigne && <p className="vide">Boîtier hors ligne : la commande sera perdue s'il ne revient pas</p>}
    </section>
  );
}
