#!/usr/bin/env python3
"""PN — alertes de passage de grade des non-gradés, postées dans le salon Discord « Alertes-promotions ».

Paliers :
  Cadet → Policier adjoint dès le 3ᵉ jour du badge (adhésion = jour 1, heure de Paris) avec 200 points de recrutement.
  Policier adjoint → éligible formation Gardien de la paix : 3 jours de grade + 300 points de recrutement (total)

Le registre badges_cadets.json suit l'adhésion au groupe Cadet par identifiant Habbo stable.
Le palier Policier adjoint → formation conserve son suivi de l'humeur et de ses 72 heures.
Une alerte par membre et par palier ; état gardé dans etat_grades.json.
Depuis le 28/10/2026, les alertes sont postées par le bot PN · Alertes (serveur Hetzner, serveur/pn_bot.py) :
l'alerte Cadet → Policier adjoint porte la procédure et un bouton vert « Badge attribué » (Hauts gradés et plus).
La clé « _pn » de l'embed (membre, palier) est lue puis retirée par le bot avant l'envoi.

Variables d'environnement : WIRED_ROOM_ID, WIRED_READ_KEY (lecture), DISCORD_WEBHOOK (secret),
HABBO_HOST (défaut www.habbo.fr), VAR_PR (défaut pts_recrutement), TEST=1 pour poster un message de test.
"""
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from serveur.habbo_http import ApiRequestError, request_json

HOST = os.environ.get("HABBO_HOST", "www.habbo.fr")
ROOM = os.environ.get("WIRED_ROOM_ID", "").strip()
KEY = os.environ.get("WIRED_READ_KEY", "").strip()
HOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
VAR_PR = (os.environ.get("VAR_PR") or "pts_recrutement").strip()
VAR_ALL = "matricule"          # tous les visiteurs du QG en ont un : sert à repérer les Cadets dès leur arrivée
ETAT = "etat_grades.json"
BADGES_CADETS = "badges_cadets.json"
PARIS = ZoneInfo("Europe/Paris")
GROUPE_CADET = "g-hhfr-3dcf0cad12934ba41c258628d82bde40"
# Les gradés peuvent conserver leurs anciens badges et administrer le groupe Cadet.
GROUPES_SUPERIEURS = (
    "g-hhfr-98146efd0f202d369c38a5d5265d124e",  # Policier adjoint
    "g-hhfr-f9e03b6a56b74caf069c4b3a3d80aabd",  # Gardien de la paix
    "g-hhfr-e8723fe59e066f9847347ab936a0e9c6",  # Gradé débutant
    "g-hhfr-f1c6e473ac8a29547c44a4675b750585",  # Gradé intermédiaire
    "g-hhfr-d7080b2c1318a56acc06cf8b2b7568be",  # Gradé confirmé
    "g-hhfr-c52059ba6f45cc055c9fb5b9b5a3bc64",  # Haut gradé
    "g-hhfr-c2e7127da613e13151bd947c7593bb95",  # Superviseur
    "g-hhfr-918ddc2eee80c1dc557c1e4d4e023828",  # Direction
)
RECHECK = timedelta(hours=3)   # humeur relue au plus toutes les 3 h par membre
MAX_CHECKS = 60                # humeurs lues par passage (service toutes les 2 min, cache de 3 h)
NOW = datetime.now(timezone.utc)

PALIERS = {
    "cadet": {"label": "Cadet", "seuil": 200, "jours": 3, "cible": "Policier adjoint",
              "texte": "peut passer **Policier adjoint**", "couleur": 0x2F8FD4},
    "pa": {"label": "Policier adjoint", "seuil": 300, "jours": 3, "cible": "formation Gardien de la paix",
           "texte": "est éligible à la **formation Gardien de la paix**", "couleur": 0xE3B34A},
}
RE_GRADE = re.compile(r"\[\s*p\.?\s*n\.?\s*\]\s*(cadet(?:te)?|policier[\s-]*adjoint|policiere[\s-]*adjointe)\b")


def req(url, headers=None, data=None, method=None):
    h = {"Accept": "application/json", "User-Agent": "pn-alertes/1.0"}
    h.update(headers or {})
    r = urllib.request.Request(url, headers=h, data=data, method=method)
    service = "Habbo" if urllib.parse.urlsplit(url).hostname == HOST else "Discord"
    return request_json(r, service=service)


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
    return "cadet" if g.group(1).startswith("cadet") else "pa"


def motto_of(name):
    data = req(f"https://{HOST}/api/public/users?name={urllib.parse.quote(name)}")
    time.sleep(0.3)
    return None if data is None else (data.get("motto") or "")


def post(embed):
    meta = embed.pop("_pn", None) or {}
    if not HOOK:
        print("[Discord] pas de webhook configuré : message non envoyé →", embed.get("title"))
        return False
    role_id = {"cadet": "1552758582141784225", "pa": "1552122047034032221"}.get(meta.get("palier"))
    payload = {"username": "Alertes PN", "embeds": [embed],
               "allowed_mentions": {"parse": [], "roles": [role_id] if role_id else []}}
    if role_id:
        payload["content"] = f"<@&{role_id}>"
    req(HOOK + ("&" if "?" in HOOK else "?") + "wait=true", {"Content-Type": "application/json"},
        json.dumps(payload).encode(),
        "POST")
    time.sleep(1)
    return True


def iso(d):
    return d.isoformat(timespec="seconds")


def lire_registre(nom):
    with Path(nom).open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Registre invalide : {nom}")
    return data


def enregistrer(nom, data):
    path = Path(nom)
    temporaire = path.with_suffix(".tmp")
    temporaire.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    temporaire.replace(path)


def membres_groupe(groupe):
    chemin = f"/api/public/groups/{groupe}/members"
    rows = req(f"https://{HOST}{chemin}")
    # Chaque groupe officiel a au moins son propriétaire. Une réponse vide/incomplète
    # ne peut servir de preuve qu'un badge a été retiré.
    if not isinstance(rows, list) or not rows or not all(
        isinstance(r, dict) and r.get("uniqueId") and r.get("name") for r in rows
    ):
        raise ApiRequestError("response", endpoint=chemin)
    return {r["uniqueId"]: r for r in rows}


def cadets_du_jour(aujourdhui):
    precedents = lire_registre(BADGES_CADETS)
    membres = membres_groupe(GROUPE_CADET)
    superieurs = set()
    for groupe in GROUPES_SUPERIEURS:
        superieurs.update(membres_groupe(groupe))

    registre = {}
    for hid, membre in membres.items():
        depuis = precedents[hid]["depuis"] if hid in precedents else aujourdhui.isoformat()
        registre[hid] = {"pseudo": membre["name"], "depuis": depuis}

    for hid in precedents.keys() - membres.keys():
        # Une omission de la liste publique, à elle seule, ne casse pas l'ancienneté.
        chemin = f"/api/public/users/{urllib.parse.quote(hid)}/groups"
        try:
            groupes = req(f"https://{HOST}{chemin}")
            if groupes == []:
                profil = req(f"https://{HOST}/api/public/users/{urllib.parse.quote(hid)}")
                if not isinstance(profil, dict) or profil.get("profileVisible") is not True:
                    groupes = None
        except ApiRequestError as exc:
            if exc.status not in (401, 403):
                raise
            groupes = None
        if groupes is None:
            registre[hid] = precedents[hid]  # profil inaccessible : conserver la date, différer l'alerte
            continue
        if not isinstance(groupes, list) or not all(isinstance(g, dict) and g.get("id") for g in groupes):
            raise ApiRequestError("response", endpoint=chemin)
        ids = {g["id"] for g in groupes}
        if GROUPE_CADET in ids:
            registre[hid] = precedents[hid]
            membres[hid] = {"name": precedents[hid]["pseudo"]}
            if ids.intersection(GROUPES_SUPERIEURS):
                superieurs.add(hid)

    if registre != precedents:
        enregistrer(BADGES_CADETS, registre)
    return {r["name"].casefold(): (registre[hid]["depuis"], r.get("motto"))
            for hid, r in membres.items() if hid not in superieurs}


def main():
    if os.environ.get("TEST") == "1":
        post({"title": "✅ Système d'alertes connecté",
              "description": "Ce salon recevra une alerte dès qu'un **Cadet** ou un **Policier adjoint** "
                             "remplit les conditions pour monter.\n\n"
                             "• Cadet → Policier adjoint dès le 3ᵉ jour du badge avec 200 points de recrutement. "
                             "Le jour d’adhésion compte comme jour 1, en heure de Paris.\n"
                             "• Policier adjoint → formation Gardien de la paix : 3 jours + 300 points de recrutement",
              "color": 0x0B3A5E, "footer": {"text": "Police Nationale · vérification toutes les 2 minutes"},
              "timestamp": iso(NOW)})
        print("Message de test envoyé.")
        return
    if not ROOM or not KEY:
        sys.exit("WIRED_ROOM_ID et WIRED_READ_KEY sont obligatoires.")

    try:
        etat = lire_registre(ETAT)
    except FileNotFoundError:
        etat = {}

    pr = all_values(VAR_PR)
    membres = all_values(VAR_ALL)
    for uid, (name, _) in pr.items():
        membres.setdefault(uid, (name, 0))
    aujourdhui = NOW.astimezone(PARIS).date()
    cadets = cadets_du_jour(aujourdhui)
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
        badge = cadets.get((e["pseudo"] or "").casefold())
        if badge:
            jour_badge, humeur = badge
            debut = datetime.fromisoformat(jour_badge).replace(tzinfo=PARIS)
            e["grade"], e["depuis"] = "cadet", iso(debut.astimezone(timezone.utc))
            if humeur is not None and (humeur != e.get("humeur") or not last or NOW - datetime.fromisoformat(last) >= RECHECK):
                e["humeur"], e["verifie"] = humeur, iso(NOW)
            continue
        if e.get("grade") == "cadet":
            e["grade"], e["depuis"] = None, None
            e.pop("verifie", None)
            last = None
        if last and NOW - datetime.fromisoformat(last) < RECHECK:
            continue
        if checks >= MAX_CHECKS or not e["pseudo"]:
            continue
        motto = motto_of(e["pseudo"])
        checks += 1
        e["verifie"] = iso(NOW)
        if motto is None:
            continue
        g = "pa" if grade_from_motto(motto) == "pa" else None
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
        if g == "cadet":
            if e["pseudo"].casefold() not in cadets or e.get("badge", {}).get("cadet"):
                continue
            jours = (aujourdhui - depuis.astimezone(PARIS).date()).days + 1
            anciennete = f"Jour {jours} (adhésion incluse)"
            preuve = {"name": "Badge Habbo", "value": "[PN] Cadet", "inline": False}
            origine = "Badge Cadet depuis le"
        else:
            jours = (NOW - depuis).days
            anciennete = f"{jours} jours (≥ {p['jours']})"
            preuve = {"name": "Humeur", "value": f"`{(e.get('humeur') or '')[:100]}`", "inline": False}
            origine = "Grade vu dans l'humeur depuis le"
        if pts < p["seuil"] or jours < p["jours"] or g in e.get("alertes", {}):
            continue
        embed = {
            "title": f"🎖️ {e['pseudo']} {p['texte'].replace('**', '')}",
            "description": f"**{e['pseudo']}** {p['texte']}.",
            "color": p["couleur"],
            "fields": [
                {"name": "Grade actuel", "value": p["label"], "inline": True},
                {"name": "Ancienneté", "value": anciennete, "inline": True},
                {"name": "Points de recrutement", "value": f"{pts} (≥ {p['seuil']})", "inline": True},
                preuve,
            ],
            "thumbnail": {"url": f"https://{HOST}/habbo-imaging/avatarimage?user={urllib.parse.quote(e['pseudo'])}"
                                 "&direction=2&head_direction=3&headonly=1&size=l"},
            "footer": {"text": f"{origine} {depuis.astimezone(PARIS):%d/%m/%Y}"},
            "timestamp": iso(NOW),
            "_pn": {"uid": uid, "palier": g, "pseudo": e["pseudo"]},
        }
        if g == "cadet":
            embed["fields"].append({"name": "Procédure · Hauts gradés et plus", "inline": False, "value": (
                "Le passage est possible dès le 3ᵉ jour du badge avec 200 PR. Le jour d’adhésion compte comme jour 1.\n"
                f"**1.** Remets le badge **Policier adjoint** à **{e['pseudo']}** sur Habbo.\n"
                f"**2.** Clique sur **Badge attribué** : l'alerte passe en vert avec ton pseudo et tu gagnes **1 XP** en jeu "
                f"(retiré si tu cliques ensuite sur **Annuler**).")})
        if post(embed):
            e.setdefault("alertes", {})[g] = iso(NOW)
            enregistrer(ETAT, etat)
            envoyees += 1

    enregistrer(ETAT, etat)
    print(f"[Discord] {envoyees} alerte(s) envoyée(s)")


if __name__ == "__main__":
    main()
