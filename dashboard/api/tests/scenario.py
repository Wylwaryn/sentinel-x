"""Scénario de test de l'API dashboard, exécuté DANS le conteneur dashboard de la pile de test.

Lancé par test_dashboard.sh. Les secrets arrivent dans /tmp/test_secrets.json (jamais dans argv).
Les mesures et alertes sont insérées avec le compte administrateur PostgreSQL, comme le ferait
l'API d'ingestion : on vérifie ce que le dashboard en fait (LISTEN/NOTIFY, vues, droits).
"""
import asyncio
import json
import ssl
import time
import urllib.error
import urllib.request

import aiomqtt
import psycopg
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

S = json.load(open("/tmp/test_secrets.json"))
API = "http://127.0.0.1:8000"
WS = "ws://127.0.0.1:8000/ws"
ORIGIN = S["origin"]
SERIE = "TEST-ESP-01"
JPEG = b"\xff\xd8\xff\xe0" + b"sentinel" * 100 + b"\xff\xd9"

PASS = FAIL = 0


def ok(label):
    global PASS
    PASS += 1
    print(f"  OK    {label}", flush=True)


def ko(label):
    global FAIL
    FAIL += 1
    print(f"  ÉCHEC {label}", flush=True)


def check(obtenu, attendu, label):
    if obtenu == attendu:
        ok(label)
    else:
        ko(f"{label} (attendu « {attendu} », obtenu « {obtenu} »)")


# ---------------------------------------------------------------- HTTP
def http(methode, chemin, corps=None, cookie=None, origin=ORIGIN, type_="application/json", ip=None):
    """ip : simule une autre IP réelle (comme si Caddy l'avait posée dans X-Forwarded-For)."""
    data = None
    headers = {"X-Forwarded-For": ip} if ip else {}
    if corps is not None:
        data = corps if isinstance(corps, bytes) else json.dumps(corps).encode()
        headers["Content-Type"] = type_
    if cookie:
        headers["Cookie"] = cookie
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(API + chemin, data=data, method=methode, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def code(*a, **k):
    return http(*a, **k)[0]


def corps_json(*a, **k):
    return json.loads(http(*a, **k)[1])


def login(email, password):
    status, _, headers = http("POST", "/api/v1/auth/login", {"email": email, "password": password})
    cookie = headers.get("Set-Cookie", "")
    return status, cookie.split(";")[0] if cookie else None, cookie


# ---------------------------------------------------------------- base (compte admin, comme l'ingestion)
def admin_sql(sql, params=()):
    with psycopg.connect(host="postgres", dbname=S["db"], user=S["admin_user"], password=S["admin_pw"],
                         autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchone() if cur.description else None


def dashboard_sql(sql):
    """Requête directe avec le rôle sentinel_dashboard : renvoie l'erreur PostgreSQL ou None."""
    try:
        with psycopg.connect(host="postgres", dbname=S["db"], user="sentinel_dashboard",
                             password=S["dashboard_pw"], autocommit=True) as conn:
            conn.execute(sql)
        return None
    except psycopg.Error as e:
        return type(e).__name__


# ---------------------------------------------------------------- WebSocket
async def attendre(ws, predicat, delai=3.0, debut=None):
    """Premier message qui vérifie predicat, et le temps écoulé depuis debut (s), par défaut l'appel."""
    debut = debut or time.monotonic()
    fin = time.monotonic() + delai
    while (reste := fin - time.monotonic()) > 0:
        try:
            brut = await asyncio.wait_for(ws.recv(), reste)
        except asyncio.TimeoutError:
            break
        msg = brut if isinstance(brut, bytes) else json.loads(brut)
        if predicat(msg):
            return msg, time.monotonic() - debut
    return None, None


async def fermeture(cookie=None, origin=ORIGIN):
    headers = {}
    if cookie:
        headers["Cookie"] = cookie
    try:
        async with connect(WS, additional_headers=headers, origin=origin) as ws:
            await asyncio.wait_for(ws.recv(), 3)
            return "ouvert"
    except ConnectionClosed as e:
        return e.rcvd.code if e.rcvd else None
    except asyncio.TimeoutError:
        return "ouvert"


def mqtt(user):
    return aiomqtt.Client(hostname="mosquitto", port=8883, username=user, password=S[f"mqtt_{user}"],
                          identifier=f"test-dashboard-{user}", tls_context=ssl.create_default_context(cafile="/certs/ca.crt"))


async def main():
    dev = admin_sql("SELECT id_dispositif FROM dispositif WHERE numero_serie = %s", (SERIE,))[0]

    print("== Surface exposée ==")
    check(code("GET", "/healthz"), 200, "healthz")
    check(code("GET", "/docs"), 404, "/docs désactivé")
    check(code("GET", "/openapi.json"), 404, "/openapi.json désactivé")
    _, _, h = http("GET", "/healthz")
    check(h.get("server"), None, "pas d'en-tête Server")

    print("== Authentification ==")
    check(code("GET", "/api/v1/dispositifs"), 401, "sans session : 401")
    check(code("GET", "/api/v1/dispositifs", cookie="sentinel_session=jeton.invente.xx"), 401, "jeton inventé : 401")
    # Jeton « alg: none » forgé à la main : refusé
    forge = "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJzdWIiOiIxIiwiZXhwIjo5OTk5OTk5OTk5fQ."
    check(code("GET", "/api/v1/dispositifs", cookie=f"sentinel_session={forge}"), 401, "jeton alg=none : 401")
    check(login("admin@test.local", "mauvais-mot-de-passe")[0], 401, "mauvais mot de passe : 401")
    check(login("inconnu@test.local", "peu-importe-123")[0], 401, "email inconnu : 401 (même réponse)")
    check(login("' OR '1'='1' --", "x' OR '1'='1")[0], 401, "injection SQL dans l'email : 401")
    check(login("inactif@test.local", S["password"])[0], 401, "compte désactivé : 401")

    status, admin, set_cookie = login("  ADMIN@test.local ", S["password"])
    check(status, 200, "connexion ADMIN (email normalisé)")
    check(all(x in set_cookie for x in ("HttpOnly", "SameSite=strict", "Secure")), True,
          "cookie HttpOnly + SameSite=Strict + Secure")
    _, operateur, _ = login("operateur@test.local", S["password"])
    _, lecteur, _ = login("lecteur@test.local", S["password"])
    check(bool(operateur and lecteur), True, "connexion OPERATEUR et LECTEUR")
    check(corps_json("GET", "/api/v1/auth/me", cookie=lecteur)["utilisateur"]["role"], "LECTEUR", "/auth/me")
    if ORIGIN:
        check(code("POST", "/api/v1/auth/login", {"email": "x@y.z", "password": "x"}, origin="https://pirate.example"),
              403, "Origin étranger refusé (anti CSRF)")

    print("== Lecture ==")
    liste = corps_json("GET", "/api/v1/dispositifs", cookie=lecteur)
    check(any(d["numero_serie"] == SERIE for d in liste), True, "LECTEUR voit les dispositifs (v_dispositif_etat)")
    check(code("GET", f"/api/v1/dispositifs/{dev}/mesures?minutes=15", cookie=lecteur), 200, "historique 15 min")
    check(code("GET", f"/api/v1/dispositifs/{dev}/mesures?minutes=99999", cookie=lecteur), 422, "période hors liste : 422")
    check(code("GET", "/api/v1/dispositifs/1%20OR%201=1/mesures", cookie=lecteur), 422, "injection dans l'URL : 422")

    print("== Temps réel (LISTEN/NOTIFY -> WebSocket) ==")
    check(await fermeture(), 4401, "WebSocket sans session : fermé (4401)")
    if ORIGIN:
        check(await fermeture(admin, origin="https://pirate.example"), 4403, "WebSocket depuis un autre site : fermé (4403)")

    async with connect(WS, additional_headers={"Cookie": lecteur}, origin=ORIGIN, max_size=2**21) as ws, \
            mqtt("vision") as vision, mqtt("esp") as esp:
        await esp.subscribe(f"sentinel/cmd/{SERIE}", qos=1)

        t0 = time.monotonic()
        admin_sql("INSERT INTO mesure (id_dispositif, temperature_c, humidite_pct, gaz_brut, mouvement_detecte) "
                  "VALUES (%s, 22.5, 41, 180, false)", (dev,))
        msg, duree = await attendre(ws, lambda m: isinstance(m, dict) and m["type"] == "mesure", debut=t0)
        check(bool(msg and msg["data"]["gaz_brut"] == 180 and msg["data"]["instant"].endswith("Z")), True,
              "mesure reçue en WebSocket, instant UTC « Z »")
        check(duree is not None and duree < 1, True, f"mesure en moins d'1 s ({duree and round(duree * 1000)} ms)")

        t0 = time.monotonic()
        admin_sql("INSERT INTO alerte (id_dispositif, type_alerte, origine, niveau, zone, pir_confirme, score_ia,"
                  " chemin_capture) VALUES (%s, 'INTRUSION', 'FUSION', 'CRITIQUE', 'zone_interdite', true, 0.91,"
                  " 'captures/0123456789abcdef0123456789abcdef.jpg')", (dev,))
        msg, duree = await attendre(ws, lambda m: isinstance(m, dict) and m["type"] == "alerte", debut=t0)
        check(bool(msg and msg["data"]["niveau"] == "CRITIQUE" and msg["data"]["operation"] == "INSERT"), True,
              "alerte CRITIQUE reçue en WebSocket")
        check(duree is not None and duree < 1, True, f"alerte CRITIQUE en moins d'1 s ({duree and round(duree * 1000)} ms)")
        id_alerte = msg["data"]["id_alerte"] if msg else 0

        admin_sql("INSERT INTO alerte (type_alerte, origine, niveau, ip_source) "
                  "VALUES ('SCAN_PORTS', 'RESEAU_IA', 'CRITIQUE', '192.168.137.66')")
        msg, _ = await attendre(ws, lambda m: isinstance(m, dict) and m["type"] == "alerte", delai=1.5)
        check(msg, None, "alerte RESEAU_IA jamais envoyée au dashboard")

        await vision.publish("sentinel/video/cam1", JPEG)
        msg, duree = await attendre(ws, lambda m: isinstance(m, bytes))
        check(msg, JPEG, "image webcam MQTT relayée en binaire")
        await vision.publish("sentinel/video/cam1", b"<script>alert(1)</script>")
        msg, _ = await attendre(ws, lambda m: isinstance(m, bytes), delai=1)
        check(msg, None, "message vidéo qui n'est pas un JPEG : ignoré")

        print("== Alertes ==")
        ouvertes = corps_json("GET", "/api/v1/alertes", cookie=lecteur)
        check(any(a["id_alerte"] == id_alerte for a in ouvertes), True, "alerte visible dans /alertes")
        check(any(a["origine"] == "RESEAU_IA" for a in corps_json("GET", "/api/v1/alertes?toutes=true", cookie=admin)),
              False, "aucune RESEAU_IA dans /alertes")
        check(ouvertes[0]["niveau"], "CRITIQUE", "CRITIQUE en tête de liste")
        check(code("POST", f"/api/v1/alertes/{id_alerte}/acquitter", cookie=lecteur), 403, "LECTEUR ne peut pas acquitter")
        check(code("POST", f"/api/v1/alertes/{id_alerte}/acquitter", cookie=operateur), 200, "OPERATEUR acquitte")
        msg, _ = await attendre(ws, lambda m: isinstance(m, dict) and m["type"] == "alerte" and m["data"]["operation"] == "UPDATE")
        check(msg and msg["data"]["statut"], "ACQUITTEE", "acquittement notifié aux navigateurs")
        check(code("POST", f"/api/v1/alertes/{id_alerte}/acquitter", cookie=operateur), 409, "acquitter deux fois : 409")
        check(code("POST", "/api/v1/alertes/999999/resoudre", cookie=operateur), 404, "alerte inconnue : 404")
        reseau = admin_sql("SELECT id_alerte FROM alerte WHERE origine = 'RESEAU_IA' LIMIT 1")[0]
        check(code("POST", f"/api/v1/alertes/{reseau}/resoudre", cookie=admin), 404, "alerte RESEAU_IA : 404 (invisible)")
        check(code("POST", f"/api/v1/alertes/{id_alerte}/resoudre", cookie=operateur), 200, "OPERATEUR résout")
        resolue = admin_sql("SELECT statut, id_resolu_par IS NOT NULL, date_resolution + heure_resolution "
                            "<= (now() AT TIME ZONE 'UTC') + interval '1 second' FROM alerte WHERE id_alerte = %s",
                            (id_alerte,))
        check(resolue, ("RESOLUE", True, True), "résolution enregistrée (auteur, date UTC)")
        check(any(a["id_alerte"] == id_alerte for a in corps_json("GET", "/api/v1/alertes", cookie=lecteur)), False,
              "alerte résolue retirée des alertes ouvertes")
        check(code("GET", f"/api/v1/alertes/{id_alerte}/capture", cookie=lecteur), 404, "capture absente du disque : 404")

        print("== Commandes MQTT ==")
        cmd = {"commande": {"actionneur": "buzzer", "etat": "on", "duree_ms": 3000}}
        check(code("POST", f"/api/v1/dispositifs/{dev}/commandes", cmd, cookie=lecteur), 403, "LECTEUR ne peut pas commander")
        check(code("POST", f"/api/v1/dispositifs/{dev}/commandes", cmd, cookie=operateur), 202, "OPERATEUR : buzzer")
        try:
            recu = await asyncio.wait_for(anext(aiter(esp.messages)), 3)
            check(json.loads(recu.payload), cmd["commande"], f"l'ESP reçoit la commande sur sentinel/cmd/{SERIE}")
        except asyncio.TimeoutError:
            ko("l'ESP reçoit la commande")
        led = {"commande": {"actionneur": "led", "couleur": "rouge", "etat": "clignote"}}
        check(code("POST", f"/api/v1/dispositifs/{dev}/commandes", led, cookie=admin), 202, "ADMIN : LED rouge clignote")
        for mauvais, label in [
            ({"commande": {"actionneur": "buzzer", "etat": "on", "duree_ms": 600000}}, "buzzer 10 min"),
            ({"commande": {"actionneur": "led", "couleur": "bleu", "etat": "on"}}, "couleur inconnue"),
            ({"commande": {"actionneur": "relais", "etat": "on"}}, "actionneur inconnu"),
            ({"commande": {"actionneur": "led", "couleur": "vert", "etat": "on", "topic": "#"}}, "champ en trop"),
        ]:
            check(code("POST", f"/api/v1/dispositifs/{dev}/commandes", mauvais, cookie=operateur), 422, f"commande refusée : {label}")
        check(code("POST", "/api/v1/dispositifs/999999/commandes", cmd, cookie=operateur), 404, "dispositif inconnu : 404")

    print("== Images de référence (ADMIN) ==")
    uid = admin_sql("SELECT id_utilisateur FROM utilisateur WHERE email = 'operateur@test.local'")[0]
    check(code("GET", "/api/v1/images-reference", cookie=operateur), 403, "OPERATEUR : 403")
    st, b, _ = http("POST", f"/api/v1/images-reference?id_utilisateur={uid}", JPEG, cookie=admin, type_="image/jpeg")
    check(st, 201, "ADMIN ajoute une image")
    id_image = json.loads(b).get("id_image_reference") if st == 201 else 0
    check(code("POST", f"/api/v1/images-reference?id_utilisateur={uid}", b"<?php system($_GET[0]); ?>",
               cookie=admin, type_="image/jpeg"), 422, "fichier qui n'est pas un JPEG : 422")
    check(code("POST", f"/api/v1/images-reference?id_utilisateur={uid}", JPEG, cookie=admin, type_="text/html"),
          415, "mauvais Content-Type : 415")
    check(code("POST", f"/api/v1/images-reference?id_utilisateur={uid}", b"\xff\xd8\xff" + b"0" * (3 * 1024 * 1024),
               cookie=admin, type_="image/jpeg"), 413, "image de plus de 2 Mo : 413")
    st, b, h = http("GET", f"/api/v1/images-reference/{id_image}/fichier", cookie=admin)
    check((st, b, h.get("content-type")), (200, JPEG, "image/jpeg"), "image relue telle quelle")
    check(code("PATCH", f"/api/v1/images-reference/{id_image}", {"active": False}, cookie=admin), 200, "désactiver l'image")
    check(admin_sql("SELECT active FROM image_reference WHERE id_image_reference = %s", (id_image,))[0], False,
          "image désactivée en base")

    print("== Compte de service SERVICE_VISION (lecture des images uniquement) ==")
    st, service, _ = login("service@test.local", S["password"])
    check(st, 200, "connexion du compte de service")
    images = corps_json("GET", "/api/v1/images-reference", cookie=service)
    check(bool(images) and all({"utilisateur_role", "utilisateur_actif"} <= set(i) for i in images), True,
          "liste des images avec utilisateur_role et utilisateur_actif")
    check(code("GET", f"/api/v1/images-reference/{id_image}/fichier", cookie=service), 200, "lecture d'une image")
    for methode, chemin, corps, label in [
        ("GET", "/api/v1/dispositifs", None, "dispositifs"),
        ("GET", f"/api/v1/dispositifs/{dev}/mesures", None, "mesures"),
        ("GET", "/api/v1/alertes", None, "alertes"),
        ("GET", f"/api/v1/alertes/{id_alerte}/capture", None, "captures"),
        ("GET", "/api/v1/utilisateurs", None, "liste des utilisateurs"),
        ("POST", f"/api/v1/alertes/{id_alerte}/acquitter", None, "acquitter"),
        ("POST", f"/api/v1/dispositifs/{dev}/commandes", cmd, "commandes"),
        ("PATCH", f"/api/v1/images-reference/{id_image}", {"active": True}, "modifier une image"),
    ]:
        check(code(methode, chemin, corps, cookie=service), 403, f"service : {label} refusé (403)")
    check(code("POST", f"/api/v1/images-reference?id_utilisateur={uid}", JPEG, cookie=service, type_="image/jpeg"),
          403, "service : ajout d'image refusé (403)")
    check(await fermeture(service), 4403, "service : WebSocket refusé (4403)")
    check(code("GET", "/api/v1/images-reference", cookie=service, ip="192.168.137.66"), 403,
          "session de service volée, rejouée depuis une autre IP : 403 à chaque requête")

    print("== Utilisateurs (ADMIN) ==")
    compte = {"nom": "Nouveau membre", "email": "Nouveau@Test.local", "role": "LECTEUR",
              "mot_de_passe": "un-mot-de-passe-solide", "mot_de_passe_admin": S["password"]}
    check(code("POST", "/api/v1/utilisateurs", {**compte, "mot_de_passe_admin": "faux"}, cookie=admin), 403,
          "mauvais mot de passe ADMIN ressaisi : 403")
    check(code("POST", "/api/v1/utilisateurs", compte, cookie=operateur), 403, "OPERATEUR ne peut pas créer de compte")
    check(code("POST", "/api/v1/utilisateurs", {**compte, "role": "SERVICE_VISION"}, cookie=admin), 422,
          "rôle SERVICE_VISION refusé depuis l'API")
    check(code("POST", "/api/v1/utilisateurs", {**compte, "mot_de_passe": "court"}, cookie=admin), 422,
          "mot de passe de moins de 12 caractères : 422")
    check(code("POST", "/api/v1/utilisateurs", {**compte, "actif": False}, cookie=admin), 422, "champ en trop : 422")
    st, b, _ = http("POST", "/api/v1/utilisateurs", compte, cookie=admin)
    check(st, 201, "ADMIN crée un compte LECTEUR")
    nouveau = json.loads(b).get("id_utilisateur") if st == 201 else 0
    check(admin_sql("SELECT email, mot_de_passe_hash LIKE '$argon2id$%%', actif FROM utilisateur WHERE id_utilisateur = %s",
                    (nouveau,)), ("nouveau@test.local", True, True), "email normalisé, hash Argon2id, compte actif")
    check(login("nouveau@test.local", "un-mot-de-passe-solide")[0], 200, "le nouveau compte se connecte")
    check(code("POST", "/api/v1/utilisateurs", {**compte, "email": " NOUVEAU@test.local"}, cookie=admin), 409,
          "email déjà utilisé : 409")
    check(code("PATCH", f"/api/v1/utilisateurs/{nouveau}", {"actif": False}, cookie=admin), 200, "désactiver le compte")
    check(login("nouveau@test.local", "un-mot-de-passe-solide")[0], 401, "compte désactivé : connexion refusée")
    moi = corps_json("GET", "/api/v1/auth/me", cookie=admin)["utilisateur"]["id_utilisateur"]
    check(code("PATCH", f"/api/v1/utilisateurs/{moi}", {"actif": False}, cookie=admin), 409,
          "un ADMIN ne peut pas se désactiver lui-même")
    check(code("PATCH", f"/api/v1/utilisateurs/{nouveau}", {"actif": True}, cookie=operateur), 403,
          "OPERATEUR ne peut pas réactiver un compte")
    check(code("PATCH", "/api/v1/utilisateurs/999999", {"actif": True}, cookie=admin), 404, "compte inconnu : 404")

    print("== Droits PostgreSQL du rôle dashboard (défense en profondeur) ==")
    check(dashboard_sql("SELECT * FROM alerte"), "InsufficientPrivilege", "table alerte brute : refusée")
    check(dashboard_sql("UPDATE v_alerte_supervision SET niveau = 'INFORMATION'"), "InsufficientPrivilege", "changer un niveau : refusé")
    check(dashboard_sql("INSERT INTO mesure (id_dispositif) VALUES (1)"), "InsufficientPrivilege", "insérer une mesure : refusé")
    check(dashboard_sql("UPDATE utilisateur SET id_role = 1"), "InsufficientPrivilege", "changer un rôle : refusé")

    print("== Force brute ==")
    codes = [login("lecteur@test.local", f"essai-{i}")[0] for i in range(6)]
    check(codes[-1], 429, "6e échec en 5 min : 429")
    check(login("lecteur@test.local", S["password"])[0], 429, "même le bon mot de passe attend la fin du blocage")

    print(f"\n{PASS} OK, {FAIL} échec(s)")
    raise SystemExit(1 if FAIL else 0)


asyncio.run(main())
