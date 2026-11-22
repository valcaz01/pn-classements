"""Suivi privé du QG, à la demande, depuis le navigateur Habbo Classic du PC."""

import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import re
import secrets
import time

import aiohttp
import discord
from discord.ext import tasks

from direction_todo import name_key


LOG = logging.getLogger("pn.suivi_qg")
SALON = "suivi-du-qg"
WEBHOOK = "Liaison Habbo Classic"
TITRE = "Fréquentation du QG"
SUIVIS = ("PN, FBI, CIA, Gendarmerie RF, Centre militaire RF et Police nationale RF. "
          "Le rang hôtel suit l’ordre affiché par Habbo. Les QG à égalité partagent le même rang RP.")
HOTE = re.compile(r"https://[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com")
MAX_REPONSE = 2_000_000
CHEMIN = "/pn-qg/releve"


def signature(jeton, donnee):
    return hmac.new(jeton.encode("ascii"), donnee, hashlib.sha256).hexdigest()


def inscription(contenu, jeton):
    mots = contenu.split()
    if len(mots) != 4 or mots[0] != "PN-QG/1":
        return None
    _, date, url, mac = mots
    if (not date.isascii() or not date.isdigit() or len(date) != 10 or not HOTE.fullmatch(url)
            or len(url) > 180 or not re.fullmatch(r"[0-9a-f]{64}", mac)):
        return None
    if not hmac.compare_digest(signature(jeton, f"annonce {date} {url}".encode()), mac):
        return None
    return int(date), url


def prive(salon, role, moi):
    if not (salon.overwrites_for(salon.guild.default_role).view_channel is False
            and (salon.overwrites_for(role).view_channel is True or role.permissions.administrator is True)
            and salon.permissions_for(role).read_message_history):
        return False
    return all(cible.id in (role.id, moi.id) for cible, droits in salon.overwrites.items()
               if droits.view_channel is True)


def decoder(corps, mac, nonce, jeton):
    attendu = signature(jeton, b"reponse " + nonce.encode() + b"\n" + corps)
    if not re.fullmatch(r"[0-9a-f]{64}", mac) or not hmac.compare_digest(attendu, mac):
        raise ValueError("Réponse non authentifiée")
    donnees = json.loads(corps)
    texte = donnees["texte"]
    if donnees["nonce"] != nonce or not isinstance(texte, str) or not 1 <= len(texte) <= 1200:
        raise ValueError("Relevé invalide")
    image = base64.b64decode(donnees["png"], validate=True) if donnees["png"] else None
    if image is not None and not image.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Format de capture invalide")
    return texte, image


class Jumelage(discord.ui.Modal, title="Relier le PC Habbo"):
    adresse = discord.ui.TextInput(label="Adresse HTTPS affichée sur le PC", max_length=180)
    code = discord.ui.TextInput(label="Code de jumelage à usage unique", min_length=24, max_length=80)

    def __init__(self, suivi):
        super().__init__(timeout=300)
        self.suivi = suivi

    async def on_submit(self, inter):
        if not await self.suivi.autoriser(inter):
            return
        await inter.response.defer(ephemeral=True)
        url, code = self.adresse.value.strip().rstrip("/"), self.code.value.strip()
        if not HOTE.fullmatch(url) or not re.fullmatch(r"[a-zA-Z0-9_-]{24,80}", code) or not self.suivi.hook:
            await inter.edit_original_response(content="Adresse, code ou liaison du salon invalide.")
            return
        nonce = secrets.token_hex(16)
        hook = self.suivi.hook
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as http:
                async def envoyer(data, attendu):
                    async with http.post(url+"/pn-qg/jumeler",json=data,allow_redirects=False) as rep:
                        if rep.status != 200:
                            raise ValueError
                        bloc = await rep.content.read(1025)
                        if len(bloc) > 1024 or not hmac.compare_digest(json.loads(bloc)["preuve"], signature(code,attendu)):
                            raise ValueError
                await envoyer({"etape":"verifier","nonce":nonce,"preuve":signature(code,("demande "+nonce).encode())},
                              ("pc "+nonce).encode())
                liaison = {"etape":"relier","nonce":nonce,"guild_id":str(inter.guild_id),
                           "channel_id":str(inter.channel_id),"webhook":hook.url}
                liaison["preuve"] = signature(code,f"relier {nonce} {inter.guild_id} {inter.channel_id} {hook.url}".encode())
                await envoyer(liaison,("relie "+nonce).encode())
            self.suivi.enregistrer(url=url,date=int(time.time()))
            await self.suivi.actualiser()
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError):
            await inter.edit_original_response(content="Le PC n’a pas confirmé le jumelage. Vérifie l’adresse et le code local.")
            return
        await inter.edit_original_response(content="PC relié. Le premier relevé est affiché dans ce salon.")


class RelierPC(discord.ui.View):
    def __init__(self, suivi):
        super().__init__(timeout=180)
        self.suivi = suivi

    @discord.ui.button(label="Relier le PC")
    async def relier(self, inter, bouton):
        if await self.suivi.autoriser(inter):
            await inter.response.send_modal(Jumelage(self.suivi))


class BoutonsQG(discord.ui.View):
    def __init__(self, suivi):
        super().__init__(timeout=None)
        self.suivi = suivi

    async def interaction_check(self, interaction):
        return await self.suivi.autoriser(interaction)

    @discord.ui.button(label="Actualiser", emoji="🔄", style=discord.ButtonStyle.primary, custom_id="pn:qg:actualiser")
    async def actualiser(self, interaction, bouton):
        await interaction.response.defer()
        try:
            await self.suivi.actualiser()
        except (RuntimeError, discord.HTTPException):
            await interaction.followup.send("Le salon ne permet pas de publier le relevé pour le moment.", ephemeral=True)

    @discord.ui.button(label="QG suivis", custom_id="pn:qg:liste")
    async def liste(self, interaction, bouton):
        if not self.suivi.etat.get("url"):
            await interaction.response.send_message(SUIVIS, view=RelierPC(self.suivi), ephemeral=True)
        else:
            await interaction.response.send_message(SUIVIS, ephemeral=True)


class SuiviQG:
    def __init__(self, client, guild_id, chemin):
        self.client, self.guild_id, self.chemin = client, guild_id, chemin
        self.etat = json.loads(chemin.read_text("utf-8")) if chemin.exists() else {}
        self.vue = BoutonsQG(self)
        self.hook = self.lecture = None
        self.installation = asyncio.Lock()
        self.commande_prete = False

    def enregistrer(self, **valeurs):
        self.etat.update(valeurs)
        tmp = self.chemin.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.etat), encoding="utf-8")
        tmp.replace(self.chemin)

    def demarrer(self):
        self.client.add_view(self.vue)
        self.maintenir.start()

    async def contexte(self, creer=False):
        guild = self.client.get_guild(self.guild_id)
        if guild is None:
            raise RuntimeError("Serveur PN indisponible")
        roles = [r for r in guild.roles if name_key(r.name) == "direction"]
        categories = [c for c in guild.categories if name_key(c.name) == "direction"]
        if len(roles) != 1 or len(categories) != 1:
            raise RuntimeError("Rôle ou catégorie Direction ambigu")
        role, categorie = roles[0], categories[0]
        ident = self.etat.get("salon")
        if ident:
            salon = await guild.fetch_channel(ident)
        else:
            trouves = [c for c in categorie.text_channels if name_key(c.name) == name_key(SALON)]
            if len(trouves) == 1:
                salon = await guild.fetch_channel(trouves[0].id)
            elif creer and not trouves:
                salon = await guild.create_text_channel(SALON, category=categorie,
                    topic="Fréquentation du QG de la Police Nationale et classements Habbo.",
                    overwrites={
                        guild.default_role: discord.PermissionOverwrite(view_channel=False),
                        role: discord.PermissionOverwrite(view_channel=True, read_message_history=True,
                                                         use_application_commands=True),
                        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                            read_message_history=True, embed_links=True, attach_files=True, manage_webhooks=True),
                    }, reason="Suivi privé du QG demandé par la Direction")
            else:
                raise RuntimeError("Salon du suivi introuvable ou ambigu")
        if (not isinstance(salon, discord.TextChannel) or salon.category_id != categorie.id
                or not prive(salon, role, guild.me)):
            raise RuntimeError("Les permissions du suivi ne sont plus limitées à la Direction")
        if not ident:
            self.enregistrer(salon=salon.id)
        return salon, role

    async def autoriser(self, inter):
        if inter.guild_id == self.guild_id and inter.channel_id == self.etat.get("salon"):
            try:
                _, role = await self.contexte()
                if isinstance(inter.user, discord.Member) and role in inter.user.roles:
                    return True
            except (RuntimeError, discord.HTTPException):
                pass
        await inter.response.send_message("Le suivi est réservé à la Direction dans son salon privé.", ephemeral=True)
        return False

    async def panneau(self, salon):
        ident = self.etat.get("panneau")
        if ident:
            try:
                return await salon.fetch_message(ident)
            except discord.NotFound:
                pass
        async for msg in salon.history(limit=30):
            if msg.author.id == self.client.user.id and msg.embeds and msg.embeds[0].title == TITRE:
                self.enregistrer(panneau=msg.id)
                return msg
        carte = discord.Embed(title=TITRE, description="Clique sur **Actualiser** pour lire le navigateur Habbo Classic.",
                              color=0x17476A)
        msg = await salon.send(embed=carte, view=self.vue, allowed_mentions=discord.AllowedMentions.none())
        self.enregistrer(panneau=msg.id)
        return msg

    async def liaison(self, salon):
        hooks = [h for h in await salon.webhooks() if h.name == WEBHOOK and h.type == discord.WebhookType.incoming]
        if len(hooks) == 1:
            hook = hooks[0]
        elif not hooks:
            hook = await salon.create_webhook(name=WEBHOOK, reason="Liaison du navigateur Habbo pour ce salon")
        else:
            raise RuntimeError("Plusieurs liaisons portent le même nom")
        if not hook.token or hook.channel_id != salon.id:
            raise RuntimeError("Liaison du salon indisponible")
        if self.etat.get("hook") != hook.id:
            self.enregistrer(hook=hook.id, url=None, date=0)
        self.hook = hook

    async def annonce(self, message):
        hook = self.hook
        if (not hook or message.webhook_id != hook.id or message.channel.id != self.etat.get("salon")):
            return
        try:
            await self.contexte()
            avis = await hook.fetch_message(message.id)
            valeur = inscription(avis.content, hook.token)
            if valeur is None:
                return
            date, url = valeur
            if date > time.time() + 60 or time.time() - date > 86400:
                return
            if date >= self.etat.get("date", 0):
                self.enregistrer(url=url, date=date)
            await hook.delete_message(message.id)
            if not self.etat.get("premier"):
                await self.actualiser()
        except discord.NotFound:
            pass
        except Exception as exc:
            LOG.warning("Annonce du suivi indisponible (%s)", type(exc).__name__)

    async def obtenir(self):
        url, hook = self.etat.get("url"), self.hook
        if not url or not hook or not HOTE.fullmatch(url):
            return "Le PC de suivi est indisponible. Réessaie quand Habbo Classic est connecté.", None
        nonce, date = secrets.token_hex(16), str(int(time.time()))
        entetes = {"X-PN-Date": date, "X-PN-Nonce": nonce,
                   "X-PN-Signature": signature(hook.token, f"lecture {date} {nonce}".encode())}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=44)) as http:
                async with http.post(url+CHEMIN, headers=entetes, allow_redirects=False) as rep:
                    if rep.status != 200:
                        raise ValueError("PC indisponible")
                    corps = bytearray()
                    async for bloc in rep.content.iter_chunked(65536):
                        corps.extend(bloc)
                        if len(corps) > MAX_REPONSE:
                            raise ValueError("Relevé trop grand")
                    return decoder(bytes(corps), rep.headers.get("X-PN-Signature",""), nonce, hook.token)
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError):
            return "Le PC de suivi ne répond pas. Réessaie quand Habbo Classic est connecté.", None

    async def publier(self):
        texte, image = await self.obtenir()
        # Revérifier le salon après la lecture avant toute publication de l’image.
        async with self.installation:
            salon, _ = await self.contexte()
            message = await self.panneau(salon)
            carte = discord.Embed(title=TITRE, description=texte.removeprefix("🏛 QG · Police Nationale\n")
                                  .removeprefix("QG · Police Nationale\n"), color=0x17476A if image else 0x657787)
            fichiers = []
            if image:
                nom = "navigateur-habbo.png"
                carte.set_image(url="attachment://"+nom)
                fichiers.append(discord.File(io.BytesIO(image), filename=nom, description="Navigateur d’appartements Habbo"))
            await message.edit(embed=carte, attachments=fichiers, view=self.vue,
                               allowed_mentions=discord.AllowedMentions.none())
            if image and not self.etat.get("premier"):
                self.enregistrer(premier=True)
            return message.jump_url

    async def actualiser(self):
        if self.lecture is None or self.lecture.done():
            self.lecture = asyncio.create_task(self.publier())
        return await asyncio.shield(self.lecture)

    async def commande(self, interaction):
        if (interaction.type != discord.InteractionType.application_command
                or not interaction.data or interaction.data.get("name") != "qg"):
            return
        if not await self.autoriser(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            lien = await self.actualiser()
            await interaction.edit_original_response(content=f"[Voir le relevé du QG]({lien})")
        except (RuntimeError, discord.HTTPException):
            await interaction.edit_original_response(content="Le salon ne permet pas de publier le relevé pour le moment.")

    @tasks.loop(minutes=3)
    async def maintenir(self):
        try:
            async with self.installation:
                salon, _ = await self.contexte(creer=True)
                await self.panneau(salon)
                await self.liaison(salon)
            if not self.commande_prete:
                await self.client.http.upsert_guild_command(self.client.application_id, self.guild_id, {
                    "name":"qg","type":1,"default_member_permissions":"8",
                    "description":"Actualiser la fréquentation du QG dans le salon de la Direction."})
                self.commande_prete = True
            async for message in salon.history(limit=15):
                if message.webhook_id == self.hook.id:
                    await self.annonce(message)
        except (RuntimeError, OSError, discord.HTTPException) as exc:
            LOG.warning("Installation du suivi du QG (%s)", str(exc)[:220])

    @maintenir.before_loop
    async def pret(self):
        await self.client.wait_until_ready()
