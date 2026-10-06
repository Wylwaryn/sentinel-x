// Appels à l'API dashboard (même origine, derrière Caddy).
// La session est un cookie HttpOnly : le JavaScript ne voit jamais le jeton.

export class ErreurApi extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

// Appelé quand l'API répond 401 (session expirée) : App.jsx revient à l'écran de connexion
let surDeconnexion = () => {};
export function quandDeconnecte(fn) {
  surDeconnexion = fn;
}

export async function api(chemin, { methode = "GET", corps, type } = {}) {
  const options = { method: methode, headers: {} };
  if (corps instanceof Blob) {
    options.body = corps;
    options.headers["Content-Type"] = type || corps.type;
  } else if (corps !== undefined) {
    options.body = JSON.stringify(corps);
    options.headers["Content-Type"] = "application/json";
  }

  let reponse;
  try {
    reponse = await fetch(`/api/v1${chemin}`, options);
  } catch {
    throw new ErreurApi(0, "API injoignable");
  }
  if (reponse.status === 401 && chemin !== "/auth/login") surDeconnexion("Session expirée, reconnectez-vous");

  const donnees = await reponse.json().catch(() => null);
  if (!reponse.ok) {
    const detail = typeof donnees?.detail === "string" ? donnees.detail : `erreur ${reponse.status}`;
    throw new ErreurApi(reponse.status, detail);
  }
  return donnees;
}

// URL du WebSocket : wss:// en HTTPS, ws:// en HTTP (dev)
export function urlWebSocket() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/ws`;
}
