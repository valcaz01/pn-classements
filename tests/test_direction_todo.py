import sys
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "serveur"))
from direction_todo import AddTask, DirectionBoard, PANEL_TITLE, PARIS, TaskList, TaskStore


async def messages(items):
    for item in items:
        yield item


class DirectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "tasks.sqlite3"
        self.board = DirectionBoard(MagicMock(), 100, 101, self.path)
        self.board.store = TaskStore(self.path)
        self.addCleanup(self.board.store.db.close)
        self.board.channel_id, self.board.role_id = 200, 42
        self.board.client.user.id = 123
        self.channel = MagicMock()
        self.channel.permissions_for.return_value.view_channel = True
        self.channel.send = AsyncMock()
        self.channel.history.side_effect = lambda **kw: messages([])
        self.now = datetime(2026, 11, 19, 18, tzinfo=PARIS)

    def member(self, user_id, role=42):
        member = MagicMock(spec=discord.Member)
        member.id, member.guild.owner_id, member.bot = user_id, 1, False
        member.roles = [SimpleNamespace(id=role)]
        member.guild_permissions.administrator = False
        member.mention = f"<@{user_id}>"
        return member

    def posted_messages(self):
        posted = {}
        def components(view):
            return [SimpleNamespace(to_dict=lambda data=data: data) for data in view.to_components()] if view else []
        async def send(*args, embed, view=None, **kwargs):
            message = SimpleNamespace(id=1000 + self.channel.send.await_count, author=self.board.client.user,
                embeds=[embed], components=components(view), pinned=False,
                jump_url=f"https://discord.com/channels/100/200/{1000 + self.channel.send.await_count}")
            async def edit(**changes):
                message.embeds = [changes["embed"]]
                message.components = components(changes["view"])
            async def delete():
                del posted[message.id]
            async def pin(**kwargs):
                message.pinned = True
            message.edit, message.delete, message.pin = AsyncMock(side_effect=edit), AsyncMock(side_effect=delete), AsyncMock(side_effect=pin)
            posted[message.id] = message
            return message
        async def fetch(message_id):
            if message_id not in posted:
                raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")
            return posted[message_id]
        self.channel.send.side_effect = send
        self.channel.fetch_message = AsyncMock(side_effect=fetch)
        self.channel.history.side_effect = lambda **kw: messages(list(posted.values())[::-1])
        return posted

    async def submit(self, initiator, assignee, priority=None):
        modal = AddTask(self.board)
        response = SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock())
        interaction = SimpleNamespace(id=1101, guild_id=100, channel_id=200, channel=self.channel,
            guild=SimpleNamespace(fetch_member=AsyncMock(return_value=assignee)), user=initiator,
            response=response, edit_original_response=AsyncMock())
        fields = [{"type":18, "component":{"type":4, "custom_id":modal.task.custom_id, "value":"Relire la notice"}},
                  {"type":18, "component":{"type":5, "custom_id":modal.person.custom_id, "values":[str(assignee.id)]}}]
        if priority is not None:
            fields.append({"type":18, "component":{"type":3, "custom_id":modal.priority.custom_id, "values":[str(priority)]}})
        modal._refresh(interaction, fields, {})
        self.board.refresh = AsyncMock()
        await modal.on_submit(interaction)
        return interaction

    async def test_director_assigns_to_another_without_admin_privileges(self):
        alice, bob = self.member(10), self.member(20)
        await self.submit(alice, bob)
        await self.submit(alice, bob)  # même soumission livrée deux fois
        self.assertEqual(self.board.store.page(alice.id)[1], 0)
        rows, count, _ = self.board.store.page(bob.id)
        self.assertEqual((count, rows[0]["creator"], rows[0]["title"]), (1, alice.id, "Relire la notice"))
        self.assertEqual(rows[0]["priority"], 1)
        self.assertIn("Relire la notice", TaskList(self.board, bob.id, True).embed.description)
        self.assertFalse(self.board.store.start(rows[0]["id"], alice.id, self.now))
        self.assertTrue(self.board.store.start(rows[0]["id"], bob.id, self.now))
        self.assertIn("En cours", TaskList(self.board, bob.id, True).embed.description)
        self.assertFalse(self.board.store.finish(rows[0]["id"], 999, self.now))
        self.assertTrue(self.board.store.finish(rows[0]["id"], bob.id, self.now))
        self.assertFalse(self.board.store.finish(rows[0]["id"], alice.id, self.now))

    async def test_ordinary_member_cannot_assign_or_be_assigned(self):
        alice, guest = self.member(10), self.member(11, role=3)
        rejected = await self.submit(guest, alice)
        rejected.response.send_message.assert_awaited_once()
        await self.submit(alice, guest)
        self.assertEqual(self.board.store.page()[1], 0)

    async def test_channel_created_private_and_existing_public_channel_refused(self):
        guild = MagicMock(spec=discord.Guild)
        everyone, role = MagicMock(spec=discord.Role), MagicMock(spec=discord.Role)
        everyone.permissions.administrator = False
        everyone.is_bot_managed.return_value = False
        everyone.name, role.name, role.id = "@everyone", "👑︱𝖣irection", 42
        guild.roles, guild.default_role = [everyone, role], everyone
        category = MagicMock()
        category.text_channels, category.name = [], "Direction"
        guild.get_channel.return_value.category = category
        self.board.client.get_guild.return_value = guild
        channel = self.channel
        channel.id, channel.name = 200, "to-do-list"
        channel.guild = guild
        channel.overwrites = {}
        def permissions(member):
            return discord.Permissions.none() if member == everyone else discord.Permissions.all()
        channel.permissions_for.side_effect = permissions
        guild.create_text_channel = AsyncMock(return_value=channel)
        self.assertEqual(await self.board.channel(), channel)
        config = guild.create_text_channel.await_args.kwargs
        self.assertEqual(config["category"], category)
        self.assertIs(config["overwrites"][everyone].view_channel, False)
        self.assertIs(config["overwrites"][role].view_channel, True)
        category.text_channels = [channel]
        self.assertEqual(await self.board.channel(), channel)
        guild.create_text_channel.assert_awaited_once()
        channel.permissions_for.side_effect = lambda member: discord.Permissions.all()
        with self.assertRaisesRegex(RuntimeError, "visibilité"):
            await self.board.channel()

    async def test_open_tasks_remain_accessible_after_restart_and_paginate(self):
        for i in range(11):
            self.board.store.add(i, f"Tâche {i}", 10, 20, self.now)
        reopened = TaskStore(self.path)
        self.addCleanup(reopened.db.close)
        self.assertEqual(len(reopened.page(20, 0)[0]), 8)
        rows, count, page = reopened.page(20, 20)
        self.assertEqual((len(rows), count, page), (3, 11, 1))
        self.assertTrue(self.board.buttons.is_persistent())

    async def test_priority_can_be_selected_and_sorts_shared_and_personal_lists(self):
        alice, bob = self.member(10), self.member(20)
        await self.submit(alice, bob, priority=3)
        urgent = self.board.store.page()[0][0]
        self.assertEqual(urgent["priority"], 3)
        self.board.store.add(2, "Normale", alice.id, bob.id, self.now)
        self.board.store.add(3, "Basse", alice.id, bob.id, self.now, priority=0)
        self.board.store.add(4, "Haute", alice.id, bob.id, self.now, priority=2)
        self.assertEqual([r["priority"] for r in self.board.store.open_tasks()], [3, 2, 1, 0])
        self.assertEqual([r["priority"] for r in self.board.store.page(bob.id)[0]], [3, 2, 1, 0])
        view = TaskList(self.board, bob.id, True)
        self.assertIn("🔴 Urgente", view.embed.description)
        self.assertIn("🟠 Haute", view.embed.description)
        self.assertIn("🔵 Normale", view.embed.description)
        self.assertIn("⚪ Basse", view.embed.description)
        self.board.store.db.close()
        self.board.store = TaskStore(self.path)
        self.addCleanup(self.board.store.db.close)
        self.assertEqual(self.board.store.page()[0][0]["priority"], 3)

    async def test_invalid_priority_does_not_create_a_task(self):
        rejected = await self.submit(self.member(10), self.member(20), priority=9)
        rejected.response.send_message.assert_awaited_once()
        self.assertEqual(self.board.store.page()[1], 0)

    async def test_existing_database_migrates_without_losing_open_or_completed_tasks(self):
        legacy_path = self.path.with_name("legacy.sqlite3")
        with sqlite3.connect(legacy_path) as legacy:
            legacy.executescript("""CREATE TABLE tasks (
                id INTEGER PRIMARY KEY, interaction_id INTEGER UNIQUE NOT NULL,
                title TEXT NOT NULL, creator INTEGER NOT NULL, assignee INTEGER NOT NULL,
                created INTEGER NOT NULL, started INTEGER, completed INTEGER, completed_by INTEGER);
                INSERT INTO tasks VALUES (1,1,'À faire',10,20,123,NULL,NULL,NULL);
                INSERT INTO tasks VALUES (2,2,'Terminée',10,20,123,124,125,20);""")
        legacy.close()
        migrated = TaskStore(legacy_path)
        self.addCleanup(migrated.db.close)
        self.assertEqual(migrated.page()[1], 1)
        self.assertEqual(migrated.history()[1], 1)
        self.assertEqual(migrated.page()[0][0]["priority"], 1)
        self.assertEqual(migrated.history()[0][0]["completed_by"], 20)
        self.assertIsNone(migrated.page()[0][0]["cancelled"])

    async def test_any_director_can_cancel_a_colleagues_task_and_history_keeps_actor(self):
        alice, bob, charlie = self.member(10), self.member(20), self.member(30)
        task_id = self.board.store.add(1, "Tâche de Bob", alice.id, bob.id, self.now, priority=2)
        self.board.store.start(task_id, bob.id, self.now)
        view = TaskList(self.board, charlie.id, False)
        interaction = SimpleNamespace(guild_id=100, channel_id=200, user=charlie,
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()))
        self.assertTrue(await view.interaction_check(interaction))
        cancel = next(item for item in view.children if isinstance(item, discord.ui.Select)
                      and item.placeholder == "Annuler une tâche")
        cancel._values = [str(task_id)]
        self.board.refresh = AsyncMock()
        await cancel.callback(interaction)
        self.board.refresh.assert_awaited_once()
        self.assertEqual(self.board.store.page()[1], 0)
        self.assertEqual(list(self.board.store.reminder_counts(self.now + timedelta(days=2))), [])
        archived = self.board.store.history()[0][0]
        self.assertEqual((archived["creator"], archived["assignee"], archived["cancelled_by"], archived["priority"]), (10, 20, 30, 2))
        self.assertFalse(self.board.store.start(task_id, bob.id, self.now))
        self.assertFalse(self.board.store.finish(task_id, bob.id, self.now))
        self.assertFalse(self.board.store.cancel(task_id, charlie.id, self.now))
        history = TaskList(self.board, alice.id, False, history=True)
        self.assertIn("Annulée par <@30>", history.embed.description)
        self.assertIn("Responsable : <@20>", history.embed.description)
        self.assertIn("(Paris)", history.embed.description)
        self.assertFalse(history.children)
        guest = self.member(40, role=3)
        interaction.user = guest
        self.assertFalse(await TaskList(self.board, guest.id, False).interaction_check(interaction))

    async def test_history_preserves_completed_tasks_after_restart_and_paginates(self):
        for i in range(11):
            task_id = self.board.store.add(i, f"Terminée {i}", 10, 20, self.now)
            self.board.store.finish(task_id, 20, self.now + timedelta(minutes=i))
        reopened = TaskStore(self.path)
        self.addCleanup(reopened.db.close)
        self.assertEqual(reopened.page()[1], 0)
        rows, total, page = reopened.history(1)
        self.assertEqual((len(rows), total, page), (3, 11, 1))
        self.assertEqual(rows[0]["title"], "Terminée 2")
        view = TaskList(self.board, 10, False, history=True)
        self.assertTrue(view.previous.disabled)
        self.assertFalse(view.next_page.disabled)
        self.assertIn("Terminée par <@20>", view.embed.description)
        self.assertEqual([button.label for button in view.children], ["Précédent", "Suivant"])

    async def test_shared_board_shows_every_assignee_and_task_without_clicking(self):
        posted = self.posted_messages()
        task_ids = [self.board.store.add(i, "_*" * 70, 10, 20 if i % 2 else 30, self.now) for i in range(17)]
        panel = await self.board.panel(self.channel)
        self.assertEqual(len(posted), 3)
        text = "\n".join(m.embeds[0].description for m in posted.values())
        for task_id in task_ids:
            self.assertEqual(text.count(f"**#{task_id}**"), 1)
        self.assertIn("<@20>", text)
        self.assertIn("<@30>", text)
        for message in posted.values():
            self.assertLessEqual(len(message.embeds[0].description), 4096)
            self.assertLessEqual(len(message.embeds[0]), 6000)
        restarted = DirectionBoard(self.board.client, 100, 101, self.path)
        restarted.store = self.board.store
        await restarted.panel(self.channel)
        self.assertEqual(self.channel.send.await_count, 3)
        for task_id in task_ids[:9]:
            self.board.store.finish(task_id, 10, self.now)
        await restarted.panel(self.channel)
        self.assertEqual(list(posted), [panel.id])
        text = posted[panel.id].embeds[0].description
        for task_id in task_ids[:9]:
            self.assertNotIn(f"**#{task_id}**", text)
        for task_id in task_ids[9:]:
            self.assertIn(f"**#{task_id}**", text)

    async def test_shared_board_recovers_after_send_before_saving_message_id(self):
        posted = self.posted_messages()
        for i in range(9):
            self.board.store.add(i, f"Tâche {i}", 10, 20, self.now)
        remember = self.board.store.remember
        def fail_save(name, value):
            if name == "panel_2":
                raise OSError("Sauvegarde interrompue")
            remember(name, value)
        with patch.object(self.board.store, "remember", side_effect=fail_save):
            with self.assertRaises(OSError):
                await self.board.panel(self.channel)
        self.assertEqual(len(posted), 2)
        await self.board.panel(self.channel)
        self.assertEqual(self.channel.send.await_count, 2)
        extra = next(m for m in posted.values() if m.embeds[0].title != PANEL_TITLE)
        del posted[extra.id]
        await self.board.panel(self.channel)
        self.assertEqual(len(posted), 2)
        self.assertEqual(self.channel.send.await_count, 3)

    async def test_assignment_mentions_only_assignee_once_and_recovers_after_send(self):
        self.posted_messages()
        bob = self.member(20)
        self.channel.guild.fetch_member = AsyncMock(return_value=bob)
        task_id = self.board.store.add(1234, "Relire **la notice** @everyone", 10, bob.id, self.now)
        panel = SimpleNamespace(jump_url="https://discord.com/channels/100/200/300")
        await self.board.notify_assignments(self.channel, panel)
        sent = self.channel.send.await_args
        self.assertEqual(sent.args, ("<@20>, une tâche t’a été attribuée.",))
        allowed = sent.kwargs["allowed_mentions"]
        self.assertEqual(allowed.users, [bob])
        self.assertFalse(allowed.everyone)
        self.assertFalse(allowed.roles)
        self.assertFalse(allowed.replied_user)
        self.assertNotIn("@everyone", sent.kwargs["embed"].description)
        self.assertIn(panel.jump_url, sent.kwargs["embed"].description)
        await self.board.notify_assignments(self.channel, panel)
        self.board.store.remember(f"assigned_{task_id}", "")
        await self.board.notify_assignments(self.channel, panel)
        self.assertEqual(self.channel.send.await_count, 1)

    async def test_assignment_notice_skips_completed_or_inaccessible_tasks(self):
        self.posted_messages()
        done = self.board.store.add(1, "Déjà faite", 10, 20, self.now)
        self.board.store.finish(done, 10, self.now)
        self.board.store.add(2, "Ouverte", 10, 30, self.now)
        panel = SimpleNamespace(jump_url="https://discord.com/channels/100/200/300")
        self.channel.guild.fetch_member = AsyncMock(return_value=self.member(30, role=3))
        await self.board.notify_assignments(self.channel, panel)
        self.channel.send.assert_not_awaited()
        self.channel.guild.fetch_member.assert_awaited_once_with(30)
        self.channel.guild.fetch_member.return_value = self.member(30)
        self.channel.permissions_for.return_value.view_channel = False
        await self.board.notify_assignments(self.channel, panel)
        self.channel.send.assert_not_awaited()

    async def test_daily_reminder_repeats_until_task_is_completed(self):
        bob = self.member(20)
        self.channel.guild.fetch_member = AsyncMock(return_value=bob)
        panel = SimpleNamespace(jump_url="https://discord.com/channels/100/200/300")
        task_id = self.board.store.add(1, "À faire", 10, bob.id, self.now - timedelta(hours=25))
        await self.board.remind(self.channel, panel, self.now)
        await self.board.remind(self.channel, panel, self.now + timedelta(hours=2))
        await self.board.remind(self.channel, panel, self.now + timedelta(hours=23))
        self.assertEqual(self.channel.send.await_count, 1)
        await self.board.remind(self.channel, panel, self.now + timedelta(days=1))
        self.assertEqual(self.channel.send.await_count, 2)
        self.board.store.finish(task_id, bob.id, self.now + timedelta(days=1))
        await self.board.remind(self.channel, panel, self.now + timedelta(days=2))
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_reminder_only_for_pending_24h_and_recovers_after_send_before_save(self):
        alice, bob = self.member(10), self.member(20)
        self.channel.guild.fetch_member = AsyncMock(return_value=bob)
        panel = SimpleNamespace(jump_url="https://discord.com/channels/100/200/300")
        self.board.store.add(501, "En attente", alice.id, bob.id, self.now - timedelta(hours=25))
        self.board.store.add(502, "Récente", bob.id, alice.id, self.now)
        done = self.board.store.add(503, "Terminée", bob.id, alice.id, self.now - timedelta(days=2))
        self.board.store.finish(done, alice.id, self.now)
        await self.board.remind(self.channel, panel, self.now - timedelta(minutes=1))
        self.channel.send.assert_not_awaited()
        await self.board.remind(self.channel, panel, self.now)
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.channel.send.await_args.args, (bob.mention,))
        await self.board.remind(self.channel, panel, self.now)
        self.board.store.remember("reminded_20", "")
        sent = SimpleNamespace(author=self.board.client.user, embeds=[self.channel.send.await_args.kwargs["embed"]],
                               raw_mentions=[20])
        self.channel.history.side_effect = lambda **kw: messages([sent])
        await self.board.remind(self.channel, panel, self.now)
        self.assertEqual(self.channel.send.await_count, 1)
