import { heure } from "./format.js";

// Overlay plein écran « ALERTE INTRUS » : une personne non reconnue a été détectée par la vision.
// Alerte visuelle, pas un verrou d'accès — mais on NE PEUT PAS la masquer d'un clic : elle reste tant
// que l'alerte n'est pas ACQUITTÉE (action réservée aux OPERATEUR/ADMIN). Une reconnaissance ratée
// (faible lumière, profil) se traite en acquittant, jamais en balayant le bandeau. L'image est une
// capture figée du flux webcam au moment de la détection (déjà annotée par l'IA : cadre + « inconnu »).
export default function Intrus({ intrus, peutAgir, onAcquitter }) {
  if (!intrus) return null;
  return (
    <div className="intrus-overlay" role="alertdialog" aria-label="Alerte intrus">
      <div className="intrus-cadre">
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
        {peutAgir ? (
          <button className="bouton intrus-acquitter" onClick={onAcquitter}>Acquitter l'alerte</button>
        ) : (
          <span className="intrus-note">Alerte à traiter par un opérateur</span>
        )}
      </div>
    </div>
  );
}
