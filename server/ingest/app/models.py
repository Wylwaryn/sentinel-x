"""Formats d'entrée et traduction vers le catalogue de la base.

La vision envoie son propre vocabulaire (info/warning/critical, presence, loitering...).
On accepte aussi directement les valeurs de la base (CRITIQUE, RODEUR...), pour l'IDS.
"""
from ipaddress import IPv4Address, IPv6Address

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

SERIE_PATTERN = r"^[A-Za-z0-9_.:-]{1,100}$"

# Miroir de la contrainte ck_type_selon_origine : on répond 422 au lieu d'une erreur SQL.
CATALOGUE = {
    "VISION_IA": {"PRESENCE", "RODEUR", "INTRUSION", "APPROCHE_RAPIDE"},
    "FUSION": {"PRESENCE", "RODEUR", "INTRUSION", "APPROCHE_RAPIDE"},
    "PIR": {"ANGLE_MORT"},
    "CAPTEURS_IA": {"ANOMALIE_ENVIRONNEMENTALE"},
    "SYSTEME": {"DISPOSITIF_HORS_LIGNE"},
    "RESEAU_IA": {"SCAN_PORTS", "DENI_DE_SERVICE", "FORCE_BRUTE", "TRAFIC_ANORMAL"},
}
NIVEAUX = {"INFORMATION", "AVERTISSEMENT", "CRITIQUE"}

ORIGINES_ALIAS = {"vision": "VISION_IA", "fusion": "FUSION", "pir": "PIR",
                  "capteurs": "CAPTEURS_IA", "network": "RESEAU_IA", "ids": "RESEAU_IA"}
TYPES_ALIAS = {"presence": "PRESENCE", "loitering": "RODEUR", "intrusion": "INTRUSION",
               "fast_approach": "APPROCHE_RAPIDE", "pir_blind_spot": "ANGLE_MORT",
               "env_anomaly": "ANOMALIE_ENVIRONNEMENTALE", "port_scan": "SCAN_PORTS",
               "dos": "DENI_DE_SERVICE", "brute_force": "FORCE_BRUTE",
               "anomalous_traffic": "TRAFIC_ANORMAL"}
NIVEAUX_ALIAS = {"info": "INFORMATION", "warning": "AVERTISSEMENT", "critical": "CRITIQUE"}


def _translate(value: str, alias: dict[str, str], valid) -> str | None:
    value = value.strip()
    translated = alias.get(value.lower(), value.upper())
    return translated if translated in valid else None


def translate_origine(value: str) -> str | None:
    return _translate(value, ORIGINES_ALIAS, CATALOGUE)


def translate_type(value: str, origine: str) -> str | None:
    return _translate(value, TYPES_ALIAS, CATALOGUE[origine])


def translate_niveau(value: str) -> str | None:
    return _translate(value, NIVEAUX_ALIAS, NIVEAUX)


class Telemetry(BaseModel):
    """Message MQTT de l'ESP8266 sur sentinel/telemetry. null = capteur en défaut."""
    model_config = ConfigDict(extra="ignore")

    serie: str = Field(pattern=SERIE_PATTERN)
    temperature_c: float | None = Field(default=None, ge=-40, le=80, allow_inf_nan=False)
    humidite_pct: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    gaz_brut: int | None = Field(default=None, ge=0, le=1023)
    pir: bool | None = None


class AlertIn(BaseModel):
    """POST /api/v1/alerts.

    Deux nommages acceptés pour chaque champ :
      - vocabulaire BDD (contrat de CLAUDE.md, IDS) : origine, type_alerte, niveau, numero_serie...
      - format historique de host/vision/alerts.py : source, type, level, serie...
    """
    model_config = ConfigDict(extra="ignore")

    type: str = Field(max_length=40, validation_alias=AliasChoices("type_alerte", "type"))
    level: str = Field(max_length=20, validation_alias=AliasChoices("niveau", "level"))
    source: str = Field(max_length=20, validation_alias=AliasChoices("origine", "source"))
    serie: str | None = Field(default=None, pattern=SERIE_PATTERN,
                              validation_alias=AliasChoices("numero_serie", "serie"))
    pir_confirmed: bool | None = Field(default=None, validation_alias=AliasChoices("pir_confirme", "pir_confirmed"))
    track_id: int | None = None
    zone: str | None = Field(default=None, max_length=50)
    message: str | None = Field(default=None, max_length=500)
    detail: dict = Field(default_factory=dict, validation_alias=AliasChoices("details", "detail"))
    ip_source: IPv4Address | IPv6Address | None = None
    score: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False,
                                validation_alias=AliasChoices("score_ia", "score"))
    # Réaction de l'IDS (alerte / bloquee) : journalisée, pas stockée en base.
    action: str | None = Field(default=None, max_length=30, pattern=r"^[a-z_]+$")
    # JPEG 640x480 : environ 100 Ko en base64. La taille du corps est déjà bornée par le middleware.
    snapshot_jpeg_b64: str | None = None

    def build_message(self, type_alerte: str) -> str:
        if self.message:
            return self.message
        detail_msg = self.detail.get("message") if isinstance(self.detail, dict) else None
        if isinstance(detail_msg, str) and detail_msg:
            return detail_msg[:500]
        parts = [type_alerte]
        if self.track_id is not None:
            parts.append(f"piste {self.track_id}")
        if self.zone:
            parts.append(f"zone {self.zone}")
        if self.ip_source:
            parts.append(f"source {self.ip_source}")
        return " ".join(parts)[:500]
