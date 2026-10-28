#!/usr/bin/env python3
"""PN — service du serveur Hetzner (bot Discord « PN · Alertes »).

Toutes les 15 minutes (heure pile, :00 :15 :30 :45, heure de Paris) :
  1. classements du forum : lance build_classement.py (classement.json, noms.json, classement-state.json) ;
  2. alertes de promotion : lance alertes_grade.py ; les alertes sont postées par le bot dans #Alertes-promotions
     (une seule par membre et par palier, état dans etat_grades.json) ;
  3. publie les fichiers modifiés sur GitHub (le forum lit classement.json sur raw.githubusercontent.com).

Chaque lundi à 00 h 00 (Paris) : lit pts_recrutement_semaine, la remet à 0 pour tout le monde (API Web,
clé d'écriture), puis poste le podium (3 premiers) dans #Classement-hebdo (Direction).
Garde : une seule remise par semaine (hebdo-state.json) ; rattrapage jusqu'à 6 h après minuit, sinon avertissement.

Diagnostic posté dans #Classement-hebdo au premier démarrage d'une version : variables trouvées, test de la clé
d'écriture (réécriture d'une valeur identique), état de la publication GitHub (+ clé de déploiement à ajouter).
Alertes Cadet → Policier adjoint (28/10/2026, demande de Val) : procédure dans l'alerte + bouton vert « Badge attribué »,
cliquable par les rôles Haut gradé, Supervision et Direction (ou administrateurs). Au clic, l'alerte passe en vert
avec le pseudo de celui qui a attribué le badge ; l'information est gardée dans etat_grades.json (clé « badge »).
28/10 (11 h 57, demande de Val) : le clic donne 1 XP en jeu (variable xp, API Web) au compte Habbo de celui qui
clique (pseudo Discord = pseudo Habbo, sinon demandé une fois puis retenu dans /opt/pn/lock/liens_habbo.json) ;
bouton rouge « Annuler » (celui qui a cliqué, ou la Direction) : alerte remise en attente et XP retiré.
Aperçu (une seule fois par valeur d'APERCU) dans #Classement-hebdo : podium hebdo avec les chiffres en cours et une
alerte d'exemple avec son bouton (bouton d'aperçu : rien n'est enregistré).
Aucun secret n'est jamais affiché ni écrit dans le dépôt.
"""
from __future__ import annotations

import asyncio
import fcntl
import importlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks

VERSION = "2026-10-28.1"   # diagnostic de mise en service (ne pas changer sans raison : il est re-posté)
APERCU = "2026-10-28-xp2"  # changer cette valeur pour reposter l'aperçu dans #Classement-hebdo
LOG = logging.getLogger("pn")
PARIS = ZoneInfo("Europe/Paris")

REPO = Path(os.environ.get("REPO_DIR", "/data/repo"))
LOCK_FILE = os.environ.get("GIT_LOCK", "/data/lock/git.lock")
SSH_DIR = Path(os.environ.get("SSH_DIR", "/data/ssh"))
HOST = os.environ.get("HABBO_HOST", "www.habbo.fr")
ROOM = os.environ.get("WIRED_ROOM_ID", "46858918").strip()
READ_KEY = os.environ.get("WIRED_READ_KEY", "").strip()
WRITE_KEY = os.environ.get("WIRED_WRITE_KEY", "").strip()
VAR_SEMAINE = os.environ.get("VAR_SEMAINE", "pts_recrutement_semaine").strip()
GUILD_ID = int(os.environ.get("GUILD_ID", "0") or 0)
ALERT_CHANNEL_ID = int(os.environ.get("ALERT_CHANNEL_ID", "0") or 0)
HEBDO_CHANNEL_ID = int(os.environ.get("HEBDO_CHANNEL_ID", "0") or 0)
CYCLE_MINUTES = int(os.environ.get("CYCLE_MINUTES", "15"))
RATTRAPAGE = timedelta(hours=6)
DATA_FILES = ["classement.json", "noms.json", "classement-state.json", "etat_grades.json", "hebdo-state.json"]
HEBDO_STATE = REPO / "hebdo-state.json"
OR, VERT, ROUGE, BLEU = 0xE3B34A, 0x188A68, 0xAF3843, 0x124669
BADGE_ROLES = [r.strip() for r in os.environ.get("BADGE_ROLES", "Haut gradé,Supervision,Direction").split(",") if r.strip()]
ETAT_LOCK = threading.Lock()   # alertes_grade.main() et les clics « Badge attribué » écrivent etat_grades.json


def norm(text):
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower().strip()


def peut_attribuer(member):
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True
    autorises = {norm(r) for r in BADGE_ROLES}
    return any(norm(role.name) in autorises for role in member.roles)


VAR_XP = (os.environ.get("VAR_XP") or "xp").strip()
LIENS = Path(LOCK_FILE).parent / "liens_habbo.json"   # {id Discord: id Habbo} — sur le serveur seulement
HABBO = {"par_nom": {}, "maj": 0.0}                   # pseudo normalisé -> (id Habbo, pseudo)
BLEU_CADET = 0x2F8FD4


def procedure(pseudo):
    return {"name": "Procédure · Hauts gradés et plus", "inline": False, "value": (
        "Conditions vérifiées par le bot (3 jours + 200 PR).\n"
        f"**1.** Remets le badge **Policier adjoint** à **{pseudo}** sur Habbo.\n"
        "**2.** Clique sur **Badge attribué** : l'alerte passe en vert avec ton pseudo et tu gagnes **1 XP** en jeu "
        "(retiré si tu cliques ensuite sur **Annuler**).\n"
        "La demande du sujet « Gestion du badge Policier adjoint » du forum se valide à part.")}


def refresh_habbo():
    """Annuaire des comptes Habbo connus de la salle (porteurs de matricule et d'XP), pour retrouver un pseudo."""
    par_nom = {}
    for var in ("matricule", VAR_XP):
        try:
            for hid, name, _ in holders(var):
                if name:
                    par_nom[norm(name)] = (hid, name)
        except Exception:  # noqa: BLE001
            LOG.exception("annuaire Habbo : %s illisible", var)
    if par_nom:
        HABBO["par_nom"], HABBO["maj"] = par_nom, time.time()


def habbo_de(member, saisi=None):
    """(id Habbo, pseudo) du membre Discord : lien mémorisé, pseudo saisi, puis pseudo de serveur / global."""
    liens = read_json(LIENS, {})
    if saisi is None and str(member.id) in liens:
        return tuple(liens[str(member.id)])
    noms = [saisi] if saisi else [getattr(member, "nick", None), member.display_name, getattr(member, "global_name", None), member.name]
    for n in noms:
        if n and norm(n) in HABBO["par_nom"]:
            trouve = HABBO["par_nom"][norm(n)]
            liens[str(member.id)] = list(trouve)
            write_json(LIENS, liens)
            return trouve
    return None


def ajuster_xp(hid, delta):
    """Ajoute delta à la variable XP du compte Habbo (API Web, clé d'écriture). Renvoie la nouvelle valeur."""
    path = f"/variables/user/{urllib.parse.quote(VAR_XP)}/users/{hid}"
    cur = api(path)
    valeur = max(0, int((cur or {}).get("value", 0) or 0) + delta)
    res = api(path, method="PATCH", write=True, body={"value": str(valeur)})
    if res is None:  # le compte n'avait pas encore la variable : la créer
        api(path, method="PUT", write=True, body={"value": str(valeur)})
    return valeur


def set_badge(uid, info):
    with ETAT_LOCK:
        path = REPO / "etat_grades.json"
        etat = read_json(path, {})
        if uid in etat:
            if info:
                etat[uid].setdefault("badge", {})["cadet"] = info
            else:
                etat[uid].get("badge", {}).pop("cadet", None)
            path.write_text(json.dumps(etat, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def badge_info(uid):
    return (read_json(REPO / "etat_grades.json", {}).get(uid) or {}).get("badge", {}).get("cadet")


def vue_attribuee(uid, nom):
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(style=discord.ButtonStyle.secondary, disabled=True, emoji="✅",
                                 label=f"Badge attribué par {nom}"[:80], custom_id=f"pn:badge-fait:{uid}"))
    v.add_item(AnnulerButton(uid))
    return v


def vue_attente(uid):
    v = discord.ui.View(timeout=None)
    v.add_item(BadgeButton(uid))
    return v


async def attribuer(interaction, uid, habbo, deja_repondu=False):
    msg = interaction.message
    e = msg.embeds[0].to_dict() if msg and msg.embeds else {}
    if any(f.get("name", "").startswith("✅ Badge attribué") for f in e.get("fields", [])):
        if deja_repondu:
            await interaction.followup.send("Ce badge a déjà été confirmé.", ephemeral=True)
        else:
            await interaction.response.send_message("Ce badge a déjà été confirmé.", ephemeral=True)
        return
    hid, pseudo = habbo
    now = datetime.now(PARIS)
    apercu = uid == "apercu"
    e["color"] = VERT
    e["title"] = "✅ " + re.sub(r"^\S+\s", "", e.get("title", ""))
    e["fields"] = [f for f in e.get("fields", []) if not f.get("name", "").startswith("Procédure")]
    e["fields"].append({"name": "✅ Badge attribué", "inline": False,
                        "value": f"Par {interaction.user.mention} (Habbo : **{discord.utils.escape_markdown(pseudo)}**, "
                                 f"+1 XP) le {now:%d/%m} à {now:%H}h{now:%M}"})
    vue = vue_attribuee(uid, interaction.user.display_name)
    if deja_repondu:
        await msg.edit(embed=discord.Embed.from_dict(e), view=vue)
    else:
        await interaction.response.edit_message(embed=discord.Embed.from_dict(e), view=vue)
    try:
        total = await asyncio.to_thread(ajuster_xp, hid, 1)
        await interaction.followup.send(f"Merci ! **+1 XP** pour **{pseudo}** sur Habbo (total : {total} XP)."
                                        + (" C'est l'essai : clique sur **Annuler** pour le retirer." if apercu else ""),
                                        ephemeral=True)
        xp_ok = True
    except Exception as exc:  # noqa: BLE001
        LOG.exception("XP non crédité")
        await interaction.followup.send(f"Badge confirmé, mais l'XP n'a pas pu être crédité ({str(exc)[:150]}). "
                                        "Préviens la Direction.", ephemeral=True)
        xp_ok = False
    if apercu:
        return
    await asyncio.to_thread(set_badge, uid, {"par": interaction.user.display_name, "par_id": str(interaction.user.id),
                                             "habbo_id": hid, "habbo": pseudo, "xp": xp_ok,
                                             "le": now.isoformat(timespec="seconds")})


class PseudoModal(discord.ui.Modal, title="Ton pseudo Habbo"):
    pseudo = discord.ui.TextInput(label="Pseudo Habbo (pour te créditer 1 XP)", max_length=40)

    def __init__(self, uid):
        super().__init__(timeout=300)
        self.uid = uid

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        saisi = str(self.pseudo.value).strip()
        trouve = await asyncio.to_thread(habbo_de, interaction.user, saisi)
        if not trouve:
            await asyncio.to_thread(refresh_habbo)
            trouve = await asyncio.to_thread(habbo_de, interaction.user, saisi)
        if not trouve:
            await interaction.followup.send(
                f"Je ne trouve pas **{discord.utils.escape_markdown(saisi)}** parmi les comptes passés au QG. "
                "Vérifie l'orthographe exacte de ton pseudo Habbo, puis reclique sur **Badge attribué**.", ephemeral=True)
            return
        await attribuer(interaction, self.uid, trouve, deja_repondu=True)


class BadgeButton(discord.ui.DynamicItem[discord.ui.Button], template=r"pn:badge:(?P<uid>[0-9]+|apercu)"):
    """Bouton vert « Badge attribué » sous les alertes Cadet → Policier adjoint (persistant après redémarrage)."""

    def __init__(self, uid: str):
        super().__init__(discord.ui.Button(style=discord.ButtonStyle.success, label="Badge attribué", emoji="✅",
                                           custom_id=f"pn:badge:{uid}"))
        self.uid = uid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["uid"])

    async def callback(self, interaction: discord.Interaction):
        if not peut_attribuer(interaction.user):
            await interaction.response.send_message(
                "Seuls les **Hauts gradés**, la **Supervision** et la **Direction** peuvent confirmer l'attribution "
                "du badge.", ephemeral=True)
            return
        habbo = habbo_de(interaction.user)
        if not habbo:  # pseudo Discord différent du pseudo Habbo : on le demande une fois, puis on le retient
            await interaction.response.send_modal(PseudoModal(self.uid))
            return
        await attribuer(interaction, self.uid, habbo)


class AnnulerButton(discord.ui.DynamicItem[discord.ui.Button], template=r"pn:annul:(?P<uid>[0-9]+|apercu)"):
    """Bouton « Annuler » : remet l'alerte en attente et retire l'XP donné à celui qui avait confirmé."""

    def __init__(self, uid: str):
        super().__init__(discord.ui.Button(style=discord.ButtonStyle.danger, label="Annuler", emoji="↩️",
                                           custom_id=f"pn:annul:{uid}"))
        self.uid = uid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["uid"])

    async def callback(self, interaction: discord.Interaction):
        msg = interaction.message
        e = msg.embeds[0].to_dict() if msg and msg.embeds else {}
        champ = next((f for f in e.get("fields", []) if f.get("name", "").startswith("✅ Badge attribué")), None)
        if not champ:
            await interaction.response.send_message("Cette alerte est déjà en attente.", ephemeral=True)
            return
        m = re.search(r"<@!?(\d+)>", champ.get("value", ""))
        auteur = m.group(1) if m else None
        direction = isinstance(interaction.user, discord.Member) and (
            interaction.user.guild_permissions.administrator
            or any(norm(r.name) == "direction" for r in interaction.user.roles))
        if str(interaction.user.id) != auteur and not direction:
            await interaction.response.send_message(
                "Seul celui qui a cliqué sur **Badge attribué** (ou la Direction) peut annuler.", ephemeral=True)
            return
        pseudo = re.sub(r"^\S+\s", "", e.get("title", "")).split(" peut passer")[0]
        e["color"] = BLEU_CADET
        e["title"] = "🎖️ " + re.sub(r"^\S+\s", "", e.get("title", ""))
        e["fields"] = [f for f in e.get("fields", []) if not f.get("name", "").startswith("✅ Badge attribué")]
        e["fields"].append(procedure(pseudo))
        await interaction.response.edit_message(embed=discord.Embed.from_dict(e), view=vue_attente(self.uid))
        if self.uid == "apercu":  # essai : retrouver le compte Habbo crédité à partir du message
            mh = re.search(r"Habbo : \*\*(.+?)\*\*", champ.get("value", ""))
            trouve = HABBO["par_nom"].get(norm(mh.group(1).replace("\\", ""))) if mh else None
            info = {"xp": bool(trouve), "habbo_id": trouve and trouve[0], "habbo": trouve and trouve[1]}
        else:
            info = await asyncio.to_thread(badge_info, self.uid)
        retrait = ""
        if info and info.get("xp") and info.get("habbo_id"):
            try:
                total = await asyncio.to_thread(ajuster_xp, info["habbo_id"], -1)
                retrait = f" **−1 XP** pour **{info.get('habbo')}** (total : {total} XP)."
            except Exception as exc:  # noqa: BLE001
                retrait = f" ⚠️ L'XP n'a pas pu être retiré ({str(exc)[:120]}) : préviens la Direction."
        if self.uid != "apercu":
            await asyncio.to_thread(set_badge, self.uid, None)
        await interaction.followup.send("Attribution annulée : l'alerte est de nouveau en attente." + retrait,
                                        ephemeral=True)


# ---------------------------------------------------------------- API Web Habbo (variables WIRED)

def api(path, params=None, body=None, method=None, write=False):
    url = f"https://{HOST}/api/public/rooms/{ROOM}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Accept": "application/json", "User-Agent": "pn-serveur/1.0", "X-Wired-Read-Key": READ_KEY}
    data = None
    if write:
        headers["X-Wired-Write-Key"] = WRITE_KEY
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, headers=headers, data=data, method=method)
    for _ in range(5):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(int(e.headers.get("Retry-After") or 10) + 1)
                continue
            if e.code == 404:
                return None
            raise RuntimeError(f"API Habbo {e.code} sur {path.split('?')[0]} : "
                               f"{e.read()[:200].decode('utf-8', 'replace')}") from None
    raise RuntimeError(f"API Habbo : trop de requêtes refusées ({path})")


def holders(var):
    """[(user_id, pseudo, valeur)] pour tous les porteurs d'une variable utilisateur."""
    out, page = [], 1
    while True:
        data = api(f"/variables/user/{urllib.parse.quote(var)}/users",
                   {"order_by": "value", "order_dir": "desc", "page": page, "size": 100})
        if data is None:
            raise RuntimeError(f"variable « {var} » introuvable dans la salle {ROOM}")
        items = data.get("items") or []
        for it in items:
            u = it.get("user") or {}
            v = (it.get("variable") or {}).get("value", it.get("value", 0))
            if u.get("id") is not None:
                out.append((str(u["id"]), u.get("name") or "", int(v)))
        if len(items) < 100 or page >= 50:
            return out
        page += 1
        time.sleep(0.6)


def set_values(var, values):
    """Écrit {user_id: valeur} par lots de 50 (opération batch de l'API). Renvoie la liste des échecs."""
    echecs, items = [], list(values.items())
    for i in range(0, len(items), 50):
        chunk = items[i:i + 50]
        res = api(f"/variables/user/{urllib.parse.quote(var)}/batch", method="POST", write=True,
                  body={"requests": [{"op_id": uid, "method": "PATCH", "path": f"users/{uid}",
                                      "body": {"value": str(v)}} for uid, v in chunk]})
        for r in (res or {}).get("results") or []:
            if int(r.get("status", 0)) >= 300:
                echecs.append(f"{r.get('op_id')} ({r.get('status')})")
        time.sleep(1)
    return echecs


def variable_names():
    data = api("/variables") or {}
    items = data.get("items") if isinstance(data, dict) else data
    names = []
    for it in items or []:
        if isinstance(it, dict):
            names.append(f"{it.get('name')} ({it.get('scope') or it.get('type') or '?'})")
        else:
            names.append(str(it))
    return names


# ---------------------------------------------------------------- Git (publication des fichiers du forum)

def git(*args, check=True):
    env = dict(os.environ, GIT_SSH_COMMAND=(
        f"ssh -i {SSH_DIR / 'id_ed25519'} -o UserKnownHostsFile={SSH_DIR / 'known_hosts'} "
        "-o StrictHostKeyChecking=accept-new -o BatchMode=yes"))
    r = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, env=env, timeout=120)
    if check and r.returncode:
        raise RuntimeError(f"git {args[0]} : {(r.stderr or r.stdout).strip()[:300]}")
    return r


def publish(message):
    """Commit des fichiers de données puis push (verrou partagé avec le script de mise à jour du serveur)."""
    Path(LOCK_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        present = [f for f in DATA_FILES if (REPO / f).exists()] + (["hebdo"] if (REPO / "hebdo").exists() else [])
        git("add", "--", *present)
        if git("diff", "--cached", "--quiet", check=False).returncode:
            git("commit", "-q", "-m", message)
        git("fetch", "-q", "https://github.com/valcaz01/pn-classements.git", "main")
        if git("rebase", "-q", "--autostash", "FETCH_HEAD", check=False).returncode:
            git("rebase", "--abort", check=False)
            raise RuntimeError("rebase impossible (conflit) : publication reportée")
        ahead = git("rev-list", "--count", "FETCH_HEAD..HEAD").stdout.strip()
        if ahead != "0":
            git("push", "-q", "origin", "HEAD:main")
        return ahead


# ---------------------------------------------------------------- semaines (heure de Paris)

def monday_of(dt):
    d = dt.astimezone(PARIS)
    m = (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return m


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def avatar(name, head=False):
    q = urllib.parse.quote(name)
    if head:
        return f"https://{HOST}/habbo-imaging/avatarimage?user={q}&direction=2&head_direction=3&headonly=1&size=l"
    return f"https://{HOST}/habbo-imaging/avatarimage?user={q}&direction=2&head_direction=3&gesture=sml&size=l"


def podium_embeds(rows, debut, fin, remise):
    """Podium : un en-tête + une carte par place (avatar en pied, pour habiller les bots du QG)."""
    titre = f"🏆 Classement recrutement — semaine du {debut:%d/%m} au {fin:%d/%m}"
    top = [r for r in sorted(rows, key=lambda r: (-r[2], r[1].casefold())) if r[2] > 0][:3]
    if not top:
        return [discord.Embed(title=titre, description="Aucun point de recrutement cette semaine.", color=OR)]
    head = discord.Embed(title=titre, color=OR,
                         description="Podium à reporter sur les bots du QG.")
    head.set_footer(text=f"Points de la semaine remis à 0 le {remise:%d/%m} à 00 h 00")
    embeds = [head]
    for (uid, name, value), medaille, couleur in zip(top, ["🥇", "🥈", "🥉"], [OR, 0xC3CFDA, 0xCF8A55]):
        e = discord.Embed(description=f"{medaille} **{discord.utils.escape_markdown(name)}** · {value} pts",
                          color=couleur)
        e.set_thumbnail(url=avatar(name))
        embeds.append(e)
    return embeds


# ---------------------------------------------------------------- bot

class PNBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents(guilds=True), allowed_mentions=discord.AllowedMentions.none())
        self.cycle_lock = asyncio.Lock()
        self.last_slot = None
        self.last_errors = {}
        self.started = False

    async def setup_hook(self):
        self.add_dynamic_items(BadgeButton, AnnulerButton)
        self.tick.start()

    async def on_ready(self):
        LOG.info("Connecté : %s (version %s)", self.user, VERSION)
        if not self.started:
            self.started = True
            asyncio.create_task(self.demarrage())

    async def demarrage(self):
        await asyncio.to_thread(refresh_habbo)
        await self.diagnostic()
        try:
            await self.apercu()
        except Exception as exc:  # noqa: BLE001
            await self.report("Aperçu", f"{exc}\n```{traceback.format_exc()[-800:]}```")

    async def channel(self, cid):
        return self.get_channel(cid) or await self.fetch_channel(cid)

    async def report(self, key, text):
        """Erreur signalée dans #Classement-hebdo, au plus une fois par heure et par type."""
        LOG.error("%s : %s", key, text)
        if time.time() - self.last_errors.get(key, 0) < 3600 or not HEBDO_CHANNEL_ID:
            return
        self.last_errors[key] = time.time()
        try:
            ch = await self.channel(HEBDO_CHANNEL_ID)
            await ch.send(embed=discord.Embed(title=f"⚠️ {key}", description=text[:1500], color=ROUGE))
        except discord.HTTPException:
            LOG.exception("signalement impossible")

    # -- toutes les minutes : cycle de 15 min et remise du lundi
    @tasks.loop(seconds=30)
    async def tick(self):
        now = datetime.now(PARIS)
        try:
            await self.weekly(now)
        except Exception as exc:  # noqa: BLE001
            await self.report("Classement hebdo", f"{exc}\n```{traceback.format_exc()[-800:]}```")
        slot = now.replace(second=0, microsecond=0, minute=now.minute - now.minute % CYCLE_MINUTES)
        if slot != self.last_slot and not self.cycle_lock.locked():
            self.last_slot = slot
            asyncio.create_task(self.cycle())

    @tick.before_loop
    async def before_tick(self):
        await self.wait_until_ready()

    async def cycle(self):
        async with self.cycle_lock:
            env = dict(os.environ)
            # 1. classements du forum
            try:
                r = await asyncio.to_thread(subprocess.run, [sys.executable, "build_classement.py"], cwd=REPO,
                                            env=env, capture_output=True, text=True, timeout=600)
                if r.returncode:
                    raise RuntimeError((r.stderr or r.stdout).strip()[-800:])
                LOG.info("classements : %s", r.stdout.strip().splitlines()[-1:] or "")
            except Exception as exc:  # noqa: BLE001
                await self.report("Classements du forum", str(exc))
            await asyncio.to_thread(refresh_habbo)  # annuaire des pseudos Habbo pour le bouton « Badge attribué »
            # 2. alertes de promotion (postées par le bot)
            try:
                await self.alerts()
            except Exception as exc:  # noqa: BLE001
                await self.report("Alertes de promotion", f"{exc}\n```{traceback.format_exc()[-800:]}```")
            # 3. publication GitHub
            try:
                await asyncio.to_thread(publish, f"Classements {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}")
            except Exception as exc:  # noqa: BLE001
                await self.report("Publication GitHub", str(exc))

    async def alerts(self):
        loop = asyncio.get_running_loop()
        ch = await self.channel(ALERT_CHANNEL_ID)

        async def send(embed):
            meta = embed.pop("_pn", None) or {}
            view = None
            if meta.get("palier") == "cadet":
                view = discord.ui.View(timeout=None)
                view.add_item(BadgeButton(str(meta["uid"])))
            await ch.send(embed=discord.Embed.from_dict(embed), view=view)

        def post(embed):
            try:
                asyncio.run_coroutine_threadsafe(send(embed), loop).result(60)
                time.sleep(1)
                return True
            except Exception:  # noqa: BLE001  (alerte non marquée : elle repartira au passage suivant)
                LOG.exception("alerte non envoyée")
                return False

        def run():
            os.chdir(REPO)
            if str(REPO) not in sys.path:
                sys.path.insert(0, str(REPO))
            os.environ.pop("TEST", None)
            os.environ["DISCORD_WEBHOOK"] = ""
            mod = importlib.reload(sys.modules["alertes_grade"]) if "alertes_grade" in sys.modules \
                else importlib.import_module("alertes_grade")
            mod.post = post
            with ETAT_LOCK:
                mod.main()

        await asyncio.to_thread(run)

    # -- lundi 00 h 00 : podium de la semaine écoulée puis remise à zéro
    async def weekly(self, now):
        monday = monday_of(now)
        state = read_json(HEBDO_STATE, None)
        if state is None:  # première mise en service : la semaine en cours est déjà entamée
            write_json(HEBDO_STATE, {"derniere_remise": monday.date().isoformat(), "publie": True,
                                     "mise_en_service": now.isoformat(timespec="seconds")})
            return
        semaine = monday.date().isoformat()
        if state.get("derniere_remise") == semaine:
            if not state.get("publie"):
                await self.post_podium(state, semaine)
            return
        if now - monday > RATTRAPAGE:
            if state.get("averti") != semaine:
                state["averti"] = semaine
                write_json(HEBDO_STATE, state)
                await self.report("Remise à zéro non faite",
                                  f"Le serveur était indisponible lundi entre 00 h 00 et 06 h 00 : "
                                  f"`{VAR_SEMAINE}` n'a pas été remise à 0 cette semaine. À faire à la main "
                                  f"(Inspection) ou me le demander.")
            return
        rows = await asyncio.to_thread(holders, VAR_SEMAINE)
        debut, fin = monday - timedelta(days=7), monday - timedelta(days=1)
        write_json(REPO / "hebdo" / f"{debut.date().isoformat()}.json",
                   {"semaine_du": debut.date().isoformat(), "au": fin.date().isoformat(),
                    "classement": [{"id": u, "pseudo": n, "points": v}
                                   for u, n, v in sorted(rows, key=lambda r: -r[2]) if v > 0]})
        echecs = await asyncio.to_thread(set_values, VAR_SEMAINE, {u: 0 for u, _, v in rows if v != 0})
        state = {"derniere_remise": semaine, "publie": False, "echecs": echecs,
                 "fichier": f"hebdo/{debut.date().isoformat()}.json"}
        write_json(HEBDO_STATE, state)
        await self.post_podium(state, semaine)
        try:
            await asyncio.to_thread(publish, f"Classement hebdo {debut:%d/%m}–{fin:%d/%m}")
        except Exception as exc:  # noqa: BLE001
            await self.report("Publication GitHub", str(exc))

    async def post_podium(self, state, semaine):
        monday = datetime.fromisoformat(semaine).replace(tzinfo=PARIS)
        snap = read_json(REPO / state["fichier"], {"classement": []})
        rows = [(r["id"], r["pseudo"], r["points"]) for r in snap["classement"]]
        embeds = podium_embeds(rows, monday - timedelta(days=7), monday - timedelta(days=1), monday)
        if state.get("echecs"):
            embeds.append(discord.Embed(color=ROUGE, description="⚠️ Remise à 0 refusée pour : "
                                        + ", ".join(state["echecs"])[:900]))
        ch = await self.channel(HEBDO_CHANNEL_ID)
        await ch.send(embeds=embeds)
        state["publie"] = True
        write_json(HEBDO_STATE, state)

    # -- aperçu pour la Direction (une fois par valeur d'APERCU)
    async def apercu(self):
        state_file = Path(LOCK_FILE).parent / "apercu-version"
        if state_file.exists() and state_file.read_text().strip() == APERCU:
            return
        ch = await self.channel(HEBDO_CHANNEL_ID)
        now = datetime.now(PARIS)
        monday = monday_of(now) + timedelta(days=7)
        rows = await asyncio.to_thread(holders, VAR_SEMAINE)
        intro = discord.Embed(title="👀 Aperçu des messages du bot", color=BLEU, description=(
            "Visible de la Direction seulement. **Rien n'est remis à zéro et rien n'est enregistré.**\n\n"
            f"**1. Classement hebdo** — voici le message qui partira lundi {monday:%d/%m} à 00 h 00, "
            "avec les points de cette semaine **à l'heure actuelle** (le podium peut encore changer).\n"
            "**2. Alerte Cadet → Policier adjoint** — exemple de ce que verront les formateurs et les hauts gradés "
            "dans Alertes-promotions. **Badge attribué** donne 1 XP en jeu à celui qui clique ; **Annuler** le lui "
            "retire (seul celui qui a cliqué, ou la Direction, peut annuler).\n"
            "⚠️ Ici, l'essai est **réel** pour l'XP : après avoir cliqué sur Badge attribué, clique sur Annuler."))
        await ch.send(embed=intro)
        await ch.send(embeds=podium_embeds(rows, monday - timedelta(days=7), monday - timedelta(days=1), monday))
        pseudo = (sorted(rows, key=lambda r: -r[2])[0][1] if rows else "") or "sami75.19"
        exemple = {
            "title": f"🎖️ {pseudo} peut passer Policier adjoint",
            "description": f"**{pseudo}** peut passer **Policier adjoint**.\n*Exemple : ce membre n'est pas réellement concerné.*",
            "color": 0x2F8FD4,
            "fields": [
                {"name": "Grade actuel", "value": "Cadet", "inline": True},
                {"name": "Ancienneté", "value": "3 jours (≥ 3)", "inline": True},
                {"name": "Points de recrutement", "value": "214 (≥ 200)", "inline": True},
                {"name": "Humeur", "value": "`[PN] Cadet`", "inline": False},
                procedure(pseudo),
            ],
            "thumbnail": {"url": avatar(pseudo, head=True)},
            "footer": {"text": f"Grade vu dans l'humeur depuis le {now - timedelta(days=3):%d/%m/%Y} · APERÇU"},
            "timestamp": now.isoformat(timespec="seconds"),
        }
        view = discord.ui.View(timeout=None)
        view.add_item(BadgeButton("apercu"))
        await ch.send(embed=discord.Embed.from_dict(exemple), view=view)
        state_file.write_text(APERCU)

    # -- diagnostic de mise en service
    async def diagnostic(self):
        state_file = Path(LOCK_FILE).parent / "diagnostic-version"  # dossier persistant du serveur
        if state_file.exists() and state_file.read_text().strip() == VERSION:
            return
        lignes = [f"Version `{VERSION}` · salle `{ROOM}`"]
        try:
            names = await asyncio.to_thread(variable_names)
            lignes.append("Variables de la salle : " + (", ".join(f"`{n}`" for n in names) or "aucune"))
        except Exception as exc:  # noqa: BLE001
            lignes.append(f"❌ Lecture des variables : {exc}")
        try:
            rows = await asyncio.to_thread(holders, VAR_SEMAINE)
            lignes.append(f"✅ `{VAR_SEMAINE}` : {len(rows)} porteur(s), "
                          f"{sum(1 for r in rows if r[2] > 0)} avec des points cette semaine")
            if rows:
                uid, name, value = rows[-1]  # réécrit la même valeur : aucun changement visible
                echecs = await asyncio.to_thread(set_values, VAR_SEMAINE, {uid: value})
                lignes.append("✅ Clé d'écriture valide (test sans effet : même valeur réécrite)" if not echecs
                              else f"❌ Clé d'écriture refusée ({', '.join(echecs)})")
        except Exception as exc:  # noqa: BLE001
            lignes.append(f"❌ `{VAR_SEMAINE}` : {exc}")
        try:
            ahead = await asyncio.to_thread(publish, "Mise en service du serveur PN")
            lignes.append(f"✅ Publication GitHub opérationnelle ({ahead} commit(s) envoyé(s))")
        except Exception as exc:  # noqa: BLE001
            pub = (SSH_DIR / "id_ed25519.pub").read_text().strip() if (SSH_DIR / "id_ed25519.pub").exists() else "?"
            lignes.append(f"❌ Publication GitHub : {str(exc)[:200]}\nClé de déploiement à ajouter au dépôt "
                          f"(clé publique, sans risque) :\n```{pub}```")
        lignes.append(f"Prochaine remise à zéro : lundi {monday_of(datetime.now(PARIS)) + timedelta(days=7):%d/%m} à 00 h 00")
        ch = await self.channel(HEBDO_CHANNEL_ID)
        await ch.send(embed=discord.Embed(title="🖥️ Serveur PN en service", description="\n".join(lignes)[:4000],
                                          color=BLEU))
        state_file.write_text(VERSION)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    missing = [k for k in ("DISCORD_TOKEN", "WIRED_READ_KEY", "WIRED_WRITE_KEY") if not os.environ.get(k, "").strip()
               or os.environ[k].startswith("COLLE_ICI")]
    if missing or not (ALERT_CHANNEL_ID and HEBDO_CHANNEL_ID):
        sys.exit("Configuration incomplète dans /opt/pn/.env : " + ", ".join(missing or ["identifiants de salons"]))
    PNBot().run(os.environ["DISCORD_TOKEN"].strip(), log_handler=None)


if __name__ == "__main__":
    main()
