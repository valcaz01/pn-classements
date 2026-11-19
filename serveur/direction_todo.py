"""Tableau privé de la Direction. La base SQLite vit sur le volume privé du bot."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks


LOG = logging.getLogger("pn.direction")
PARIS = ZoneInfo("Europe/Paris")
CHANNEL_NAME = "to-do-list"
PANEL_TITLE = "Tâches de la Direction"
PAGE_SIZE = 8
BLUE = 0x124669
REASON = "Tableau des tâches de la Direction"
PRIORITIES = (("Basse", "⚪"), ("Normale", "🔵"), ("Haute", "🟠"), ("Urgente", "🔴"))


def name_key(name):
    plain = unicodedata.normalize("NFKD", name).casefold()
    return "".join(c for c in plain if c.isascii() and c.isalnum())


def director(member, role_id):
    return isinstance(member, discord.Member) and not member.bot and (
        member.id == member.guild.owner_id or any(r.id == role_id for r in member.roles)
    )


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


def list_embed(rows, total, page, title, archived=False):
    lines = []
    for row in rows:
        label = discord.utils.escape_markdown(discord.utils.escape_mentions(row["title"]))
        state = "En cours" if row["started"] is not None else "À faire"
        if row["completed"] is not None or row["cancelled"] is not None:
            closed = "completed" if row["completed"] is not None else "cancelled"
            verb = "✅ Terminée" if closed == "completed" else "🚫 Annulée"
            stamp = datetime.fromtimestamp(row[closed], PARIS).strftime("%d/%m/%Y à %H:%M")
            state = f'{verb} par <@{row[closed + "_by"]}> le {stamp} (Paris)'
        priority, emoji = PRIORITIES[row["priority"]]
        lines.append(f'**#{row["id"]}** {label}\n{emoji} {priority} · Responsable : <@{row["assignee"]}>\n{state}')
    text = "\n\n".join(lines) or ("Aucune tâche terminée ou annulée." if archived else "Aucune tâche en attente.")
    embed = discord.Embed(title=title, description=text, color=BLUE)
    if total:
        pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        scope = "dans l’historique" if archived else "en attente"
        embed.set_footer(text=f"{total} tâche{'s' if total > 1 else ''} {scope} · Page {page + 1}/{pages}")
    return embed


class AddTask(discord.ui.Modal, title="Ajouter une tâche"):
    def __init__(self, board):
        super().__init__(timeout=600)
        self.board = board
        self.task = discord.ui.TextInput(max_length=140, placeholder="Ex. Relire la notice des patrouilles")
        self.person = discord.ui.UserSelect(required=True, placeholder="Choisir un membre")
        self.priority = discord.ui.Select(placeholder="Choisir la priorité", options=[
            discord.SelectOption(label=label, value=str(level), emoji=emoji, default=level == 1)
            for level, (label, emoji) in enumerate(PRIORITIES)])
        self.add_item(discord.ui.Label(text="Tâche à faire", component=self.task))
        self.add_item(discord.ui.Label(text="À qui confier cette tâche ?", component=self.person,
                                       description="Choisis un membre de la Direction."))
        self.add_item(discord.ui.Label(text="Priorité de la tâche", component=self.priority))

    async def on_submit(self, interaction):
        if not await self.board.authorize(interaction):
            return
        title = " ".join(self.task.value.split())
        if not title or not self.person.values:
            await interaction.response.send_message("Indique une tâche et son responsable.", ephemeral=True)
            return
        priority = self.priority.values[0] if self.priority.values else "1"
        if priority not in {str(level) for level in range(len(PRIORITIES))}:
            await interaction.response.send_message("Choisis une priorité dans la liste.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        selected = self.person.values[0]
        try:
            member_id = int(selected.id if hasattr(selected, "id") else selected)
            member = await interaction.guild.fetch_member(member_id)
        except discord.NotFound:
            member = None
        except discord.HTTPException:
            await interaction.edit_original_response(content="Discord n’a pas pu vérifier le membre. Réessaie dans un instant.")
            return
        if not director(member, self.board.role_id) or not interaction.channel.permissions_for(member).view_channel:
            await interaction.edit_original_response(content="Choisis un membre de la Direction ayant accès à ce salon.")
            return
        task_id = self.board.store.add(interaction.id, title, interaction.user.id, member.id,
                                       datetime.now(PARIS), int(priority))
        await interaction.edit_original_response(content=f"Tâche **#{task_id}** attribuée à {member.mention}.")
        await self.board.refresh()


class TaskList(discord.ui.View):
    def __init__(self, board, member_id, personal, page=0, history=False):
        super().__init__(timeout=600)
        self.board, self.member_id, self.personal = board, member_id, personal
        self.history = history
        if history:
            rows, total, self.page = board.store.history(page)
        else:
            rows, total, self.page = board.store.page(member_id if personal else None, page)
        title = "Historique des tâches" if history else "Mes tâches" if personal else "Toutes les tâches"
        self.embed = list_embed(rows, total, self.page, title, archived=history)
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
        await interaction.response.edit_message(embed=view.embed, view=view if view.children else None)

    @discord.ui.button(label="Précédent", row=3)
    async def previous(self, interaction, button):
        await self.show(interaction, self.page - 1)

    @discord.ui.button(label="Suivant", row=3)
    async def next_page(self, interaction, button):
        await self.show(interaction, self.page + 1)


class BoardButtons(discord.ui.View):
    def __init__(self, board):
        super().__init__(timeout=None)
        self.board = board

    async def interaction_check(self, interaction):
        return await self.board.authorize(interaction)

    @discord.ui.button(label="Ajouter une tâche", custom_id="pn:direction:add", style=discord.ButtonStyle.success)
    async def add(self, interaction, button):
        await interaction.response.send_modal(AddTask(self.board))

    async def show(self, interaction, personal, history=False):
        view = TaskList(self.board, interaction.user.id, personal, history=history)
        if view.children:
            await interaction.response.send_message(embed=view.embed, view=view, ephemeral=True)
        else:
            await interaction.response.send_message(embed=view.embed, ephemeral=True)

    @discord.ui.button(label="Mes tâches", custom_id="pn:direction:mine", style=discord.ButtonStyle.primary)
    async def mine(self, interaction, button):
        await self.show(interaction, True)

    @discord.ui.button(label="Toutes les tâches", custom_id="pn:direction:all")
    async def all_tasks(self, interaction, button):
        await self.show(interaction, False)

    @discord.ui.button(label="Historique", custom_id="pn:direction:history")
    async def history(self, interaction, button):
        await self.show(interaction, False, history=True)


class DirectionBoard:
    def __init__(self, client, guild_id, reference_id, path):
        self.client, self.guild_id, self.reference_id = client, guild_id, reference_id
        self.path, self.store = path, None
        self.buttons = BoardButtons(self)
        self.lock = asyncio.Lock()
        self.role_id = None
        self.channel_id = None

    def start(self):
        self.client.add_view(self.buttons)
        self.maintain.start()

    async def authorize(self, interaction):
        if (interaction.guild_id == self.guild_id and interaction.channel_id == self.channel_id
                and self.role_id and director(interaction.user, self.role_id)):
            return True
        await interaction.response.send_message("Ce tableau est réservé à la Direction dans son salon privé.", ephemeral=True)
        return False

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
                                                    read_message_history=True),
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

    async def panel_message(self, channel, embed, setting, buttons=None):
        message = None
        saved = self.store.setting(setting)
        if saved:
            try:
                message = await channel.fetch_message(int(saved))
            except discord.NotFound:
                pass
        if message is None:
            async for recent in channel.history(limit=50):
                if recent.author.id == self.client.user.id and any(e.title == embed.title for e in recent.embeds):
                    message = recent
                    break
        if message is not None and (message.author.id != self.client.user.id
                or not any(e.title == embed.title for e in message.embeds)):
            raise RuntimeError("Le message enregistré ne correspond pas au tableau de la Direction.")
        if message is None:
            message = await channel.send(embed=embed, view=buttons, silent=True)
        elif (message.embeds != [embed] or bool(message.components) != bool(buttons)
                or (buttons is not None and [c.to_dict() for c in message.components] != buttons.to_components())):
            await message.edit(embed=embed, view=buttons)
        if saved != str(message.id):
            self.store.remember(setting, message.id)
        return message

    async def panel(self, channel):
        rows = self.store.open_tasks()
        total = len(rows)
        count = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        previous_count = int(self.store.setting("panel_count") or 1)
        message = None
        for page in range(count):
            title = PANEL_TITLE if page == 0 else f"{PANEL_TITLE} · suite {page + 1}"
            embed = list_embed(rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE], total, page, title)
            if total:
                embed.set_footer(text=f"{total} tâche{'s' if total > 1 else ''} en attente au total")
            else:
                embed.description = ("Aucune tâche en attente. Les tâches de tous les membres de la Direction "
                    "s’affichent directement ici jusqu’à leur clôture. Utilise **Ajouter une tâche** pour en attribuer une.")
            if page + 1 > previous_count:
                self.store.remember("panel_count", page + 1)
            part = await self.panel_message(channel, embed, "panel" if page == 0 else f"panel_{page + 1}",
                                            self.buttons if page == 0 else None)
            if page == 0:
                message = part
        for page in range(count, previous_count):
            setting = f"panel_{page + 1}"
            saved = self.store.setting(setting)
            if saved:
                try:
                    extra = await channel.fetch_message(int(saved))
                    if extra.author.id != self.client.user.id or not any(
                            e.title == f"{PANEL_TITLE} · suite {page + 1}" for e in extra.embeds):
                        raise RuntimeError("Le message supplémentaire ne correspond pas au tableau.")
                    await extra.delete()
                except discord.NotFound:
                    pass
                self.store.remember(setting, "")
        self.store.remember("panel_count", count)
        if not message.pinned and channel.permissions_for(channel.guild.me).pin_messages:
            try:
                await message.pin(reason=REASON)
            except discord.Forbidden:
                LOG.info("Le tableau reste accessible dans le salon sans épinglage.")
        return message

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
            message = await self.panel(channel)
            await self.notify_assignments(channel, message)
            await self.remind(channel, message, datetime.now(PARIS))

    @tasks.loop(minutes=5)
    async def maintain(self):
        try:
            await self.refresh()
        except Exception:
            LOG.exception("Tableau de la Direction temporairement indisponible")

    @maintain.before_loop
    async def wait_ready(self):
        await self.client.wait_until_ready()
