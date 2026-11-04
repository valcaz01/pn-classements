#!/bin/bash
# PN — mise à jour automatique du serveur (timer systemd, toutes les 2 minutes).
# Récupère le dépôt public, garde les commits locaux de données par-dessus, relance le service si serveur/ a changé.
set -euo pipefail
PN_ROOT="${PN_ROOT:-/opt/pn}"
cd "$PN_ROOT/repo"
exec 9>"$PN_ROOT/lock/git.lock"
flock 9
git fetch -q https://github.com/valcaz01/pn-classements.git main
if ! git rebase -q --autostash FETCH_HEAD; then git rebase --abort || true; exit 1; fi
service_tree=$(git rev-parse HEAD:serveur)
state_file="$PN_ROOT/lock/deployed-service-tree"
deployed_tree=$(cat "$state_file" 2>/dev/null || true)
flock -u 9
if [[ "$service_tree" != "$deployed_tree" ]]; then
  # Le bot peut avoir déjà récupéré ce code par son propre fetch/rebase.
  # Comparer à la dernière version réellement relancée, pas au HEAD avant fetch.
  echo "Nouvelle version du service : reconstruction et redémarrage"
  docker compose -f serveur/compose.yaml up -d --build --force-recreate
  printf '%s\n' "$service_tree" > "$state_file.tmp"
  mv -- "$state_file.tmp" "$state_file"
fi
