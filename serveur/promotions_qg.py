"""Rappels privés aux hauts gradés, en réponse à la demande de badge encore ouverte."""
import asyncio
import json
import logging
import secrets
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord

from groupes_pn import GROUPE_CADET, GROUPE_PA
from habbo_http import ApiRequestError, request_json


LOG = logging.getLogger("pn.promotions.qg")
PARIS = ZoneInfo("Europe/Paris")


def cadets_en_ligne(host):
    def membres(groupe):
        chemin = f"/api/public/groups/{groupe}/members"
        rows = request_json(urllib.request.Request(f"https://{host}{chemin}", headers={"User-Agent": "PN-Promotions/1.0"}))
        if not isinstance(rows, list) or not rows or not all(isinstance(m, dict) and m.get("uniqueId") for m in rows):
            raise ApiRequestError("response", endpoint=chemin)
        return {m["uniqueId"]: m for m in rows}
    cadets = membres(GROUPE_CADET)
    adjoints = membres(GROUPE_PA)
    return {hid for hid, m in cadets.items() if m.get("online") is True and hid not in adjoints}


def bouton_attente(message, uid):
    return any(getattr(b, "custom_id", None) == f"pn:badge:{uid}" and not b.disabled
               for ligne in message.components for b in getattr(ligne, "children", ()))


class AlertesBadgesQG:
    def __init__(self, client, salon, role, repo, chemin, host, delai):
        self.client, self.salon, self.role = client, salon, role
        self.repo, self.chemin, self.host, self.delai = repo, chemin, host, delai
        self.etat = json.loads(chemin.read_text(encoding="utf-8")) if chemin.exists() else {}
        self.instant = self.reessayer = 0
        self.presents = {}
        self.tache = None
        self.recherches = {}

    def sauver(self, uid, **valeurs):
        ligne = {**self.etat.get(uid, {}), **valeurs}
        if ligne == self.etat.get(uid):
            return
        nouveau = {**self.etat, uid: ligne}
        tmp = self.chemin.with_suffix(".tmp")
        tmp.write_text(json.dumps(nouveau, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.chemin)
        self.etat = nouveau

    def demandes(self):
        etat = json.loads((self.repo / "etat_grades.json").read_text(encoding="utf-8"))
        badges = json.loads((self.repo / "badges_cadets.json").read_text(encoding="utf-8"))
        par_nom = {f["pseudo"].casefold(): (hid, f["depuis"]) for hid, f in badges.items()}
        demandes = []
        for uid, fiche in etat.items():
            alerte = fiche.get("alertes", {}).get("cadet")
            nom = fiche.get("pseudo", "")
            if not (uid.isdecimal() and alerte and fiche.get("grade") == "cadet" and nom
                    and not fiche.get("badge", {}).get("cadet")):
                continue
            membre = par_nom.get(nom.casefold())
            if membre and datetime.fromisoformat(alerte).astimezone(PARIS).date().isoformat() >= membre[1]:
                demandes.append({"uid": uid, "hid": membre[0], "pseudo": nom, "alerte": alerte})
        return demandes

    def observer(self, demandes, presents):
        self.instant = time.monotonic()
        self.presents = {f["uid"]: f for f in demandes if f["hid"] in presents}
        for uid, avant in tuple(self.etat.items()):
            absences = avant.get("absences", 2)
            if uid not in self.presents:
                if absences < 2:
                    self.sauver(uid, absences=absences + 1)
            elif absences < 2 or time.time() - avant.get("dernier", 0) < self.delai:
                self.sauver(uid, absences=0)
        if self.presents and (self.tache is None or self.tache.done()):
            self.tache = asyncio.create_task(self.traiter())

    def toujours_present(self, fiche):
        return time.monotonic() - self.instant < 45 and self.presents.get(fiche["uid"]) == fiche

    async def demande_discord(self, ch, fiche):
        uid, origine = fiche["uid"], fiche["alerte"]
        suivi = self.etat.get(uid, {})
        if suivi.get("alerte") == origine and suivi.get("message"):
            try:
                msg = await ch.fetch_message(suivi["message"])
            except discord.NotFound:
                return None
            return msg if msg.author.id == self.client.user.id and bouton_attente(msg, uid) else None
        if time.monotonic() < self.recherches.get((uid, origine), 0):
            return None
        self.recherches[uid, origine] = time.monotonic() + 120
        debut = datetime.fromisoformat(origine) - timedelta(minutes=2)
        async for msg in ch.history(after=debut, oldest_first=False, limit=None):
            if msg.author.id == self.client.user.id and bouton_attente(msg, uid):
                self.sauver(uid, message=msg.id, alerte=origine)
                return msg
        return None

    async def rappeler(self, ch, fiche):
        uid = fiche["uid"]
        message = await self.demande_discord(ch, fiche)
        if message is None:
            return
        envoi = self.etat.get(uid, {}).get("envoi")
        if envoi and envoi["message"] == message.id:
            depuis = datetime.fromtimestamp(envoi["debut"] - 2, timezone.utc)
            async for msg in ch.history(after=depuis, oldest_first=False, limit=None):
                if (msg.author.id == self.client.user.id and msg.reference and msg.reference.message_id == message.id
                        and msg.content.startswith(f"<@&{self.role}>") and "est en ligne au QG" in msg.content):
                    self.sauver(uid, dernier=msg.created_at.timestamp(), envoi=None, absences=0)
                    return
        else:
            envoi = {"message": message.id, "debut": time.time(), "nonce": secrets.token_hex(10)}
        if not self.toujours_present(fiche):
            return
        # Le HG a pu valider pendant la lecture de l'historique ou du profil.
        courant = await ch.fetch_message(message.id)
        if not bouton_attente(courant, uid) or not self.toujours_present(fiche):
            return
        self.sauver(uid, envoi=envoi)
        nom = discord.utils.escape_mentions(discord.utils.escape_markdown(fiche["pseudo"]))
        msg = await ch.send(f"<@&{self.role}> **{nom}** est en ligne au QG et attend son badge **Policier adjoint**.",
                            reference=courant, nonce=envoi["nonce"], allowed_mentions=discord.AllowedMentions(
                                everyone=False, users=False, roles=[discord.Object(id=self.role)], replied_user=False))
        self.sauver(uid, dernier=msg.created_at.timestamp(), envoi=None, absences=0)

    async def traiter(self):
        try:
            a_verifier = [f for uid, f in self.presents.items() if self.etat.get(uid, {}).get("absences", 2) == 2]
            if not a_verifier or time.monotonic() < self.reessayer:
                return
            connectes = await asyncio.to_thread(cadets_en_ligne, self.host)
            ch = await self.client.channel(self.salon)
            role = ch.guild.get_role(self.role)
            if not role or not (role.mentionable or ch.permissions_for(ch.guild.me).mention_everyone):
                raise RuntimeError("Le rôle Haut gradé ne peut pas être mentionné dans Alertes-promotions.")
            for fiche in a_verifier:
                if fiche["hid"] in connectes and self.toujours_present(fiche):
                    await self.rappeler(ch, fiche)
        except ApiRequestError as exc:
            self.reessayer = time.monotonic() + max(30, exc.retry_delay or 0)
            LOG.warning("Rappel des badges au QG reporté : %s", exc)
        except (discord.HTTPException, OSError, RuntimeError):
            LOG.exception("Rappel des badges au QG reporté")
