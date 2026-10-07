// Verrou du dashboard sur le PC HÔTE : tant que la vision n'a pas reconnu le titulaire du compte
// connecté, l'écran est bloqué par cet overlay. AUCUN bouton : il ne se lève que par la reconnaissance
// du bon visage (pas de « Masquer », pas d'acquittement). Le flux caméra est affiché pour aider à se
// positionner. Risque assumé : si la reconnaissance échoue, l'hôte reste bloqué (recours : autre PC).
export default function Verrou({ nom, image }) {
  return (
    <div className="verrou-overlay" role="alertdialog" aria-label="Dashboard verrouillé">
      <div className="verrou-cadre">
        <div className="verrou-bandeau">🔒 DASHBOARD VERROUILLÉ</div>
        {image ? (
          <img className="verrou-photo" src={image} alt="Caméra de surveillance" />
        ) : (
          <div className="verrou-photo verrou-photo-vide">En attente du flux vidéo…</div>
        )}
        <div className="verrou-infos">
          <strong>Présente ton visage à la caméra pour déverrouiller</strong>
          <span>Compte connecté : {nom}</span>
        </div>
      </div>
    </div>
  );
}
