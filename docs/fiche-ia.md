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

**Réponse.** Deux modes dans `config.json` :
- **`alerte`** (par défaut) : journal `logs/ids_events.jsonl` + envoi à l'API (`RESEAU_IA`) ;
- **`blocage`** : en plus, **règle de pare-feu Windows entrante temporaire** (`netsh`, TTL 300 s)
  pour l'IP fautive au-delà du score de blocage (0,85). Exige un terminal administrateur ; l'IP est
  validée avant d'être passée à `netsh` (jamais de chaîne brute venue du réseau).

Seuils (`config.json`) : alerte 0,5, critique 0,85, blocage 0,85, cooldown 30 s par (IP, type).

**Validation.** 13+ tests. Modèle **amorcé sur du trafic synthétique** (`simulate.py`) ; à
**ré-entraîner sur le trafic réel** de la table (`record` puis `train`) pendant l'intégration.

---

## 3. Maintenance prédictive (IA capteurs) — anticiper l'incident environnemental

**Rôle.** À partir de la télémétrie de l'ESP (DHT22 température/humidité, MQ-2 gaz), détecter une
**dérive** avant qu'elle n'atteigne une limite dangereuse, en nommer la nature, et **chiffrer le
délai** restant. Elle juge des **évolutions**, pas des seuils fixes.

**Chaîne de traitement** (`host/predictive/`)
1. **Entrée** : abonnement MQTTS `sentinel/telemetry` (compte `capteurs`, lecture seule).
2. **Caractéristiques cinétiques** (`features.py`), sur une fenêtre **courte (1 min)** et **longue
   (5 min)**, pour chaque capteur : écart à la médiane longue, **pente courte**, **pente longue**,
   volatilité, **accélération** (pente courte − pente longue). Entre capteurs :
   **corrélations température/gaz et température/humidité**. Qualité : part de valeurs **manquantes**
   et **figées** (capteur bloqué). Tout est relatif à l'historique récent.
3. **Étage 1 — détection** (non supervisée, sur le fonctionnement normal) : **Isolation Forest** +
   **écart statistique** (max |z|). Score final = max des deux sur [0, 1] (même raison que l'IDS :
   l'écart statistique couvre la saturation de l'Isolation Forest sur les valeurs extrêmes).
4. **Prévision** : quand une tendance est **significative** (pente au-delà de celles du
   fonctionnement normal, **seuil appris**), projection linéaire jusqu'aux **limites d'exploitation**
   (`config.json` : température 45 °C, gaz 700). « Au rythme actuel, 45 °C dans ~18 min. » Les limites
   **ne déclenchent rien seules** : la détection vient de l'étage 1, la prévision chiffre l'urgence.
5. **Étage 2 — type d'incident** (supervisé, Random Forest) : `SURCHAUFFE`, `FUITE_GAZ`,
   `CORRELATION_TEMP_GAZ`, `CAPTEUR_DEFAILLANT`, sinon `DERIVE_INDETERMINEE`.
6. **Alerte par épisode** : persistance sur 2 évaluations avant d'alerter, **une alerte par épisode**
   (début, aggravation, type précisé, prévision), rappel toutes les 10 min. Évite le matraquage.

**Sorties.** API d'ingestion (`CAPTEURS_IA`/`ANOMALIE_ENVIRONNEMENTALE`, avec score, message et
détail : sous-type, prévision, épisode).

**Mesures sur données synthétiques jamais vues** : détection **90 %**, typage **97,5 %**, **< 1
fausse alerte par heure**. Surchauffe à +1 °C/min : alerte à +30 s, délai annoncé 20,9 min pour
19,0 min réelles. **À ré-entraîner sur la télémétrie réelle** dès que l'ESP émet
(`record --minutes 30` puis `train`).

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
