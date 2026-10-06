import { useEffect, useState } from "react";
import { api } from "./api.js";

const VIDE = { nom: "", email: "", role: "LECTEUR", mot_de_passe: "", confirmation: "", mot_de_passe_admin: "" };

// Comptes du dashboard (ADMIN) : création avec ressaisie du mot de passe de l'ADMIN, activation / désactivation.
// onCree(id) : enchaîne sur la prise de photo de référence du nouveau compte.
export default function Utilisateurs({ moi, onCree, version }) {
  const [comptes, setComptes] = useState([]);
  const [form, setForm] = useState(VIDE);
  const [message, setMessage] = useState("");
  const [enCours, setEnCours] = useState(false);

  async function charger() {
    setComptes(await api("/utilisateurs"));
  }

  useEffect(() => {
    charger().catch((e) => setMessage(e.message));
  }, [version]);

  const champ = (nom) => ({ value: form[nom], onChange: (e) => setForm({ ...form, [nom]: e.target.value }) });

  async function creer(e) {
    e.preventDefault();
    setMessage("");
    if (form.mot_de_passe !== form.confirmation) return setMessage("Les deux mots de passe diffèrent");
    setEnCours(true);
    try {
      const { confirmation, ...corps } = form;
      const d = await api("/utilisateurs", { methode: "POST", corps });
      setMessage(`Compte « ${form.nom} » créé`);
      setForm(VIDE);
      await charger();
      onCree(d.id_utilisateur);
    } catch (err) {
      setMessage(`Création refusée : ${err.message}`);
      setForm((f) => ({ ...f, mot_de_passe_admin: "" }));
    } finally {
      setEnCours(false);
    }
  }

  async function basculer(c) {
    try {
      await api(`/utilisateurs/${c.id_utilisateur}`, { methode: "PATCH", corps: { actif: !c.actif } });
      await charger();
    } catch (err) {
      setMessage(err.message);
    }
  }

  return (
    <section className="panneau">
      <h2>Utilisateurs (ADMIN)</h2>
      <form className="formulaire" onSubmit={creer} autoComplete="off">
        <input placeholder="Nom" required maxLength={100} {...champ("nom")} />
        <input type="email" placeholder="Email" required maxLength={255} {...champ("email")} />
        <select {...champ("role")}>
          <option value="LECTEUR">LECTEUR</option>
          <option value="OPERATEUR">OPERATEUR</option>
          <option value="ADMIN">ADMIN</option>
        </select>
        <input type="password" placeholder="Mot de passe (12 car. min.)" required minLength={12} maxLength={256}
          autoComplete="new-password" {...champ("mot_de_passe")} />
        <input type="password" placeholder="Confirmation" required minLength={12} maxLength={256}
          autoComplete="new-password" {...champ("confirmation")} />
        <input type="password" placeholder="VOTRE mot de passe (ADMIN)" required maxLength={256}
          autoComplete="current-password" {...champ("mot_de_passe_admin")} />
        <button className="bouton" type="submit" disabled={enCours}>{enCours ? "Création…" : "Créer le compte"}</button>
      </form>
      {message && <p className="vide">{message}</p>}

      <ul className="liste">
        {comptes.map((c) => (
          <li key={c.id_utilisateur} className={`compte ${c.actif ? "" : "inactive"}`}>
            <span className="element-texte">
              <strong>{c.nom}</strong>
              <small>{c.email} · {c.role}{c.actif ? "" : " · désactivé"}</small>
            </span>
            <span className="alerte-actions">
              {c.role !== "SERVICE_VISION" && (
                <button className="bouton" onClick={() => onCree(c.id_utilisateur)}>Photos</button>
              )}
              {c.id_utilisateur !== moi.id_utilisateur && (
                <button className="bouton" onClick={() => basculer(c)}>{c.actif ? "Désactiver" : "Réactiver"}</button>
              )}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
