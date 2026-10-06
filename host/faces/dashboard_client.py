"""Client de l'API dashboard (compte ADMIN) pour l'enrôlement : utilisateurs et images de référence.

Se comporte comme le navigateur : cookie de session HttpOnly, en-tête Origin attendu par l'API,
certificat vérifié avec notre CA. Le mot de passe n'est jamais stocké.
"""
from pathlib import Path

import requests

HOST = Path(__file__).resolve().parent.parent
DEFAULT_BASE = "https://192.168.137.1"
DEFAULT_CA = HOST / "certs" / "ca.crt"


class DashboardError(RuntimeError):
    pass


class DashboardClient:
    def __init__(self, base=DEFAULT_BASE, ca=DEFAULT_CA, timeout=10):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.s = requests.Session()
        self.s.verify = str(ca)
        self.s.headers["Origin"] = self.base
        self.user = None

    def _check(self, r):
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise DashboardError(f"{r.status_code} : {detail}")
        return r

    def login(self, email, password, allowed_roles=("ADMIN",)):
        r = self._check(self.s.post(f"{self.base}/api/v1/auth/login",
                                    json={"email": email, "password": password}, timeout=self.timeout))
        self.user = r.json()["utilisateur"]
        if self.user["role"] not in allowed_roles:
            raise DashboardError(f"rôle {self.user['role']} insuffisant (attendu : {', '.join(allowed_roles)})")
        return self.user

    def users(self):
        return self._check(self.s.get(f"{self.base}/api/v1/utilisateurs", timeout=self.timeout)).json()

    def images(self):
        return self._check(self.s.get(f"{self.base}/api/v1/images-reference", timeout=self.timeout)).json()

    def image_file(self, id_image):
        return self._check(self.s.get(f"{self.base}/api/v1/images-reference/{id_image}/fichier",
                                      timeout=self.timeout)).content

    def upload(self, id_utilisateur, jpeg):
        r = self.s.post(f"{self.base}/api/v1/images-reference", params={"id_utilisateur": id_utilisateur},
                        data=jpeg, headers={"Content-Type": "image/jpeg"}, timeout=self.timeout)
        return self._check(r).json()["id_image_reference"]

    def logout(self):
        try:
            self.s.post(f"{self.base}/api/v1/auth/logout", timeout=self.timeout)
        except requests.RequestException:
            pass
