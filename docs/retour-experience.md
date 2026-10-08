# Retour d'expérience — incidents d'exploitation du 8 octobre 2026

Le jour du pentest, notre infrastructure a connu une série d'incidents. Aucun n'a ouvert de brèche,
mais l'un d'eux nous a fait **perdre une partie des journaux du broker MQTT** pendant l'attaque. Nous
le documentons tel quel : un choix technique de notre IA réseau est à l'origine de la chaîne.

Heures en UTC (heure de Paris = UTC + 2).

## 1. La chaîne en une phrase

La capture réseau de l'IDS mettait la carte Wi-Fi en mode promiscuous → le pilote Wi-Fi de Windows
plantait → il fallait redémarrer le PC hôte, ce qui coupait la VM brutalement → ajoutés aux blocages
de la VM par manque de mémoire, ces arrêts brutaux ont fini par **détacher les journaux Docker de
Mosquitto**, qui n'ont plus rien écrit de 09:21 à 17:02.

## 2. Incident 1 — perte de la carte Wi-Fi (7 et 8 octobre)

**Symptôme.** La carte Wi-Fi du PC hôte disparaissait : plus aucun réseau visible, point d'accès
coupé, ESP et téléphones déconnectés. Seul un redémarrage de Windows la faisait revenir. Deux fois,
le 7 puis le 8 octobre.

**Cause.** L'IDS capture le trafic avec scapy (`AsyncSniffer`, via Npcap). Sans réglage explicite,
scapy met l'interface en **mode promiscuous**. Or cette carte Wi-Fi sert **en même temps de point
d'accès** (Mobile Hotspot de Windows). Les deux usages ensemble font planter le pilote.

**Correction.** Capture sans mode promiscuous (`promisc=False`, option `capture.promisc` dans
`host/ids/config.json`). On ne perd rien en détection : le PC hôte est la **passerelle** du réseau
`192.168.137.0/24`, donc tout le trafic des clients le traverse et reste visible. Aucune nouvelle perte
de la carte n'a été signalée pendant le pentest, IDS actif.

**Leçon.** Une option par défaut d'une bibliothèque (ici, le promiscuous de scapy) peut être sans
risque sur un PC classique et dangereuse sur une machine qui fait aussi point d'accès. Un outil de
sécurité peut dégrader la **disponibilité** du système qu'il protège.

## 3. Incident 2 — redémarrages en cascade de la VM

La VM serveur a redémarré **6 fois** le 8 octobre, pour deux raisons :
- les **redémarrages du PC hôte** imposés par la perte de la carte Wi-Fi, qui arrêtent la VM sans
  l'éteindre proprement ;
- des **blocages de la VM par manque de mémoire** : la VM (10 Go) se met à utiliser le swap quand
  l'hôte (23 Go) fait tourner en même temps Docker Desktop, la vision et Windows. La VM ne répond plus,
  il faut l'éteindre de force.

PostgreSQL (journal WAL) et le système de fichiers ext4 ont encaissé ces coupures sans perte de
données. Les 5 conteneurs sont repartis seuls à chaque démarrage.

## 4. Incident 3 — journaux du broker MQTT perdus pendant le pentest

**Constat (après le pentest).** Les journaux Docker de Mosquitto s'arrêtent à **09:21:36 UTC**, deux
minutes avant l'un des redémarrages de la VM. Plus aucune ligne ensuite : ni les connexions de
l'après-midi, ni le redémarrage du conteneur.

**Cause (diagnostic de la session VM).** Dans la série d'arrêts brutaux, le flux de sortie du
conteneur s'est détaché du pilote de journaux Docker (`json-file`). Il ne s'est rattaché qu'au premier
**redémarrage propre**, à 17:02 UTC. Vérifié à 17:12 : une connexion de test est de nouveau journalisée.

**Ce qui n'a PAS été touché.**
- Le broker a fonctionné normalement tout l'après-midi : la supervision de la VM le voit en service,
  avec 4 à 5 clients connectés et aucune erreur.
- Les protections sont restées actives tout du long : TLS obligatoire, un compte par client, liste
  blanche des topics (ACL). Des journaux absents ne veulent pas dire des contrôles absents.

**Ce que nous avons perdu.** Le journal des connexions au vrai broker (port 8883) pendant toute la
fenêtre du pentest (12:00 → 15:02 UTC). Il ne peut pas être reconstitué.

**Preuves de remplacement.**

| Source | Ce qu'elle montre pendant le pentest |
|---|---|
| Supervision de la VM (`sentinel-monitor`, un relevé par minute) | **Jamais plus de 5 clients MQTT** connectés sur les 165 relevés : exactement nos 5 comptes (ESP, vision, maintenance prédictive, dashboard, supervision). Limite : une tentative brève entre deux relevés ne serait pas vue. |
| Journaux du vrai dashboard (conteneur actif depuis 10:42 UTC) | Aucune des 7 adresses repérées comme hostiles par l'IDS : ni connexion réussie, ni tentative refusée. |
| Journaux de l'IDS et du honeypot (PC hôte) | L'activité des attaquants, enregistrée en dehors de la VM, donc non concernée par l'incident. |

## 5. Incident lié — dashboard injoignable après une coupure du point d'accès

Pour fermer l'accès depuis le réseau de l'école, nous avions lié les redirections VirtualBox (443,
8883, ports du honeypot) à l'adresse du point d'accès `192.168.137.1` au lieu de `0.0.0.0`. Après une
coupure du point d'accès, VirtualBox n'a **pas recréé** ses sockets sur cette adresse : dashboard et
broker sont devenus injoignables depuis le Wi-Fi de la table (`ERR_CONNECTION_REFUSED`).

**Décision.** Retour à `0.0.0.0`, la configuration d'origine, qui survit aux coupures. Le réseau de
l'école reste bloqué par le **pare-feu Windows** (443, 8883 et ports du honeypot autorisés seulement
depuis `192.168.137.0/24`, 8 règles vérifiées). On garde une seule couche de protection au lieu de
deux, en échange de la disponibilité pour la démo.

## 6. Ce que nous changerions

| Problème | Amélioration | Statut |
|---|---|---|
| Journaux du broker dépendants du seul pilote Docker | Mosquitto écrit aussi dans un fichier sur un volume dédié (`log_dest file`) | Proposée par la session VM, **non appliquée** (gel du code) |
| Options par défaut des outils de capture | Toujours fixer `promisc` explicitement ; tester l'IDS sur la machine réelle avant le jour J | Corrigé (`promisc=False`) |
| Manque de mémoire sur l'hôte | Fermer Docker Desktop, démarrer la VM avant la vision (`host\start.bat`) ; à terme, plus de RAM ou une VM plus légère | Procédure écrite |
| Arrêts brutaux de la VM | Arrêt propre (ACPI) avant toute extinction forcée ; vérifier les journaux après chaque redémarrage | Procédure écrite |
| Redirections liées à une adresse qui peut disparaître | Lier aux adresses stables, filtrer au pare-feu | Corrigé (retour à `0.0.0.0`) |

## 7. Pourquoi nous le documentons

L'IDS a fait son travail de détection pendant tout le pentest, mais **notre propre outil de sécurité a
provoqué un incident de disponibilité**, qui a coûté une partie de notre traçabilité. Nous préférons le
montrer, avec sa cause et ses correctifs, plutôt que présenter un journal incomplet comme s'il était
complet.
