import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "serveur"))
from direction_todo import AddTask, DirectionBoard, PARIS, TaskList, TaskStore


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

    async def submit(self, initiator, assignee):
        modal = AddTask(self.board)
        response = SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock())
        interaction = SimpleNamespace(id=1101, guild_id=100, channel_id=200, channel=self.channel,
            guild=SimpleNamespace(fetch_member=AsyncMock(return_value=assignee)), user=initiator,
            response=response, edit_original_response=AsyncMock())
        fields = [{"type":18, "component":{"type":4, "custom_id":modal.task.custom_id, "value":"Relire la notice"}},
                  {"type":18, "component":{"type":5, "custom_id":modal.person.custom_id, "values":[str(assignee.id)]}}]
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
