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
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks
from habbo_http import ApiRequestError, request_json
from groupes_pn import EXCLUS_FORMATIONS, GROUPE_GDP, GROUPE_PA


SALON = 1574352702413086810
FORMATEUR = 1552122047034032221
TITRE = "🎓 Formations Gardien de la paix à faire"
TYPES = {"gdp": "Formation Gardien de la paix", "qualifiante": "Formation qualifiante"}
PARIS = ZoneInfo("Europe/Paris")
HOST = os.environ.get("HABBO_HOST", "www.habbo.fr")
LOG = logging.getLogger("pn.formations")
SANS_MENTIONS = discord.AllowedMentions.none()
MENTION_SALON = discord.AllowedMentions(everyone=True, users=False, roles=False, replied_user=False)


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


def presences_habbo(fiches):
    def lire(chemin):
        return request_json(urllib.request.Request(f"https://{HOST}/api/public/{chemin}",
                            headers={"Accept": "application/json", "User-Agent": "PN-Formations/1.0"}))
    groupes = {GROUPE_GDP if f.get("type") == "qualifiante" else GROUPE_PA for f in fiches}
    membres = []
    for groupe in groupes:
        lot = lire(f"groups/{groupe}/members")
        if not isinstance(lot, list) or not lot or not all(isinstance(m, dict) and m.get("uniqueId") for m in lot):
            raise ApiRequestError("response", endpoint="membres du groupe de formation")
        membres.extend(lot)
    par_id = {m["uniqueId"]: m for m in membres}
    par_nom = {m["name"].casefold(): m for m in membres if m.get("name")}
    resultat = {}
    for f in fiches:
        profil = par_id.get(f["hid"]) if f.get("hid") else par_nom.get(f["pseudo"].casefold())
        if profil is None:
            # Une formation peut encore attendre sa confirmation après un changement de badge.
            chemin = ("users/" + urllib.parse.quote(f["hid"], safe="") if f.get("hid") else
                      "users?" + urllib.parse.urlencode({"name": f["pseudo"]}))
            try:
                profil = lire(chemin)
            except ApiRequestError:
                profil = None
            if not isinstance(profil, dict) or profil.get("profileVisible") is not True:
                profil = {}
        resultat[cle(f)] = profil.get("online") is True
    return resultat


def carte_liste(fiches, en_ligne, genre):
    lignes = [f"• {pseudo(f['pseudo'])}" + (" 🟢" if cle(f) in en_ligne else "") for f in fiches]
    contenu = "\n".join(lignes) or ("Aucun gardien de la paix en attente de formation qualifiante."
                                    if genre == "qualifiante" else "Aucun policier adjoint en attente de formation.")
    fichier = None
    if len(contenu.encode("utf-16-le")) > 6800:
        # La liste complète reste sur le même message si elle dépasse la limite Discord.
        fichier = "\n".join(lignes).encode("utf-8")
        contenu = "La liste complète des pseudos est jointe à ce message."
    carte = discord.Embed(title=TYPES[genre], description=contenu)
    n = len(fiches)
    carte.set_footer(text=f"{n} formation{'s' if n > 1 else ''} en attente · 🟢 En ligne sur Habbo")
    return carte, fichier


def est_tableau(msg):
    def contient_menu(elements):
        return any(getattr(e, "custom_id", None) == "pn:formas:type"
                   or contient_menu(getattr(e, "children", ())) for e in elements)
    return (bool(msg.embeds and msg.embeds[0].title == TITRE)
            or contient_menu(getattr(msg, "components", ())))


class TableauFormations:
    def __init__(self, client, guild_id, chemin):
        self.client, self.guild_id, self.chemin = client, guild_id, chemin
        self.etat = (json.loads(chemin.read_text(encoding="utf-8")) if chemin.exists() else
                     {"attente": {}, "historique": {}, "annonces": [], "panneau": 0, "migration": False})
        self.verrou = asyncio.Lock()
        self.reconcilier = True
        self.presences = {}
        self.consultations = {}
        self.vue = BoutonsFormations(self)

    def demarrer(self):
        self.client.add_view(self.vue)
        self.suivre_presences.start()

    def noter_presence(self, k, instant, connecte):
        if instant >= self.presences.get(k, (0, False))[0]:
            self.presences[k] = (instant, connecte)

    @tasks.loop(minutes=2)
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
        if not fiches:
            return
        debut = time.time()
        # Réutiliser les présences que la collecte des badges vient déjà de vérifier.
        if all(debut - self.presences.get(cle(f), (0, False))[0] < 60 for f in fiches):
            return
        try:
            resultats = await asyncio.to_thread(presences_habbo, fiches)
        except ApiRequestError as exc:
            LOG.warning("Présences Habbo indisponibles : %s", exc)
            resultats = {cle(f): False for f in fiches}
        async with self.verrou:
            ch = await self.client.channel(SALON)
            verifier_salon(ch, self.guild_id)
            for k, connecte in resultats.items():
                if k in self.etat["attente"]:
                    self.noter_presence(k, debut, connecte)
            await self.publier(ch, [])

    def sauver(self, **changements):
        nouveau = {**self.etat, **changements}
        if nouveau != self.etat:
            tmp = self.chemin.with_suffix(".tmp")
            tmp.write_text(json.dumps(nouveau, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.chemin)
            self.etat = nouveau

    def liste(self, genre=None):
        return sorted((f for f in self.etat["attente"].values() if genre is None or f.get("type", "gdp") == genre),
                      key=lambda f: (f["depuis"], f["pseudo"].casefold()))

    def incorporer(self, candidats):
        attente = {k: f for k, f in self.etat["attente"].items() if f.get("hid") not in EXCLUS_FORMATIONS}
        par_uid = {(f.get("type", "gdp"), f["uid"]): k for k, f in attente.items()}
        for source in candidats:
            fiche = dict(source)
            k = cle(fiche)
            if k in self.etat["historique"] or fiche.get("hid") in EXCLUS_FORMATIONS:
                continue
            identite = (fiche.get("type", "gdp"), fiche["uid"])
            if "observation" in fiche:
                self.noter_presence(k, fiche.pop("observation"), fiche.pop("en_ligne"))
            ancien = par_uid.get(identite)
            if ancien and ancien != k:
                if attente[ancien]["depuis"] > fiche["depuis"]:
                    continue
                del attente[ancien]
            attente[k] = fiche
            par_uid[identite] = k
        self.sauver(attente=attente)

    def terminer(self, k, membre):
        fiche = self.etat["attente"].get(k)
        if fiche is None:
            return None
        attente = dict(self.etat["attente"])
        del attente[k]
        archive = {**fiche, "par": membre.display_name, "par_id": str(membre.id),
                   "le": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self.sauver(attente=attente, historique={**self.etat["historique"], k: archive})
        self.presences.pop(k, None)
        return fiche

    async def autoriser(self, inter):
        if inter.guild_id == self.guild_id and inter.channel_id == SALON and isinstance(inter.user, discord.Member):
            try:
                verifier_salon(inter.channel, self.guild_id)
            except RuntimeError:
                pass
            else:
                if inter.user.guild_permissions.administrator or any(r.id == FORMATEUR for r in inter.user.roles):
                    return True
        await inter.response.send_message("Ce tableau est réservé aux formateurs dans leur salon privé.", ephemeral=True)
        return False

    async def actualiser(self, candidats):
        async with self.verrou:
            ch = await self.client.channel(SALON)
            verifier_salon(ch, self.guild_id)
            await self.publier(ch, candidats)

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
            self.sauver(panneau=courant.id, annonces=sorted(annonces), envoi=None)
            envoi = None

        self.incorporer([*reprises, *candidats])
        cles = set(self.etat["attente"])
        nouveaux = cles - annonces
        vue = BoutonsFormations(self)
        rendu = vue.signature()

        # Une nouvelle liste mentionne le salon une fois pour le lot. Une simple
        # correction, une confirmation ou un redémarrage ne doit réveiller personne.
        if courant is None or nouveaux:
            if nouveaux and not ch.permissions_for(ch.guild.me).mention_everyone:
                raise RuntimeError("PN · Alertes doit pouvoir mentionner @everyone dans le salon des formations.")
            if envoi is None:
                envoi = {"apres": courant.id if courant else 0, "cles": sorted(cles), "nonce": secrets.token_hex(10)}
                self.sauver(envoi=envoi)
            self.reconcilier = True
            if nouveaux:
                genres = {self.etat["attente"][k].get("type", "gdp") for k in nouveaux}
                vue = BoutonsFormations(self, annoncer=genres)
            courant = await ch.send(view=vue, files=vue.pieces(), nonce=envoi["nonce"],
                                    allowed_mentions=MENTION_SALON if nouveaux else SANS_MENTIONS)
        elif self.etat.get("rendu") != rendu or courant.embeds:
            await courant.edit(content=None, embed=None, view=vue, attachments=vue.pieces(), allowed_mentions=SANS_MENTIONS)
        self.sauver(panneau=courant.id, annonces=sorted(cles), envoi=None, rendu=rendu)
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
            vue = BoutonsFormations(self, genre, auteur=auteur)
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
    def __init__(self, tableau, genre="gdp", *, auteur=None, annoncer=()):
        super().__init__(timeout=None if auteur is None else 900)
        self.tableau, self.genre, self.auteur = tableau, genre, auteur
        en_ligne = {k for k, (t, oui) in tableau.presences.items() if oui and time.time() - t < 240}
        self.carte, self.fichier = carte_liste(tableau.liste(genre), en_ligne, genre)
        self.compteurs = {g: len(tableau.liste(g)) for g in TYPES}
        menu = discord.ui.Select(custom_id="pn:formas:type", placeholder="Choisir une formation", options=[
            discord.SelectOption(label=nom, value=g, default=g == genre, description=f"{self.compteurs[g]} en attente")
            for g, nom in TYPES.items()])
        async def choisir(inter):
            selection = menu.values[0]
            vue = BoutonsFormations(tableau, selection, auteur=inter.user.id)
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
        boite.add_item(discord.ui.TextDisplay("### 🎓 Formations à faire"))
        boite.add_item(discord.ui.ActionRow(menu))
        boite.add_item(discord.ui.TextDisplay(self.carte.description))
        if self.fichier:
            boite.add_item(discord.ui.File("attachment://formations-a-faire.txt"))
        boite.add_item(discord.ui.TextDisplay(f"-# {self.carte.footer.text}"))
        boite.add_item(discord.ui.ActionRow(faite, historique))
        self.add_item(boite)

    def pieces(self):
        return [discord.File(io.BytesIO(self.fichier), filename="formations-a-faire.txt")] if self.fichier else []

    def signature(self):
        contenu = json.dumps([self.genre, self.carte.description, self.compteurs], ensure_ascii=False).encode()
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


class VuePrivee(discord.ui.View):
    def __init__(self, tableau, auteur):
        super().__init__(timeout=300)
        self.tableau, self.auteur = tableau, auteur

    async def interaction_check(self, inter):
        if inter.user.id != self.auteur:
            await inter.response.send_message("Ouvre ton propre menu depuis le tableau.", ephemeral=True)
            return False
        return await self.tableau.autoriser(inter)


class PagesFormations(VuePrivee):
    def __init__(self, tableau, auteur, historique, page=0, genre="gdp"):
        super().__init__(tableau, auteur)
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
                lignes.append(f"**{pseudo(f['pseudo'])}** · {TYPES[f.get('type', 'gdp')]}\n"
                              f"{heure:%d/%m/%y à %H:%M} · {pseudo(f['par'])}")
            self.carte = discord.Embed(title="📚 Historique des formations",
                                       description="\n".join(lignes) or "Aucune formation confirmée depuis ce tableau.")
        else:
            self.carte = discord.Embed(title=TYPES[genre], description="Choisis la personne qui a passé sa formation."
                                       if lot else "La liste des formations à faire est vide.")
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
                    await inter.response.edit_message(content=f"Confirmer pour **{pseudo(fiche['pseudo'])}** "
                                                      f"la formation « {TYPES[genre]} » ?", embed=None,
                                                      view=Confirmation(tableau, auteur, k), allowed_mentions=SANS_MENTIONS)
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
        vue = PagesFormations(self.tableau, self.auteur, self.historique, self.page + delta, self.genre)
        await inter.response.edit_message(embed=vue.carte, view=vue if vue.children else None)

    @discord.ui.button(label="Précédente", row=1)
    async def precedente(self, inter, bouton):
        await self.naviguer(inter, -1)

    @discord.ui.button(label="Suivante", row=1)
    async def suivante(self, inter, bouton):
        await self.naviguer(inter, 1)


class Confirmation(VuePrivee):
    def __init__(self, tableau, auteur, k):
        super().__init__(tableau, auteur)
        self.k = k

    @discord.ui.button(label="Confirmer", style=discord.ButtonStyle.success)
    async def confirmer(self, inter, bouton):
        await inter.response.defer()
        async with self.tableau.verrou:
            fiche = self.tableau.terminer(self.k, inter.user)
            if fiche:
                texte = f"La formation de **{pseudo(fiche['pseudo'])}** est enregistrée dans l’historique."
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
