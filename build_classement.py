#!/usr/bin/env python3
"""PN — classement.json pour la page « Classements » et le widget podium du forum.

Lit les variables utilisateur permanentes de l'appart (add-on « API Web ») et écrit
classement.json avec les scores totaux et hebdomadaires. Les totaux permanents
sont échantillonnés toutes les 15 minutes ; leur différence alimente la semaine.

Variables d'environnement :
  HABBO_HOST       hôtel (défaut www.habbo.fr)
  WIRED_ROOM_ID    id de l'appart qui porte l'add-on (salle Test / QG)
  WIRED_READ_KEY   clé de LECTURE de l'add-on (jamais la clé d'écriture)
  VAR_XP           nom de la variable XP (défaut xp)
  VAR_PR           nom de la variable points de recrutement (défaut pts_recrutement_total)
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
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

HOST = os.environ.get("HABBO_HOST", "www.habbo.fr")
ROOM = os.environ.get("WIRED_ROOM_ID", "").strip()
KEY = os.environ.get("WIRED_READ_KEY", "").strip()
VARS = {"xp": os.environ.get("VAR_XP", "xp").strip(), "pr": (os.environ.get("VAR_PR") or "pts_recrutement_total").strip()}
TOP = int(os.environ.get("TOP", "50"))
OUT = os.environ.get("OUT", "classement.json")
STATE = os.environ.get("STATE", "classement-state.json")
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
    rows = []
    page = 1
    while True:
        data = get(f"/variables/user/{urllib.parse.quote(var)}/users",
                   {"order_by": "value", "order_dir": "desc", "page": page, "size": 100})
        if data is None:
            raise RuntimeError(f"Variable WIRED « {var} » introuvable ; publication annulée.")
        items = (data or {}).get("items", [])
        rows.extend(parse(i) for i in items)
        if len(items) < 100:
            break
        page += 1
        time.sleep(PAUSE)
    print(f"[API] variable « {var} » : {len(rows)} valeur(s)")
    return [r for r in rows if r[2] > 0]


def read_json(path, fallback):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return fallback


def sunday_start(now):
    local_date = now.astimezone(ZoneInfo("Europe/Paris")).date()
    days_since_sunday = (local_date.weekday() + 1) % 7
    return (local_date - timedelta(days=days_since_sunday)).isoformat()


def main():
    if not ROOM or not KEY:
        sys.exit("WIRED_ROOM_ID et WIRED_READ_KEY sont obligatoires.")
    cache = read_json("noms.json", {})
    previous_data = read_json(OUT, {})
    state = read_json(STATE, {})

    now = datetime.now(timezone.utc)
    current_week = sunday_start(now)
    current = {key: {} for key in VARS}
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
                current[key][uid] = {"name": cache[uid], "value": value}

    # Au premier lancement, prendre les scores courants comme point de départ.
    # Les gains antérieurs à l'activation du suivi ne sont pas reconstituables.
    previous_totals = state.get("last_totals", {}) if state.get("initialized") else {
        key: {uid: row["value"] for uid, row in rows.items()}
        for key, rows in current.items()
    }
    if state.get("week_start") == current_week:
        weekly = state.get("week_values") or {key: {} for key in VARS}
    else:
        weekly = {key: {} for key in VARS}

    for key, rows in current.items():
        before = previous_totals.get(key, {})
        week_rows = weekly.setdefault(key, {})
        for uid, row in rows.items():
            delta = max(0, row["value"] - int(before.get(uid, 0)))
            if delta:
                old_week = week_rows.get(uid, {})
                week_rows[uid] = {
                    "name": row["name"],
                    "value": int(old_week.get("value", 0)) + delta,
                }
            # Une correction manuelle peut abaisser le total en cours de semaine.
            # Le classement hebdomadaire ne doit alors pas afficher davantage
            # de points que la variable WIRED actuelle du joueur.
            if uid in week_rows:
                week_rows[uid]["value"] = min(int(week_rows[uid]["value"]), row["value"])

    # Une variable absente devient une base à zéro, afin de ne pas recompter
    # un ancien total si elle est recréée plus tard.
    last_totals = {key: {uid: 0 for uid in state.get("last_totals", {}).get(key, {})}
                   for key in VARS}
    for key, rows in current.items():
        last_totals[key].update({uid: row["value"] for uid, row in rows.items()})

    out = {"updated": now.isoformat(timespec="seconds"), "week_start": current_week}
    for key in VARS:
        total_rows = sorted(current[key].values(), key=lambda r: (-r["value"], r["name"].casefold()))
        week_rows = sorted(weekly.get(key, {}).values(), key=lambda r: (-r["value"], r["name"].casefold()))
        out[key] = {
            "total": total_rows[:TOP],
            "week": [r for r in week_rows if r["value"] > 0][:TOP],
        }

    if (previous_data.get("xp") == out.get("xp") and previous_data.get("pr") == out.get("pr")
            and previous_data.get("week_start") == current_week):
        out["updated"] = previous_data.get("updated", out["updated"])

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    with open("noms.json", "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump({"initialized": True, "week_start": current_week, "last_totals": last_totals,
                   "week_values": weekly}, f, ensure_ascii=False, indent=1)
    print(f"XP : {len(out['xp']['total'])} totaux / {len(out['xp']['week'])} semaine · "
          f"PR : {len(out['pr']['total'])} totaux / {len(out['pr']['week'])} semaine → {OUT}")


if __name__ == "__main__":
    main()
