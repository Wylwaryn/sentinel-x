import { heure } from "./format.js";

// Liste des ESP (v_dispositif_etat) : un clic affiche ses courbes
export default function Dispositifs({ dispositifs, selection, onSelection, enLigne }) {
  return (
    <section className="panneau">
      <h2>Dispositifs</h2>
      {dispositifs.length === 0 && <p className="vide">Aucun dispositif</p>}
      <ul className="liste">
        {dispositifs.map((d) => {
          const ok = enLigne(d);
          return (
            <li key={d.id_dispositif}>
              <button className={`element ${d.id_dispositif === selection ? "element-actif" : ""}`}
                onClick={() => onSelection(d.id_dispositif)}>
                <span className={ok ? "pastille-ok" : "pastille-ko"}>{ok ? "●" : "○"}</span>
                <span className="element-texte">
                  <strong>{d.nom}</strong>
                  <small>{d.numero_serie} · {d.site_nom}</small>
                  <small>{ok ? "en ligne" : "hors ligne"} · {heure(d.instant)}</small>
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
