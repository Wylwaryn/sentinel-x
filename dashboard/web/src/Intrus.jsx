import { heure } from "./format.js";

// Overlay plein écran « ALERTE INTRUS » : une personne non reconnue a été détectée par la vision.
// C'est une ALERTE VISUELLE, jamais un verrou d'accès : la reconnaissance peut mal lire (faible
// lumière, profil), un faux positif n'affiche qu'un bandeau de trop. L'image est une capture figée
// du flux webcam au moment de la détection (déjà annotée par l'IA : cadre + « inconnu »).
export default function Intrus({ intrus, onFermer }) {
  if (!intrus) return null;
  return (
    <div className="intrus-overlay" role="alertdialog" aria-label="Alerte intrus" onClick={onFermer}>
      <div className="intrus-cadre" onClick={(e) => e.stopPropagation()}>
        <div className="intrus-bandeau">⚠ ALERTE INTRUS</div>
        {intrus.image ? (
          <img className="intrus-photo" src={intrus.image} alt="Personne non identifiée" />
        ) : (
          <div className="intrus-photo intrus-photo-vide">Image vidéo indisponible</div>
        )}
        <div className="intrus-infos">
          <strong>{intrus.message || "Personne non identifiée"}</strong>
          <span>
            {intrus.zone ? `Zone : ${intrus.zone.replace(/_/g, " ")} · ` : ""}
            {heure(intrus.instant)}
          </span>
        </div>
        <button className="bouton intrus-fermer" onClick={onFermer}>Masquer</button>
      </div>
    </div>
  );
}
