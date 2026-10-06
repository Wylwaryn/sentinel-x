import { useEffect, useRef, useState } from "react";
import { api, urlWebSocket } from "./api.js";
import { heure, valeur, TYPES } from "./format.js";
import { alarme } from "./son.js";
import Dispositifs from "./Dispositifs.jsx";
import Alertes from "./Alertes.jsx";
import Courbe from "./Courbe.jsx";
import Camera from "./Camera.jsx";
import Commandes from "./Commandes.jsx";
import Journal from "./Journal.jsx";
import ImagesReference from "./ImagesReference.jsx";

const HORS_LIGNE_MS = 30000;   // même seuil que v_dispositif_etat et l'API d'ingestion
const RECONNEXION_MS = 3000;   // délai avant de retenter le WebSocket
const NB_EVENEMENTS = 30;      // lignes gardées dans le journal
// ecart : trou au-delà duquel la courbe s'interrompt (mesures brutes, ou tranches agrégées par l'API)
const PERIODES = [
  { minutes: 15, nom: "15 min", ecart: HORS_LIGNE_MS },
  { minutes: 60, nom: "1 h", ecart: HORS_LIGNE_MS },
  { minutes: 360, nom: "6 h", ecart: 150000 },
  { minutes: 1440, nom: "24 h", ecart: 600000 },
];
const RANG = { CRITIQUE: 0, AVERTISSEMENT: 1, INFORMATION: 2 };

// Alertes ouvertes : les plus graves puis les plus récentes d'abord (même ordre que l'API)
function trier(alertes) {
  return [...alertes].sort((a, b) => RANG[a.niveau] - RANG[b.niveau] || (b.instant > a.instant ? 1 : -1));
}

// Point de courbe : l'instant ISO UTC devient un nombre (ms) pour l'axe du temps
function point(m) {
  return { ...m, t: Date.parse(m.instant), pir: m.mouvement_detecte ? 1 : 0 };
}

export default function Dashboard({ utilisateur, onDeconnexion, onSessionExpiree }) {
  const peutAgir = utilisateur.role === "OPERATEUR" || utilisateur.role === "ADMIN";
  const estAdmin = utilisateur.role === "ADMIN";

  const [dispositifs, setDispositifs] = useState([]);
  const [vus, setVus] = useState({});            // id_dispositif -> Date.now() de la dernière mesure reçue
  const [selection, setSelection] = useState(null);
  const [periode, setPeriode] = useState(15);
  const [mesures, setMesures] = useState([]);
  const [alertes, setAlertes] = useState([]);
  const [connexion, setConnexion] = useState("connexion"); // "connexion", "ouverte" ou "fermee"
  const [image, setImage] = useState(null);      // URL blob de la dernière image webcam
  const [imageRecue, setImageRecue] = useState(0);
  const [journal, setJournal] = useState([]);
  const [maintenant, setMaintenant] = useState(Date.now());

  // Les gestionnaires du WebSocket lisent la sélection courante via des refs (pas de reconnexion à chaque clic)
  const selectionRef = useRef(selection);
  const periodeRef = useRef(periode);
  selectionRef.current = selection;
  periodeRef.current = periode;

  function noter(texte, alerte = false) {
    const h = new Date().toLocaleTimeString("fr-FR", { timeZone: "Europe/Paris" });
    setJournal((j) => [{ heure: h, texte, alerte }, ...j].slice(0, NB_EVENEMENTS));
  }

  // ---------- Chargements REST (au démarrage, à la reconnexion, et sur événement) ----------
  async function chargerDispositifs() {
    const liste = await api("/dispositifs");
    setDispositifs(liste);
    const now = Date.now();
    setVus((v) => {
      const suivant = { ...v };
      for (const d of liste) if (d.en_ligne && !suivant[d.id_dispositif]) suivant[d.id_dispositif] = now;
      return suivant;
    });
    setSelection((s) => s ?? liste[0]?.id_dispositif ?? null);
  }

  async function chargerAlertes() {
    setAlertes(await api("/alertes"));
  }

  async function chargerMesures(id = selectionRef.current, minutes = periodeRef.current) {
    if (id === null) return setMesures([]);
    const liste = await api(`/dispositifs/${id}/mesures?minutes=${minutes}`);
    if (id === selectionRef.current && minutes === periodeRef.current) setMesures(liste.map(point));
  }

  function toutRecharger() {
    chargerDispositifs().catch(() => {});
    chargerAlertes().catch(() => {});
    chargerMesures().catch(() => {});
  }

  useEffect(() => {
    chargerMesures(selection, periode).catch(() => {});
  }, [selection, periode]);

  // ---------- Messages temps réel ----------
  function recevoirMesure(m) {
    setVus((v) => ({ ...v, [m.id_dispositif]: Date.now() }));
    setDispositifs((liste) => liste.map((d) => (d.id_dispositif === m.id_dispositif ? { ...d, ...m } : d)));
    // Périodes longues = points agrégés par l'API : on n'y ajoute pas de mesures brutes
    if (m.id_dispositif === selectionRef.current && periodeRef.current <= 60) {
      const p = point(m);
      const limite = p.t - periodeRef.current * 60000;
      setMesures((liste) => [...liste.filter((x) => x.t > limite), p]);
    }
  }

  function recevoirAlerte(a) {
    // Affichage immédiat avec le contenu de la notification…
    setAlertes((liste) => {
      const autres = liste.filter((x) => x.id_alerte !== a.id_alerte);
      return a.statut === "RESOLUE" ? autres : trier([{ ...liste.find((x) => x.id_alerte === a.id_alerte), ...a }, ...autres]);
    });
    if (a.operation === "INSERT") {
      noter(`${a.niveau} · ${TYPES[a.type_alerte] ?? a.type_alerte}${a.message ? " · " + a.message : ""}`, a.niveau !== "INFORMATION");
      if (a.niveau === "CRITIQUE") alarme();
      if (a.type_alerte === "DISPOSITIF_HORS_LIGNE") setVus((v) => ({ ...v, [a.id_dispositif]: 0 }));
    }
    // …puis la ligne complète (capture, score) depuis la vue
    chargerAlertes().catch(() => {});
  }

  useEffect(() => {
    let ws;
    let minuteur;
    let derniereImage = null;

    function connecter() {
      setConnexion("connexion");
      ws = new WebSocket(urlWebSocket()); // le cookie de session part avec la requête
      ws.binaryType = "blob";
      ws.onopen = () => {
        setConnexion("ouverte");
        noter("Temps réel connecté");
        toutRecharger(); // rattrape ce qui a pu arriver pendant la coupure
      };
      ws.onmessage = (e) => {
        if (e.data instanceof Blob) {
          // Image JPEG de la webcam : on libère la précédente pour ne pas saturer la mémoire
          const url = URL.createObjectURL(new Blob([e.data], { type: "image/jpeg" }));
          if (derniereImage) URL.revokeObjectURL(derniereImage);
          derniereImage = url;
          setImage(url);
          setImageRecue(Date.now());
          return;
        }
        let msg;
        try {
          msg = JSON.parse(e.data);
        } catch {
          return; // ne jamais planter sur un message bizarre
        }
        if (msg.type === "mesure") recevoirMesure(msg.data);
        else if (msg.type === "alerte") recevoirAlerte(msg.data);
        else if (msg.type === "resync") toutRecharger();
      };
      ws.onclose = (e) => {
        if (e.code === 4401) return onSessionExpiree("Session expirée, reconnectez-vous");
        setConnexion("fermee");
        minuteur = setTimeout(connecter, RECONNEXION_MS);
      };
    }

    connecter();
    return () => {
      clearTimeout(minuteur);
      if (ws) {
        ws.onclose = null;
        ws.close();
      }
      if (derniereImage) URL.revokeObjectURL(derniereImage);
    };
  }, []);

  // Horloge : recalcule chaque seconde l'état en ligne / hors ligne (aucune requête)
  useEffect(() => {
    const horloge = setInterval(() => setMaintenant(Date.now()), 1000);
    return () => clearInterval(horloge);
  }, []);

  // ---------- Actions ----------
  async function changerStatut(alerte, action) {
    try {
      await api(`/alertes/${alerte.id_alerte}/${action}`, { methode: "POST" });
      noter(`Alerte n°${alerte.id_alerte} ${action === "acquitter" ? "acquittée" : "résolue"}`);
    } catch (err) {
      noter(`Alerte n°${alerte.id_alerte} : ${err.message}`, true);
    }
    // La mise à jour arrive aussi par NOTIFY ; on recharge au cas où le statut avait déjà changé
    chargerAlertes().catch(() => {});
  }

  async function envoyerCommande(commande, libelle) {
    try {
      await api(`/dispositifs/${selection}/commandes`, { methode: "POST", corps: { commande } });
      noter(`Commande envoyée : ${libelle}`);
    } catch (err) {
      noter(`Commande « ${libelle} » refusée : ${err.message}`, true);
    }
  }

  // ---------- État affiché ----------
  const enLigne = (d) => (vus[d.id_dispositif] ?? 0) > maintenant - HORS_LIGNE_MS;
  const courant = dispositifs.find((d) => d.id_dispositif === selection);
  const courantEnLigne = courant ? enLigne(courant) : false;
  const critiques = alertes.filter((a) => a.niveau === "CRITIQUE" && a.statut === "NOUVELLE");
  const videoActive = maintenant - imageRecue < 3000;
  const chiffre = location.protocol === "https:";
  const ecart = PERIODES.find((p) => p.minutes === periode).ecart;

  return (
    <div className="page">
      <header className="entete">
        <h1>SENTINEL-X</h1>
        <div className="badges">
          <span className={`badge ${connexion === "ouverte" ? "badge-ok" : "badge-critique"}`}>
            {connexion === "ouverte" ? "● Temps réel" : connexion === "connexion" ? "Connexion…" : "○ Temps réel coupé"}
          </span>
          {/* Preuve visible du chiffrement pour le jury */}
          <span className={`badge ${chiffre ? "badge-ok" : "badge-attention"}`}>
            {chiffre ? "🔒 HTTPS / WSS" : "Non chiffré (dev)"}
          </span>
          <span className="badge">👤 {utilisateur.nom} · {utilisateur.role}</span>
          <button className="badge" onClick={onDeconnexion}>Déconnexion</button>
        </div>
      </header>

      {/* Bandeau : rouge clignotant tant qu'une alerte CRITIQUE n'est pas acquittée */}
      {critiques.length > 0 ? (
        <div className="bandeau bandeau-alerte" role="alert">
          ⚠ {critiques.length} ALERTE{critiques.length > 1 ? "S" : ""} CRITIQUE{critiques.length > 1 ? "S" : ""} ·{" "}
          {TYPES[critiques[0].type_alerte] ?? critiques[0].type_alerte}
          {critiques[0].zone ? ` (${critiques[0].zone})` : ""} à {heure(critiques[0].instant)}
          {critiques[0].message ? ` · ${critiques[0].message}` : ""}
        </div>
      ) : connexion !== "ouverte" ? (
        <div className="bandeau bandeau-attention">Temps réel coupé : reconnexion…</div>
      ) : (
        <div className="bandeau bandeau-ok">✓ Aucune alerte critique en attente</div>
      )}

      <div className="disposition">
        <aside className="colonne">
          <Dispositifs dispositifs={dispositifs} selection={selection} onSelection={setSelection} enLigne={enLigne} />
          <Alertes alertes={alertes} dispositifs={dispositifs} peutAgir={peutAgir} onAction={changerStatut} />
        </aside>

        <main className="colonne">
          {courant ? (
            <>
              <section className="panneau tuiles">
                <div>
                  <h2>{courant.nom}</h2>
                  <p className="vide">
                    {courant.numero_serie} · {courant.site_nom} · dernière mesure {heure(courant.instant)}
                  </p>
                </div>
                <span className={`badge ${courantEnLigne ? "badge-ok" : "badge-critique"}`}>
                  {courantEnLigne ? "● En ligne" : "○ Hors ligne"}
                </span>
                <div className="tuile"><span>Température</span><strong>{valeur(courant.temperature_c, "°C")}</strong></div>
                <div className="tuile"><span>Humidité</span><strong>{valeur(courant.humidite_pct, "%")}</strong></div>
                <div className="tuile"><span>Gaz (brut)</span><strong>{valeur(courant.gaz_brut, "", 0)}</strong></div>
                <div className={`tuile ${courant.mouvement_detecte && courantEnLigne ? "tuile-alerte" : ""}`}>
                  <span>PIR</span><strong>{courant.mouvement_detecte ? "Mouvement" : "Calme"}</strong>
                </div>
              </section>

              <div className="periodes">
                Période :
                {PERIODES.map((p) => (
                  <button key={p.minutes} className={`bouton ${periode === p.minutes ? "bouton-actif" : ""}`}
                    onClick={() => setPeriode(p.minutes)}>{p.nom}</button>
                ))}
              </div>

              <div className="grille">
                <Courbe titre="Température" unite="°C" cle="temperature_c" couleur="#3987e5" mesures={mesures} ecartMax={ecart} />
                <Courbe titre="Humidité" unite="%" cle="humidite_pct" couleur="#199e70" mesures={mesures} ecartMax={ecart} />
                <Courbe titre="Gaz (MQ-2)" unite="" cle="gaz_brut" couleur="#d95926" mesures={mesures} ecartMax={ecart} />
                <Courbe titre="Mouvement (PIR)" unite="" cle="pir" couleur="#d03b3b" mesures={mesures} ecartMax={ecart} marches />
              </div>
            </>
          ) : (
            <section className="panneau vide">Aucun dispositif enregistré en base.</section>
          )}

          <div className="grille">
            <Camera image={image} active={videoActive} />
            <div className="colonne">
              <Commandes envoyer={envoyerCommande} actif={peutAgir && courant && connexion === "ouverte"}
                raison={!peutAgir ? "Rôle LECTEUR : consultation uniquement" : !courant ? "Aucun dispositif" : ""}
                enLigne={courantEnLigne} />
              <Journal evenements={journal} />
            </div>
          </div>

          {estAdmin && <ImagesReference />}
        </main>
      </div>
    </div>
  );
}
