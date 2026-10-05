"""Aides à l'explication d'une alerte (le type vient du classifieur, cf. model.py)."""
from features import FEATURES


def top_features(contributions, n=3):
    """Les caractéristiques qui s'écartent le plus de la normale : le « pourquoi » de l'alerte."""
    ranked = sorted(range(len(FEATURES)), key=lambda i: contributions[i], reverse=True)
    return [FEATURES[i] for i in ranked[:n]]


def level(score, critical_score):
    return "CRITIQUE" if score >= critical_score else "AVERTISSEMENT"
