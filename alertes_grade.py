#!/usr/bin/env python3
"""PN — alertes de promotion des cadets et de formation des policiers adjoints.

Paliers :
  Cadet → Policier adjoint dès le 3ᵉ jour du badge (adhésion = jour 1, heure de Paris) avec 200 points de recrutement.
  Policier adjoint → formation Gardien de la paix dès le 3ᵉ jour du badge avec 300 PR cumulés.
  Gardien de la paix → formation qualifiante dès le 3ᵉ jour du badge.

Le registre badges_cadets.json suit l'adhésion au groupe Cadet par identifiant Habbo stable.
Le bot garde le suivi des formations hors du dépôt et les annonce dans le salon privé des formateurs.
Depuis le 28/10/2026, les alertes sont postées par le bot PN · Alertes (serveur Hetzner, serveur/pn_bot.py) :
l'alerte Cadet → Policier adjoint porte la procédure et un bouton vert « Badge attribué » (Hauts gradés et plus).
La clé « _pn » de l'embed (membre, palier) est lue puis retirée par le bot avant l'envoi.

Variables d'environnement : WIRED_ROOM_ID, WIRED_READ_KEY (lecture), DISCORD_WEBHOOK (secret),
HABBO_HOST (défaut www.habbo.fr), VAR_PR (défaut pts_recrutement_total), TEST=1 pour poster un message de test.
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
from serveur.groupes_pn import EXCLUS_FORMATIONS, GROUPE_CADET, GROUPE_GDP, GROUPE_PA, GROUPES_GRADES, GROUPES_SUPERIEURS

HOST = os.environ.get("HABBO_HOST", "www.habbo.fr")
ROOM = os.environ.get("WIRED_ROOM_ID", "").strip()
KEY = os.environ.get("WIRED_READ_KEY", "").strip()
HOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
VAR_PR = (os.environ.get("VAR_PR") or "pts_recrutement_total").strip()
VAR_ALL = "matricule"          # tous les visiteurs du QG en ont un : sert à repérer les Cadets dès leur arrivée
ETAT = "etat_grades.json"
BADGES_CADETS = "badges_cadets.json"
PARIS = ZoneInfo("Europe/Paris")
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
    chemin = f"/variables/user/{urllib.parse.quote(var)}/users"
    while True:
        data = wired(chemin, {"order_by": "value", "order_dir": "desc", "page": page, "size": 100})
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ApiRequestError("response", endpoint=chemin)
        items = data["items"]
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
    if meta.get("palier") == "pa":
        # Le webhook historique publie dans Alertes-promotions. Seul le bot
        # connecté peut contrôler les permissions du salon privé des formateurs.
        return False
    if not HOOK:
        print("[Discord] pas de webhook configuré : message non envoyé →", embed.get("title"))
        return False
    role_id = "1552758582141784225" if meta.get("palier") == "cadet" else None
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


def suivre_badge(groupe, groupes_superieurs, listes, precedents, aujourdhui, exclus=()):
    membres = {hid: membre for hid, membre in listes[groupe].items() if hid not in exclus}
    superieurs = set().union(*(listes[g] for g in groupes_superieurs))
    registre = {}
    for hid, membre in membres.items():
        fiche = dict(precedents.get(hid, {"depuis": aujourdhui.isoformat()}))
        fiche["pseudo"] = membre["name"]
        registre[hid] = fiche

    for hid in precedents.keys() - membres.keys() - set(exclus):
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
        if groupe in ids:
            registre[hid] = precedents[hid]
            membres[hid] = {"name": precedents[hid]["pseudo"]}
            if ids.intersection(groupes_superieurs):
                superieurs.add(hid)

    return registre, {hid: r for hid, r in membres.items() if hid not in superieurs}


def cadets_du_jour(aujourdhui, listes):
    precedents = lire_registre(BADGES_CADETS)
    registre, membres = suivre_badge(GROUPE_CADET, GROUPES_SUPERIEURS, listes, precedents, aujourdhui)
    if registre != precedents:
        enregistrer(BADGES_CADETS, registre)
    return {r["name"].casefold(): (registre[hid]["depuis"], r.get("motto"))
            for hid, r in membres.items()}


def formations_a_faire(chemin, listes, visiteurs, pr, etat, aujourdhui):
    try:
        suivi = lire_registre(chemin)
    except FileNotFoundError:
        suivi = {"membres": {}, "verifie_le": None}
    precedents = suivi["membres"]
    derniere_lecture = datetime.fromisoformat(suivi["verifie_le"]) if suivi["verifie_le"] else None
    registre, actifs = suivre_badge(GROUPE_PA, GROUPES_SUPERIEURS[1:], listes, precedents, aujourdhui, EXCLUS_FORMATIONS)
    gardiens, gardiens_actifs = suivre_badge(GROUPE_GDP, GROUPES_GRADES, listes, suivi.get("gardiens", {}),
                                             aujourdhui, EXCLUS_FORMATIONS)
    ids = {}
    for uid, (nom, _) in visiteurs.items():
        nom = pr.get(uid, ("", 0))[0] or nom or etat.get(uid, {}).get("pseudo")
        if nom:
            ids[nom.casefold()] = uid

    suivi = {"membres": registre, "gardiens": gardiens, "verifie_le": iso(NOW)}
    for hid, membre in actifs.items():
        fiche = registre[hid]
        uid = fiche.get("uid") or ids.get(membre["name"].casefold())
        if uid is None:
            continue
        fiche["uid"] = uid
        if hid not in precedents:
            # Le clic du HG confirme la remise du badge au candidat (uid WIRED).
            # Une ancienne confirmation ne date pas une nouvelle adhésion.
            attribution = etat.get(uid, {}).get("badge", {}).get("cadet", {}).get("le")
            if attribution:
                date = datetime.fromisoformat(attribution)
                if date.tzinfo and date <= NOW and (derniere_lecture is None or date > derniere_lecture):
                    fiche["depuis"] = date.astimezone(PARIS).date().isoformat()

    enregistrer(chemin, suivi)
    palier = PALIERS["pa"]
    candidats = []
    for hid, membre in actifs.items():
        fiche = registre[hid]
        uid = fiche.get("uid")
        if uid not in pr:
            continue
        pts = pr[uid][1]
        depuis = datetime.fromisoformat(fiche["depuis"]).date()
        jours = (aujourdhui - depuis).days + 1
        if pts < palier["seuil"] or jours < palier["jours"]:
            continue
        candidats.append({"type": "gdp", "uid": uid, "hid": hid, "pseudo": membre["name"], "depuis": fiche["depuis"], "pr": pts,
                          "en_ligne": membre.get("online"), "observation": NOW.timestamp()})
    for hid, membre in gardiens_actifs.items():
        depuis = datetime.fromisoformat(gardiens[hid]["depuis"]).date()
        if (aujourdhui - depuis).days + 1 >= 3:
            candidats.append({"type": "qualifiante", "uid": hid, "hid": hid, "pseudo": membre["name"],
                              "depuis": depuis.isoformat(), "en_ligne": membre.get("online"),
                              "observation": NOW.timestamp()})
    return candidats


def main(*, suivi_formations=None):
    if os.environ.get("TEST") == "1":
        post({"title": "✅ Système d'alertes connecté",
              "description": "Ce salon signale les **Cadets** qui peuvent passer **Policier adjoint** dès le "
                             "3ᵉ jour du badge avec 200 points de recrutement. Le jour d’adhésion compte comme "
                             "jour 1, en heure de Paris.",
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
    listes = {g: membres_groupe(g) for g in (GROUPE_CADET, *GROUPES_SUPERIEURS)}
    cadets = cadets_du_jour(aujourdhui, listes)
    candidats = None
    if suivi_formations is not None:
        candidats = formations_a_faire(suivi_formations, listes, membres, pr, etat, aujourdhui)
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
        if g != "cadet" or not e.get("depuis"):
            continue
        p = PALIERS[g]
        pts = pr.get(uid, ("", 0))[1]
        depuis = datetime.fromisoformat(e["depuis"])
        if e["pseudo"].casefold() not in cadets or e.get("badge", {}).get("cadet"):
            continue
        jours = (aujourdhui - depuis.astimezone(PARIS).date()).days + 1
        anciennete = f"Jour {jours} (adhésion incluse)"
        preuve = {"name": "Badge Habbo", "value": "[PN] Cadet", "inline": False}
        origine = "Badge Cadet depuis le"
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
    return candidats


if __name__ == "__main__":
    main()
