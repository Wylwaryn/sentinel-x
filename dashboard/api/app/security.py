"""Authentification du dashboard : Argon2id, jeton JWT dans un cookie HttpOnly, droits par rôle.

- Le jeton n'est jamais lisible par le JavaScript (HttpOnly) : une faille XSS ne peut pas le voler.
- SameSite=Strict : le navigateur ne l'envoie pas depuis un autre site (pas de CSRF).
- Le compte est relu en base à chaque requête : un compte désactivé perd l'accès immédiatement.
"""
import logging
import time
from collections import deque

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

log = logging.getLogger("dashboard.security")

COOKIE = "sentinel_session"
ROLES = ("LECTEUR", "OPERATEUR", "ADMIN")

# Droits applicatifs (fiche API dashboard, §5)
PEUT_AGIR = frozenset({"OPERATEUR", "ADMIN"})   # acquitter, résoudre, buzzer, LEDs
PEUT_ADMINISTRER = frozenset({"ADMIN"})         # images de référence

_hasher = PasswordHasher()  # Argon2id, paramètres recommandés par la RFC 9106
# Hash d'un mot de passe jetable : vérifié quand l'email est inconnu, pour que la réponse
# prenne le même temps (on ne doit pas pouvoir deviner les emails existants).
_HASH_LEURRE = _hasher.hash("leurre-sentinel-x")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(hash_: str | None, password: str) -> bool:
    try:
        return _hasher.verify(hash_ or _HASH_LEURRE, password) and hash_ is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


class Tokens:
    def __init__(self, secret: str, hours: float):
        self.secret = secret
        self.ttl = int(hours * 3600)

    def emettre(self, id_utilisateur: int) -> str:
        now = int(time.time())
        return jwt.encode({"sub": str(id_utilisateur), "iat": now, "exp": now + self.ttl},
                          self.secret, algorithm="HS256")

    def lire(self, token: str | None) -> tuple[int, int] | None:
        """(id_utilisateur, expiration epoch) si le jeton est valide, sinon None."""
        if not token:
            return None
        try:
            # algorithms imposé : pas de jeton « alg: none » ni de confusion d'algorithme.
            claims = jwt.decode(token, self.secret, algorithms=["HS256"], options={"require": ["exp", "sub"]})
            return int(claims["sub"]), int(claims["exp"])
        except (jwt.PyJWTError, ValueError):
            return None


class LoginLimiter:
    """Anti force brute : 5 échecs en 5 min par IP ou par email -> 429 jusqu'à la fin de la fenêtre."""

    def __init__(self, max_echecs: int = 5, fenetre_s: float = 300):
        self.max = max_echecs
        self.fenetre = fenetre_s
        self._echecs: dict[str, deque] = {}

    def _purge(self, cle: str, now: float) -> deque:
        q = self._echecs.setdefault(cle, deque())
        while q and q[0] < now - self.fenetre:
            q.popleft()
        return q

    def bloque(self, *cles: str) -> bool:
        now = time.monotonic()
        if len(self._echecs) > 10000:  # garde-fou mémoire
            self._echecs.clear()
        return any(len(self._purge(c, now)) >= self.max for c in cles)

    def echec(self, *cles: str):
        now = time.monotonic()
        for c in cles:
            self._purge(c, now).append(now)

    def succes(self, *cles: str):
        for c in cles:
            self._echecs.pop(c, None)
