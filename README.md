# pn-classements
Classements XP et points de recrutement du forum P.N (pn-rpg.fr)

Pour le passage de Cadet à Policier adjoint, le bot vérifie l'adhésion au
groupe Habbo Cadet et les 200 points de recrutement cumulés. Le jour d’adhésion
compte comme jour 1. Un badge obtenu mardi ouvre le passage jeudi dès 0 h,
au début du 3ᵉ jour, heure de Paris. Le suivi
continue si l'humeur ou le pseudo change. Les dates des cadets déjà présents
le 20 novembre 2026 proviennent des attributions confirmées sur le forum.
Pour les nouvelles adhésions, le registre retient le jour où le badge est vu
dans le groupe. Un retrait confirmé termine cette période d'ancienneté.

Dans **🎓︱𝖥ormas-à-faire**, PN · Alertes ajoute au tableau chaque
policier adjoint qui atteint **300 PR cumulés** et son **3ᵉ jour de badge**, en
comptant le jour de remise en heure de Paris. Les attributions déjà confirmées
par un HG sont reprises au premier passage. Sans confirmation datée, le suivi
commence le jour où le badge est observé. Un gardien de la paix ou un gradé
qui conserve le badge inférieur est exclu. Les nouveaux pseudos rejoignent
un seul tableau. Sa vue par défaut liste la formation Gardien de la paix.
Le menu du haut ouvre une vue personnelle des **formations qualifiantes**
pour les gardiens de la paix dès le 3ᵉ jour du badge. Ce deuxième passage
n’a pas de seuil de PR. Les gradés et les comptes exclus sont écartés du suivi.
Le bot mentionne `@everyone` une fois par lot de nouveaux noms dans ce salon privé. Les formateurs
utilisent **Formation faite**, choisissent le pseudo et collent le lien du sujet ou du message de formation
sur le forum PN. Ils peuvent ouvrir le lien avant de confirmer.
Chaque pseudo est suivi de la date du badge correspondant entre parenthèses, au format `JJ/MM/AA`.
La mention « 🟢 en ligne » apparaît lorsque Habbo indique le policier connecté. Pour une personne
hors ligne, le bot affiche la dernière connexion communiquée par son profil public, en heure de Paris.
Il revérifie les présences toutes les deux minutes et modifie le message sans mention.
**Retirer de la liste** se trouve au bas du tableau du salon. Seuls les Hauts gradés, la Supervision
et la Direction qui ont accès au salon peuvent l’utiliser. Pour la formation qualifiante, ils
sélectionnent cette formation dans le menu et retrouvent le bouton dans leur vue personnelle. Le retrait confirmé
s’applique au badge et à la formation en cours. Il est conservé à part des formations effectuées.
**Historique des formations** affiche le policier, le formateur, la date de confirmation et le lien du sujet
pour les nouvelles formations. Le salon est en lecture seule pour les formateurs. Le suivi des
badges et du tableau persiste dans les fichiers privés `formations-pa.json`
et `tableau-formations.json` de `/opt/pn/lock/`.

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

Le salon privé `to-do-list` de la Direction affiche une carte par tâche ouverte.
Le tableau indique combien de tâches restent à faire et combien sont en cours.
Chaque carte indique le responsable, l’état et la priorité. Le tableau se
prolonge dans des messages supplémentaires au-delà de huit cartes.

« Ajouter une tâche » ouvre une liste privée de responsables ayant le rôle
Direction et accès au salon. « Rechercher par pseudo » permet aussi de retrouver
un collègue qui vient de recevoir le rôle, même s’il n’a encore pris part
à aucune tâche. Le choix du responsable ouvre le formulaire avec le titre
et les quatre priorités Basse, Normale (par défaut), Haute et Urgente.
L’attribution mentionne immédiatement le responsable dans le fil de notifications.
Les priorités les plus élevées apparaissent en premier.
Tous les membres de la Direction peuvent annuler une tâche, même
attribuée à un collègue. Le bouton « Historique » conserve les tâches terminées
et annulées, avec leur responsable, leur priorité, la date et l’auteur de la
clôture, dans la base persistante privée du bot.
Les tâches restent affichées jusqu’à leur clôture. Un rappel mentionne chaque
responsable une fois par jour à partir de 18 h, heure de Paris, pour ses tâches
ouvertes depuis au moins 24 heures. Les mentions et les rappels arrivent dans
le fil « 🔔 Notifications des tâches » rattaché au tableau épinglé.
Les anciens avis du bot y sont transférés silencieusement. Après un échange
dans le salon, le tableau réapparaît en bas, avec ses boutons. L’épingle
renvoie alors vers les cartes et garde son adresse permanente. Les listes privées
(y compris l’historique), les confirmations d’attribution et l’annonce de création
du fil disparaissent automatiquement après deux minutes.

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
