"""Détecteur EMBARQUÉ (ESP8266) : jumeau Python, ligne pour ligne, du code de firmware/src/main.cpp (edgeAi*).

Principe « le PC apprend, l'ESP applique » : l'ESP8266 (80 MHz, ~50 Ko de RAM libre, pas de FPU) ne peut
pas exécuter les forêts aléatoires du PC (plusieurs Mo). Mais ce que le PC a APPRIS tient en une vingtaine
de nombres, exportés dans firmware/include/ml_params.h (`sentinel_predictive.py export-esp`) :
  * ligne de base de la pièce, bornes d'alerte (avertissement / critique) ;
  * pentes normales maximales (5 min) : au-delà, la tendance est significative.

Toutes les 10 s, l'ESP range sa mesure dans un tampon circulaire de 5 min, calcule la pente (moindres carrés)
de chaque capteur et le délai avant la borne critique au rythme actuel. Il alerte :
  CRITIQUE       borne critique franchie, ou atteinte dans <= 10 min (tendance significative)
  AVERTISSEMENT  borne d'avertissement franchie, ou borne critique dans <= 30 min
sur deux évaluations consécutives (un pic isolé ne déclenche rien). Le type vient de règles qui reprennent les
signatures apprises par le PC (une surchauffe fait baisser l'humidité relative : ce n'est pas « air sec »).

Ce jumeau sert à VALIDER le détecteur embarqué sur la télémétrie réelle avant de flasher (edge-eval).
"""
import math

WINDOW_N = 30            # 30 x 10 s = 5 min
STEP_S = 10
MIN_POINTS = 12          # 2 min de mesures valides avant de parler de tendance
HORIZON_WARN_MIN = 30
HORIZON_CRIT_MIN = 10
STAB_S = 300             # stabilisation du MQ-2 après sa préchauffe
FIRE_GAS_RATIO = 0.25    # dérive du gaz qui, avec une température en hausse, signe un risque d'incendie
FIRE_GAS_DELTA = 15      # ou gaz au-dessus de la ligne de base de cette marge
FIRE_TEMP_RATIO = 0.25   # hausse de température suffisante QUAND le gaz dérive aussi

# Priorité à niveau égal (le plus dangereux d'abord)
KINDS = ["RISQUE_FEU", "SURCHAUFFE", "GAZ", "HUMIDITE_HAUTE", "AIR_SEC"]
# Équivalents côté PC (sous-types de la maintenance prédictive)
PC_KIND = {"RISQUE_FEU": "CORRELATION_TEMP_GAZ", "SURCHAUFFE": "SURCHAUFFE", "GAZ": "FUITE_GAZ",
           "HUMIDITE_HAUTE": "HUMIDITE_ELEVEE", "AIR_SEC": "HUMIDITE_BASSE"}
LEVELS = {None: 0, "AVERTISSEMENT": 1, "CRITIQUE": 2}


def slope_per_min(ts, vs):
    """Pente des moindres carrés (unités / minute) ; None si trop peu de points valides."""
    pts = [(t, v) for t, v in zip(ts, vs) if v is not None and not math.isnan(v)]
    if len(pts) < MIN_POINTS or pts[-1][0] - pts[0][0] < (MIN_POINTS - 1) * STEP_S:
        return None
    n = len(pts)
    mt = sum(t for t, _ in pts) / n
    mv = sum(v for _, v in pts) / n
    den = sum((t - mt) ** 2 for t, _ in pts)
    return sum((t - mt) * (v - mv) for t, v in pts) / den * 60.0 if den else None


def eta_slope(slope, recent, sign):
    """Pente qui chiffre le délai : la pente sur 5 min décide s'il y a une tendance, mais en début de montée
    elle mélange encore du « plat » ; la pente des 2 dernières minutes, si elle est plus raide vers la borne,
    donne un délai plus juste (et plus prudent)."""
    return recent if recent is not None and slope is not None and sign * recent > sign * slope else slope


def minutes_to(last, crit, slope, sign):
    """Minutes avant la borne critique (sign +1 : borne haute, -1 : basse) si la tendance va vers elle."""
    if last is None or slope is None or sign * slope <= 0:
        return None
    return max(0.0, sign * (crit - last) / slope)


class EdgeDetector:
    def __init__(self, p):
        self.p = p                     # paramètres de ml_params.h (dict)
        self.buf = []                  # [(t, temp, hum, gaz)], 30 au plus
        self.streak = 0
        self.gas_low_t = None          # dernier instant où le MQ-2 était en préchauffe
        self.state = (None, None, None)

    def push(self, t, temp, hum, gaz):
        self.buf.append((t, temp, hum, gaz))
        del self.buf[:-WINDOW_N]

    def gas_ok(self, t, gaz):
        p = self.p
        if gaz is None or gaz < p["gaz_prechauffe"]:
            self.gas_low_t = t
            return False
        return self.gas_low_t is None or t - self.gas_low_t >= STAB_S or gaz >= p["gaz_warn"]

    def raw(self, t):
        """(type, niveau, minutes) de l'évaluation courante, sans persistance."""
        p = self.p
        ts = [x[0] for x in self.buf]
        temp, hum, gaz = (next((x[i] for x in reversed(self.buf) if x[i] is not None), None) for i in (1, 2, 3))
        gok = self.gas_ok(t, self.buf[-1][3] if self.buf else None)
        col = lambda i: [x[i] for x in self.buf]  # noqa: E731
        k = MIN_POINTS
        st, sh = slope_per_min(ts, col(1)), slope_per_min(ts, col(2))
        sg = slope_per_min(ts, col(3)) if gok else None
        rt, rh = slope_per_min(ts[-k:], col(1)[-k:]), slope_per_min(ts[-k:], col(2)[-k:])
        rg = slope_per_min(ts[-k:], col(3)[-k:]) if gok else None
        t_up = st is not None and st > p["slope_temp"]
        g_up = sg is not None and sg > p["slope_gaz"]
        h_up = sh is not None and sh > p["slope_hum"]
        h_down = sh is not None and sh < -p["slope_hum"]
        cands = []

        def judge(kind, value, warn, crit, sign, trend, slope, recent):
            m = minutes_to(value, crit, eta_slope(slope, recent, sign), sign) if trend else None
            if value is not None and sign * (value - crit) >= 0 or (m is not None and m <= HORIZON_CRIT_MIN):
                cands.append((kind, "CRITIQUE", m))
            elif value is not None and sign * (value - warn) >= 0 or (m is not None and m <= HORIZON_WARN_MIN):
                cands.append((kind, "AVERTISSEMENT", m))

        # Risque d'incendie : la température monte ET le gaz dérive, même lentement (signature apprise par le PC :
        # échauffement corrélé au gaz). Seuil gaz plus bas que pour une fuite seule : quart de la pente normale max.
        gas_drift = gok and ((sg is not None and sg > FIRE_GAS_RATIO * p["slope_gaz"])
                             or (gaz is not None and gaz >= p["base_gaz"] + FIRE_GAS_DELTA))
        judge("RISQUE_FEU" if gas_drift else "SURCHAUFFE", temp, p["temp_warn"], p["temp_crit"], 1, t_up, st, rt)
        if gok:
            judge("GAZ", gaz, p["gaz_warn"], p["gaz_crit"], 1, g_up, sg, rg)
        if not t_up:  # une surchauffe fait varier l'humidité relative : elle est déjà couverte
            judge("HUMIDITE_HAUTE", hum, p["hum_high_warn"], p["hum_high_crit"], 1, h_up, sh, rh)
            judge("AIR_SEC", hum, p["hum_low_warn"], p["hum_low_crit"], -1, h_down, sh, rh)
        # Le gaz dérive ET la température monte, même sous le seuil d'une surchauffe seule : signature
        # d'échauffement corrélé au gaz -> l'alerte (gaz ou température) est requalifiée en risque d'incendie.
        fire = gas_drift and st is not None and st > FIRE_TEMP_RATIO * p["slope_temp"]
        if fire:
            cands = [("RISQUE_FEU" if k in ("GAZ", "SURCHAUFFE") else k, lv, m) for k, lv, m in cands]
        if not cands:
            return None, None, None
        return max(cands, key=lambda c: (LEVELS[c[1]], -KINDS.index(c[0])))

    def evaluate(self, t):
        """Avec persistance : l'état ne change d'alerte qu'après 2 évaluations suspectes consécutives."""
        kind, level, m = self.raw(t)
        self.streak = self.streak + 1 if level else 0
        if level is None:
            self.state = (None, None, None)
        elif self.streak >= 2:
            self.state = (kind, level, m)
        return self.state


def params_from_model(model, cfg):
    """Paramètres embarqués à partir du modèle entraîné et de la config (contenu de ml_params.h)."""
    from bounds import compute_bounds
    b = compute_bounds(cfg["limites"], model.baseline)
    base = model.baseline or {"temp": None, "hum": None, "gaz": cfg["limites"]["gaz"]["reference_defaut"]}
    return {
        "base_temp": base["temp"], "base_hum": base["hum"], "base_gaz": base["gaz"],
        "temp_warn": b["temp_haut"]["avertissement"], "temp_crit": b["temp_haut"]["critique"],
        "hum_high_warn": b["hum_haut"]["avertissement"], "hum_high_crit": b["hum_haut"]["critique"],
        "hum_low_warn": b["hum_bas"]["avertissement"], "hum_low_crit": b["hum_bas"]["critique"],
        "gaz_warn": b["gaz_haut"]["avertissement"], "gaz_crit": b["gaz_haut"]["critique"],
        "gaz_prechauffe": round(cfg["prechauffe_gaz"]["ratio_base"] * base["gaz"]),
        "slope_temp": round(model.slope_floor["temp"], 3), "slope_hum": round(model.slope_floor["hum"], 3),
        "slope_gaz": round(model.slope_floor["gaz"], 3),
    }


def replay(params, samples):
    """Rejoue (t, temp, hum, gaz) comme l'ESP : une mesure toutes les 10 s dans le tampon, une évaluation.
    Retourne [(t, type, niveau, minutes)] à chaque CHANGEMENT d'état d'alerte (ce que l'OLED afficherait)."""
    det, out, prev = EdgeDetector(params), [], (None, None)
    next_t = samples[0][0]
    for t, temp, hum, gaz in samples:
        if t < next_t:
            continue
        next_t = t + STEP_S
        det.push(t, temp, hum, gaz)
        kind, level, m = det.evaluate(t)
        if (kind, level) != prev:
            out.append((t, kind, level, m))
            prev = (kind, level)
    return out
