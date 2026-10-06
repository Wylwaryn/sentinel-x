// Journal des derniers événements (alertes, commandes, connexions), le plus récent en haut
export default function Journal({ evenements }) {
  return (
    <section className="panneau">
      <h2>Journal</h2>
      {evenements.length === 0 && <p className="vide">Aucun événement</p>}
      <ul className="journal">
        {evenements.map((e, i) => (
          <li key={i} className={e.alerte ? "journal-alerte" : ""}>
            <span className="journal-heure">{e.heure}</span> {e.alerte ? "⚠" : "•"} {e.texte}
          </li>
        ))}
      </ul>
    </section>
  );
}
