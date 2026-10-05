"""Garde ASGI devant /api/ : jeton Bearer et taille du corps vérifiés AVANT toute lecture du corps.

Un attaquant sans jeton ne peut donc ni faire parser du JSON ni envoyer de gros corps.
"""
import hmac
import json
import logging

log = logging.getLogger("ingest.security")


async def _reply(send, status: int, detail: str, headers=()):
    body = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()), *headers]})
    await send({"type": "http.response.body", "body": body})


class ApiGuard:
    def __init__(self, app, tokens: dict[str, str], max_body_bytes: int):
        self.app = app
        self.tokens = [(client, token.encode()) for client, token in tokens.items()]
        self.max_body = max_body_bytes

    def _client_for(self, header: bytes | None) -> str | None:
        if not header or not header.startswith(b"Bearer "):
            return None
        presented = header[7:].strip()
        found = None
        # On compare avec TOUS les jetons, en temps constant, sans sortir avant la fin.
        for client, token in self.tokens:
            if hmac.compare_digest(presented, token):
                found = client
        return found

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)

        headers = dict(scope["headers"])
        client = self._client_for(headers.get(b"authorization"))
        peer = (scope.get("client") or ("?",))[0]
        if client is None:
            log.warning("accès refusé (jeton absent ou invalide) depuis %s : %s %s",
                        peer, scope["method"], scope["path"])
            return await _reply(send, 401, "non autorisé", [(b"www-authenticate", b"Bearer")])

        if scope["method"] in ("POST", "PUT", "PATCH"):
            length = headers.get(b"content-length")
            if length is None or not length.isdigit():
                return await _reply(send, 411, "Content-Length obligatoire")
            if int(length) > self.max_body:
                log.warning("corps trop gros (%s octets) refusé pour %s", length.decode(), client)
                return await _reply(send, 413, "corps trop volumineux")

        scope.setdefault("state", {})["client"] = client
        return await self.app(scope, receive, send)
