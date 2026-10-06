import { useState } from "react";
import { api } from "./api.js";

// Écran de connexion : l'API vérifie le mot de passe (Argon2id) et pose un cookie de session HttpOnly
export default function Login({ onConnexion, message }) {
  const [email, setEmail] = useState("");
  const [motDePasse, setMotDePasse] = useState("");
  const [erreur, setErreur] = useState("");
  const [enCours, setEnCours] = useState(false);

  async function envoyer(e) {
    e.preventDefault(); // empêche le rechargement de la page
    setErreur("");
    setEnCours(true);
    try {
      const d = await api("/auth/login", { methode: "POST", corps: { email, password: motDePasse } });
      onConnexion(d.utilisateur);
    } catch (err) {
      setErreur(err.status === 401 ? "Email ou mot de passe incorrect" : err.message);
    } finally {
      setEnCours(false);
      setMotDePasse("");
    }
  }

  return (
    <div className="login-page">
      <form className="panneau login" onSubmit={envoyer}>
        <h1>SENTINEL-X</h1>
        <p className="vide">Centre de commandement · accès restreint</p>

        {message && <p className="login-erreur">{message}</p>}

        <label>
          Email
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus autoComplete="username" />
        </label>
        <label>
          Mot de passe
          <input type="password" value={motDePasse} onChange={(e) => setMotDePasse(e.target.value)} required autoComplete="current-password" />
        </label>

        {erreur && <p className="login-erreur">⚠ {erreur}</p>}

        <button className="bouton bouton-on" type="submit" disabled={enCours}>
          {enCours ? "Connexion…" : "Se connecter"}
        </button>
      </form>
    </div>
  );
}
