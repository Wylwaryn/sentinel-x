import { useEffect, useState } from "react";
import { api } from "./api.js";
import { dateHeure } from "./format.js";

// Images de référence (reconnaissance des personnes autorisées) : ADMIN uniquement, vérifié par l'API
export default function ImagesReference() {
  const [images, setImages] = useState([]);
  const [utilisateurs, setUtilisateurs] = useState([]);
  const [pour, setPour] = useState("");
  const [fichier, setFichier] = useState(null);
  const [message, setMessage] = useState("");

  async function charger() {
    const [i, u] = await Promise.all([api("/images-reference"), api("/utilisateurs")]);
    setImages(i);
    setUtilisateurs(u);
    setPour((p) => p || String(u[0]?.id_utilisateur ?? ""));
  }

  useEffect(() => {
    charger().catch((e) => setMessage(e.message));
  }, []);

  async function ajouter(e) {
    e.preventDefault();
    setMessage("");
    try {
      await api(`/images-reference?id_utilisateur=${encodeURIComponent(pour)}`, {
        methode: "POST", corps: fichier, type: "image/jpeg",
      });
      setFichier(null);
      e.target.reset();
      setMessage("Image ajoutée");
      await charger();
    } catch (err) {
      setMessage(`Ajout refusé : ${err.message}`);
    }
  }

  async function basculer(img) {
    try {
      await api(`/images-reference/${img.id_image_reference}`, { methode: "PATCH", corps: { active: !img.active } });
      await charger();
    } catch (err) {
      setMessage(err.message);
    }
  }

  return (
    <section className="panneau">
      <h2>Images de référence (ADMIN)</h2>
      <form className="formulaire" onSubmit={ajouter}>
        <select value={pour} onChange={(e) => setPour(e.target.value)} required>
          {utilisateurs.map((u) => (
            <option key={u.id_utilisateur} value={u.id_utilisateur}>{u.nom} ({u.role})</option>
          ))}
        </select>
        <input type="file" accept="image/jpeg" required onChange={(e) => setFichier(e.target.files[0] ?? null)} />
        <button className="bouton" type="submit" disabled={!fichier || !pour}>Ajouter</button>
      </form>
      {message && <p className="vide">{message}</p>}

      {images.length === 0 && <p className="vide">Aucune image</p>}
      <div className="vignettes">
        {images.map((img) => (
          <figure key={img.id_image_reference} className={img.active ? "" : "inactive"}>
            <img src={`/api/v1/images-reference/${img.id_image_reference}/fichier`} alt={img.utilisateur_nom} loading="lazy" />
            <figcaption>
              {img.utilisateur_nom}<br />
              <small className="vide">{dateHeure(img.instant)}</small><br />
              <button className="bouton" onClick={() => basculer(img)}>{img.active ? "Désactiver" : "Réactiver"}</button>
            </figcaption>
          </figure>
        ))}
      </div>
    </section>
  );
}
