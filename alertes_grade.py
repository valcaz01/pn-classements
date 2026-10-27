#!/usr/bin/env python3
"""PN — alertes de passage de grade des non-gradés, postées dans le salon Discord « Alertes-promotions ».

Paliers (décisions de Val, 27/10/2026) :
  Cadet            → Policier adjoint                     : 3 jours de grade + 200 points de recrutement
  Policier adjoint → éligible formation Gardien de la paix : 3 jours de grade + 300 points de recrutement (total)

Grade lu dans l'humeur Habbo (« [PN] Cadet [XX] », « [PN] Policier adjoint [XX] ») via l'API publique,
ancienneté = date où le script voit ce grade pour la première fois dans l'humeur.
Une alerte par membre et par palier ; état gardé dans etat_grades.json.

Variables d'environnement : WIRED_ROOM_ID, WIRED_READ_KEY (lecture), DISCORD_WEBHOOK (secret),
HABBO_HOST (défaut www.habbo.fr), VAR_PR (défaut pts_recrutement), TEST=1 pour poster un message de test.
"""
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HOST = os.environ.get("HABBO_HOST", "www.habbo.fr")
ROOM = os.environ.get("WIRED_ROOM_ID", "").strip()
KEY = os.environ.get("WIRED_READ_KEY", "").strip()
HOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
VAR_PR = (os.environ.get("VAR_PR") or "pts_recrutement").strip()
VAR_ALL = "matricule"          # tous les visiteurs du QG en ont un : sert à repérer les Cadets dès leur arrivée
ETAT = "etat_grades.json"
RECHECK = timedelta(hours=3)   # humeur relue au plus toutes les 3 h par membre
MAX_CHECKS = 60                # humeurs lues par passage (le workflow tourne toutes les 15 min)
NOW = datetime.now(timezone.utc)

PALIERS = {
    "cadet": {"label": "Cadet", "seuil": 200, "jours": 3, "cible": "Policier adjoint",
              "texte": "peut passer **Policier adjoint**", "couleur": 0x2F8FD4},
    "pa": {"label": "Policier adjoint", "seuil": 300, "jours": 3, "cible": "formation Gardien de la paix",
           "texte": "est éligible à la **formation Gardien de la paix**", "couleur": 0xE3B34A},
}
RE_GRADE = re.compile(r"\[\s*p\.?\s*n\.?\s*\]\s*(cadet|policier[\s\-]*adjoint)\b")


def req(url, headers=None, data=None, method=None):
    h = {"Accept": "application/json", "User-Agent": "pn-alertes/1.0"}
    h.update(headers or {})
    r = urllib.request.Request(url, headers=h, data=data, method=method)
    for _ in range(4):
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                body = resp.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(int(e.headers.get("Retry-After") or 5) + 1)
                continue
            if e.code == 404:
                return None
            print(f"[HTTP] {e.code} {url.split('?')[0]} : {e.read()[:200]!r}", file=sys.stderr)
            raise
    raise RuntimeError("trop de requêtes refusées")


def wired(path, params=None):
    url = f"https://{HOST}/api/public/rooms/{ROOM}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    res = req(url, {"X-Wired-Read-Key": KEY})
    time.sleep(0.6)
    return res


def all_values(var):
    """{user_id: (pseudo, valeur)} pour tous les porteurs de la variable (pages de 100)."""
    out, page = {}, 1
    while True:
        data = wired(f"/variables/user/{urllib.parse.quote(var)}/users",
                     {"order_by": "value", "order_dir": "desc", "page": page, "size": 100}) or {}
        items = data.get("items") or []
        for it in items:
            u = it.get("user") or {}
            v = (it.get("variable") or {}).get("value", it.get("value", 0))
            if u.get("id") is not None:
                out[str(u["id"])] = (u.get("name") or "", int(v))
        if len(items) < 100 or page >= 50:
            return out
        page += 1


def grade_from_motto(motto):
    m = unicodedata.normalize("NFKD", motto or "").encode("ascii", "ignore").decode().lower()
    g = RE_GRADE.search(m)
    if not g:
        return None
    return "cadet" if g.group(1) == "cadet" else "pa"


def motto_of(name):
    data = req(f"https://{HOST}/api/public/users?name={urllib.parse.quote(name)}")
    time.sleep(0.3)
    return None if data is None else (data.get("motto") or "")


def post(embed):
    if not HOOK:
        print("[Discord] pas de webhook configuré : message non envoyé →", embed.get("title"))
        return False
    req(HOOK + ("&" if "?" in HOOK else "?") + "wait=true", {"Content-Type": "application/json"},
        json.dumps({"username": "Alertes PN", "embeds": [embed], "allowed_mentions": {"parse": []}}).encode(),
        "POST")
    time.sleep(1)
    return True


def iso(d):
    return d.isoformat(timespec="seconds")


def main():
    if os.environ.get("TEST") == "1":
        post({"title": "✅ Système d'alertes connecté",
              "description": "Ce salon recevra une alerte dès qu'un **Cadet** ou un **Policier adjoint** "
                             "remplit les conditions pour monter.\n\n"
                             "• Cadet → Policier adjoint : 3 jours + 200 points de recrutement\n"
                             "• Policier adjoint → formation Gardien de la paix : 3 jours + 300 points de recrutement",
              "color": 0x0B3A5E, "footer": {"text": "Police Nationale · vérification toutes les 15 minutes"},
              "timestamp": iso(NOW)})
        print("Message de test envoyé.")
        return
    if not ROOM or not KEY:
        sys.exit("WIRED_ROOM_ID et WIRED_READ_KEY sont obligatoires.")

    try:
        etat = json.load(open(ETAT, encoding="utf-8"))
    except (OSError, ValueError):
        etat = {}

    pr = all_values(VAR_PR)
    membres = all_values(VAR_ALL)
    for uid, (name, _) in pr.items():
        membres.setdefault(uid, (name, 0))
    print(f"[API] {len(membres)} visiteurs suivis, {len(pr)} avec des points de recrutement")

    # Ordre de relecture des humeurs : d'abord ceux proches d'un palier, puis les plus anciennement vérifiés.
    def prio(uid):
        e = etat.get(uid, {})
        pts = pr.get(uid, ("", 0))[1]
        return (0 if pts >= 200 else 1, e.get("verifie", ""))
    checks = 0
    for uid in sorted(membres, key=prio):
        name = membres[uid][0] or etat.get(uid, {}).get("pseudo", "")
        e = etat.setdefault(uid, {"pseudo": name, "grade": None, "depuis": None, "alertes": {}})
        e["pseudo"] = name or e.get("pseudo")
        last = e.get("verifie")
        if last and NOW - datetime.fromisoformat(last) < RECHECK:
            continue
        if checks >= MAX_CHECKS or not e["pseudo"]:
            continue
        motto = motto_of(e["pseudo"])
        checks += 1
        e["verifie"] = iso(NOW)
        if motto is None:
            continue
        g = grade_from_motto(motto)
        e["humeur"] = motto
        if g != e.get("grade"):
            e["grade"], e["depuis"] = g, (iso(NOW) if g else None)
    print(f"[Habbo] {checks} humeurs relues")

    envoyees = 0
    for uid, e in etat.items():
        g = e.get("grade")
        if g not in PALIERS or not e.get("depuis"):
            continue
        p = PALIERS[g]
        pts = pr.get(uid, ("", 0))[1]
        depuis = datetime.fromisoformat(e["depuis"])
        jours = (NOW - depuis).total_seconds() / 86400
        if pts < p["seuil"] or jours < p["jours"] or g in e.get("alertes", {}):
            continue
        embed = {
            "title": f"🎖️ {e['pseudo']} {p['texte'].replace('**', '')}",
            "description": f"**{e['pseudo']}** {p['texte']}.",
            "color": p["couleur"],
            "fields": [
                {"name": "Grade actuel", "value": p["label"], "inline": True},
                {"name": "Ancienneté", "value": f"{int(jours)} jours (≥ {p['jours']})", "inline": True},
                {"name": "Points de recrutement", "value": f"{pts} (≥ {p['seuil']})", "inline": True},
                {"name": "Humeur", "value": f"`{(e.get('humeur') or '')[:100]}`", "inline": False},
            ],
            "thumbnail": {"url": f"https://{HOST}/habbo-imaging/avatarimage?user={urllib.parse.quote(e['pseudo'])}"
                                 "&direction=2&head_direction=3&headonly=1&size=l"},
            "footer": {"text": f"Grade vu dans l'humeur depuis le {depuis.astimezone(timezone(timedelta(hours=1))).strftime('%d/%m/%Y')}"},
            "timestamp": iso(NOW),
        }
        if post(embed):
            e.setdefault("alertes", {})[g] = iso(NOW)
            envoyees += 1

    with open(ETAT, "w", encoding="utf-8") as f:
        json.dump(etat, f, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"[Discord] {envoyees} alerte(s) envoyée(s)")


if __name__ == "__main__":
    main()
