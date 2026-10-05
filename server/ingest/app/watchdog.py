"""Détection DISPOSITIF_HORS_LIGNE : aucune mesure reçue depuis offline_after_s.

Une alerte par passage en ligne -> hors ligne. Au démarrage, les dispositifs déjà
silencieux sont notés sans alerte (pas de doublon à chaque redémarrage de l'API).
Un dispositif qui n'a jamais envoyé de mesure est ignoré (pas encore installé).
"""
import asyncio
import logging

log = logging.getLogger("ingest.watchdog")


class OfflineWatchdog:
    def __init__(self, settings, db):
        self.settings = settings
        self.db = db
        self.offline: set[int] = set()
        self._first_pass = True

    async def run(self):
        while True:
            try:
                await self.check()
            except Exception:
                log.exception("vérification des dispositifs impossible")
            await asyncio.sleep(self.settings.watchdog_interval_s)

    async def check(self):
        limit = self.settings.offline_after_s
        for device, serie, silence in await self.db.silences():
            if silence is None:
                continue
            if silence > limit and device not in self.offline:
                self.offline.add(device)
                if self._first_pass:
                    log.info("%s déjà hors ligne au démarrage (%.0f s)", serie, silence)
                    continue
                id_alerte = await self.db.insert_alerte(
                    id_dispositif=device, type_alerte="DISPOSITIF_HORS_LIGNE", origine="SYSTEME",
                    niveau="CRITIQUE",
                    message=f"Aucune mesure reçue de {serie} depuis {silence:.0f} s")
                log.warning("%s hors ligne : alerte %s", serie, id_alerte)
            elif silence <= limit and device in self.offline:
                self.offline.discard(device)
                log.info("%s de nouveau en ligne", serie)
        self._first_pass = False
