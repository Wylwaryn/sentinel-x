"""Bornes d'alerte des capteurs (DHT22 + MQ-2) : la couche de sécurité SOUS l'IA.

Deux niveaux par borne :
  avertissement  prévention : agir AVANT le problème (ex. 35 °C, 80 % d'humidité)
  critique       danger : agir tout de suite (ex. 45 °C : risque matériel et de départ de feu)

Rôle des bornes à côté du modèle :
  * l'IA détecte une DÉRIVE par rapport au fonctionnement normal appris, souvent bien avant les bornes ;
  * la prévision chiffre le délai avant la borne CRITIQUE (« 45 °C dans ~18 min ») ;
  * franchir une borne déclenche une alerte même si le modèle hésite : filet de sécurité déterministe.

Le MQ-2 n'est pas étalonné en ppm (valeur brute via un pont diviseur) : ses bornes sont RELATIVES à la
ligne de base apprise sur la pièce (médiane du fonctionnement normal), pas des valeurs absolues.
"""

# Type d'incident associé à chaque borne (quand le modèle n'a pas de type plus précis)
BOUND_KIND = {"temp_haut": "SURCHAUFFE", "hum_haut": "HUMIDITE_ELEVEE",
              "hum_bas": "HUMIDITE_BASSE", "gaz_haut": "FUITE_GAZ"}


def compute_bounds(limites, baseline=None):
    """Bornes absolues à partir de la config (`limites`) et de la ligne de base apprise.
    Retour : {clé: {"capteur", "sens", "avertissement", "critique"}}."""
    base_gaz = (baseline or {}).get("gaz") or limites["gaz"]["reference_defaut"]
    t, h, g = limites["temp"], limites["hum"], limites["gaz"]
    return {
        "temp_haut": {"capteur": "temp", "sens": "haut",
                      "avertissement": t["avertissement_haut"], "critique": t["critique_haut"]},
        "hum_haut": {"capteur": "hum", "sens": "haut",
                     "avertissement": h["avertissement_haut"], "critique": h["critique_haut"]},
        "hum_bas": {"capteur": "hum", "sens": "bas",
                    "avertissement": h["avertissement_bas"], "critique": h["critique_bas"]},
        "gaz_haut": {"capteur": "gaz", "sens": "haut",
                     "avertissement": round(base_gaz + g["avertissement_delta"]),
                     "critique": round(base_gaz + g["critique_delta"])},
    }


def _beyond(value, bound, level):
    limit = bound[level]
    return value >= limit if bound["sens"] == "haut" else value <= limit


def check_bounds(last, bounds, skip=()):
    """Borne la plus grave franchie MAINTENANT : (clé, "CRITIQUE"|"AVERTISSEMENT") ou None.
    `last` : dernières valeurs {"temp", "hum", "gaz"} (None si capteur muet). `skip` : capteurs ignorés."""
    worst = None
    for key, b in bounds.items():
        v = last.get(b["capteur"])
        if v is None or b["capteur"] in skip:
            continue
        if _beyond(v, b, "critique"):
            return key, "CRITIQUE"
        if worst is None and _beyond(v, b, "avertissement"):
            worst = (key, "AVERTISSEMENT")
    return worst
