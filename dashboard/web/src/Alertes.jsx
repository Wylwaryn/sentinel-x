import { useState } from "react";
import { dateHeure, ORIGINES, TYPES } from "./format.js";

const CLASSE = { CRITIQUE: "niveau-critique", AVERTISSEMENT: "niveau-attention", INFORMATION: "niveau-info" };

// Alertes physiques ouvertes (v_alerte_supervision : jamais de RESEAU_IA)
export default function Alertes({ alertes, dispositifs, peutAgir, onAction }) {
  const [capture, setCapture] = useState(null); // id de l'alerte dont on montre la capture
  const nomDispositif = (id) => dispositifs.find((d) => d.id_dispositif === id)?.nom ?? `ESP ${id}`;

  return (
    <section className="panneau">
      <h2>Alertes ouvertes ({alertes.length})</h2>
      {alertes.length === 0 && <p className="vide">Aucune alerte ouverte</p>}
      <ul className="liste">
        {alertes.map((a) => (
          <li key={a.id_alerte} className={`alerte ${CLASSE[a.niveau]} ${a.statut === "NOUVELLE" ? "alerte-nouvelle" : ""}`}>
            <div className="alerte-titre">
              <strong>{TYPES[a.type_alerte] ?? a.type_alerte}</strong>
              <span className="etiquette">{a.niveau}</span>
            </div>
            <small>
              {nomDispositif(a.id_dispositif)} · {ORIGINES[a.origine] ?? a.origine}
              {a.zone ? ` · ${a.zone}` : ""}
              {a.pir_confirme ? " · PIR confirmé" : ""}
              {a.score_ia != null ? ` · score ${Number(a.score_ia).toFixed(2)}` : ""}
            </small>
            {a.message && <small>{a.message}</small>}
            <small className="vide">{dateHeure(a.instant)} · {a.statut.toLowerCase()}</small>

            <div className="alerte-actions">
              {a.chemin_capture && (
                <button className="bouton" onClick={() => setCapture(capture === a.id_alerte ? null : a.id_alerte)}>
                  {capture === a.id_alerte ? "Masquer" : "Capture"}
                </button>
              )}
              {peutAgir && a.statut === "NOUVELLE" && (
                <button className="bouton" onClick={() => onAction(a, "acquitter")}>Acquitter</button>
              )}
              {peutAgir && <button className="bouton" onClick={() => onAction(a, "resoudre")}>Résoudre</button>}
            </div>
            {capture === a.id_alerte && (
              <img className="capture" src={`/api/v1/alertes/${a.id_alerte}/capture`} alt="Capture de l'alerte" />
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
