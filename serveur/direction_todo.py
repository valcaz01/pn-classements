"""Tableau privé de la Direction. La base SQLite vit sur le volume privé du bot."""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks

from habbo_http import ApiRequestError, request_json


LOG = logging.getLogger("pn.direction")
PARIS = ZoneInfo("Europe/Paris")
CHANNEL_NAME = "to-do-list"
PANEL_TITLE = "Tâches de la Direction"
VISIBLE_TITLE = "📋 Tâches de la Direction"
THREAD_NAME = "🔔 Notifications des tâches"
TEMPORARY_MESSAGE_SECONDS = 120
PAGE_SIZE = 8
BLUE = 0x124669
REASON = "Tableau des tâches de la Direction"
PRIORITIES = (("Basse", "⚪"), ("Normale", "🔵"), ("Haute", "🟠"), ("Urgente", "🔴"))
PRIORITY_COLORS = (0x95A5A6, 0x508EE3, 0xE99A35, 0xDB5363)
SELECT_LIMIT = 25


def name_key(name):
    plain = unicodedata.normalize("NFKD", name).casefold()
    return "".join(c for c in plain if c.isascii() and c.isalnum())


def director(member, role_id):
    return isinstance(member, discord.Member) and not member.bot and (
        member.id == member.guild.owner_id or any(r.id == role_id for r in member.roles)
    )


def assignable(member, role_id, channel):
    return (isinstance(member, discord.Member) and not member.bot
            and any(role.id == role_id for role in member.roles)
            and channel.permissions_for(member).view_channel)


class TaskStore:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY, interaction_id INTEGER UNIQUE NOT NULL,
                title TEXT NOT NULL, creator INTEGER NOT NULL, assignee INTEGER NOT NULL,
                created INTEGER NOT NULL, started INTEGER, completed INTEGER, completed_by INTEGER,
                priority INTEGER NOT NULL DEFAULT 1 CHECK (priority BETWEEN 0 AND 3),
                cancelled INTEGER, cancelled_by INTEGER
            );
            CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(tasks)")}
        with self.db:
            for name, declaration in {
                "priority": "INTEGER NOT NULL DEFAULT 1 CHECK (priority BETWEEN 0 AND 3)",
                "cancelled": "INTEGER", "cancelled_by": "INTEGER",
            }.items():
                if name not in columns:
                    self.db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {declaration}")

    def setting(self, name):
        row = self.db.execute("SELECT value FROM settings WHERE name=?", (name,)).fetchone()
        return row[0] if row else None

    def remember(self, name, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (name, str(value)))

    def add(self, interaction_id, title, creator, assignee, now, priority=1):
        if priority not in range(len(PRIORITIES)):
            raise ValueError("Priorité invalide")
        with self.db:
            self.db.execute("""INSERT INTO tasks (interaction_id,title,creator,assignee,created,priority)
                               VALUES (?,?,?,?,?,?) ON CONFLICT(interaction_id) DO NOTHING""",
                            (interaction_id, title, creator, assignee, int(now.timestamp()), priority))
            return self.db.execute("SELECT id FROM tasks WHERE interaction_id=?", (interaction_id,)).fetchone()[0]

    def page(self, assignee=None, page=0):
        clause = "completed IS NULL AND cancelled IS NULL"
        params = ()
        if assignee is not None:
            clause += " AND assignee=?"
            params = (assignee,)
        total = self.db.execute("SELECT count(*) FROM tasks WHERE " + clause, params).fetchone()[0]
        page = max(0, min(page, (total - 1) // PAGE_SIZE))
        rows = self.db.execute("SELECT * FROM tasks WHERE " + clause + " ORDER BY priority DESC, id LIMIT ? OFFSET ?",
                               (*params, PAGE_SIZE, page * PAGE_SIZE)).fetchall()
        return rows, total, page

    def open_tasks(self):
        return self.db.execute("""SELECT * FROM tasks WHERE completed IS NULL AND cancelled IS NULL
                                  ORDER BY priority DESC, id""").fetchall()

    def participant_ids(self):
        return {row[0] for row in self.db.execute("SELECT creator FROM tasks UNION SELECT assignee FROM tasks")}

    def history(self, page=0):
        clause = "completed IS NOT NULL OR cancelled IS NOT NULL"
        total = self.db.execute("SELECT count(*) FROM tasks WHERE " + clause).fetchone()[0]
        page = max(0, min(page, (total - 1) // PAGE_SIZE))
        rows = self.db.execute("SELECT * FROM tasks WHERE " + clause +
            " ORDER BY coalesce(completed, cancelled) DESC, id DESC LIMIT ? OFFSET ?",
            (PAGE_SIZE, page * PAGE_SIZE)).fetchall()
        return rows, total, page

    def finish(self, task_id, member_id, now):
        with self.db:
            updated = self.db.execute("""UPDATE tasks SET completed=?, completed_by=?
                WHERE id=? AND completed IS NULL AND cancelled IS NULL AND (assignee=? OR creator=?)""",
                                      (int(now.timestamp()), member_id, task_id, member_id, member_id))
            return updated.rowcount == 1

    def start(self, task_id, member_id, now):
        with self.db:
            return self.db.execute("""UPDATE tasks SET started=? WHERE id=? AND assignee=?
                                      AND started IS NULL AND completed IS NULL AND cancelled IS NULL""",
                                   (int(now.timestamp()), task_id, member_id)).rowcount == 1

    def cancel(self, task_id, member_id, now):
        with self.db:
            return self.db.execute("""UPDATE tasks SET cancelled=?, cancelled_by=?
                WHERE id=? AND completed IS NULL AND cancelled IS NULL""",
                (int(now.timestamp()), member_id, task_id)).rowcount == 1

    def reminder_counts(self, now):
        cutoff = int(now.timestamp()) - 24 * 3600
        return self.db.execute("""SELECT assignee, count(*) AS number FROM tasks
            WHERE completed IS NULL AND cancelled IS NULL AND created<=?
            GROUP BY assignee ORDER BY assignee""", (cutoff,)).fetchall()


def list_embeds(rows, total, page, title, archived=False):
    header = discord.Embed(title=title, color=BLUE)
    if not total:
        header.description = "Aucune tâche terminée ou annulée." if archived else "Aucune tâche en attente."
    else:
        noun = "tâche" if total == 1 else "tâches"
        scope = "dans l’historique" if archived else ("ouverte" if total == 1 else "ouvertes")
        header.description = f"**{total}** {noun} {scope}"
        if total > PAGE_SIZE:
            header.set_footer(text=f"Page {page + 1} sur {(total + PAGE_SIZE - 1) // PAGE_SIZE}")
    embeds = [header]
    for row in rows:
        label = discord.utils.escape_markdown(discord.utils.escape_mentions(row["title"]))
        priority, emoji = PRIORITIES[row["priority"]]
        card = discord.Embed(color=PRIORITY_COLORS[row["priority"]])
        if len(label.encode("utf-16-le")) <= 512:  # Un titre Discord accepte 256 unités UTF-16.
            card.title = label
        else:
            card.description = f"**{label}**"
        card.add_field(name="Responsable", value=f'<@{row["assignee"]}>')
        state = "En cours" if row["started"] is not None else "À faire"
        if archived:
            closed = "completed" if row["completed"] is not None else "cancelled"
            state = "Terminée" if closed == "completed" else "Annulée"
        card.add_field(name="Avancement", value=state)
        if archived:
            stamp = datetime.fromtimestamp(row[closed], PARIS).strftime("%d/%m/%Y à %H:%M")
            card.add_field(name="Clôture", value=f'<@{row[closed + "_by"]}> · {stamp} (Paris)', inline=False)
        card.set_footer(text=f'{emoji} Priorité {priority.lower()}  ·  Tâche #{row["id"]}')
        embeds.append(card)
    return embeds


class AddTask(discord.ui.Modal, title="Ajouter une tâche"):
    def __init__(self, board, assignee):
        super().__init__(timeout=600)
        self.board = board
        self.assignee_id = assignee.id
        self.task = discord.ui.TextInput(max_length=140, placeholder="Ex. Relire la notice des patrouilles")
        self.priority = discord.ui.Select(placeholder="Choisir la priorité", options=[
            discord.SelectOption(label=label, value=str(level), emoji=emoji, default=level == 1)
            for level, (label, emoji) in enumerate(PRIORITIES)])
        self.add_item(discord.ui.Label(text="Tâche à faire", component=self.task,
                                       description=f"Responsable · {assignee.display_name}"))
        self.add_item(discord.ui.Label(text="Priorité de la tâche", component=self.priority))

    async def on_submit(self, interaction):
        if not await self.board.authorize(interaction):
            return
        title = " ".join(self.task.value.split())
        if not title:
            await interaction.response.send_message("Indique la tâche à faire.", ephemeral=True,
                                                    delete_after=TEMPORARY_MESSAGE_SECONDS)
            return
        priority = self.priority.values[0] if self.priority.values else "1"
        if priority not in {str(level) for level in range(len(PRIORITIES))}:
            await interaction.response.send_message("Choisis une priorité dans la liste.", ephemeral=True,
                                                    delete_after=TEMPORARY_MESSAGE_SECONDS)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            member = await interaction.guild.fetch_member(self.assignee_id)
        except discord.NotFound:
            member = None
        except discord.HTTPException:
            reply = await interaction.edit_original_response(content="Discord n’a pas pu vérifier le membre. Réessaie dans un instant.")
            await reply.delete(delay=TEMPORARY_MESSAGE_SECONDS)
            return
        if not assignable(member, self.board.role_id, interaction.channel):
            reply = await interaction.edit_original_response(content="Ce membre ne fait plus partie de la Direction ou n’a plus accès au salon. Choisis un autre responsable.")
            await reply.delete(delay=TEMPORARY_MESSAGE_SECONDS)
            return
        task_id = self.board.store.add(interaction.id, title, interaction.user.id, member.id,
                                       datetime.now(PARIS), int(priority))
        reply = await interaction.edit_original_response(content=f"Tâche **#{task_id}** attribuée à {member.mention}.")
        await reply.delete(delay=TEMPORARY_MESSAGE_SECONDS)
        await self.board.refresh()


class MemberSearch(discord.ui.Modal, title="Rechercher dans la Direction"):
    name_prefix = discord.ui.Label(text="Début du pseudo ou du surnom",
                                   component=discord.ui.TextInput(max_length=32))

    def __init__(self, board, previous_reply):
        super().__init__(timeout=600)
        self.board = board
        self.previous_reply = previous_reply

    async def on_submit(self, interaction):
        if await self.board.authorize(interaction):
            await self.board.show_assignees(interaction, self.name_prefix.component.value.strip().lstrip("@"))
            if self.previous_reply is not None:
                try:
                    await self.previous_reply.delete()
                except discord.HTTPException:
                    pass


class AssigneePicker(discord.ui.View):
    def __init__(self, board, owner_id, members, page=0):
        super().__init__(timeout=TEMPORARY_MESSAGE_SECONDS)
        self.board, self.owner_id, self.members, self.page = board, owner_id, members, page
        self.reply = None
        choices = members[page * SELECT_LIMIT:(page + 1) * SELECT_LIMIT]
        if choices:
            select = discord.ui.Select(placeholder="Choisir le responsable", row=0, options=[
                discord.SelectOption(label=m.display_name, description=f"@{m.name}", value=str(m.id))
                for m in choices])
            by_id = {str(m.id): m for m in choices}

            async def choose(interaction):
                member = by_id.get(select.values[0])
                if member is None:
                    await interaction.response.send_message("Choisis un responsable dans la liste.", ephemeral=True,
                                                            delete_after=TEMPORARY_MESSAGE_SECONDS)
                    return
                await interaction.response.send_modal(AddTask(board, member))
                if self.reply is not None:
                    try:
                        await self.reply.delete()
                    except discord.HTTPException:
                        pass  # La réponse éphémère a également un délai d’expiration.

            select.callback = choose
            self.add_item(select)
        if len(members) <= SELECT_LIMIT:
            self.remove_item(self.previous)
            self.remove_item(self.next_page)
        else:
            self.previous.disabled = page == 0
            self.next_page.disabled = (page + 1) * SELECT_LIMIT >= len(members)

    async def interaction_check(self, interaction):
        return interaction.user.id == self.owner_id and await self.board.authorize(interaction)

    async def show(self, interaction, page):
        view = AssigneePicker(self.board, self.owner_id, self.members, page)
        view.reply = self.reply
        await interaction.response.edit_message(view=view, delete_after=TEMPORARY_MESSAGE_SECONDS)

    @discord.ui.button(label="Précédent", row=1)
    async def previous(self, interaction, button):
        await self.show(interaction, self.page - 1)

    @discord.ui.button(label="Suivant", row=1)
    async def next_page(self, interaction, button):
        await self.show(interaction, self.page + 1)

    @discord.ui.button(label="Rechercher par pseudo", emoji="🔎", row=1)
    async def search(self, interaction, button):
        await interaction.response.send_modal(MemberSearch(self.board, self.reply))


class TaskList(discord.ui.View):
    def __init__(self, board, member_id, personal, page=0, history=False):
        super().__init__(timeout=TEMPORARY_MESSAGE_SECONDS)
        self.board, self.member_id, self.personal = board, member_id, personal
        self.history = history
        if history:
            rows, total, self.page = board.store.history(page)
        else:
            rows, total, self.page = board.store.page(member_id if personal else None, page)
        title = "Historique des tâches" if history else "Mes tâches" if personal else "Toutes les tâches"
        self.embeds = list_embeds(rows, total, self.page, title, archived=history)
        if not history:
            self.add_action("Prendre en charge une tâche", 0, board.store.start,
                            [r for r in rows if member_id == r["assignee"] and r["started"] is None])
            self.add_action("Marquer une tâche comme terminée", 1, board.store.finish,
                            [r for r in rows if member_id in (r["assignee"], r["creator"])])
            self.add_action("Annuler une tâche", 2, board.store.cancel, rows)
        self.previous.disabled = self.page == 0
        self.next_page.disabled = (self.page + 1) * PAGE_SIZE >= total
        if total <= PAGE_SIZE:
            self.remove_item(self.previous)
            self.remove_item(self.next_page)

    def add_action(self, label, row, action, tasks):
        if not tasks:
            return
        options = [discord.SelectOption(label=f'#{r["id"]} · {r["title"]}'[:100], value=str(r["id"]),
                                        emoji=PRIORITIES[r["priority"]][1]) for r in tasks]
        select = discord.ui.Select(placeholder=label, options=options, row=row)
        async def update(interaction):
            changed = action(int(select.values[0]), interaction.user.id, datetime.now(PARIS))
            await self.show(interaction, self.page)
            if changed:
                await self.board.refresh()
        select.callback = update
        self.add_item(select)

    async def interaction_check(self, interaction):
        return interaction.user.id == self.member_id and await self.board.authorize(interaction)

    async def show(self, interaction, page):
        view = TaskList(self.board, self.member_id, self.personal, page, history=self.history)
        await interaction.response.edit_message(embeds=view.embeds, view=view if view.children else None,
                                                delete_after=TEMPORARY_MESSAGE_SECONDS)

    @discord.ui.button(label="Précédent", row=3)
    async def previous(self, interaction, button):
        await self.show(interaction, self.page - 1)

    @discord.ui.button(label="Suivant", row=3)
    async def next_page(self, interaction, button):
        await self.show(interaction, self.page + 1)


class CreditBonusModal(discord.ui.Modal, title="Donner 5 crédits au QG"):
    pseudo = discord.ui.TextInput(label="Pseudo Habbo FR", max_length=32, required=True)

    def __init__(self, board):
        super().__init__(timeout=180)
        self.board = board

    async def on_submit(self, interaction):
        if not await self.board.authorize(interaction):
            return
        pseudo = self.pseudo.value.strip()
        if not pseudo or any(character.isspace() or not character.isprintable() for character in pseudo):
            await interaction.response.send_message("Saisis un pseudo Habbo sans espaces.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        query = urllib.parse.urlencode({"name": pseudo})
        request = urllib.request.Request(f"https://www.habbo.fr/api/public/users?{query}",
                                         headers={"User-Agent": "PN-RPG/1.0", "Accept": "application/json"})
        try:
            profile = await asyncio.to_thread(request_json, request)
        except ApiRequestError:
            await interaction.edit_original_response(content="Habbo ne répond pas. Réessaie plus tard.")
            return

        if profile is None:
            content = "Ce pseudo est introuvable sur Habbo FR. Vérifie son orthographe."
        elif (isinstance(profile, dict) and isinstance(profile.get("uniqueId"), str) and profile["uniqueId"]
              and isinstance(profile.get("name"), str)
              and profile["name"].casefold() == pseudo.casefold()):
            name = discord.utils.escape_markdown(discord.utils.escape_mentions(profile["name"]))
            status = "preview"
            if self.board.credit_bonus is not None:
                status = await asyncio.to_thread(self.board.credit_bonus.issue, interaction.id,
                                                 interaction.user.id, profile["uniqueId"], profile["name"])
            content = {
                "preview": f"**Mode test.** Pseudo vérifié : **{name}**. Le retrait de 5 crédits attend l’activation au QG.",
                "issued": f"**5 crédits sont à retirer au QG** par **{name}** au point de retrait des bonus.",
                "pending": f"**{name}** a déjà 5 crédits à retirer au QG.",
                "duplicate": "Cette demande a déjà été enregistrée.",
                "unregistered": "Habbo a trouvé ce pseudo, mais le QG ne connaît pas encore ce membre. Fais-le passer au QG.",
                "writing": "Une attribution est à vérifier. Vérifie le droit de retrait au QG avant de réessayer.",
                "uncertain": "Habbo n’a pas confirmé l’écriture. Vérifie le droit de retrait au QG avant de réessayer.",
            }.get(status, "Le QG n’a pas accepté la demande. Aucun versement ne peut être confirmé.")
        else:
            content = "Habbo n’a pas confirmé le pseudo exact. Réessaie plus tard."
        await interaction.edit_original_response(content=content, allowed_mentions=discord.AllowedMentions.none())


class BoardButtons(discord.ui.View):
    def __init__(self, board):
        super().__init__(timeout=None)
        self.board = board

    async def interaction_check(self, interaction):
        return await self.board.authorize(interaction)

    @discord.ui.button(label="Ajouter une tâche", custom_id="pn:direction:add", style=discord.ButtonStyle.success)
    async def add(self, interaction, button):
        await self.board.show_assignees(interaction)

    async def show(self, interaction, personal, history=False):
        view = TaskList(self.board, interaction.user.id, personal, history=history)
        options = {"view": view} if view.children else {}
        await interaction.response.send_message(embeds=view.embeds, ephemeral=True,
                                               delete_after=TEMPORARY_MESSAGE_SECONDS, **options)

    @discord.ui.button(label="Mes tâches", custom_id="pn:direction:mine", style=discord.ButtonStyle.primary)
    async def mine(self, interaction, button):
        await self.show(interaction, True)

    @discord.ui.button(label="Toutes les tâches", custom_id="pn:direction:all")
    async def all_tasks(self, interaction, button):
        await self.show(interaction, False)

    @discord.ui.button(label="Historique", custom_id="pn:direction:history")
    async def history(self, interaction, button):
        await self.show(interaction, False, history=True)

    @discord.ui.button(label="Donner 5 crédits", custom_id="pn:direction:credit-preview",
                       emoji="🪙", row=1)
    async def credit_preview(self, interaction, button):
        await interaction.response.send_modal(CreditBonusModal(self.board))


class DirectionBoard:
    def __init__(self, client, guild_id, reference_id, path, *, credit_bonus=None):
        self.client, self.guild_id, self.reference_id = client, guild_id, reference_id
        self.path, self.store = path, None
        self.credit_bonus = credit_bonus
        self.buttons = BoardButtons(self)
        self.lock = asyncio.Lock()
        self.role_id = None
        self.channel_id = None
        self.visibility_task = None
        self.thread_notice_cleanup = None

    def start(self):
        self.client.add_view(self.buttons)
        self.maintain.start()

    async def authorize(self, interaction):
        if (interaction.guild_id == self.guild_id and interaction.channel_id == self.channel_id
                and self.role_id and director(interaction.user, self.role_id)):
            return True
        await interaction.response.send_message("Ce tableau est réservé à la Direction dans son salon privé.",
                                               ephemeral=True, delete_after=TEMPORARY_MESSAGE_SECONDS)
        return False

    async def assignees(self, interaction, prefix):
        guild, channel = interaction.guild, interaction.channel
        role = guild.get_role(self.role_id)
        if role is None:
            return []
        if self.client.intents.members and guild.chunked:
            members = role.members
            if prefix:
                members = [m for m in members if any(name.casefold().startswith(prefix.casefold())
                           for name in (m.display_name, m.global_name or "", m.name))]
        elif prefix:
            members = await guild.query_members(query=prefix, limit=100, cache=False)
        else:
            ids = self.store.participant_ids() | {interaction.user.id, guild.owner_id} | {m.id for m in role.members}
            # Le bot peut fonctionner sans l’intent privilégié des membres.
            # Les collègues actifs dans les discussions privées sont aussi proposés au premier usage.
            discussion = next((c for c in channel.category.text_channels if name_key(c.name) == "discussions"), None)
            if discussion is not None and not discussion.permissions_for(guild.default_role).view_channel:
                try:
                    async for message in discussion.history(limit=100):
                        if not message.author.bot:
                            ids.add(message.author.id)
                except discord.HTTPException:
                    LOG.info("Historique des discussions de la Direction indisponible.")
            ids = sorted(uid for uid in ids if uid is not None)
            members = []
            for offset in range(0, len(ids), 100):
                members.extend(await guild.query_members(user_ids=ids[offset:offset + 100], limit=100, cache=False))
        return sorted((m for m in members if assignable(m, self.role_id, channel)),
                      key=lambda m: (m.display_name.casefold(), m.name.casefold(), m.id))

    async def show_assignees(self, interaction, prefix=None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            members = await self.assignees(interaction, prefix)
            prompt = "**À qui confier cette tâche ?**"
            if not members:
                prompt = ("Aucun membre de la Direction trouvé. Essaie un autre début de pseudo."
                          if prefix else "Recherche le pseudo d’un membre de la Direction.")
        except (discord.HTTPException, asyncio.TimeoutError):
            members = []
            prompt = "Discord n’a pas pu charger les membres de la Direction. Relance la recherche."
        view = AssigneePicker(self, interaction.user.id, members)
        view.reply = await interaction.edit_original_response(content=prompt, view=view)
        await view.reply.delete(delay=TEMPORARY_MESSAGE_SECONDS)

    async def channel(self):
        guild = self.client.get_guild(self.guild_id)
        if not guild:
            raise RuntimeError("Serveur PN indisponible.")
        roles = [r for r in guild.roles if name_key(r.name) == "direction"]
        reference = guild.get_channel(self.reference_id)
        category = getattr(reference, "category", None)
        if category is None or name_key(category.name) != "direction":
            categories = [c for c in guild.categories if name_key(c.name) == "direction"]
            category = categories[0] if len(categories) == 1 else None
        if len(roles) != 1 or category is None:
            raise RuntimeError("Rôle ou catégorie Direction introuvable ou ambigu.")
        role, me = roles[0], guild.me
        channels = [c for c in category.text_channels if name_key(c.name) == name_key(CHANNEL_NAME)]
        if len(channels) > 1:
            raise RuntimeError("Plusieurs tableaux portent le même nom dans Direction.")
        if channels:
            channel = channels[0]
        else:
            if not category.permissions_for(me).manage_channels:
                raise RuntimeError("Le bot ne peut pas créer to-do-list dans Direction.")
            channel = await guild.create_text_channel(CHANNEL_NAME, category=category, reason=REASON,
                topic="Tâches de la Direction · Ajouter, attribuer et suivre les tâches.",
                overwrites={
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    role: discord.PermissionOverwrite(view_channel=True, read_message_history=True),
                    me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                                    read_message_history=True, create_public_threads=True,
                                                    send_messages_in_threads=True),
                })
        access = channel.permissions_for(role)
        if channel.permissions_for(guild.default_role).view_channel or not (access.view_channel and access.read_message_history):
            raise RuntimeError("La visibilité du tableau ne correspond pas à la Direction.")
        # Une permission explicite à un rôle ordinaire ne doit pas exposer les tâches.
        for other in guild.roles:
            if other != role and not other.permissions.administrator and not other.is_bot_managed():
                if channel.permissions_for(other).view_channel:
                    raise RuntimeError("Un autre rôle peut lire le salon du tableau.")
        for target, overwrite in channel.overwrites.items():
            if overwrite.view_channel is not True or isinstance(target, discord.Role) or target.id == me.id:
                continue
            if isinstance(target, discord.Object) and target.type == discord.Role:
                raise RuntimeError("Accès du tableau accordé à un rôle inconnu.")
            try:
                member = target if isinstance(target, discord.Member) else await guild.fetch_member(target.id)
            except discord.NotFound:
                continue
            if not (director(member, role.id) or member.guild_permissions.administrator):
                raise RuntimeError("Une permission individuelle rend le tableau accessible hors Direction.")
        perms = channel.permissions_for(me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links and perms.read_message_history):
            raise RuntimeError("Le bot a besoin de voir le salon, lire son historique, envoyer et intégrer des liens.")
        self.role_id, self.channel_id = role.id, channel.id
        return channel

    async def panel_message(self, channel, embeds, setting, buttons=None, after_id=None):
        title = embeds[0].title
        message = old = None
        saved = self.store.setting(setting)
        if saved:
            try:
                message = await channel.fetch_message(int(saved))
            except discord.NotFound:
                pass
        def matches(candidate):
            return candidate.author.id == self.client.user.id and candidate.embeds and candidate.embeds[0].title == title

        if message is not None and not matches(message):
            raise RuntimeError("Le message enregistré ne correspond pas au tableau de la Direction.")
        if message is not None and after_id is not None and message.id <= after_id:
            old, message = message, None
        if message is None:
            history = (channel.history(after=discord.Object(id=after_id), oldest_first=False, limit=None)
                       if after_id is not None else channel.history(limit=50))
            async for recent in history:
                if matches(recent) and (after_id is None or recent.id > after_id):
                    message = recent
                    break
        if message is None:
            message = await channel.send(embeds=embeds, view=buttons, silent=True)
        elif (message.embeds != embeds or bool(message.components) != bool(buttons)
                or (buttons is not None and [c.to_dict() for c in message.components] != buttons.to_components())):
            await message.edit(embeds=embeds, view=buttons)
        if old is not None:
            try:
                await old.delete()
            except discord.NotFound:
                pass
        if saved != str(message.id):
            self.store.remember(setting, message.id)
        return message

    async def discard_page(self, channel, key, title):
        saved = self.store.setting(key)
        if saved:
            try:
                message = await channel.fetch_message(int(saved))
                if (message.author.id != self.client.user.id or not message.embeds
                        or message.embeds[0].title != title):
                    raise RuntimeError("Le message enregistré ne correspond pas à la page du tableau.")
                await message.delete()
            except discord.NotFound:
                pass
            self.store.remember(key, "")

    async def panel(self, channel, *, after_id=None, link_to=None):
        prefix, heading = ("panel", PANEL_TITLE) if after_id is None else ("visible", VISIBLE_TITLE)
        rows = [] if link_to else self.store.open_tasks()
        total = len(rows)
        started = sum(row["started"] is not None for row in rows)
        count = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        count_key = f"{prefix}_count"
        previous_count = int(self.store.setting(count_key) or 1)
        message = None
        for page in range(count):
            title = heading if page == 0 else f"{heading} · suite {page + 1}"
            embeds = list_embeds(rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE], total, page, title)
            if link_to:
                destination = f"https://discord.com/channels/{self.guild_id}/{channel.id}/{link_to}"
                embeds[0].description = f"[Ouvrir le tableau à jour ↗]({destination})"
            elif total:
                embeds[0].description = f"**{total - started}** à faire　·　**{started}** en cours"
            else:
                embeds[0].description = "Aucune tâche en attente. Utilise **Ajouter une tâche** pour en attribuer une."
            thread_id = self.store.setting("notification_thread")
            if thread_id:
                embeds[0].description += f"　·　[🔔 Notifications](https://discord.com/channels/{self.guild_id}/{thread_id})"
            if page + 1 > previous_count:
                self.store.remember(count_key, page + 1)
            part = await self.panel_message(channel, embeds, prefix if page == 0 else f"{prefix}_{page + 1}",
                                            self.buttons if page == 0 and not link_to else None, after_id)
            if page == 0:
                message = part
        for page in range(count, previous_count):
            await self.discard_page(channel, f"{prefix}_{page + 1}", f"{heading} · suite {page + 1}")
        self.store.remember(count_key, count)
        if after_id is None and not message.pinned and channel.permissions_for(channel.guild.me).pin_messages:
            try:
                await message.pin(reason=REASON)
            except discord.Forbidden:
                LOG.info("Le tableau reste accessible dans le salon sans épinglage.")
        return message

    async def keep_visible(self, channel, panel):
        # Le lien de l’épingle reste permanent, même quand les cartes suivent une discussion.
        previous = self.store.setting("visible")
        async for recent in channel.history(after=panel, limit=None, oldest_first=False):
            if recent.id <= panel.id or recent.type not in (discord.MessageType.default, discord.MessageType.reply):
                continue
            if recent.author.id == self.client.user.id and recent.embeds:
                title = recent.embeds[0].title or ""
                if any(title == root or title.startswith(root + " · suite ") for root in (PANEL_TITLE, VISIBLE_TITLE)):
                    continue
            visible = await self.panel(channel, after_id=recent.id)
            if previous != str(visible.id):
                await self.panel(channel, link_to=visible.id)
            return
        if previous:
            await self.panel(channel)
            count = int(self.store.setting("visible_count") or 1)
            for page in range(count, 1, -1):
                await self.discard_page(channel, f"visible_{page}", f"{VISIBLE_TITLE} · suite {page}")
            await self.discard_page(channel, "visible", VISIBLE_TITLE)
            self.store.remember("visible_count", 0)

    async def notification_thread(self, panel):
        thread = panel.thread
        if thread is None:
            try:
                thread = await panel.fetch_thread()
            except discord.NotFound:
                try:
                    thread = await panel.create_thread(name=THREAD_NAME, reason=REASON)
                except discord.HTTPException as error:
                    if error.code != 160004:  # Un fil a déjà été créé sur ce message.
                        raise
                    thread = await panel.fetch_thread()
        if thread.parent_id != panel.channel.id or thread.type != discord.ChannelType.public_thread:
            raise RuntimeError("Le fil de notifications ne correspond pas au salon privé du tableau.")
        return thread

    async def clear_thread_creation_notice(self, channel, thread):
        # Le message système du salon et le fil sont deux objets Discord distincts.
        lifetime = timedelta(seconds=TEMPORARY_MESSAGE_SECONDS)
        created = thread.created_at or discord.utils.snowflake_time(thread.id)
        await discord.utils.sleep_until(created + lifetime)
        before = created + lifetime if thread.created_at else None
        try:
            async for msg in channel.history(after=created - timedelta(seconds=5), before=before,
                                             oldest_first=True, limit=None):
                if (msg.type != discord.MessageType.thread_created or msg.author.id != self.client.user.id
                        or msg.reference is None or msg.reference.channel_id != thread.id):
                    continue
                await discord.utils.sleep_until(msg.created_at + lifetime)
                try:
                    await msg.delete()
                except discord.NotFound:
                    pass
                break
            self.store.remember("cleared_thread_notice", thread.id)
        except discord.HTTPException:
            LOG.exception("Nettoyage de l’annonce de création du fil reporté")

    async def archive_public_notices(self, channel, thread, panel):
        if self.store.setting("archived_public_notices") == str(thread.id):
            return
        notices = []
        async for msg in channel.history(after=panel, limit=None, oldest_first=True):
            if (msg.author.id != self.client.user.id or len(msg.embeds) != 1 or msg.pinned or msg.reactions
                    or msg.attachments or len(msg.raw_mentions) != 1):
                continue
            title = msg.embeds[0].title or ""
            if re.fullmatch(r"Nouvelle tâche #\d+|Rappel du \d{2}/\d{2}/\d{4}", title):
                notices.append(msg)
        if notices:
            copies = {}
            async for copy in thread.history(limit=None):
                if (copy.author.id == self.client.user.id and copy.reference and
                        copy.reference.type == discord.MessageReferenceType.forward and copy.flags.forwarded):
                    copies[copy.reference.message_id] = copy
            for msg in notices:
                copy = copies.get(msg.id)
                if copy is None:
                    copy = await thread.send(reference=msg.to_reference(type=discord.MessageReferenceType.forward),
                                             silent=True, allowed_mentions=discord.AllowedMentions.none())
                # Discord atteste la copie immuable par le drapeau et la référence au message source.
                if not (copy.flags.forwarded and copy.reference and copy.reference.message_id == msg.id
                        and copy.reference.type == discord.MessageReferenceType.forward):
                    raise RuntimeError("Discord n’a pas confirmé la copie de l’avis dans le fil.")
                title = msg.embeds[0].title
                if title.startswith("Nouvelle tâche #"):
                    self.store.remember(f'assigned_{title.split("#")[1]}', copy.id)
                else:
                    key = f"reminded_{msg.raw_mentions[0]}"
                    day = datetime.strptime(title, "Rappel du %d/%m/%Y").date().isoformat()
                    if day > (self.store.setting(key) or ""):
                        self.store.remember(key, day)
                try:
                    await msg.delete()
                except discord.NotFound:
                    pass
        self.store.remember("archived_public_notices", thread.id)

    def schedule_visibility(self, message):
        if (message.channel.id == self.channel_id and message.author.id != self.client.user.id
                and message.type in (discord.MessageType.default, discord.MessageType.reply)
                and (self.visibility_task is None or self.visibility_task.done())):
            self.visibility_task = asyncio.create_task(self.refresh_after_discussion())

    async def refresh_after_discussion(self):
        await asyncio.sleep(5)
        try:
            await self.refresh()
        except Exception:
            LOG.exception("Actualisation du tableau après un message reportée")

    async def notify_assignments(self, channel, panel):
        pending = [row for row in self.store.open_tasks()
                   if not self.store.setting(f'assigned_{row["id"]}')]
        if not pending:
            return
        delivered = {}
        async for recent in channel.history(limit=50):
            if recent.author.id == self.client.user.id:
                for embed in recent.embeds:
                    delivered[embed.title] = recent.id
        for row in pending:
            setting = f'assigned_{row["id"]}'
            title = f'Nouvelle tâche #{row["id"]}'
            if title in delivered:
                self.store.remember(setting, delivered[title])
                continue
            try:
                member = await channel.guild.fetch_member(row["assignee"])
            except discord.NotFound:
                continue
            if not director(member, self.role_id) or not channel.permissions_for(member).view_channel:
                continue
            label = discord.utils.escape_markdown(discord.utils.escape_mentions(row["title"]))
            priority, emoji = PRIORITIES[row["priority"]]
            embed = discord.Embed(title=title, color=BLUE,
                description=f"{label}\n{emoji} Priorité : {priority}\n\n[Ouvrir le tableau]({panel.jump_url})")
            notice = await channel.send(f"{member.mention}, une tâche t’a été attribuée.", embed=embed,
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[member], replied_user=False))
            self.store.remember(setting, notice.id)

    async def remind(self, channel, panel, now):
        day = now.date().isoformat()
        if now.hour < 18:
            return
        pending = [r for r in self.store.reminder_counts(now)
                   if self.store.setting(f'reminded_{r["assignee"]}') != day]
        if not pending:
            return
        marker = f"Rappel du {now:%d/%m/%Y}"
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        already_sent = set()
        async for recent in channel.history(after=midnight, limit=None):
            if recent.author.id == self.client.user.id and any(e.title == marker for e in recent.embeds):
                already_sent.update(recent.raw_mentions)
        for row in pending:
            user_id, count = row["assignee"], row["number"]
            if user_id not in already_sent:
                try:
                    member = await channel.guild.fetch_member(user_id)
                except discord.NotFound:
                    member = None
                if director(member, self.role_id) and channel.permissions_for(member).view_channel:
                    qty = "1 tâche t’attend" if count == 1 else f"{count} tâches t’attendent"
                    embed = discord.Embed(title=marker, color=BLUE, description=(
                        f"{qty} depuis au moins 24 h. [Ouvre le tableau]({panel.jump_url}) et choisis **Mes tâches**."
                    ))
                    nonce = f"{user_id:x}{now:%y%m%d}"
                    await channel.send(member.mention, embed=embed, nonce=nonce, allowed_mentions=discord.AllowedMentions(
                        everyone=False, roles=False, users=[member], replied_user=False))
            self.store.remember(f"reminded_{user_id}", day)

    async def refresh(self):
        async with self.lock:
            self.channel_id = None
            if self.store is None:
                self.store = TaskStore(self.path)
            channel = await self.channel()
            message = await self.panel(channel, link_to=self.store.setting("visible"))
            try:
                thread = await self.notification_thread(message)
                if self.store.setting("notification_thread") != str(thread.id):
                    self.store.remember("notification_thread", thread.id)
                    message = await self.panel(channel, link_to=self.store.setting("visible"))
                if (self.store.setting("cleared_thread_notice") != str(thread.id)
                        and (self.thread_notice_cleanup is None or self.thread_notice_cleanup.done())):
                    self.thread_notice_cleanup = asyncio.create_task(self.clear_thread_creation_notice(channel, thread))
                try:
                    await self.archive_public_notices(channel, thread, message)
                except (discord.HTTPException, RuntimeError):
                    LOG.exception("Archivage des anciens avis reporté")
            finally:
                await self.keep_visible(channel, message)
            await self.notify_assignments(thread, message)
            await self.remind(thread, message, datetime.now(PARIS))

    @tasks.loop(minutes=5)
    async def maintain(self):
        try:
            await self.refresh()
        except Exception:
            LOG.exception("Tableau de la Direction temporairement indisponible")

    @maintain.before_loop
    async def wait_ready(self):
        await self.client.wait_until_ready()
