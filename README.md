# pn-classements
Classements XP et points de recrutement du forum P.N (pn-rpg.fr)

Le serveur collecte les données toutes les deux minutes. Le widget et la page
Classements relisent le fichier publié toutes les deux minutes sans interroger
directement Habbo. « Actualisé à » indique la dernière collecte réussie, heure
de Paris ; en cas d'erreur, les dernières valeurs et leur heure sont conservées.

Les totaux viennent des variables permanentes WIRED de la salle configurée. Le
classement hebdomadaire additionne automatiquement les augmentations observées
par l'API, du lundi 00 h au dimanche 23 h 59, heure de Paris. La nouvelle
semaine commence le lundi à 00 h. Le
premier lancement crée un point de départ : les scores gagnés avant l'activation
du suivi ne peuvent pas être reconstitués.

La publication GitHub réessaie les erreurs de service et de connexion jusqu'à
quatre fois, avec des pauses de 2, 8 et 20 secondes. Les relevés sont enregistrés
localement avant l'envoi ; une publication ratée est rattrapée au cycle suivant.
Un refus d'authentification SSH peut aussi être transitoire pendant un incident
GitHub : les nouvelles tentatives utilisent la même clé de déploiement.

Le bot prévient la Direction après au moins trois cycles en échec et dix minutes
de blocage, puis rappelle le problème après 24 heures si nécessaire. Il confirme
le rétablissement une fois les envois rattrapés. Ce suivi est conservé hors du
dépôt dans `/opt/pn/lock/publication-health.json`, y compris après un redémarrage.
Les erreurs techniques complètes restent dans les logs du serveur.

GitHub reste la source lue par le forum : pendant son indisponibilité, le forum
conserve les dernières valeurs publiées et leur véritable heure de collecte.
Ces reprises concernent uniquement les échanges Git ; elles ne rejouent aucune
attribution de points, action sur un badge ou remise à zéro Habbo.

Les lectures Habbo ont aussi leur propre reprise : trois tentatives GET au
maximum pour un timeout, une coupure réseau, une erreur 500/502/503/504 ou une
réponse JSON invalide, avec un délai socket de 20 secondes et des pauses de
2 puis 5 secondes. Un refus 429 respecte `Retry-After` ; si le délai demandé
dépasse 30 secondes, la lecture est reportée au cycle suivant plutôt que de
bloquer le bot. Les refus de configuration (401/403) ne sont pas rejoués. Les
POST/PATCH/PUT, dont l'attribution d'XP et la remise à zéro, ne sont jamais
rejoués automatiquement après une réponse incertaine.

La santé des classements et des promotions est suivie séparément dans
`/opt/pn/lock/collection-health.json`. Une seule notification regroupe les
travaux encore bloqués après dix minutes et au moins trois cycles en échec
du même travail ; rappel après 24 heures et message de rétablissement quand
les deux vérifications ont réussi. Un succès des classements ne masque pas
une panne des promotions. Les traces techniques restent dans les logs.
Une lecture de classement incomplète conserve les fichiers et l'heure de
la dernière collecte valide ; la reprise compte les gains depuis ce dernier
relevé sans les compter deux fois. Les commits du service portent le repère
`PN-Collector: 2026-11-04.1` pour vérifier le déploiement de cette correction.

La mise à jour du serveur compare le dossier `serveur/` à la dernière version
relancée, conservée dans `/opt/pn/lock/deployed-service-tree`. Elle recharge donc
une correction même si le bot l'a déjà récupérée avant le timer système.

Vérification hors ligne, avec des dépôts Git locaux et des pannes simulées :
`python -m unittest discover -s tests -v`.
