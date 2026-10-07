// GÉNÉRÉ par host/predictive/sentinel_predictive.py export-esp : NE PAS MODIFIER À LA MAIN.
// 2026-10-07 09:53 UTC. Paramètres APPRIS par le PC sur la télémétrie réelle :
// le PC apprend, l'ESP applique (détecteur embarqué edgeAi* de main.cpp, jumeau Python : edge.py).
#pragma once

// Ligne de base de la pièce (médianes du fonctionnement normal réel)
#define ML_BASE_TEMP 26.200f
#define ML_BASE_HUM 68.700f
#define ML_BASE_GAZ 67

// Bornes d'alerte : avertissement (agir avant) / critique (danger)
#define ML_TEMP_WARN 35.000f
#define ML_TEMP_CRIT 45.000f
#define ML_HUM_HIGH_WARN 80.000f
#define ML_HUM_HIGH_CRIT 90.000f
#define ML_HUM_LOW_WARN 30.000f
#define ML_HUM_LOW_CRIT 20.000f
#define ML_GAZ_WARN 97.000f   // relatif à la ligne de base du MQ-2 (non étalonné)
#define ML_GAZ_CRIT 147.000f
#define ML_GAZ_PRECHAUFFE 47.000f   // en dessous : MQ-2 en préchauffe, gaz ignoré

// Pentes normales maximales apprises (5 min, p99.5, par minute) : au-delà, tendance significative
#define ML_SLOPE_TEMP 0.658f
#define ML_SLOPE_HUM 1.766f
#define ML_SLOPE_GAZ 3.714f
