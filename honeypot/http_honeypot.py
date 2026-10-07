#!/usr/bin/env python3
"""Faux dashboard HTTP : sert une page de login credible et LOGGE chaque requete (methode, chemin,
en-tetes, corps, identifiants tentes). Aucune authentification reelle, aucune donnee, aucune sortie.
(Explications detaillees : voir README.md)

Usage : http_honeypot.py --port 8080
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from common import record, safe_text

MAX_BODY = 8192  # on ne lit pas un corps geant (anti-saturation memoire)

# Fausse page : ressemble a un dashboard, ne mene a rien. Volontairement banale et credible.
LOGIN_PAGE = b"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<title>Supervision - Connexion</title></head><body style="font-family:sans-serif">
<h2>Supervision interne</h2>
<form method="post" action="/login">
<p>Utilisateur <input name="user"></p>
<p>Mot de passe <input name="password" type="password"></p>
<p><button>Connexion</button></p></form></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "nginx"      # en-tete Server credible et banal (ne trahit pas un honeypot Python)
    sys_version = ""

    def _log(self, event, extra=None):
        record({"service": "http", "event": event, "src_ip": self.client_address[0],
                "src_port": self.client_address[1], "method": self.command, "path": self.path,
                "headers": {k: v for k, v in self.headers.items()}, **(extra or {})})

    def _page(self, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(LOGIN_PAGE)))
        self.end_headers()
        self.wfile.write(LOGIN_PAGE)

    def do_GET(self):
        self._log("http")
        self._page()

    def do_POST(self):
        length = min(int(self.headers.get("Content-Length", 0) or 0), MAX_BODY)
        body = self.rfile.read(length) if length else b""
        creds = parse_qs(body.decode("latin-1"))     # ex. {'user': ['admin'], 'password': ['123456']}
        # On logge CE QUI EST TENTE (login/mot de passe, charge d'injection...) : c'est la valeur du leurre.
        self._log("http", {"body_txt": safe_text(body), "tentative": {k: v[0] for k, v in creds.items()}})
        self._page(401)                              # toujours "echec" : personne n'entre jamais

    def log_message(self, *a):                        # on coupe le log par defaut (le notre suffit)
        pass


def main():
    ap = argparse.ArgumentParser(description="Honeypot HTTP (faux dashboard)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--bind", default="0.0.0.0")
    args = ap.parse_args()
    record({"service": "http", "event": "listen", "dst_port": args.port})
    ThreadingHTTPServer((args.bind, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
