"""Accès PostgreSQL avec le rôle sentinel_dashboard. Requêtes toujours paramétrées.

Le rôle ne lit les alertes qu'à travers v_alerte_supervision (sans RESEAU_IA) et ne peut modifier
que leur statut et leur résolution : PostgreSQL le garantit (server/db/tests/test_droits.sh).
Toutes les dates et heures sont en UTC ; la conversion vers Europe/Paris se fait dans le navigateur.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

# Périodes proposées pour les courbes (minutes) -> pas d'agrégation (None = mesures brutes).
# Au-delà d'une heure on regroupe par tranche : la courbe garde environ 360 points.
PERIODES = {15: None, 60: None, 360: timedelta(minutes=1), 1440: timedelta(minutes=4)}

_ALERTES_ORDRE = (
    " ORDER BY CASE niveau WHEN 'CRITIQUE' THEN 0 WHEN 'AVERTISSEMENT' THEN 1 ELSE 2 END,"
    " date_alerte DESC, heure_alerte DESC LIMIT %s")


def instant(d, h) -> str | None:
    """date + heure UTC -> ISO 8601 avec « Z », lisible directement par new Date() côté navigateur."""
    if d is None or h is None:
        return None
    return f"{d}T{h}Z"


def jsonable(row: dict) -> dict:
    """Decimal -> float, dates/heures -> texte ISO."""
    out = {}
    for k, v in row.items():
        if isinstance(v, Decimal):
            v = float(v)
        elif isinstance(v, datetime):
            v = v.isoformat() + "Z"
        elif isinstance(v, (date, time)):
            v = v.isoformat()
        out[k] = v
    return out


class Database:
    def __init__(self, settings):
        self.conninfo = {
            "host": settings.db_host,
            "dbname": settings.db_name,
            "user": settings.db_user,
            "password": settings.db_password,
            "application_name": "sentinel-dashboard",
            # La base est déjà en UTC (01-schema.sql) ; on le redit pour CURRENT_DATE / LOCALTIME.
            "options": "-c timezone=UTC",
        }
        self.pool = AsyncConnectionPool(
            kwargs={**self.conninfo, "autocommit": True, "row_factory": dict_row},
            min_size=1,
            max_size=8,
            open=False,
        )

    async def open(self):
        await self.pool.open(wait=False)

    async def close(self):
        await self.pool.close()

    async def _all(self, sql, params=()):
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchall()

    async def _one(self, sql, params=()):
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchone()

    async def _count(self, sql, params=()) -> int:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return cur.rowcount

    async def ping(self) -> bool:
        return (await self._one("SELECT 1 AS ok"))["ok"] == 1

    # ---------------- Utilisateurs ----------------
    async def utilisateur_par_email(self, email: str):
        return await self._one(
            "SELECT u.id_utilisateur, u.nom, u.mot_de_passe_hash, u.actif, r.code AS role "
            "FROM utilisateur u JOIN role r ON r.id_role = u.id_role "
            "WHERE lower(trim(u.email)) = lower(trim(%s))", (email,))

    async def utilisateur_par_id(self, id_utilisateur: int):
        return await self._one(
            "SELECT u.id_utilisateur, u.nom, u.actif, r.code AS role "
            "FROM utilisateur u JOIN role r ON r.id_role = u.id_role "
            "WHERE u.id_utilisateur = %s", (id_utilisateur,))

    async def hash_utilisateur(self, id_utilisateur: int) -> str | None:
        row = await self._one(
            "SELECT mot_de_passe_hash FROM utilisateur WHERE id_utilisateur = %s", (id_utilisateur,))
        return row["mot_de_passe_hash"] if row else None

    async def utilisateurs(self):
        rows = await self._all(
            "SELECT u.id_utilisateur, u.nom, u.email, u.actif, r.code AS role, u.date_creation, u.heure_creation "
            "FROM utilisateur u JOIN role r ON r.id_role = u.id_role ORDER BY u.nom")
        for r in rows:
            r["instant"] = instant(r["date_creation"], r["heure_creation"])
        return [jsonable(r) for r in rows]

    async def creer_utilisateur(self, nom: str, email: str, role: str, hash_: str) -> int | None:
        """Droits limités par PostgreSQL à ces 4 colonnes (03-comptes-dashboard.sql). None si le rôle n'existe pas.
        Email déjà pris : psycopg.errors.UniqueViolation."""
        row = await self._one(
            "INSERT INTO utilisateur (id_role, nom, email, mot_de_passe_hash) "
            "SELECT id_role, %s, lower(trim(%s)), %s FROM role WHERE code = %s RETURNING id_utilisateur",
            (nom, email, hash_, role))
        return row["id_utilisateur"] if row else None

    async def activer_utilisateur(self, id_utilisateur: int, actif: bool) -> bool:
        return await self._count(
            "UPDATE utilisateur SET actif = %s WHERE id_utilisateur = %s", (actif, id_utilisateur)) == 1

    # ---------------- Dispositifs et mesures ----------------
    async def dispositifs(self):
        rows = await self._all("SELECT * FROM v_dispositif_etat ORDER BY nom")
        for r in rows:
            r["instant"] = instant(r["date_mesure"], r["heure_mesure"])
        return [jsonable(r) for r in rows]

    async def numero_serie(self, id_dispositif: int) -> str | None:
        row = await self._one(
            "SELECT numero_serie FROM dispositif WHERE id_dispositif = %s", (id_dispositif,))
        return row["numero_serie"] if row else None

    async def mesures(self, id_dispositif: int, minutes: int):
        """Historique d'un ESP sur une période bornée (jamais toute la table)."""
        periode = timedelta(minutes=minutes)
        tranche = PERIODES[minutes]
        # Le filtre sur date_mesure seule permet d'utiliser l'index (id_dispositif, date_mesure, heure_mesure).
        where = (" FROM mesure WHERE id_dispositif = %(id)s"
                 " AND date_mesure >= ((now() AT TIME ZONE 'UTC') - %(p)s)::date"
                 " AND date_mesure + heure_mesure > (now() AT TIME ZONE 'UTC') - %(p)s")
        if tranche is None:
            sql = ("SELECT date_mesure + heure_mesure AS instant, temperature_c, humidite_pct,"
                   " gaz_brut, mouvement_detecte" + where + " ORDER BY date_mesure, heure_mesure")
        else:
            sql = ("SELECT date_bin(%(t)s, date_mesure + heure_mesure, TIMESTAMP '2000-01-01') AS instant,"
                   " round(avg(temperature_c), 2) AS temperature_c, round(avg(humidite_pct), 2) AS humidite_pct,"
                   " round(avg(gaz_brut))::int AS gaz_brut, bool_or(mouvement_detecte) AS mouvement_detecte"
                   + where + " GROUP BY 1 ORDER BY 1")
        rows = await self._all(sql, {"id": id_dispositif, "p": periode, "t": tranche})
        return [jsonable(r) for r in rows]

    # ---------------- Alertes (vue sans RESEAU_IA) ----------------
    async def alertes(self, ouvertes: bool, limite: int):
        where = " WHERE statut <> 'RESOLUE'" if ouvertes else ""
        rows = await self._all("SELECT * FROM v_alerte_supervision" + where + _ALERTES_ORDRE, (limite,))
        for r in rows:
            r["instant"] = instant(r["date_alerte"], r["heure_alerte"])
        return [jsonable(r) for r in rows]

    async def alerte_existe(self, id_alerte: int) -> bool:
        return await self._one(
            "SELECT 1 AS ok FROM v_alerte_supervision WHERE id_alerte = %s", (id_alerte,)) is not None

    async def acquitter(self, id_alerte: int) -> bool:
        return await self._count(
            "UPDATE v_alerte_supervision SET statut = 'ACQUITTEE' "
            "WHERE id_alerte = %s AND statut = 'NOUVELLE'", (id_alerte,)) == 1

    async def resoudre(self, id_alerte: int, id_utilisateur: int) -> bool:
        return await self._count(
            "UPDATE v_alerte_supervision SET statut = 'RESOLUE', id_resolu_par = %s, "
            "date_resolution = CURRENT_DATE, heure_resolution = LOCALTIME(6) "
            "WHERE id_alerte = %s AND statut <> 'RESOLUE'", (id_utilisateur, id_alerte)) == 1

    async def chemin_capture(self, id_alerte: int) -> str | None:
        row = await self._one(
            "SELECT chemin_capture FROM v_alerte_supervision WHERE id_alerte = %s", (id_alerte,))
        return row["chemin_capture"] if row else None

    # ---------------- Images de référence (ADMIN) ----------------
    async def images_reference(self):
        rows = await self._all(
            "SELECT i.id_image_reference, i.id_utilisateur, u.nom AS utilisateur_nom, r.code AS utilisateur_role,"
            " u.actif AS utilisateur_actif, i.id_ajoute_par, i.date_ajout, i.heure_ajout, i.active "
            "FROM image_reference i JOIN utilisateur u ON u.id_utilisateur = i.id_utilisateur "
            "JOIN role r ON r.id_role = u.id_role "
            "ORDER BY i.date_ajout DESC, i.heure_ajout DESC")
        for r in rows:
            r["instant"] = instant(r["date_ajout"], r["heure_ajout"])
        return [jsonable(r) for r in rows]

    async def ajouter_image(self, id_utilisateur: int, id_ajoute_par: int, chemin: str) -> int:
        row = await self._one(
            "INSERT INTO image_reference (id_utilisateur, id_ajoute_par, chemin_fichier) "
            "VALUES (%s, %s, %s) RETURNING id_image_reference", (id_utilisateur, id_ajoute_par, chemin))
        return row["id_image_reference"]

    async def activer_image(self, id_image: int, active: bool) -> bool:
        return await self._count(
            "UPDATE image_reference SET active = %s WHERE id_image_reference = %s", (active, id_image)) == 1

    async def chemin_image(self, id_image: int) -> str | None:
        row = await self._one(
            "SELECT chemin_fichier FROM image_reference WHERE id_image_reference = %s", (id_image,))
        return row["chemin_fichier"] if row else None
