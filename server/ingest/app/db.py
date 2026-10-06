"""Accès PostgreSQL avec le rôle sentinel_ingest. Requêtes toujours paramétrées.

L'horodatage (date_xxx, heure_xxx) est posé par la base à l'insertion, en UTC :
on ne fait jamais confiance à l'heure fournie par un client.
"""
import time

from psycopg_pool import AsyncConnectionPool

_DEVICE_TTL_S = 30.0
_DEVICE_CACHE_MAX = 1000


class Database:
    def __init__(self, settings):
        self.pool = AsyncConnectionPool(
            kwargs={
                "host": settings.db_host,
                "dbname": settings.db_name,
                "user": settings.db_user,
                "password": settings.db_password,
                "autocommit": True,
                "application_name": "sentinel-ingest",
            },
            min_size=1,
            max_size=5,
            open=False,
        )
        self._devices: dict[str, tuple[int | None, float]] = {}

    async def open(self):
        await self.pool.open(wait=False)

    async def close(self):
        await self.pool.close()

    async def device_id(self, serie: str) -> int | None:
        """id_dispositif d'un numéro de série, avec cache (y compris négatif, contre le spam)."""
        now = time.monotonic()
        cached = self._devices.get(serie)
        if cached and cached[1] > now:
            return cached[0]
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id_dispositif FROM dispositif WHERE numero_serie = %s", (serie,))
            row = await cur.fetchone()
        if len(self._devices) >= _DEVICE_CACHE_MAX:
            self._devices.clear()
        device = row[0] if row else None
        self._devices[serie] = (device, now + _DEVICE_TTL_S)
        return device

    async def insert_mesure(self, id_dispositif, t) -> int:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO mesure (id_dispositif, temperature_c, humidite_pct, gaz_brut, mouvement_detecte) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id_mesure",
                (id_dispositif, t.temperature_c, t.humidite_pct, t.gaz_brut, t.pir))
            return (await cur.fetchone())[0]

    async def insert_alerte(self, *, id_dispositif, type_alerte, origine, niveau, message,
                            ip_source=None, score_ia=None, zone=None, pir_confirme=None,
                            chemin_capture=None, id_personne_reconnue=None) -> int:
        # id_personne_reconnue : la clé étrangère vers utilisateur est vérifiée par PostgreSQL avec les
        # droits du propriétaire de la table : le rôle ingest n'a toujours aucun accès à utilisateur.
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO alerte (id_dispositif, type_alerte, origine, niveau, message, "
                "ip_source, score_ia, zone, pir_confirme, chemin_capture, id_personne_reconnue) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id_alerte",
                (id_dispositif, type_alerte, origine, niveau, message,
                 ip_source, score_ia, zone, pir_confirme, chemin_capture, id_personne_reconnue))
            return (await cur.fetchone())[0]

    async def silences(self) -> list[tuple[int, str, float | None]]:
        """(id_dispositif, numero_serie, secondes depuis la dernière mesure ou None)."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT d.id_dispositif, d.numero_serie, "
                "       EXTRACT(EPOCH FROM (now() AT TIME ZONE 'UTC') - (m.date_mesure + m.heure_mesure)) "
                "FROM dispositif d "
                "LEFT JOIN LATERAL ("
                "    SELECT date_mesure, heure_mesure FROM mesure "
                "    WHERE mesure.id_dispositif = d.id_dispositif "
                "    ORDER BY date_mesure DESC, heure_mesure DESC LIMIT 1"
                ") m ON TRUE")
            return [(r[0], r[1], None if r[2] is None else float(r[2])) for r in await cur.fetchall()]
