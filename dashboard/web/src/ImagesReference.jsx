import { useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import { dateHeure } from "./format.js";

const COTE_MAX = 640; // la reconnaissance n'a pas besoin de plus (et l'envoi reste léger)
const CLE_CAMERA = "sentinel.camera"; // dernière caméra choisie, mémorisée dans ce navigateur

function lireCamera() {
  try {
    return localStorage.getItem(CLE_CAMERA) || "";
  } catch {
    return ""; // stockage bloqué (navigation privée…) : caméra par défaut
  }
}

function memoriserCamera(id) {
  try {
    localStorage.setItem(CLE_CAMERA, id);
  } catch {
    // sans importance : on redemandera
  }
}

// Message lisible pour les erreurs de getUserMedia
function erreurCamera(err) {
  if (err.name === "NotReadableError") return "Caméra utilisée par un autre programme (la vision ?) : choisissez-en une autre";
  if (err.name === "NotAllowedError") return "Caméra indisponible : accès refusé";
  if (err.name === "NotFoundError") return "Aucune caméra trouvée";
  return `Caméra indisponible : ${err.message}`;
}

// Image de la caméra -> JPEG (qualité 0,9, plus grand côté 640 px)
function capturer(video) {
  const echelle = Math.min(1, COTE_MAX / Math.max(video.videoWidth, video.videoHeight));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(video.videoWidth * echelle);
  canvas.height = Math.round(video.videoHeight * echelle);
  canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
  return new Promise((ok) => canvas.toBlob(ok, "image/jpeg", 0.9));
}

// Images de référence (reconnaissance des membres de l'équipe) : ADMIN uniquement, vérifié par l'API.
// Photo prise avec la caméra de l'appareil qui affiche le dashboard, ou fichier JPEG.
export default function ImagesReference({ choisi, version }) {
  const [images, setImages] = useState([]);
  const [utilisateurs, setUtilisateurs] = useState([]);
  const [pour, setPour] = useState("");
  const [fichier, setFichier] = useState(null);
  const [message, setMessage] = useState("");
  const [cameraOuverte, setCameraOuverte] = useState(false);
  const [apercu, setApercu] = useState(null); // { blob, url } de la photo prise, avant envoi
  // Plusieurs caméras possibles (sur le PC hôte, la webcam USB est occupée par la vision)
  const [cameras, setCameras] = useState([]);
  const [cameraId, setCameraId] = useState(lireCamera);
  const videoRef = useRef(null);
  const fluxRef = useRef(null);
  const sectionRef = useRef(null);

  async function charger() {
    const [i, u] = await Promise.all([api("/images-reference"), api("/utilisateurs")]);
    setImages(i);
    const personnes = u.filter((x) => x.role !== "SERVICE_VISION" && x.actif);
    setUtilisateurs(personnes);
    setPour((p) => p || String(personnes[0]?.id_utilisateur ?? ""));
  }

  useEffect(() => {
    charger().catch((e) => setMessage(e.message));
  }, [version]);

  // « Photos » ou compte tout juste créé : on sélectionne la personne et on amène la section à l'écran
  useEffect(() => {
    if (choisi) {
      setPour(String(choisi));
      sectionRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [choisi]);

  function arreterCamera() {
    fluxRef.current?.getTracks().forEach((t) => t.stop());
    fluxRef.current = null;
    setCameraOuverte(false);
  }

  // Caméra coupée si on quitte l'écran (déconnexion, fermeture)
  useEffect(() => arreterCamera, []);

  // La balise <video> n'existe qu'une fois la caméra ouverte : on y branche le flux après l'affichage
  useEffect(() => {
    if (cameraOuverte && videoRef.current) videoRef.current.srcObject = fluxRef.current;
  }, [cameraOuverte]);

  // Les noms des caméras ne sont donnés qu'après une première autorisation : on liste après getUserMedia
  async function listerCameras() {
    try {
      const appareils = await navigator.mediaDevices.enumerateDevices();
      setCameras(appareils.filter((a) => a.kind === "videoinput"));
    } catch {
      setCameras([]);
    }
  }

  useEffect(() => {
    listerCameras();
  }, []);

  async function ouvrirCamera(id = cameraId) {
    setMessage("");
    setApercu(null);
    fluxRef.current?.getTracks().forEach((t) => t.stop()); // l'ancienne caméra est libérée avant d'en ouvrir une autre
    fluxRef.current = null;
    setCameraOuverte(false);
    try {
      let flux;
      try {
        flux = await navigator.mediaDevices.getUserMedia({ video: id ? { deviceId: { exact: id } } : true, audio: false });
      } catch (err) {
        // Caméra mémorisée débranchée : on retombe sur celle par défaut
        if (!id || (err.name !== "OverconstrainedError" && err.name !== "NotFoundError")) throw err;
        flux = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
      }
      fluxRef.current = flux;
      const utilisee = flux.getVideoTracks()[0]?.getSettings().deviceId || id;
      if (utilisee) {
        setCameraId(utilisee);
        memoriserCamera(utilisee);
      }
      setCameraOuverte(true);
    } catch (err) {
      setMessage(erreurCamera(err)); // la liste reste active pour en choisir une autre
    }
    await listerCameras();
  }

  function choisirCamera(id) {
    setCameraId(id);
    memoriserCamera(id);
    if (cameraOuverte) ouvrirCamera(id); // bascule tout de suite si l'aperçu est affiché
  }

  async function prendrePhoto() {
    const blob = await capturer(videoRef.current);
    arreterCamera(); // dès la photo prise
    setApercu({ blob, url: URL.createObjectURL(blob) });
  }

  function oublierApercu() {
    if (apercu) URL.revokeObjectURL(apercu.url);
    setApercu(null);
  }

  async function envoyer(blob) {
    setMessage("");
    try {
      await api(`/images-reference?id_utilisateur=${encodeURIComponent(pour)}`, {
        methode: "POST", corps: blob, type: "image/jpeg",
      });
      const nb = images.filter((i) => String(i.id_utilisateur) === pour && i.active).length + 1;
      setMessage(`Photo ajoutée (${nb} active${nb > 1 ? "s" : ""} pour cette personne ; 3 à 5 conseillées)`);
      await charger();
      return true;
    } catch (err) {
      setMessage(`Ajout refusé : ${err.message}`);
      return false;
    }
  }

  async function envoyerApercu() {
    if (await envoyer(apercu.blob)) oublierApercu();
  }

  async function envoyerFichier(e) {
    e.preventDefault();
    if (await envoyer(fichier)) {
      setFichier(null);
      e.target.reset();
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
    <section className="panneau" ref={sectionRef}>
      <h2>Images de référence (ADMIN)</h2>
      <p className="vide">
        3 à 5 photos par personne : de face, léger profil gauche et droit, un seul visage, bonne lumière.
        Désactiver une image la retire de la reconnaissance à la synchronisation suivante.
      </p>

      <div className="formulaire">
        <select key={utilisateurs.length} value={pour} onChange={(e) => setPour(e.target.value)}>
          {utilisateurs.map((u) => (
            <option key={u.id_utilisateur} value={u.id_utilisateur}>{u.nom} ({u.role})</option>
          ))}
        </select>
        {cameras.length > 1 && (
          <select value={cameraId} onChange={(e) => choisirCamera(e.target.value)} title="Caméra utilisée pour la photo">
            {!cameras.some((c) => c.deviceId === cameraId) && <option value={cameraId}>Caméra par défaut</option>}
            {cameras.map((c, i) => (
              <option key={c.deviceId || i} value={c.deviceId}>{c.label || `Caméra ${i + 1}`}</option>
            ))}
          </select>
        )}
        {!cameraOuverte && !apercu && (
          <button className="bouton bouton-actif" onClick={() => ouvrirCamera()} disabled={!pour}>Prendre une photo</button>
        )}
      </div>

      {cameraOuverte && (
        <div className="prise-photo">
          <video ref={videoRef} autoPlay playsInline muted className="video" />
          <div className="alerte-actions">
            <button className="bouton bouton-actif" onClick={prendrePhoto}>📸 Capturer</button>
            <button className="bouton" onClick={arreterCamera}>Annuler</button>
          </div>
        </div>
      )}
      {apercu && (
        <div className="prise-photo">
          <img src={apercu.url} alt="Photo prise" className="video" />
          <div className="alerte-actions">
            <button className="bouton bouton-actif" onClick={envoyerApercu}>Envoyer</button>
            <button className="bouton" onClick={() => { oublierApercu(); ouvrirCamera(); }}>Reprendre</button>
            <button className="bouton" onClick={oublierApercu}>Annuler</button>
          </div>
        </div>
      )}

      <form className="formulaire" onSubmit={envoyerFichier}>
        <input type="file" accept="image/jpeg" required onChange={(e) => setFichier(e.target.files[0] ?? null)} />
        <button className="bouton" type="submit" disabled={!fichier || !pour}>Envoyer le fichier</button>
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
