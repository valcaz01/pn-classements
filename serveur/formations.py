"""Listes privées des formations Gardien de la paix et qualifiante, et historique des confirmations."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import secrets
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from functools import cache
from typing import NamedTuple
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks
from adhesions import groupes_publics_du_membre
from habbo_http import ApiRequestError, request_json
from groupes_pn import EXCLUS_FORMATIONS, GROUPE_GDP, GROUPE_PA
from presence_formations_qg import ControlePresenceQG, identite


SALON = 1574352702413086810
FORMATEUR = 1552122047034032221
ROLES_RETRAIT = frozenset({
    1552758582141784225,  # Haut gradé
    1550033596289646632,  # Supervision
    1550033386926645268,  # Direction
})
TITRE = "🎓 Formations Gardien de la paix à faire"
TYPES = {"gdp": "Formation Gardien de la paix", "qualifiante": "Formation qualifiante"}
BADGES_FORMATION = {"gdp": (GROUPE_PA, "Policier adjoint"), "qualifiante": (GROUPE_GDP, "Gardien de la paix")}
PARIS = ZoneInfo("Europe/Paris")
INTERVALLE_PRESENCE = 30  # secondes
VALIDITE_PRESENCE = 3 * INTERVALLE_PRESENCE
DELAI_MENTION_QG = 60 * 60  # secondes, pour un même identifiant Habbo
ORDRE_PRESENCE = {True: 0, False: 1, None: 2}
HOST = os.environ.get("HABBO_HOST", "www.habbo.fr")
LOG = logging.getLogger("pn.formations")
SANS_MENTIONS = discord.AllowedMentions.none()


def verifier_salon(ch, guild_id):
    guild = ch.guild
    role, moi = guild.get_role(FORMATEUR), guild.me
    if ch.id != SALON or guild.id != guild_id or not role or not moi:
        raise RuntimeError("Le salon privé des formateurs est introuvable.")
    lecture = ch.permissions_for(role)
    if (ch.overwrites_for(guild.default_role).view_channel is not False
            or not (lecture.view_channel and lecture.read_message_history) or lecture.send_messages):
        raise RuntimeError("Le salon privé doit être en lecture seule pour les formateurs.")
    for cible, droits in ch.overwrites.items():
        if droits.view_channel is not True or cible.id in (role.id, moi.id, guild.owner_id):
            continue
        if isinstance(cible, discord.Role):
            if cible.permissions.administrator or (cible.tags and cible.tags.bot_id == moi.id):
                continue
        elif isinstance(cible, discord.Member) and cible.guild_permissions.administrator:
            continue
        raise RuntimeError("Un accès supplémentaire au salon des formations doit être vérifié.")
    acces = ch.permissions_for(moi)
    if not (acces.view_channel and acces.send_messages and acces.embed_links and acces.read_message_history):
        raise RuntimeError("PN · Alertes doit pouvoir lire et publier la liste dans son salon.")


def cle(fiche):
    prefixe = "" if fiche.get("type", "gdp") == "gdp" else "qualifiante/"
    return f"{prefixe}{fiche['uid']}/{fiche['depuis']}"


def ancienne_alerte(embed):
    """Reconnaît exclusivement le format individuel envoyé avant le tableau."""
    ref = re.fullmatch(r"Suivi de formation · (\d+)/(\d{8})", embed.footer.text or "")
    titre = re.fullmatch(r"🎓 (.+) peut passer la formation Gardien de la paix", embed.title or "")
    if not (ref and titre):
        return None
    for champ in embed.fields:
        points = re.fullmatch(r"(\d+) PR", champ.value)
        if champ.name == "Points de recrutement cumulés" and points:
            return {"uid": ref[1], "depuis": date.fromisoformat(ref[2]).isoformat(),
                    "pseudo": titre[1], "pr": int(points[1])}
    return None


def pseudo(nom):
    return discord.utils.escape_mentions(discord.utils.escape_markdown(nom))


def lien_sujet_forum(valeur):
    """Lien Forumactif permanent, y compris une page précise ou un message du cockpit."""
    if not valeur:
        return None
    texte = valeur.strip()
    if "://" not in texte:
        texte = "https://" + texte
    try:
        url = urllib.parse.urlsplit(texte)
    except ValueError:
        return None
    if url.scheme not in ("http", "https") or url.netloc.lower() not in (
            "www.pn-rpg.fr", "pn-rpg.fr", "pn-rpg.forumactif.com"):
        return None
    route = re.fullmatch(r"/t([1-9][0-9]{0,15})(?:p([0-9]{1,16}))?(?:-[^/]*)?/?", url.path)
    parametres = dict(urllib.parse.parse_qsl(url.query))
    if route:
        sujet, debut = route.groups()
    elif url.path.rstrip("/") in ("/viewtopic", "/viewtopic.forum", "/viewtopic.php"):
        sujet, debut = parametres.get("t"), parametres.get("start")
        post = parametres.get("p", "")
        if re.fullmatch(r"[1-9][0-9]{0,15}", post):
            return f"https://www.pn-rpg.fr/viewtopic?p={post}#{post}"
    else:
        return None
    if not re.fullmatch(r"[1-9][0-9]{0,15}", sujet or ""):
        return None
    debut = debut or parametres.get("start", "")
    page = f"p{debut}" if re.fullmatch(r"[1-9][0-9]{0,15}", debut) else ""
    ancre = f"#{url.fragment}" if re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", url.fragment) else ""
    return f"https://www.pn-rpg.fr/t{sujet}{page}-{ancre}"


def peut_retirer(membre):
    return isinstance(membre, discord.Member) and any(r.id in ROLES_RETRAIT for r in membre.roles)


class Presence(NamedTuple):
    en_ligne: bool | None
    dernier_acces: datetime | None = None
    badge: bool | None = None


def presence_publique(profil, membre):
    if not isinstance(profil, dict) or profil.get("profileVisible") is not True:
        return Presence(membre.get("online"))
    en_ligne = profil.get("online", membre.get("online"))
    if en_ligne is not False:
        return Presence(en_ligne)
    try:
        acces = datetime.fromisoformat(profil["lastAccessTime"])
    except (KeyError, TypeError, ValueError):
        return Presence(False)
    return Presence(False, acces.astimezone(PARIS) if acces.tzinfo else None)


def verifier_badges(fiches, lire, controles):
    groupes = {cle(f): BADGES_FORMATION[f.get("type", "gdp")][0] for f in fiches}
    par_id, par_nom = {}, {}
    for groupe in set(groupes.values()):
        lot = lire(f"groups/{groupe}/members")
        if not isinstance(lot, list) or not lot or not all(
            isinstance(m, dict) and m.get("uniqueId") and m.get("name") for m in lot
        ):
            raise ApiRequestError("response", endpoint="membres du groupe de formation")
        par_id[groupe] = {m["uniqueId"]: m for m in lot}
        par_nom[groupe] = {m["name"].casefold(): m for m in lot}
    membres, badges, absents = {}, {}, []
    for f in fiches:
        k = cle(f)
        groupe = groupes[k]
        membre = par_id[groupe].get(f["hid"]) if f.get("hid") else par_nom[groupe].get(f["pseudo"].casefold())
        membres[k] = membre or {}
        badges[k] = True if membre else None
        if membre:
            controles.pop(k, None)
        elif f.get("hid"):
            absents.append(f)
    # Les groupes sont relus pour tous. Si plusieurs profils manquent à la fois,
    # vérifier en priorité ceux qui attendent depuis le plus longtemps.
    absents.sort(key=lambda f: controles.get(cle(f), 0))
    for f in absents[:4]:
        k = cle(f)
        controles[k] = time.monotonic()
        try:
            ids = groupes_publics_du_membre(f["hid"], groupes[k], lire)
        except ApiRequestError as exc:
            if exc.kind == "rate_limit":
                raise
            LOG.warning("Confirmation du badge de formation reportée : %s", exc)
            if exc.kind in {"unavailable", "timeout", "network"}:
                break
            continue
        if ids is not None:
            badges[k] = groupes[k] in ids
    return membres, badges


def presences_habbo(fiches, cache_profils=None, controles_badge=None):
    @cache
    def lire(chemin):
        return request_json(urllib.request.Request(f"https://{HOST}/api/public/{chemin}",
                            headers={"Accept": "application/json", "User-Agent": "PN-Formations/1.0"}))
    membres, badges = verifier_badges(fiches, lire, controles_badge if controles_badge is not None else {})
    if cache_profils is None:
        cache_profils = {}
    instant = time.monotonic()
    profils, resultat = {}, {}
    # Lire d'abord les profils les moins récemment consultés. Le statut des deux
    # groupes est toujours frais, même si les dates individuelles doivent attendre.
    fiches = sorted(fiches, key=lambda f: cache_profils.get(identite(f), (0,))[0])
    budget = 4
    profils_disponibles = True
    for f in fiches:
        hid = identite(f)
        membre = membres[cle(f)]
        if membre.get("online") is True:
            resultat[cle(f)] = Presence(True)
            cache_profils.pop(hid, None)
        else:
            lu, avant = cache_profils.get(hid, (0, None))
            if membre.get("online") is False and avant and avant.en_ligne is False and instant - lu < 300:
                resultat[cle(f)] = avant
                continue
            chemin = ("users/" + urllib.parse.quote(f["hid"], safe="") if f.get("hid") else
                      "users?" + urllib.parse.urlencode({"name": f["pseudo"]}))
            if profils_disponibles and budget and chemin not in profils:
                budget -= 1
                try:
                    profils[chemin] = lire(chemin)
                except ApiRequestError as exc:
                    if exc.kind == "rate_limit":
                        raise
                    if exc.kind in {"unavailable", "timeout", "network"}:
                        profils_disponibles = False
                        LOG.warning("Lecture des autres profils reportée : %s", exc)
            if chemin in profils:
                observation = presence_publique(profils[chemin], membre)
                cache_profils[hid] = instant, observation
            else:
                observation = avant if avant and membre.get("online") is False and avant.en_ligne is False else Presence(membre.get("online"))
            resultat[cle(f)] = observation
    return {k: presence._replace(badge=badges[k]) for k, presence in resultat.items()}


def carte_liste(fiches, presences, genre, au_qg=None):
    entrees = []
    connectes = 0
    inconnus = 0
    grade = {"gdp": "P.Adj", "qualifiante": "GPX"}[genre]
    for fiche in fiches:
        depuis = date.fromisoformat(fiche["depuis"])
        badge = f"({grade} depuis {depuis:%d/%m/%y})"
        presence = presences.get(cle(fiche), Presence(None))
        dans_qg = au_qg is not None and identite(fiche) in au_qg
        if dans_qg or presence.en_ligne is True:
            lieu = " · au QG" if dans_qg else ""
            detail = f" 🟢 en ligne{lieu} {badge}"
            connectes += 1
        elif presence.dernier_acces is not None:
            detail = f" {badge}\nDernière connexion le {presence.dernier_acces:%d/%m/%y à %H:%M}"
        elif presence.en_ligne is False:
            detail = f" {badge}\nDernière connexion non communiquée"
        else:
            detail = f" {badge}\nPrésence indisponible"
            inconnus += 1
        entrees.append((fiche["pseudo"], detail))
    lignes = [f"**{pseudo(nom)}**{detail}" for nom, detail in entrees]
    contenu = "\n\n".join(lignes) or ("Aucun gardien de la paix en attente de formation qualifiante."
                                     if genre == "qualifiante" else "Aucun policier adjoint en attente de formation.")
    fichier = None
    if len(contenu.encode("utf-16-le")) > 6800:
        fichier = "\n\n".join(nom + detail for nom, detail in entrees).encode("utf-8")
        notice = "La liste complète est en pièce jointe."
        apercu = "\n\n".join([*lignes[:5], notice])
        contenu = apercu if len(apercu.encode("utf-16-le")) <= 6800 else notice
    carte = discord.Embed(title=TYPES[genre], description=contenu)
    resume = f"{len(fiches)} en attente"
    if connectes or (fiches and not inconnus):
        resume += f" · {connectes} en ligne"
    if au_qg is not None and fiches:
        resume += f" · {sum(identite(f) in au_qg for f in fiches)} au QG"
    carte.set_footer(text=resume)
    return carte, fichier


def est_tableau(msg):
    def contient_menu(elements):
        return any(getattr(e, "custom_id", None) == "pn:formas:type"
                   or contient_menu(getattr(e, "children", ())) for e in elements)
    return (bool(msg.embeds and msg.embeds[0].title == TITRE)
            or contient_menu(getattr(msg, "components", ())))


class TableauFormations:
    def __init__(self, client, guild_id, chemin, *, api_qg=None):
        self.client, self.guild_id, self.chemin = client, guild_id, chemin
        self.etat = (json.loads(chemin.read_text(encoding="utf-8")) if chemin.exists() else
                     {"attente": {}, "historique": {}, "annonces": [], "panneau": 0, "migration": False})
        if self.etat.get("visites_qg") and "dernieres_mentions_qg" not in self.etat:
            # L'ancienne version connaissait les visites annoncées, sans dater les mentions.
            self.sauver(dernieres_mentions_qg=dict.fromkeys(self.etat["visites_qg"], time.time()))
        self.verrou = asyncio.Lock()
        self.reconcilier = True
        self.presences = {}
        self.cache_profils = {}
        self.badges = {}
        self.controles_badge = {}
        self.controle_qg = ControlePresenceQG(api_qg) if api_qg else None
        self.lecture_qg = None
        self.statut_qg = None
        self.prochain_qg = self.prochain_statut = 0
        self.consultations = {}
        self.vue = BoutonsFormations(self)

    def demarrer(self):
        self.client.add_view(self.vue)
        self.suivre_presences.start()
        if self.controle_qg:
            self.suivre_qg.start()

    def noter_presence(self, k, instant, presence, *, du_groupe=False):
        precedent, etat = self.presences.get(k, (0, Presence(None)))
        if instant < precedent:
            return
        # Le relevé des badges ne fournit aucune date de connexion. Un second
        # relevé hors ligne ne doit pas effacer la date lue sur le profil.
        if du_groupe and presence.en_ligne is False and etat.en_ligne is False:
            return
        self.presences[k] = (instant, presence)

    @tasks.loop(seconds=INTERVALLE_PRESENCE)
    async def suivre_presences(self):
        try:
            await self.actualiser_presences()
        except (discord.HTTPException, RuntimeError, OSError):
            LOG.exception("Actualisation de la présence des formations reportée")

    @suivre_presences.before_loop
    async def avant_presences(self):
        await self.client.wait_until_ready()

    async def actualiser_presences(self):
        fiches = self.liste()
        instant = time.monotonic()
        if not fiches or instant < self.prochain_statut:
            return
        self.prochain_statut = instant + INTERVALLE_PRESENCE - 2
        debut = time.time()
        suivis = {identite(f) for f in fiches}
        self.cache_profils = {k: v for k, v in self.cache_profils.items() if k in suivis}
        self.controles_badge = {k: v for k, v in self.controles_badge.items() if k in self.etat["attente"]}
        try:
            resultats = await asyncio.to_thread(presences_habbo, fiches, self.cache_profils, self.controles_badge)
        except ApiRequestError as exc:
            LOG.warning("Présences Habbo indisponibles : %s", exc)
            self.prochain_statut = time.monotonic() + max(30, exc.retry_delay or 0)
            resultats = {cle(f): Presence(None) for f in fiches}
        async with self.verrou:
            ch = await self.client.channel(SALON)
            verifier_salon(ch, self.guild_id)
            self.appliquer_badges(resultats, debut)
            for k, presence in resultats.items():
                if k in self.etat["attente"]:
                    self.noter_presence(k, debut, presence)
            await self.actualiser_rendu(ch)

    def appliquer_badges(self, resultats, debut):
        pertes = {}
        for k, lecture in resultats.items():
            fiche = self.etat["attente"].get(k)
            if fiche is None or debut < self.badges.get(k, (0, None))[0]:
                continue
            self.badges[k] = (debut, lecture.badge)
            if lecture.badge is False and fiche.get("hid"):
                pertes[k] = debut
        self.retirer_badges_perdus(pertes)

    def retirer_badges_perdus(self, pertes):
        if not pertes:
            return
        attente = dict(self.etat["attente"])
        departs = {genre: dict(dates) for genre, dates in self.etat.get("departs_badge", {}).items()}
        for k, instant in pertes.items():
            fiche = attente.pop(k)
            dates = departs.setdefault(fiche.get("type", "gdp"), {})
            dates[fiche["hid"]] = max(instant, dates.get(fiche["hid"], 0))
        # La fiche et la fin de son adhésion sont persistées ensemble.
        self.sauver(attente=attente, departs_badge=departs)
        for k in pertes:
            self.badges.pop(k, None)
            self.presences.pop(k, None)
        LOG.info("Départ du badge confirmé pour %d fiche(s) de formation", len(pertes))

    def au_qg(self):
        if self.lecture_qg is not None:
            instant, presents = self.lecture_qg
            if time.monotonic() - instant < VALIDITE_PRESENCE:
                return presents
        return None

    @tasks.loop(seconds=INTERVALLE_PRESENCE)
    async def suivre_qg(self):
        try:
            await self.actualiser_qg()
        except (discord.HTTPException, RuntimeError, OSError):
            LOG.exception("Actualisation de la présence au QG reportée")

    @suivre_qg.before_loop
    async def avant_qg(self):
        await self.client.wait_until_ready()
        if self.etat.get("consigne_delai_qg"):
            return
        try:
            ch = await self.client.channel(SALON)
            verifier_salon(ch, self.guild_id)
            ancienne = "Quand un candidat est détecté au QG, les formateurs sont mentionnés une fois par visite."
            nouvelle = "Le bot mentionne les formateurs à l’arrivée, au plus une fois par heure et par candidat."
            if ancienne in (ch.topic or ""):
                if not ch.permissions_for(ch.guild.me).manage_channels:
                    return
                await ch.edit(topic=ch.topic.replace(ancienne, nouvelle, 1), reason="Délai des mentions de formation au QG")
            self.sauver(consigne_delai_qg=True)
        except (discord.HTTPException, RuntimeError, OSError):
            LOG.warning("Description du salon des formations inchangée", exc_info=True)

    async def actualiser_qg(self):
        fiches = self.liste()
        instant = time.monotonic()
        # Une première sonde vérifie la liaison même si personne n'attend de formation.
        if instant < self.prochain_qg or (not fiches and self.lecture_qg is not None):
            return
        self.prochain_qg = instant + INTERVALLE_PRESENCE - 2
        try:
            numero, nombre, presents = await asyncio.to_thread(self.controle_qg.relever, fiches)
        except ApiRequestError as exc:
            self.prochain_qg = time.monotonic() + max(60, exc.retry_delay or 0)
            LOG.warning("Sonde de formation QG reportée : %s", exc)
            raison = getattr(exc, "indication", None) or {
                "timeout": "délai dépassé", "network": "connexion interrompue",
                "rate_limit": "limite d’appels atteinte", "access": "accès refusé",
            }.get(exc.kind, "réponse Habbo indisponible")
            code = f", HTTP {exc.status}" if exc.status else ""
            self.statut_qg = f"QG indisponible ({raison}{code})"
            return
        if self.lecture_qg is None:
            if not nombre:
                LOG.info("Contrôle QG %s : en attente du premier écho d'un avatar", numero)
                self.statut_qg = "QG en attente d’un écho WIRED"
                if not fiches:
                    self.prochain_qg = time.monotonic() + 120
                return
            LOG.info("Liaison WIRED formations confirmée, contrôle %s repris par %s avatars", numero, nombre)
        async with self.verrou:
            self.statut_qg = None
            self.lecture_qg = time.monotonic(), presents
            suivis = {identite(f) for f in self.etat["attente"].values()}
            visites = {}
            for hid, absences in self.etat.get("visites_qg", {}).items():
                if hid in suivis and (hid in presents or absences == 0):
                    visites[hid] = 0 if hid in presents else 1
            limite = time.time() - DELAI_MENTION_QG
            mentions = {hid: dernier_envoi for hid, dernier_envoi in self.etat.get("dernieres_mentions_qg", {}).items()
                        if dernier_envoi > limite}
            # Un retour pendant l'heure est une visite silencieuse, même si le joueur
            # reste sur place jusqu'après l'expiration du délai.
            visites.update(dict.fromkeys(presents.intersection(mentions, suivis), 0))
            self.sauver(visites_qg=visites, dernieres_mentions_qg=mentions)
            ch = await self.client.channel(SALON)
            verifier_salon(ch, self.guild_id)
            await self.actualiser_rendu(ch)

    def arrivees_qg(self):
        presents = self.au_qg() or set()
        limite = time.time() - VALIDITE_PRESENCE
        confirmes = {k for k, (instant, badge) in self.badges.items() if badge and instant > limite}
        return {identite(f): f["pseudo"] for k, f in self.etat["attente"].items() if k in confirmes
                and identite(f) in presents and identite(f) not in self.etat.get("visites_qg", {})}

    async def actualiser_rendu(self, ch):
        if (self.reconcilier or self.etat.get("envoi") or self.arrivees_qg()
                or self.etat.get("rendu") != BoutonsFormations(self).signature()):
            await self.publier(ch, [])
        else:
            await self.rafraichir_consultations()

    def sauver(self, **changements):
        nouveau = {**self.etat, **changements}
        if nouveau != self.etat:
            tmp = self.chemin.with_suffix(".tmp")
            tmp.write_text(json.dumps(nouveau, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.chemin)
            self.etat = nouveau

    def presences_recentes(self):
        limite = time.time() - VALIDITE_PRESENCE
        return {k: p for k, (instant, p) in self.presences.items() if instant > limite}

    def liste(self, genre=None, *, presences=None):
        if presences is None:
            presences = self.presences_recentes()
        au_qg = self.au_qg() or set()

        def ordre(fiche):
            presence = presences.get(cle(fiche), Presence(None))
            dans_qg = identite(fiche) in au_qg
            return not dans_qg, ORDRE_PRESENCE[presence.en_ligne], fiche["depuis"], fiche["pseudo"].casefold()

        return sorted((f for f in self.etat["attente"].values() if genre is None or f.get("type", "gdp") == genre),
                      key=ordre)

    def incorporer(self, candidats):
        attente = {k: f for k, f in self.etat["attente"].items() if f.get("hid") not in EXCLUS_FORMATIONS}
        par_uid = {(f.get("type", "gdp"), f["uid"]): k for k, f in attente.items()}
        for source in candidats:
            fiche = dict(source)
            k = cle(fiche)
            if k in self.etat["historique"] or k in self.etat.get("retraits", {}) or fiche.get("hid") in EXCLUS_FORMATIONS:
                continue
            genre = fiche.get("type", "gdp")
            depart = self.etat.get("departs_badge", {}).get(genre, {}).get(fiche.get("hid"))
            instant = fiche.get("observation", time.time())
            if depart is not None and (
                instant <= depart or fiche["depuis"] < datetime.fromtimestamp(depart, PARIS).date().isoformat()
            ):
                continue
            identite = (genre, fiche["uid"])
            if "observation" in fiche:
                self.noter_presence(k, fiche.pop("observation"), Presence(fiche.pop("en_ligne")), du_groupe=True)
            ancien = par_uid.get(identite)
            if ancien and ancien != k:
                if attente[ancien]["depuis"] > fiche["depuis"]:
                    continue
                del attente[ancien]
                self.badges.pop(ancien, None)
            if fiche.get("hid") and instant >= self.badges.get(k, (0, None))[0]:
                self.badges[k] = instant, True
            attente[k] = fiche
            par_uid[identite] = k
        self.sauver(attente=attente)

    def terminer(self, k, membre, *, retrait=False, sujet=None):
        fiche = self.etat["attente"].get(k)
        if fiche is None:
            return None
        attente = dict(self.etat["attente"])
        del attente[k]
        archive = {**fiche, "par": membre.display_name, "par_id": str(membre.id),
                   "le": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if not retrait:
            lien = lien_sujet_forum(sujet)
            if not lien:
                raise ValueError("Un lien vers le sujet de formation sur le forum PN est obligatoire.")
            archive["sujet"] = lien
        registre = "retraits" if retrait else "historique"
        self.sauver(attente=attente, **{registre: {**self.etat.get(registre, {}), k: archive}})
        self.badges.pop(k, None)
        self.presences.pop(k, None)
        return fiche

    async def autoriser(self, inter, *, retrait=False):
        if inter.guild_id == self.guild_id and inter.channel_id == SALON and isinstance(inter.user, discord.Member):
            try:
                verifier_salon(inter.channel, self.guild_id)
            except RuntimeError:
                pass
            else:
                if inter.channel.permissions_for(inter.user).view_channel:
                    if peut_retirer(inter.user):
                        return True
                    if not retrait and (inter.user.guild_permissions.administrator or any(r.id == FORMATEUR for r in inter.user.roles)):
                        return True
        texte = ("Le retrait est réservé aux Hauts gradés, à la Supervision et à la Direction." if retrait else
                 "Ce tableau est réservé aux formateurs dans leur salon privé.")
        await inter.response.send_message(texte, ephemeral=True)
        return False

    async def actualiser(self, candidats, *, fins_adhesion=None):
        async with self.verrou:
            ch = await self.client.channel(SALON)
            verifier_salon(ch, self.guild_id)
            if fins_adhesion:
                pertes = {}
                for k, f in self.etat["attente"].items():
                    ancienne = (f.get("type", "gdp"), f.get("hid"), f["depuis"])
                    if ancienne in fins_adhesion:
                        pertes[k] = fins_adhesion[ancienne]
                self.retirer_badges_perdus(pertes)
            await self.publier(ch, candidats)

    def confirmer_publication(self, message, visites_annoncees, **changements):
        if visites_annoncees:
            visites = dict(self.etat.get("visites_qg", {}))
            mentions = dict(self.etat.get("dernieres_mentions_qg", {}))
            instant = message.created_at.timestamp()
            for hid in visites_annoncees:
                visites[hid] = 0
                mentions[hid] = instant
            changements.update(visites_qg=visites, dernieres_mentions_qg=mentions)
        self.sauver(panneau=message.id, envoi=None, **changements)

    async def publier(self, ch, candidats):
        panneaux, anciennes, reprises = [], [], []
        # Une copie supplémentaire peut subsister si Discord a accepté l'envoi juste avant un arrêt.
        if self.reconcilier or self.etat.get("envoi") or not self.etat["migration"]:
            async for msg in ch.history(limit=100 if self.etat["migration"] else None):
                if msg.author.id != self.client.user.id:
                    continue
                if est_tableau(msg):
                    panneaux.append(msg)
                elif msg.embeds and not self.etat["migration"]:
                    fiche = ancienne_alerte(msg.embeds[0])
                    if fiche:
                        anciennes.append(msg)
                        reprises.append(fiche)
        ident = self.etat["panneau"]
        if ident and all(msg.id != ident for msg in panneaux):
            try:
                connu = await ch.fetch_message(ident)
                if connu.author.id == self.client.user.id and est_tableau(connu):
                    panneaux.append(connu)
            except discord.NotFound:
                pass
        panneaux.sort(key=lambda m: m.id, reverse=True)
        courant = panneaux[0] if panneaux else None
        annonces = set(self.etat["annonces"])
        annonces.update(cle(f) for f in reprises)
        envoi = self.etat.get("envoi")
        if envoi and courant and courant.id > envoi["apres"]:
            annonces.update(envoi["cles"])
            self.confirmer_publication(courant, envoi.get("visites", ()), annonces=sorted(annonces))
            envoi = None

        self.incorporer([*reprises, *candidats])
        cles = set(self.etat["attente"])
        nouveaux = cles - annonces
        arrivees = self.arrivees_qg()
        vue = BoutonsFormations(self)
        rendu = vue.signature()

        # Une nouvelle liste mentionne le salon une fois pour le lot. Une simple
        # correction, une confirmation ou un redémarrage ne doit réveiller personne.
        if courant is None or nouveaux or arrivees:
            if nouveaux and not ch.permissions_for(ch.guild.me).mention_everyone:
                raise RuntimeError("PN · Alertes doit pouvoir mentionner @everyone dans le salon des formations.")
            role = ch.guild.get_role(FORMATEUR)
            if arrivees and not (role.mentionable or ch.permissions_for(ch.guild.me).mention_everyone):
                raise RuntimeError("Le rôle Formateur doit pouvoir être mentionné dans le salon des formations.")
            if envoi is None or set(envoi.get("visites", ())) != arrivees.keys() or set(envoi["cles"]) != cles:
                envoi = {"apres": courant.id if courant else 0, "cles": sorted(cles),
                         "visites": sorted(arrivees), "nonce": secrets.token_hex(10)}
                self.sauver(envoi=envoi)
            self.reconcilier = True
            genres = {self.etat["attente"][k].get("type", "gdp") for k in nouveaux}
            vue = BoutonsFormations(self, annoncer=genres, arrivees=arrivees.values())
            mentions = discord.AllowedMentions(everyone=bool(nouveaux), users=False,
                                              roles=[discord.Object(id=FORMATEUR)] if arrivees else False,
                                              replied_user=False)
            courant = await ch.send(view=vue, files=vue.pieces(), nonce=envoi["nonce"],
                                    allowed_mentions=mentions)
        elif self.etat.get("rendu") != rendu or courant.embeds:
            await courant.edit(content=None, embed=None, view=vue, attachments=vue.pieces(), allowed_mentions=SANS_MENTIONS)
        self.confirmer_publication(courant, arrivees, annonces=sorted(cles), rendu=rendu)
        self.reconcilier = True
        for msg in [*panneaux, *anciennes]:
            if msg.id != courant.id:
                try:
                    await msg.delete()
                except discord.NotFound:
                    pass
        self.sauver(migration=True)
        self.reconcilier = False
        await self.rafraichir_consultations()

    async def rafraichir_consultations(self):
        for auteur, consultation in list(self.consultations.items()):
            inter, genre, signature = consultation
            if inter.is_expired():
                self.consultations.pop(auteur, None)
                continue
            membre = (inter.guild.get_member(auteur) if inter.guild else None) or inter.user
            vue = BoutonsFormations(self, genre, auteur=auteur, gestion=peut_retirer(membre))
            nouvelle = vue.signature()
            if signature == nouvelle:
                continue
            try:
                await inter.edit_original_response(view=vue, attachments=vue.pieces(), allowed_mentions=SANS_MENTIONS)
            except (discord.NotFound, discord.Forbidden):
                if self.consultations.get(auteur) is consultation:
                    self.consultations.pop(auteur, None)
            except discord.HTTPException:
                LOG.warning("Actualisation de la vue privée reportée", exc_info=True)
            else:
                if self.consultations.get(auteur) is consultation:
                    self.consultations[auteur] = inter, genre, nouvelle


class BoutonsFormations(discord.ui.LayoutView):
    def __init__(self, tableau, genre="gdp", *, auteur=None, annoncer=(), arrivees=(), gestion=False):
        super().__init__(timeout=None if auteur is None else 900)
        self.tableau, self.genre, self.auteur = tableau, genre, auteur
        # Le message du salon est commun à tous. Le bot contrôle le rôle au clic.
        self.afficher_retrait = auteur is None or gestion
        presences = tableau.presences_recentes()
        au_qg = tableau.au_qg()
        self.carte, self.fichier = carte_liste(tableau.liste(genre, presences=presences), presences, genre, au_qg)
        if au_qg is None and tableau.statut_qg:
            self.carte.set_footer(text=f"{self.carte.footer.text} · {tableau.statut_qg}")
        self.compteurs = {g: len(tableau.liste(g, presences=presences)) for g in TYPES}
        self.descriptions_menu = {g: f"{n} en attente · Badge {BADGES_FORMATION[g][1]} requis" for g, n in self.compteurs.items()}
        menu = discord.ui.Select(custom_id="pn:formas:type", placeholder="Choisir une formation", options=[
            discord.SelectOption(label=nom, value=g, default=auteur is not None and g == genre, description=self.descriptions_menu[g])
            for g, nom in TYPES.items()])
        async def choisir(inter):
            selection = menu.values[0]
            vue = BoutonsFormations(tableau, selection, auteur=inter.user.id, gestion=peut_retirer(inter.user))
            if auteur is None:
                await inter.response.send_message(view=vue, files=vue.pieces(), ephemeral=True, allowed_mentions=SANS_MENTIONS)
            else:
                await inter.response.edit_message(view=vue, attachments=vue.pieces(), allowed_mentions=SANS_MENTIONS)
            tableau.consultations[inter.user.id] = inter, selection, vue.signature()
        menu.callback = choisir
        faite = discord.ui.Button(label="Formation faite", emoji="✅", style=discord.ButtonStyle.success,
                                  custom_id="pn:formas:faite")
        historique = discord.ui.Button(label="Historique des formations", emoji="📚", custom_id="pn:formas:historique")
        faite.callback, historique.callback = self.faite, self.historique
        boite = discord.ui.Container(accent_colour=0xE3B34A)
        if annoncer:
            types = " et ".join(TYPES[g] for g in TYPES if g in annoncer)
            boite.add_item(discord.ui.TextDisplay(f"@everyone · Ajout à la liste « {types} »"))
        if arrivees:
            noms = ", ".join(pseudo(nom) for nom in sorted(arrivees, key=str.casefold))
            noms = noms if len(noms) <= 500 else f"{len(arrivees)} personnes en attente de formation"
            boite.add_item(discord.ui.TextDisplay(f"<@&{FORMATEUR}> · Au QG pour une formation : **{noms}**"))
        boite.add_item(discord.ui.ActionRow(menu))
        # Discord peut garder le dernier choix affiché dans le menu public de ce lecteur.
        # Ce titre identifie toujours la liste concernée par les boutons juste en dessous.
        boite.add_item(discord.ui.TextDisplay(f"### 🎓 {self.carte.title}"))
        if self.compteurs[genre]:
            boite.add_item(discord.ui.TextDisplay(self.carte.footer.text))
            boite.add_item(discord.ui.Separator())
        boite.add_item(discord.ui.TextDisplay(self.carte.description))
        if self.fichier:
            boite.add_item(discord.ui.File("attachment://formations-a-faire.txt"))
        boite.add_item(discord.ui.ActionRow(faite, historique))
        if self.afficher_retrait:
            retrait = discord.ui.Button(label="Retirer de la liste", style=discord.ButtonStyle.danger,
                                       custom_id="pn:formas:retirer")
            retrait.callback = self.retirer
            boite.add_item(discord.ui.ActionRow(retrait))
        self.add_item(boite)

    def pieces(self):
        return [discord.File(io.BytesIO(self.fichier), filename="formations-a-faire.txt")] if self.fichier else []

    def signature(self):
        contenu = json.dumps([self.genre, self.afficher_retrait, self.carte.title, self.carte.description, self.carte.footer.text,
                              self.descriptions_menu], ensure_ascii=False).encode()
        return hashlib.sha256(contenu + (self.fichier or b"")).hexdigest()

    async def interaction_check(self, inter):
        if self.auteur is not None and inter.user.id != self.auteur:
            await inter.response.send_message("Ouvre ta propre liste depuis le tableau.", ephemeral=True)
            return False
        return await self.tableau.autoriser(inter)

    async def faite(self, inter):
        if not self.tableau.liste(self.genre):
            await inter.response.send_message("La liste des formations à faire est vide.", ephemeral=True)
            return
        vue = PagesFormations(self.tableau, inter.user.id, historique=False, genre=self.genre)
        await inter.response.send_message(embed=vue.carte, view=vue, ephemeral=True)

    async def historique(self, inter):
        vue = PagesFormations(self.tableau, inter.user.id, historique=True)
        await inter.response.send_message(embed=vue.carte, view=vue if vue.children else None, ephemeral=True)

    async def retirer(self, inter):
        if await self.tableau.autoriser(inter, retrait=True):
            vue = PagesFormations(self.tableau, inter.user.id, historique=False, genre=self.genre, retrait=True)
            await inter.response.send_message(embed=vue.carte, view=vue if vue.children else None, ephemeral=True)


class VuePrivee(discord.ui.View):
    def __init__(self, tableau, auteur, *, retrait=False):
        super().__init__(timeout=300)
        self.tableau, self.auteur, self.retrait = tableau, auteur, retrait

    async def interaction_check(self, inter):
        if inter.user.id != self.auteur:
            await inter.response.send_message("Ouvre ton propre menu depuis le tableau.", ephemeral=True)
            return False
        return await self.tableau.autoriser(inter, retrait=self.retrait)


class PagesFormations(VuePrivee):
    def __init__(self, tableau, auteur, historique, page=0, genre="gdp", *, retrait=False):
        super().__init__(tableau, auteur, retrait=retrait)
        self.historique, self.genre = historique, genre
        if historique:
            fiches = sorted(tableau.etat["historique"].values(), key=lambda f: f["le"], reverse=True)
        else:
            fiches = tableau.liste(genre)
        taille = 10 if historique else 25
        derniere = max(0, (len(fiches) - 1) // taille)
        self.page = max(0, min(page, derniere))
        lot = fiches[self.page * taille:(self.page + 1) * taille]
        if historique:
            lignes = []
            for f in lot:
                heure = datetime.fromisoformat(f["le"]).astimezone(PARIS)
                ligne = (f"**{pseudo(f['pseudo'])}** · {TYPES[f.get('type', 'gdp')]}\n"
                         f"{heure:%d/%m/%y à %H:%M} · {pseudo(f['par'])}")
                if lien := f.get("sujet"):
                    ligne += f" · [Sujet de formation]({lien})"
                lignes.append(ligne)
            self.carte = discord.Embed(title="📚 Historique des formations",
                                       description="\n".join(lignes) or "Aucune formation confirmée depuis ce tableau.")
        else:
            consigne = ("Choisis la personne à retirer sans enregistrer de formation." if retrait else
                        "Choisis la personne qui a passé sa formation, puis colle le lien de son sujet sur le forum PN.")
            self.carte = discord.Embed(title=TYPES[genre], description=consigne if lot else "La liste des formations à faire est vide.")
            if lot:
                choix = discord.ui.Select(placeholder="Choisir un pseudo", row=0, options=[
                    discord.SelectOption(label=f["pseudo"], value=cle(f)) for f in lot])
                async def selection(inter):
                    k = choix.values[0]
                    fiche = tableau.etat["attente"].get(k)
                    if fiche is None:
                        await inter.response.edit_message(content="Cette personne ne figure plus dans la liste.",
                                                          embed=None, view=None)
                        return
                    if not retrait:
                        await inter.response.send_modal(SujetFormation(self, fiche))
                        return
                    nom = pseudo(fiche["pseudo"])
                    question = f"Retirer **{nom}** de la liste « {TYPES[genre]} » sans valider de formation ?"
                    await inter.response.edit_message(content=question, embed=None,
                                                      view=Confirmation(tableau, auteur, k, retrait=True), allowed_mentions=SANS_MENTIONS)
                choix.callback = selection
                self.add_item(choix)
        if derniere:
            self.carte.set_footer(text=f"Page {self.page + 1} sur {derniere + 1}")
            self.precedente.disabled = self.page == 0
            self.suivante.disabled = self.page == derniere
        else:
            self.remove_item(self.precedente)
            self.remove_item(self.suivante)

    async def naviguer(self, inter, delta):
        vue = PagesFormations(self.tableau, self.auteur, self.historique, self.page + delta, self.genre, retrait=self.retrait)
        await inter.response.edit_message(embed=vue.carte, view=vue if vue.children else None)

    @discord.ui.button(label="Précédente", row=1)
    async def precedente(self, inter, bouton):
        await self.naviguer(inter, -1)

    @discord.ui.button(label="Suivante", row=1)
    async def suivante(self, inter, bouton):
        await self.naviguer(inter, 1)


class SujetFormation(discord.ui.Modal, title="Sujet de formation"):
    def __init__(self, menu, fiche):
        super().__init__(timeout=300)
        self.menu, self.k = menu, cle(fiche)
        self.lien = discord.ui.TextInput(required=True, max_length=2000,
                                         placeholder="https://www.pn-rpg.fr/t…")
        self.add_item(discord.ui.Label(text="Lien du sujet sur le forum PN", component=self.lien,
                                       description=f"{fiche['pseudo']} · {TYPES[menu.genre]}"))

    async def on_submit(self, inter):
        menu = self.menu
        if not await menu.interaction_check(inter):
            return
        fiche = menu.tableau.etat["attente"].get(self.k)
        if fiche is None:
            await inter.response.edit_message(content="Cette personne ne figure plus dans la liste.", embed=None, view=None)
            return
        lien = lien_sujet_forum(self.lien.value)
        if not lien:
            nouveau = PagesFormations(menu.tableau, menu.auteur, historique=False, page=menu.page, genre=menu.genre)
            await inter.response.edit_message(content="Il faut un lien vers un sujet ou un message de pn-rpg.fr. "
                                               "Sélectionne à nouveau le pseudo pour corriger le lien.",
                                               embed=nouveau.carte, view=nouveau)
            return
        question = (f"Confirmer pour **{pseudo(fiche['pseudo'])}** la formation « {TYPES[menu.genre]} » ?\n"
                    f"[Ouvrir le sujet de formation]({lien})")
        await inter.response.edit_message(content=question, embed=None, allowed_mentions=SANS_MENTIONS,
                                          view=Confirmation(menu.tableau, menu.auteur, self.k, sujet=lien))


class Confirmation(VuePrivee):
    def __init__(self, tableau, auteur, k, *, retrait=False, sujet=None):
        super().__init__(tableau, auteur, retrait=retrait)
        self.k, self.sujet = k, sujet
        if retrait:
            self.confirmer.label = "Confirmer le retrait"
            self.confirmer.style = discord.ButtonStyle.danger

    @discord.ui.button(label="Confirmer", style=discord.ButtonStyle.success)
    async def confirmer(self, inter, bouton):
        if self.retrait and not await self.tableau.autoriser(inter, retrait=True):
            return
        await inter.response.defer()
        async with self.tableau.verrou:
            fiche = self.tableau.terminer(self.k, inter.user, retrait=self.retrait, sujet=self.sujet)
            if fiche:
                nom = pseudo(fiche["pseudo"])
                texte = (f"**{nom}** a été retiré de la liste." if self.retrait else
                         f"La formation de **{nom}** est enregistrée dans l’historique.")
                try:
                    verifier_salon(inter.channel, self.tableau.guild_id)
                    await self.tableau.publier(inter.channel, [])
                except (discord.HTTPException, RuntimeError):
                    texte += " Le tableau sera actualisé au prochain passage du bot."
            else:
                texte = "Cette formation a déjà été retirée de la liste."
        await inter.edit_original_response(content=texte, embed=None, view=None, allowed_mentions=SANS_MENTIONS)

    @discord.ui.button(label="Annuler")
    async def annuler(self, inter, bouton):
        await inter.response.edit_message(content="La liste est inchangée.", embed=None, view=None)
