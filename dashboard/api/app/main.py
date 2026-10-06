"""API dashboard Sentinel-X : lecture de la supervision physique, actions des opérateurs, temps réel.

Derrière Caddy (seul point d'entrée, HTTPS/WSS :443) ; aucun port publié.
  - REST  /api/v1/...   dispositifs, mesures, alertes, commandes, utilisateurs, images de référence
  - WS    /ws           mesures et alertes (LISTEN/NOTIFY), flux webcam (MQTT)

Le compte de service SERVICE_VISION (synchronisation des visages) n'a droit qu'à la lecture des images
de référence : toute autre route le refuse (403), y compris le WebSocket. Il n'est accepté que depuis
les IP de SERVICE_VISION_IPS (le PC hôte, 10.0.2.2 dans la VM), à la connexion et à chaque requête.

Utilisateurs et images de référence (données biométriques) : ADMIN ET poste hôte (HOST_ONLY_IPS). Un ADMIN
connecté depuis un autre poste (session volée, poste laissé ouvert) ne peut ni créer de compte ni voir les visages.

IP du client : uvicorn --proxy-headers (Dockerfile) remplace request.client par l'IP que Caddy met dans
X-Forwarded-For. Caddy écrase ce que le client envoie (pas de trusted_proxies) : l'en-tête ne se forge pas.
"""
import asyncio
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
import psycopg
from pydantic import BaseModel, ConfigDict, Field

from . import config
from .db import PERIODES, Database
from .realtime import Client, Hub, MqttLink, PgListener
from .security import (COOKIE, PEUT_ADMINISTRER, PEUT_AGIR, PEUT_LIRE, PEUT_LIRE_IMAGES, ROLES, SERVICE_VISION,
                       LoginLimiter, Tokens, hash_password, verify_password)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s")
log = logging.getLogger("dashboard")

settings = config.load()
db = Database(settings)
hub = Hub()
listener = PgListener(db.conninfo, hub)
mqtt = MqttLink(settings, hub)
tokens = Tokens(settings.jwt_secret, settings.session_hours)
limiter = LoginLimiter()

SERIE_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")            # jamais de + # / dans un topic MQTT
FICHIER_OK = re.compile(r"^(captures|references)/[0-9a-f]{32}\.jpg$")
EMAIL_OK = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# LED rouge du boîtier pilotée par les alertes CRITIQUE (demande IoT). Le firmware tient une commande LED
# pendant duree_ms (30 min max, LED_MAX_HOLD_MS) : une seule commande suffit, pas de renvoi périodique.
LED_CRITIQUE_ON = {"actionneur": "led", "couleur": "rouge", "etat": "clignote", "duree_ms": 1_800_000}
LED_CRITIQUE_OFF = {"actionneur": "led", "couleur": "rouge", "etat": "off"}


async def led_sur_alerte(alerte: dict):
    """Nouvelle alerte CRITIQUE : LED rouge clignotante. Alerte CRITIQUE acquittée ou résolue et plus
    aucune en attente sur ce boîtier : LED éteinte. Commande envoyée par l'API (compte MQTT dashboard)."""
    if alerte.get("niveau") != "CRITIQUE" or not alerte.get("id_dispositif"):
        return
    id_dispositif = alerte["id_dispositif"]
    try:
        if alerte.get("operation") == "INSERT" and alerte.get("statut") == "NOUVELLE":
            commande = LED_CRITIQUE_ON
        elif alerte.get("operation") == "UPDATE" and alerte.get("statut") != "NOUVELLE"                 and await db.critiques_en_attente(id_dispositif) == 0:
            commande = LED_CRITIQUE_OFF
        else:
            return
        serie = await db.numero_serie(id_dispositif)
        if not serie or not SERIE_OK.match(serie):
            return
        if await mqtt.commande(serie, commande):
            log.info("LED automatique %s vers %s (alerte %s)", commande["etat"], serie, alerte.get("id_alerte"))
        else:
            log.warning("LED automatique non envoyée vers %s : broker MQTT indisponible", serie)
    except Exception:
        log.exception("LED automatique : échec pour l'alerte %s", alerte.get("id_alerte"))


listener.sur_alerte = led_sur_alerte


@asynccontextmanager
async def lifespan(_app):
    await db.open()
    tasks = [asyncio.create_task(listener.run(), name="listen"),
             asyncio.create_task(mqtt.run(), name="mqtt")]
    log.info("API dashboard prête")
    yield
    for task in tasks:
        task.cancel()
    await db.close()


# Pas de /docs ni d'/openapi.json : rien à cartographier pour les attaquants de jeudi.
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(RequestValidationError)
async def validation_error(_request, exc: RequestValidationError):
    # Pas d'écho des valeurs reçues.
    errors = [{"champ": ".".join(str(p) for p in e["loc"][1:]), "erreur": e["msg"]} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


def _ip(request: Request) -> str:
    """IP réelle du navigateur (posée par Caddy, voir l'en-tête du fichier)."""
    return request.client.host if request.client else "?"


def _service_hors_hote(role: str, ip: str) -> bool:
    return role == SERVICE_VISION and ip not in settings.service_vision_ips


def _origine_ok(origin: str | None) -> bool:
    return not settings.allowed_origins or origin in settings.allowed_origins


@app.middleware("http")
async def garde(request: Request, call_next):
    # Défense en plus du cookie SameSite=Strict : une requête qui modifie quelque chose
    # doit venir de la page du dashboard elle-même.
    if request.method not in ("GET", "HEAD") and not _origine_ok(request.headers.get("origin")):
        return JSONResponse(status_code=403, content={"detail": "origine refusée"})
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------- utilisateur connecté
class Utilisateur(BaseModel):
    id_utilisateur: int
    nom: str
    role: str


async def _utilisateur(token: str | None) -> tuple[Utilisateur, int] | None:
    lu = tokens.lire(token)
    if lu is None:
        return None
    row = await db.utilisateur_par_id(lu[0])
    if row is None or not row["actif"]:
        return None
    return Utilisateur(id_utilisateur=row["id_utilisateur"], nom=row["nom"], role=row["role"]), lu[1]


async def session(request: Request) -> Utilisateur:
    """N'importe quel compte actif, y compris le compte de service : réservé à /auth/me."""
    trouve = await _utilisateur(request.cookies.get(COOKIE))
    if trouve is None:
        raise HTTPException(401, "non connecté")
    u = trouve[0]
    if _service_hors_hote(u.role, _ip(request)):
        log.warning("compte de service %s refusé depuis %s (hors SERVICE_VISION_IPS)", u.id_utilisateur, _ip(request))
        raise HTTPException(403, "compte de service : accès réservé au PC hôte")
    return u


async def connecte(u: Annotated[Utilisateur, Depends(session)]) -> Utilisateur:
    """Une personne (LECTEUR, OPERATEUR, ADMIN) : base de toutes les routes, refus par défaut du service."""
    if u.role not in PEUT_LIRE:
        raise HTTPException(403, "compte de service : accès limité aux images de référence")
    return u


def _poste_hote(request: Request) -> bool:
    return _ip(request) in settings.host_only_ips


def _exiger_poste_hote(u: Utilisateur, request: Request):
    if not _poste_hote(request):
        log.warning("ADMIN %s refusé hors du PC hôte depuis %s : %s %s",
                    u.id_utilisateur, _ip(request), request.method, request.url.path)
        raise HTTPException(403, "réservé au PC hôte")


async def lecteur_images(request: Request, u: Annotated[Utilisateur, Depends(session)]) -> Utilisateur:
    """ADMIN depuis le PC hôte, ou compte de service (sa propre règle d'IP est vérifiée par session)."""
    if u.role not in PEUT_LIRE_IMAGES:
        raise HTTPException(403, "réservé aux administrateurs")
    if u.role != SERVICE_VISION:
        _exiger_poste_hote(u, request)
    return u


async def operateur(u: Annotated[Utilisateur, Depends(connecte)]) -> Utilisateur:
    if u.role not in PEUT_AGIR:
        raise HTTPException(403, "réservé aux opérateurs")
    return u


async def admin(u: Annotated[Utilisateur, Depends(connecte)]) -> Utilisateur:
    if u.role not in PEUT_ADMINISTRER:
        raise HTTPException(403, "réservé aux administrateurs")
    return u


async def admin_hote(request: Request, u: Annotated[Utilisateur, Depends(admin)]) -> Utilisateur:
    """ADMIN connecté depuis le PC hôte : utilisateurs et images de référence."""
    _exiger_poste_hote(u, request)
    return u


Session = Annotated[Utilisateur, Depends(session)]
Connecte = Annotated[Utilisateur, Depends(connecte)]
LecteurImages = Annotated[Utilisateur, Depends(lecteur_images)]
Operateur = Annotated[Utilisateur, Depends(operateur)]
AdminHote = Annotated[Utilisateur, Depends(admin_hote)]


# ---------------------------------------------------------------- santé
@app.get("/healthz")
async def healthz():
    try:
        db_ok = await asyncio.wait_for(db.ping(), timeout=3)
    except Exception:
        db_ok = False
    etat = {"db": db_ok, "listen": listener.connected, "mqtt": mqtt.connected}
    return JSONResponse(status_code=200 if db_ok else 503, content={"status": "ok" if db_ok else "ko", **etat})


# ---------------------------------------------------------------- authentification
class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=256)


@app.post("/api/v1/auth/login")
async def login(body: LoginIn, request: Request):
    ip = _ip(request)
    cles = (f"ip:{ip}", f"email:{body.email.strip().lower()}")
    if limiter.bloque(*cles):
        log.warning("connexion bloquée (trop d'échecs) depuis %s", ip)
        raise HTTPException(429, "trop de tentatives, réessayez dans quelques minutes")

    row = await db.utilisateur_par_email(body.email)
    # Le hash est vérifié même si l'email est inconnu : même durée de réponse dans tous les cas.
    ok = await asyncio.to_thread(verify_password, row["mot_de_passe_hash"] if row else None, body.password)
    if not ok or not row["actif"]:
        limiter.echec(*cles)
        log.warning("échec de connexion depuis %s", ip)
        raise HTTPException(401, "email ou mot de passe incorrect")

    if _service_hors_hote(row["role"], ip):
        # Bon mot de passe mais mauvaise machine : compté comme un échec (mot de passe peut-être volé).
        limiter.echec(*cles)
        log.warning("compte de service %s : connexion refusée depuis %s (hors SERVICE_VISION_IPS)",
                    row["id_utilisateur"], ip)
        raise HTTPException(403, "compte de service : accès réservé au PC hôte")

    limiter.succes(*cles)
    log.info("connexion de l'utilisateur %s (%s) depuis %s", row["id_utilisateur"], row["role"], ip)
    utilisateur = {"id_utilisateur": row["id_utilisateur"], "nom": row["nom"], "role": row["role"],
                   "poste_hote": _poste_hote(request)}
    response = JSONResponse({"utilisateur": utilisateur})
    response.set_cookie(COOKIE, tokens.emettre(row["id_utilisateur"]), max_age=tokens.ttl, path="/",
                        httponly=True, secure=settings.cookie_secure, samesite="strict")
    return response


@app.post("/api/v1/auth/logout")
async def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE, path="/", httponly=True, secure=settings.cookie_secure, samesite="strict")
    return response


@app.get("/api/v1/auth/me")
async def moi(u: Session, request: Request):
    # poste_hote : l'interface masque Utilisateurs et Images de référence ailleurs (l'API refuse de toute façon).
    return {"utilisateur": {**u.model_dump(), "poste_hote": _poste_hote(request)}}


# ---------------------------------------------------------------- dispositifs et mesures
@app.get("/api/v1/dispositifs")
async def dispositifs(_u: Connecte):
    return await db.dispositifs()


@app.get("/api/v1/dispositifs/{id_dispositif}/mesures")
async def mesures(id_dispositif: int, _u: Connecte, minutes: int = 15):
    if minutes not in PERIODES:
        raise HTTPException(422, f"période possible : {sorted(PERIODES)} minutes")
    return await db.mesures(id_dispositif, minutes)


class Buzzer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actionneur: Literal["buzzer"]
    etat: Literal["on", "off"]
    duree_ms: int = Field(default=3000, ge=100, le=10000)   # l'ESP plafonne aussi à 10 s


class Led(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actionneur: Literal["led"]
    couleur: Literal["rouge", "vert"]
    etat: Literal["on", "off", "clignote"]


class CommandeIn(BaseModel):
    commande: Buzzer | Led = Field(discriminator="actionneur")


@app.post("/api/v1/dispositifs/{id_dispositif}/commandes", status_code=202)
async def commande(id_dispositif: int, body: CommandeIn, u: Operateur):
    serie = await db.numero_serie(id_dispositif)
    if serie is None:
        raise HTTPException(404, "dispositif inconnu")
    if not SERIE_OK.match(serie):
        raise HTTPException(409, "numéro de série inutilisable comme topic MQTT")
    payload = body.commande.model_dump(exclude={"duree_ms"} if body.commande.etat == "off" else None)
    if not await mqtt.commande(serie, payload):
        raise HTTPException(503, "broker MQTT indisponible")
    log.info("commande %s vers %s par l'utilisateur %s", payload, serie, u.id_utilisateur)
    return {"envoyee": payload, "topic": f"sentinel/cmd/{serie}"}


# ---------------------------------------------------------------- alertes
@app.get("/api/v1/alertes")
async def alertes(_u: Connecte, toutes: bool = False, limite: int = Query(50, ge=1, le=200)):
    return await db.alertes(ouvertes=not toutes, limite=limite)


async def _changer_statut(id_alerte: int, fait: bool):
    if fait:
        return {"ok": True}
    if not await db.alerte_existe(id_alerte):
        raise HTTPException(404, "alerte inconnue")
    raise HTTPException(409, "statut déjà changé")


@app.post("/api/v1/alertes/{id_alerte}/acquitter")
async def acquitter(id_alerte: int, u: Operateur):
    fait = await db.acquitter(id_alerte)
    if fait:
        log.info("alerte %s acquittée par l'utilisateur %s", id_alerte, u.id_utilisateur)
    return await _changer_statut(id_alerte, fait)


@app.post("/api/v1/alertes/{id_alerte}/resoudre")
async def resoudre(id_alerte: int, u: Operateur):
    fait = await db.resoudre(id_alerte, u.id_utilisateur)
    if fait:
        log.info("alerte %s résolue par l'utilisateur %s", id_alerte, u.id_utilisateur)
    return await _changer_statut(id_alerte, fait)


def _fichier(chemin: str | None) -> FileResponse:
    # Le chemin vient de la base, mais on ne lui fait pas confiance : format strict, pas de « .. ».
    if not chemin or not FICHIER_OK.match(chemin):
        raise HTTPException(404, "image absente")
    dossier, nom = chemin.split("/")
    base = settings.captures_dir if dossier == "captures" else settings.references_dir
    chemin_local = os.path.join(base, nom)
    if not os.path.isfile(chemin_local):
        raise HTTPException(404, "image absente")
    return FileResponse(chemin_local, media_type="image/jpeg")


@app.get("/api/v1/alertes/{id_alerte}/capture")
async def capture(id_alerte: int, _u: Connecte):
    return _fichier(await db.chemin_capture(id_alerte))


# ---------------------------------------------------------------- images de référence (ADMIN)
@app.get("/api/v1/utilisateurs")
async def utilisateurs(_u: AdminHote):
    return await db.utilisateurs()


class CompteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nom: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=255)
    role: Literal[ROLES]   # jamais SERVICE_VISION depuis l'interface (la base le refuse aussi)
    mot_de_passe: str = Field(min_length=12, max_length=256)
    mot_de_passe_admin: str = Field(min_length=1, max_length=256)


@app.post("/api/v1/utilisateurs", status_code=201)
async def creer_utilisateur(body: CompteIn, request: Request, u: AdminHote):
    """Création d'un compte par un ADMIN, qui doit ressaisir SON mot de passe : un cookie volé ne suffit pas."""
    ip = _ip(request)
    cles = (f"ip:{ip}", f"admin:{u.id_utilisateur}")
    if limiter.bloque(*cles):
        raise HTTPException(429, "trop de tentatives, réessayez dans quelques minutes")
    if not await asyncio.to_thread(verify_password, await db.hash_utilisateur(u.id_utilisateur), body.mot_de_passe_admin):
        limiter.echec(*cles)
        log.warning("création de compte refusée : mot de passe de l'ADMIN %s incorrect (%s)", u.id_utilisateur, ip)
        raise HTTPException(403, "mot de passe administrateur incorrect")
    limiter.succes(*cles)
    if not EMAIL_OK.match(body.email.strip()):
        raise HTTPException(422, "email invalide")
    nom = body.nom.strip()
    if not nom:
        raise HTTPException(422, "nom vide")

    hash_ = await asyncio.to_thread(hash_password, body.mot_de_passe)
    try:
        id_nouveau = await db.creer_utilisateur(nom, body.email, body.role, hash_)
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "email déjà utilisé")
    if id_nouveau is None:
        raise HTTPException(422, "rôle inconnu")
    log.info("compte %s (%s) créé par l'ADMIN %s", id_nouveau, body.role, u.id_utilisateur)
    return {"id_utilisateur": id_nouveau}


class ActifIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actif: bool


@app.patch("/api/v1/utilisateurs/{id_utilisateur}")
async def activer_utilisateur(id_utilisateur: int, body: ActifIn, u: AdminHote):
    if id_utilisateur == u.id_utilisateur and not body.actif:
        raise HTTPException(409, "un administrateur ne peut pas désactiver son propre compte")
    if not await db.activer_utilisateur(id_utilisateur, body.actif):
        raise HTTPException(404, "utilisateur inconnu")
    log.info("compte %s actif=%s par l'ADMIN %s", id_utilisateur, body.actif, u.id_utilisateur)
    return {"ok": True}


@app.get("/api/v1/images-reference")
async def images_reference(_u: LecteurImages):
    return await db.images_reference()


@app.post("/api/v1/images-reference", status_code=201)
async def ajouter_image(request: Request, u: AdminHote, id_utilisateur: int):
    """Corps = l'image JPEG brute (Content-Type: image/jpeg). Le nom du fichier est choisi par le serveur."""
    if request.headers.get("content-type") != "image/jpeg":
        raise HTTPException(415, "JPEG uniquement (Content-Type: image/jpeg)")
    jpeg = bytearray()
    async for morceau in request.stream():
        jpeg += morceau
        if len(jpeg) > settings.max_image_bytes:
            raise HTTPException(413, "image trop volumineuse")
    if not jpeg.startswith(b"\xff\xd8\xff"):
        raise HTTPException(422, "ce n'est pas un JPEG")

    nom = f"{uuid.uuid4().hex}.jpg"
    await asyncio.to_thread(_ecrire, os.path.join(settings.references_dir, nom), bytes(jpeg))
    try:
        id_image = await db.ajouter_image(id_utilisateur, u.id_utilisateur, f"references/{nom}")
    except Exception:
        os.remove(os.path.join(settings.references_dir, nom))
        raise HTTPException(422, "utilisateur inconnu")
    log.info("image de référence %s ajoutée pour l'utilisateur %s par %s", id_image, id_utilisateur, u.id_utilisateur)
    return {"id_image_reference": id_image}


def _ecrire(chemin: str, contenu: bytes):
    # « x » : jamais d'écrasement d'un fichier existant.
    with open(chemin, "xb") as f:
        f.write(contenu)


class ActiveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool


@app.patch("/api/v1/images-reference/{id_image}")
async def activer_image(id_image: int, body: ActiveIn, u: AdminHote):
    if not await db.activer_image(id_image, body.active):
        raise HTTPException(404, "image inconnue")
    log.info("image de référence %s active=%s par %s", id_image, body.active, u.id_utilisateur)
    return {"ok": True}


@app.get("/api/v1/images-reference/{id_image}/fichier")
async def fichier_image(id_image: int, _u: LecteurImages):
    return _fichier(await db.chemin_image(id_image))


# ---------------------------------------------------------------- WebSocket
@app.websocket("/ws")
async def websocket(ws: WebSocket):
    await ws.accept()
    if not _origine_ok(ws.headers.get("origin")):
        return await ws.close(code=4403)
    trouve = await _utilisateur(ws.cookies.get(COOKIE))
    if trouve is None:
        # 4401 : le dashboard revient à l'écran de connexion.
        return await ws.close(code=4401)
    utilisateur, expiration = trouve
    if utilisateur.role not in PEUT_LIRE:
        return await ws.close(code=4403)   # compte de service : pas de temps réel
    if hub.plein():
        return await ws.close(code=1013)

    client = Client(ws)

    async def lire():
        # Le navigateur n'envoie rien d'utile : on lit seulement pour voir la déconnexion.
        while (await ws.receive())["type"] != "websocket.disconnect":
            pass

    async def expirer():
        await asyncio.sleep(max(0, expiration - time.time()))

    taches = [asyncio.create_task(hub.servir(client)), asyncio.create_task(lire()),
              asyncio.create_task(expirer())]
    fini, _ = await asyncio.wait(taches, return_when=asyncio.FIRST_COMPLETED)
    for t in taches:
        t.cancel()
    for t in fini:
        t.exception()  # envoi vers un navigateur déjà parti : attendu, rien à signaler
    try:
        if taches[2] in fini:
            await ws.close(code=4401)   # session expirée
        elif client.trop_lent:
            await ws.close(code=1013)
    except (WebSocketDisconnect, RuntimeError):
        pass
    log.debug("WebSocket fermé pour l'utilisateur %s", utilisateur.id_utilisateur)
