# Bonus des notations — 23 novembre 2026

## Publication

Le fichier `notations-bonus.min.js` est publié dans le panneau Forumactif : Modules > Gestion des codes JavaScript > « PN — Gestion de la liste des gradés » (script 18, ressource publique 18807.js). Placement conservé : sur les sujets. `notations-bonus.js` est la version lisible correspondante.

La case « Attribuer un bonus de +5 XP de paie » est décochée par défaut. La demande est créée uniquement à l'enregistrement de la notation ou de sa correction. Le calcul des PE est inchangé. Les permissions existantes de supervision et d'édition du registre restent applicables.

Le service `serveur/bonus_notations.py` lit la file du registre officiel, crédite exclusivement `xp_paie` de 5 et conserve les reçus dans SQLite. Une contrainte par membre/semaine, également vérifiée par identifiant Habbo, évite les doubles crédits. Une écriture dont le résultat est incertain n'est jamais rejouée automatiquement. Une correction conserve une demande déjà envoyée.

Le bot relève la file à son cycle habituel de deux minutes. Le statut public est dans `bonus-notations.json`. Les clés API ne sont pas présentes dans le code du forum.

## Vérification effectuée

- 12 tests du bonus côté serveur et 4 tests du mécanisme de mise à jour réussis.
- Comparaison des PE avant/après sur les 9 grades et les 3 pôles ; tests de la case, de la file et des corrections réussis.
- En production : état `health.status = ready`, lecture de la valeur existante `xp_paie` réussie.
- Dans Chrome : 11 cases décochées visibles sur les fiches Formation ; passage en correction, cocher/décocher puis annulation vérifiés.
- Aucune notation publiée et aucun XP attribué pour le test. Une attribution réelle complète n'a donc pas été exécutée pendant cette vérification.

## Retour arrière

La sauvegarde exacte du script avant modification est conservée localement dans `forum/admin-before.js` dans le dossier de travail du 23/11. La réimporter dans le même script 18 permet de retirer l'interface. Ne pas supprimer la base SQLite des reçus : elle préserve la protection contre les doubles attributions.

Le service a été déployé par la mise à jour automatique existante à partir du commit `cdaf43ca`. Aucun accès serveur supplémentaire ni changement de pare-feu n'a été nécessaire.
