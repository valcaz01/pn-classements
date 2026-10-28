#!/bin/bash
# PN — mise à jour automatique du serveur (timer systemd, toutes les 2 minutes).
# Récupère le dépôt public, garde les commits locaux de données par-dessus, relance le service si serveur/ a changé.
set -euo pipefail
cd /opt/pn/repo
exec 9>/opt/pn/lock/git.lock
flock 9
before=$(git rev-parse HEAD)
git fetch -q https://github.com/valcaz01/pn-classements.git main
if ! git rebase -q --autostash FETCH_HEAD; then git rebase --abort || true; exit 1; fi
after=$(git rev-parse HEAD)
flock -u 9
if ! git diff --quiet "$before" "$after" -- serveur/; then
  echo "serveur/ modifié : reconstruction du service"
  docker compose -f serveur/compose.yaml up -d --build --force-recreate
fi
