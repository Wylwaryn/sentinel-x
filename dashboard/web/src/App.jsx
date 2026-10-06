import { useEffect, useState } from "react";
import "./App.css";
import { api, quandDeconnecte } from "./api.js";
import Login from "./Login.jsx";
import Dashboard from "./Dashboard.jsx";

export default function App() {
  // undefined = on ne sait pas encore (vérification du cookie), null = pas connecté
  const [utilisateur, setUtilisateur] = useState(undefined);
  const [message, setMessage] = useState(""); // ex. "Session expirée"

  function deconnexion(raison = "") {
    setMessage(raison);
    setUtilisateur(null);
  }

  // Au chargement : le cookie de session est-il encore valide ?
  useEffect(() => {
    quandDeconnecte(deconnexion);
    api("/auth/me")
      .then((d) => setUtilisateur(d.utilisateur))
      .catch(() => setUtilisateur(null));
  }, []);

  async function seDeconnecter() {
    await api("/auth/logout", { methode: "POST" }).catch(() => {});
    deconnexion();
  }

  if (utilisateur === undefined) return null;
  // Compte de service (synchronisation des visages) : aucun accès à l'interface
  if (utilisateur?.role === "SERVICE_VISION") {
    return (
      <div className="login-page">
        <div className="panneau login">
          <h1>SENTINEL-X</h1>
          <p>Compte de service : pas d'accès à l'interface.</p>
          <button className="bouton" onClick={seDeconnecter}>Déconnexion</button>
        </div>
      </div>
    );
  }
  return utilisateur ? (
    <Dashboard utilisateur={utilisateur} onDeconnexion={seDeconnecter} onSessionExpiree={deconnexion} />
  ) : (
    <Login onConnexion={(u) => { setMessage(""); setUtilisateur(u); }} message={message} />
  );
}
