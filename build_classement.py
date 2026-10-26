#!/usr/bin/env python3
"""PN — classement.json pour la page « Classements » et le widget podium du forum.

Lit les variables utilisateur permanentes de l'appart (add-on « API Web ») et écrit
classement.json : {"updated": ISO, "xp": [{"name", "value"}], "pr": [...]}.

Variables d'environnement :
  HABBO_HOST       hôtel (défaut www.habbo.fr)
  WIRED_ROOM_ID    id de l'appart qui porte l'add-on (salle Test / QG)
  WIRED_READ_KEY   clé de LECTURE de l'add-on (jamais la clé d'écriture)
  VAR_XP           nom de la variable XP (défaut xp)
  VAR_PR           nom de la variable points de recrutement (défaut pts_recrutement)
  TOP              nombre de joueurs par classement (défaut 50)
  OUT              fichier de sortie (défaut classement.json)

Aucune dépendance : bibliothèque standard uniquement.
Limites de l'API (par appart) : listes et profils 120/min, rafale 20 / 10 s → pause 0,6 s.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HOST = os.environ.get("HABBO_HOST", "www.habbo.fr")
ROOM = os.environ.get("WIRED_ROOM_ID", "").strip()
KEY = os.environ.get("WIRED_READ_KEY", "").strip()
VARS = {"xp": os.environ.get("VAR_XP", "xp").strip(), "pr": (os.environ.get("VAR_PR") or "pts_recrutement").strip()}
TOP = int(os.environ.get("TOP", "50"))
OUT = os.environ.get("OUT", "classement.json")
PAUSE = 0.6


def get(path, params=None):
    url = f"https://{HOST}/api/public/rooms/{ROOM}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-Wired-Read-Key": KEY, "Accept": "application/json",
                                               "User-Agent": "pn-classements/1.0"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(int(e.headers.get("Retry-After") or 10))
                continue
            body = e.read().decode("utf-8", "replace")[:300]
            print(f"[API] {e.code} sur {path} : {body}", file=sys.stderr)
            if e.code == 404:
                return None
            raise
    raise RuntimeError(f"Trop de requêtes refusées : {path}")


def parse(item):
    """Élément de liste : {"user": {"id", "name", ...}, "variable": {"value", ...}} (format constaté le 26/10/2026)."""
    user = item.get("user") or {}
    var = item.get("variable") or {}
    value = var.get("value", item.get("value", 0))
    uid = user.get("id", item.get("entity_id", item.get("id")))
    return uid, user.get("name") or "", int(value)


def top_values(var):
    data = get(f"/variables/user/{urllib.parse.quote(var)}/users",
               {"order_by": "value", "order_dir": "desc", "page": 1, "size": min(TOP, 100)})
    time.sleep(PAUSE)
    items = (data or {}).get("items", [])
    print(f"[API] variable « {var} » : {len(items)} valeur(s)")
    rows = [parse(i) for i in items]
    return [r for r in rows if r[2] > 0]


def main():
    if not ROOM or not KEY:
        sys.exit("WIRED_ROOM_ID et WIRED_READ_KEY sont obligatoires.")
    try:
        cache = json.load(open("noms.json", encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}

    out = {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "xp": [], "pr": []}
    for key, var in VARS.items():
        if not var:
            continue
        for uid, name, value in top_values(var):
            uid = str(uid)
            if name:
                cache[uid] = name
            elif uid not in cache:
                prof = get(f"/variables_profile/user/users/{uid}")
                time.sleep(PAUSE)
                cache[uid] = ((prof or {}).get("user") or {}).get("name") or ""
            if cache[uid]:
                out[key].append({"name": cache[uid], "value": value})

    try:  # date inchangée si les scores n'ont pas bougé : pas de commit inutile
        old = json.load(open(OUT, encoding="utf-8"))
        if old.get("xp") == out["xp"] and old.get("pr") == out["pr"]:
            out["updated"] = old.get("updated", out["updated"])
    except (OSError, ValueError):
        pass
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    with open("noms.json", "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    print(f"XP : {len(out['xp'])} joueurs · PR : {len(out['pr'])} joueurs → {OUT}")


if __name__ == "__main__":
    main()
