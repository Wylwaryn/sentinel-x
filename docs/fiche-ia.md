# Fiche technique — les trois IA de Sentinel-X

Document du dossier. Les trois modèles tournent sur le **PC hôte Windows 11** (RTX 5050, CUDA),
jamais dans la VM (le GPU ne lui est pas accessible). Ils n'ouvrent aucun port : ils ne font que
des connexions **sortantes** vers la VM (MQTTS 8883, API d'ingestion HTTPS 8443).

Principe commun aux trois : **apprendre le normal, mesurer l'écart.** Les étages de détection
sont entraînés sur le fonctionnement normal uniquement ; les seuils « métier » (limites
d'exploitation, ports de service) servent à expliquer et à chiffrer, jamais à décider seuls.

---

## 1. Vision — détection de présence et d'intrusion

**Rôle.** Repérer une personne dans le champ de la caméra, suivre ses déplacements, qualifier son
comportement (présence, rôdeur, approche rapide, intrusion en zone interdite), reconnaître les
membres autorisés de l'équipe, et fusionner avec le capteur PIR de l'ESP.

**Chaîne de traitement** (`host/vision/`)
1. **Capture** (`camera.py`) : webcam USB, exposition automatique, capture 1280×720 MJPG réduite
   en 640×480 (`INTER_AREA`, qui moyenne le bruit du capteur).
2. **Faible lumière** (`enhance.py`) : si la luminosité moyenne passe sous 100/255, CLAHE sur la
   luminance (espace LAB) + correction gamma. Inactif en pleine lumière (0,25 ms), 3,7 ms quand il
   agit. L'image rehaussée sert à l'IA **et** au flux vidéo du dashboard.
3. **Portillon de mouvement** (`MotionGate`, MOG2) : YOLO ne tourne à pleine cadence que s'il y a
   du mouvement, une personne déjà suivie, ou un PIR actif ; sinon une image toutes les 0,5 s. Cela
   économise le GPU sans rien manquer.
4. **Détection + suivi** : **YOLOv8n + ByteTrack** (Ultralytics) sur la RTX 5050, classe « personne »
   uniquement. **~10,8 ms par image.** Chaque personne reçoit un identifiant de piste stable.
5. **Zones et comportement** (`behaviour.py`) : polygones « périmètre » et « zone interdite ».
   Rôdeur après 10 s sur place, approche rapide au-delà d'une vitesse seuil, intrusion dès l'entrée
   en zone interdite.
6. **Reconnaissance faciale** (`host/faces/`) : **YuNet** (détection) + **SFace** (empreinte de 128
   valeurs), modèles OpenCV. Comparaison par similarité cosinus, **seuil 0,40**, **2 correspondances**
   requises avant de conclure. La galerie ne contient **que des empreintes** (`gallery.npz`), jamais
   de photo sur le PC hôte. Synchronisation automatique toutes les 60 s avec les images de référence
   du dashboard (compte de service `SERVICE_VISION`).
7. **Fusion PIR + caméra** (`fusion.py`) : une alerte visuelle confirmée par le PIR monte en niveau ;
   un PIR seul dans un angle mort lève `ANGLE_MORT`. Anti-répétition **par personne** (le suivi
   renumérote une personne immobile) avec un délai par type (présence/rôdeur 60 s, approche 30 s,
   intrusion 20 s). Un **membre reconnu n'est jamais traité en intrusion**.

**Sorties.** Flux vidéo annoté sur MQTT `sentinel/video/cam1` ; alertes sur l'API d'ingestion
(`VISION_IA`/`FUSION`, avec `personne_reconnue` si c'est un membre, et une capture JPEG pour les
alertes critiques).

**Validation.** 30+ tests ; essai réel avec une personne devant la caméra et le PIR de l'ESP.

---

## 2. IA réseau (IDS) — détection d'intrusion réseau

**Rôle.** Surveiller le trafic qui arrive sur le point d'accès Wi-Fi du PC hôte, repérer un
comportement réseau anormal visant le serveur, en nommer le type, et (en option) bloquer l'IP
fautive au pare-feu Windows. Elle voit **tout** le trafic qui atteint le PC hôte, y compris celui
qui ne sera jamais redirigé vers la VM, et peut agir **avant** la VM.

**Ce qu'elle détecte** : des anomalies de **flux réseau** — scan de ports, déni de service, force
brute (motifs de connexions). Ce n'est pas son rôle de voir une attaque applicative (ex. injection
SQL dans une requête HTTPS chiffrée) : ça, c'est la défense de l'API (requêtes paramétrées).

**Chaîne de traitement** (`host/ids/`)
1. **Capture** (`sentinel_ids.py`, scapy + Npcap) sur l'interface du point d'accès, par fenêtres de
   **2 s**. Les IP « protégées » sont le serveur/hôte (`192.168.137.1`).
2. **Caractéristiques** (`features.py`) : **14 valeurs par IP distante et par fenêtre**, agrégées dans
   les deux sens pour voir aussi les réponses du serveur (un `RST` = port fermé) :
   paquets/s, octets/s, taille moyenne, SYN/s, part de SYN, **nombre de ports distincts visés**,
   étalement des ports, SYN vers les services (SSH/HTTPS/MQTTS), **RST du serveur/s**, ICMP/s, UDP/s,
   nombre d'IP visées, asymétrie du trafic, paquets sortants/s. Transformation `log1p` (un flood ×1000
   ne doit pas écraser l'apprentissage).
3. **Étage 1 — « est-ce anormal ? »** (non supervisé, entraîné **uniquement sur le trafic normal**) :
   - **autoencodeur PyTorch** : l'erreur de reconstruction repère les volumes hors norme ;
   - **Isolation Forest** : repère les combinaisons inhabituelles *dans* les plages normales.
   - Chaque score est ramené sur [0, 1] par `s = 1 − 0,5^(r⁴)` (r = score/seuil, seuil = 99,5ᵉ
     percentile du normal). **Score final = max des deux** : chacun couvre l'angle mort de l'autre
     (l'Isolation Forest sature sur les valeurs très extrêmes, l'autoencodeur non).
4. **Étage 2 — « quel type ? »** (supervisé) : **Random Forest** entraînée sur des profils d'attaque
   synthétiques variés + du trafic normal → `SCAN_PORTS`, `DENI_DE_SERVICE`, `FORCE_BRUTE`. Une
   anomalie que le classifieur ne reconnaît pas (ou avec une confiance < 0,5) devient
   **`TRAFIC_ANORMAL`** : on ne force jamais une étiquette. **~17,6 ms par fenêtre** sur GPU.
5. **Reconnaissance des appareils par MAC** (`response.py`) : le DHCP du point d'accès change l'IP de
   l'ESP à chaque reconnexion, donc l'ESP est reconnu par sa **MAC** (`40:F5:20:0D:5F:21` →
   `SX-G2-01`). Une machine en **liste blanche n'est jamais bloquée mais toujours alertée** (un ESP
   compromis doit se voir ; une MAC peut être usurpée).

**Réponse.** Trois modes dans `config.json` :
- **`alerte`** : journal `logs/ids_events.jsonl` + envoi à l'API (`RESEAU_IA`) ;
- **`blocage`** : en plus, **règle de pare-feu Windows entrante temporaire** (`netsh`, TTL 300 s)
  pour l'IP fautive au-delà du score de blocage (0,85) ;
- **`leurre`** (mode du pentest) : la règle ne coupe **que les vrais services** (443 dashboard, 8883
  MQTTS) pour l'IP fautive, et laisse ouverts les ports du **honeypot** (3306, 8080). L'attaquant
  croit avoir perdu sa cible et se rabat sur le leurre, qui journalise tout ce qu'il tente.

`blocage` et `leurre` exigent un terminal administrateur ; l'IP est validée avant d'être passée à
`netsh` (jamais de chaîne brute venue du réseau). Une règle refusée est journalisée sans arrêter l'IDS.

Seuils (`config.json`) : alerte 0,5, critique 0,85, blocage 0,85, cooldown 30 s par (IP, type).

**Deux interfaces, deux politiques.**

| Interface | Ce qu'elle porte | Capture | Mode |
|---|---|---|---|
| Point d'accès `192.168.137.1` | ESP, téléphones, attaquants de la table | tout le trafic IP | `leurre` |
| Wi-Fi de l'école `10.60.60.42` | la sortie Internet de l'hôte | seulement les SYN entrants et l'ICMP vers l'hôte (sondes) | `alerte`, imposé |

Sur l'interface école, **aucun blocage n'est possible** : un faux positif couperait la passerelle, le
DNS ou une API dont l'hôte a besoin. Et aucun service n'y écoute plus (redirections VirtualBox liées
au point d'accès et à `127.0.0.1`) : il n'y a rien à couper ni vers où attirer l'attaquant.
On y gagne la **visibilité** : une sonde venue de l'école est vue, même si le pare-feu la rejette.

**Capture sans mode promiscuous.** Mettre la carte Wi-Fi en promiscuous pendant qu'elle sert de point
d'accès faisait planter le pilote Wi-Fi de Windows (carte perdue jusqu'au redémarrage, deux fois).
C'est inutile ici : l'hôte est la passerelle du point d'accès, tout le trafic des clients le traverse.

**Alertes réelles du pentest (8 oct.), et comment les lire.**

| Alerte | Interface | Ce que c'était | Réponse |
|---|---|---|---|
| `TRAFIC_ANORMAL` **CRITIQUE**, score 1,00, `192.168.137.73` (SYN, UDP, ports de service) | point d'accès | **vrai scan de ports** d'une autre équipe | **`leurre`** : règle `netsh` 443 + 8883 pour cette IP, honeypot laissé ouvert |
| `TRAFIC_ANORMAL` AVERTISSEMENT, score ~0,51, `192.168.137.212` | point d'accès | **notre ESP** (reconnu par sa MAC) : rafale de reconnexion MQTT, juste au-dessus du seuil | `liste_blanche` : journalisé, jamais bloqué |
| `TRAFIC_ANORMAL` CRITIQUE, score 1,00, `10.128.128.128` (1 ICMP/s, 80 o, aucun port) | école | **faux positif** : un ping régulier de l'infrastructure Wi-Fi de l'école. `10.128.128.128` est l'adresse virtuelle des bornes Cisco Meraki | `alerte` seulement (aucun effet sur le réseau) |

Ce que ces cas montrent :
- **Le modèle signale l'inconnu, pas forcément l'hostile.** Il n'a appris que le trafic du point
  d'accès : un ping d'infrastructure, jamais vu, sort à 1,00. C'est pourquoi il nomme `TRAFIC_ANORMAL`
  sans forcer une étiquette d'attaque, et pourquoi l'humain lit les caractéristiques (`details`).
- **La politique de réponse compte autant que le score.** Le même score 1,00 déclenche un leurre sur
  le point d'accès et une simple alerte sur l'école. Un blocage automatique sur l'école aurait coupé
  une borne Wi-Fi.
- **Amélioration possible** : ré-entraîner l'étage 1 avec du trafic normal de l'interface école, ou
  exclure les adresses d'infrastructure connues de la capture école. Choix fait pour le pentest :
  garder l'alerte visible et la documenter.

**Validation.** 13+ tests. Modèle **amorcé sur du trafic synthétique** (`simulate.py`) ; à
**ré-entraîner sur le trafic réel** de la table (`record` puis `train`) pendant l'intégration.

---

## 3. Maintenance prédictive (IA capteurs) — anticiper l'incident environnemental

**Rôle.** À partir de la télémétrie de l'ESP (DHT22 température/humidité, MQ-2 gaz), détecter une
**dérive** avant qu'elle n'atteigne un seuil dangereux, en **nommer la nature**, **chiffrer le délai**
restant et **dire quoi faire** pour régler le problème avant qu'il n'arrive.

**Données.** Entraînée sur **5 039 mesures réelles** de `SX-G2-01` (6 et 7 oct., une mesure toutes les
5 s), exportées de PostgreSQL. Fonctionnement normal mesuré dans la pièce (p0,5–p99,5) : **24,2–29,1 °C**,
**63,7–73,6 % d'humidité**, **gaz brut 53–80** (médiane 67).

### Bornes d'alerte (`host/predictive/config.json`, `bounds.py`)

| Capteur | Avertissement (agir avant) | Critique (danger) | Justification |
|---|---|---|---|
| Température | **35 °C** | **45 °C** | +6 °C au-dessus du maximum normal observé ; 45 °C : risque matériel et départ de feu |
| Humidité haute | **80 %** | **90 %** | au-dessus du normal (≤ 74 %) ; risque de condensation sur l'électronique |
| Humidité basse | **30 %** | **20 %** | air sec : risque de décharges électrostatiques |
| Gaz (MQ-2) | **base + 80** (179) | **base + 180** (279) | MQ-2 non étalonné en ppm : bornes **relatives** à la ligne de base apprise (≈ 99 une fois le capteur branché et chauffé) |

Les bornes jouent trois rôles : la **prévision** chiffre le délai avant la borne critique ; une borne
**franchie** déclenche une alerte même si le modèle hésite (**filet de sécurité déterministe**, actif
dès l'allumage) ; l'IA, elle, détecte la dérive **bien avant** les bornes.

### Chaîne de traitement (`host/predictive/`)
1. **Entrée** : MQTTS `sentinel/telemetry` (compte `capteurs`, lecture seule).
2. **Caractéristiques cinétiques** (`features.py`), sur 1 min et 5 min, par capteur : écart à la médiane,
   pentes courte et longue, volatilité, accélération ; corrélations température/gaz et
   température/humidité ; part de valeurs manquantes et figées. Tout est **relatif** à l'historique récent.
3. **Étage 1 — détection** (non supervisé, entraîné sur le **normal réel**) : Isolation Forest + écart
   statistique (max |z|), score final = max des deux.
4. **Prévision** : quand une tendance est significative (pente au-delà du normal : seuil **appris**,
   ±0,66 °C/min, ±1,8 %/min, ±3,7/min), projection jusqu'à la borne critique, **vers le haut ou vers le bas**.
5. **Étage 2 — type d'incident** (Random Forest), entraîné sur des incidents **superposés à la vraie
   télémétrie** de la pièce, chacun avec son **action préventive** :

| Type | Signature | Action préventive (dans l'alerte) |
|---|---|---|
| `SURCHAUFFE` | température qui monte (l'humidité relative baisse) | vérifier ventilation et sources de chaleur, couper l'appareil avant 45 °C |
| `CORRELATION_TEMP_GAZ` | échauffement lent + dérive du gaz : **risque d'incendie** | couper l'alimentation de l'équipement suspect, aérer, préparer l'extincteur |
| `FUITE_GAZ` | qualité de l'air dégradée (fuite, bouffée, fumée) | aérer, aucune flamme ni étincelle, couper le gaz, évacuer si ça monte |
| `HUMIDITE_ELEVEE` | humidité qui monte sans échauffement | aérer/déshumidifier, chercher une fuite d'eau, éloigner l'électronique |
| `HUMIDITE_BASSE` | humidité qui chute sans échauffement | humidifier, manipuler l'électronique avec précaution |
| `CAPTEUR_DEFAILLANT` | valeurs figées, aberrantes ou absentes | vérifier câblage et alimentation du capteur |

6. **Alerte par épisode** : persistance sur 2 évaluations, une alerte par épisode (début, aggravation,
   type précisé, prévision), rappel toutes les 10 min.

### Préchauffe du MQ-2 (problème réel trouvé dans les données)
À froid, le MQ-2 lit **~6–10** et ne rejoint sa ligne de base qu'en **~16 min** ; et cette base **dérive
d'un jour à l'autre** (~67 le 6, ~58 le 7). Sans précaution, chaque allumage du boîtier ressemblerait à
une fuite de gaz. Le service détecte la préchauffe (gaz < 70 % de la base), **exclut le gaz** de l'analyse
pendant la remontée + 5 min, et continue de surveiller température et humidité. La borne gaz reste
active : une vraie fuite fait **monter** le gaz au-dessus de la base et alerte quand même (testé).

### Résultats (évaluation honnête : entraîné le 6 oct., testé le 7, jamais vu)
`python sentinel_predictive.py evaluate`
- **Fonctionnement normal : 0 fausse alerte en 1,6 h**, deux allumages à froid du MQ-2 compris.
- **Surchauffe +1 °C/min** superposée à un tronçon réel : alerte **15 min avant 45 °C** ; délai annoncé
  17,2 min pour 15,2 min réelles.
- **Incidents superposés au réel**, rejoués dans le pipeline complet (20 min) : **type juste 47 fois sur
  49 (96 %)**, 0 fausse alerte avant le début des incidents. Surchauffe 12/12 en 2,3 min, gaz 12/12 en
  2,8 min, humidité haute 12/12 en 5,2 min, risque d'incendie (dérive lente) 8/12 en 14,7 min.
- **Limites, dites franchement** : les dérives **très lentes** d'humidité à la baisse (3/12 en 20 min)
  et les capteurs figés (2/12) sont mal vus, car le 6 oct. était une journée de tests (boîtier manipulé,
  souffle, briquet) et la température de la pièce reste souvent figée au dixième près. Plus de journées
  de fonctionnement **calme** resserreront l'enveloppe. Variante « purge des épisodes de test » essayée :
  rejetée (2 fausses alertes à l'allumage, prévision moins juste).
- **20 tests unitaires** (`pytest host/predictive`), dont : préchauffe à froid sans alerte, fuite pendant
  la stabilisation détectée, borne critique franchie avec un modèle calme, humidité qui chute vers 20 %.

**Ré-entraîner** (export de la base, puis `train`) :
`ssh sentinel-vm "sudo docker exec sentinel-postgres-1 …"` → `data/telemetry.csv`, puis
`python sentinel_predictive.py train` ; la ligne de base et les bornes gaz sont réapprises.

---

## Points communs (pour la soutenance)

- **Entraînement sur le normal, pas sur l'attaque** : les étages de détection (vision mise à part)
  n'ont jamais besoin d'exemples d'attaque pour fonctionner. Le second étage, supervisé, ne sert qu'à
  **nommer** ce qui a déjà été jugé anormal.
- **Deux modèles par détecteur, score = max** : IDS et prédictif combinent un modèle de « forme »
  (Isolation Forest) et une mesure de « volume/écart » (autoencodeur ou z-score), parce qu'un seul
  rate les angles morts de l'autre.
- **Les seuils métier expliquent, ils ne décident pas** : limites d'exploitation, ports de service,
  zones. La décision vient de l'écart au normal appris.
- **Tout sur le GPU, tout en sortant** : aucun des trois n'ouvre de port ; chacun pousse ses alertes
  vers la VM par des canaux authentifiés (jeton Bearer par client, TLS).
