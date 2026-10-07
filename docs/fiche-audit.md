# Fiche — méthode d'audit pour le pentest croisé

Document du dossier et mémo d'équipe pour jeudi après-midi : **tester le système d'un autre groupe**
proprement, et en tirer un rapport qui marque des points. On teste **que ce qui est autorisé**, avec
des **outils standards** (pas de scripts maison), et on **documente tout**.

> ⚠️ **Avant de lancer quoi que ce soit : le périmètre autorisé.** Un pentest ne se fait que sur une
> cible et une fenêtre explicitement données par l'encadrant. À faire valider **par écrit** avant de
> commencer : quels groupes / quelles IP ; la plage horaire ; les attaques **interdites** (en général
> le déni de service, qui casse la démo de tout le monde) ; qui prévenir si on trouve quelque chose de
> grave. Pas d'autorisation claire = on ne touche pas.

---

## 1. Les 5 phases d'un pentest propre

1. **Cadrage** — périmètre, autorisation, règles d'engagement (ci-dessus). On note l'heure de début.
2. **Reconnaissance** — qu'est-ce qui est exposé ? (hôtes, ports, services, versions).
3. **Cartographie** — comprendre l'application : pages, rôles, points d'entrée (formulaires, API, MQTT).
4. **Test** — pour chaque point d'entrée, les contrôles du §3, à l'aide d'outils reconnus.
5. **Rapport** — findings classés par sévérité, avec preuve, reproduction et remédiation (§4). C'est la
   partie la plus notée.

Répartition conseillée (équipe de 6) : 1 au cadrage/notes, 2 en reconnaissance+réseau, 2 en web/API,
1 qui rédige le rapport au fil de l'eau.

---

## 2. Outils standards (lancés tels quels, rien de « maison »)

| Besoin | Outil | Commande type | Ce qu'on lit |
|---|---|---|---|
| Découverte d'hôtes/ports | **nmap** | `nmap -sV -p- <IP>` | ports ouverts, services, versions |
| Scripts de reconnaissance | **nmap NSE** | `nmap -sC <IP>` | mauvaises configs courantes (jamais `--script vuln` en DoS) |
| Qualité TLS | **testssl.sh** | `testssl.sh https://<IP>` | version TLS, chiffrements, certificat |
| Scan web passif/actif | **OWASP ZAP** | mode proxy + « scan » | en-têtes manquants, XSS, injections |
| Scan serveur web | **Nikto** | `nikto -h https://<IP>` | fichiers/config à risque |
| En-têtes HTTP | **curl** | `curl -skI https://<IP>` | HSTS, CSP, cookies, en-tête `Server` |
| Test MQTT (si fourni) | **mosquitto_sub** | `mosquitto_sub -h <IP> -p 8883 ...` | ACL : accès refusé attendu sans compte |

Ces outils **rapportent** ; ils ne « défoncent » rien. On reste sur de la **découverte** et de la
**vérification**, jamais sur du déni de service ni de l'exfiltration de données réelles.

---

## 3. Grille de contrôle (que vérifier, et le résultat attendu d'un système sûr)

Reprend notre propre top 10 (`fiche-pentest.md` §7), vu **côté auditeur**. Pour chaque ligne :
« OK » si le système cible se défend, « finding » sinon (→ §4).

| # | Contrôle | Comment | Attendu d'un système sûr |
|---|---|---|---|
| 1 | Surface exposée | `nmap -sV -p- <IP>` | peu de ports ; pas de base de données ni d'admin exposés |
| 2 | TLS | `testssl.sh` | TLS 1.2+ seulement, pas de chiffrement faible |
| 3 | En-têtes de sécurité | `curl -skI` | HSTS, CSP présents ; pas d'en-tête `Server`/version |
| 4 | Authentification | essais de connexion | blocage après N échecs ; pas de compte par défaut |
| 5 | Cookies de session | `curl -skI`, DevTools | `HttpOnly`, `Secure`, `SameSite` |
| 6 | Contrôle d'accès | une page admin sans être admin | **403/401**, pas de contournement par l'URL |
| 7 | Injection SQL | un `'` dans un champ, via ZAP | pas d'erreur SQL, pas de contournement |
| 8 | XSS | `<script>` dans un champ, via ZAP | la valeur est échappée, pas exécutée |
| 9 | CSRF | requête sans en-tête d'origine | refusée |
| 10 | IoT / MQTT | abonnement sans identifiants | refusé par l'ACL |

**Règle d'or** : dès qu'un test risque de **casser** le service (flood, suppression de données), on
**s'arrête et on note** « testable mais non exécuté pour ne pas nuire ». Un bon auditeur ne casse pas
la démo qu'il audite.

---

## 4. Modèle de rapport de finding

Un finding par entrée. C'est ce format (clair, reproductible, avec remédiation) qui est noté.

```
Titre        : <court et parlant, ex. « Cookie de session sans l'attribut HttpOnly »>
Cible        : <IP / URL / service>
Sévérité     : Critique | Élevée | Moyenne | Faible | Information
Catégorie    : <ex. gestion de session, injection, mauvaise config...>
Constat      : <ce qu'on observe>
Preuve       : <commande + sortie, ou capture d'écran ; données réelles masquées>
Reproduction : <étapes numérotées pour refaire le test>
Impact       : <ce qu'un attaquant pourrait en faire>
Remédiation  : <correction concrète recommandée>
Statut       : <ouvert / corrigé pendant la séance>
```

Échelle de sévérité (repère simple) :
- **Critique** : prise de contrôle, accès aux données sans authentification.
- **Élevée** : contournement d'authentification ou d'autorisation, injection exploitable.
- **Moyenne** : fuite d'information utile, mauvaise config exploitable sous conditions.
- **Faible** : en-tête manquant, détail de version exposé.
- **Information** : observation sans impact direct.

**Synthèse en tête de rapport** : un tableau « N findings : X critiques, Y élevés… » + une phrase de
conclusion. Le jury lit ça en premier.

---

## 5. Déontologie (et ce qui fait la différence au barème)

- On teste **uniquement** le périmètre autorisé, dans la **fenêtre** donnée.
- On **ne casse pas** le service audité (pas de DoS, pas de suppression).
- On **ne divulgue pas** les findings d'un autre groupe en dehors du cadre de l'exercice.
- Si on trouve une faille grave, on **prévient** l'encadrant et le groupe concerné, on n'en abuse pas.
- On s'entraîne **avant** jeudi sur des cibles faites pour ça (voir plus bas), pas sur les autres.
