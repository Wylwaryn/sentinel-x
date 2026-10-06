// Webcam : JPEG annotés par l'IA (cadres, zones), publiés sur MQTT sentinel/video/cam1
// et relayés par l'API dans le WebSocket. Chaque image remplace la précédente.
export default function Camera({ image, active }) {
  return (
    <section className="panneau">
      <h2>Webcam · détection IA {active ? <span className="direct">● direct</span> : null}</h2>
      {image ? (
        <img className={`video ${active ? "" : "video-figee"}`} src={image} alt="Flux webcam annoté par l'IA" />
      ) : (
        <div className="scene-vide vide">En attente du flux vidéo…</div>
      )}
      {image && !active && <p className="detection vide">Flux interrompu : dernière image affichée</p>}
    </section>
  );
}
