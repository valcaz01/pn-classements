import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "serveur"))
# Le verrou Git du serveur utilise fcntl sous Linux. Ces tests ne touchent pas à Git.
if sys.platform == "win32":
    with patch.dict(sys.modules, {"fcntl": Mock()}):
        import pn_bot
else:
    import pn_bot


class SalonFormations(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.formateur = Mock(spec=discord.Role)
        self.formateur.id = pn_bot.ROLE_FORMATEUR_ID
        self.moi = Mock(id=12)
        self.everyone = Mock(spec=discord.Role)
        self.role_bot = Mock(spec=discord.Role)
        self.role_bot.id = 42
        self.role_bot.permissions.administrator = False
        self.role_bot.tags.bot_id = self.moi.id
        self.guild = Mock(id=pn_bot.GUILD_ID, default_role=self.everyone, me=self.moi, owner_id=99)
        self.guild.get_role.return_value = self.formateur
        self.salon = Mock(guild=self.guild, send=AsyncMock())
        self.salon.overwrites = {
            self.everyone: discord.PermissionOverwrite(view_channel=False),
            self.formateur: discord.PermissionOverwrite(view_channel=True),
            self.role_bot: discord.PermissionOverwrite(view_channel=True),
        }
        self.salon.overwrites_for.side_effect = self.salon.overwrites.__getitem__
        self.salon.permissions_for.return_value = discord.Permissions.all()
        self.bot = SimpleNamespace(user=self.moi, channel=AsyncMock(return_value=self.salon))
        self.embed = {"title": "Formation", "footer": {"text": "Suivi de formation · 123/20261121"},
                      "_pn": {"palier": "pa", "uid": "123", "nonce": "pa:123:20261121"}}

    async def test_seul_le_salon_prive_recoit_la_notification_et_seul_formateur_est_mentionne(self):
        await pn_bot.PNBot.envoyer_alerte(self.bot, self.embed)
        self.bot.channel.assert_awaited_once_with(pn_bot.FORMATIONS_CHANNEL_ID)
        appel = self.salon.send.await_args.kwargs
        self.assertEqual(appel["content"], f"<@&{self.formateur.id}>")
        self.assertEqual([r.id for r in appel["allowed_mentions"].roles], [self.formateur.id])
        self.assertFalse(appel["allowed_mentions"].everyone)
        self.assertIsNone(appel["view"])
        self.assertNotIn("_pn", appel["embed"].to_dict())

    async def test_aucun_repli_public_si_une_permission_permet_un_acces_non_prevu(self):
        self.salon.overwrites[self.everyone] = discord.PermissionOverwrite(view_channel=None)
        with self.assertRaises(RuntimeError):
            await pn_bot.PNBot.envoyer_alerte(self.bot, self.embed)
        self.salon.overwrites[self.everyone] = discord.PermissionOverwrite(view_channel=False)
        autre = Mock(spec=discord.Role)
        autre.id, autre.tags, autre.permissions.administrator = 890, None, False
        self.salon.overwrites[autre] = discord.PermissionOverwrite(view_channel=True)
        with self.assertRaises(RuntimeError):
            await pn_bot.PNBot.envoyer_alerte(self.bot, self.embed)
        self.salon.send.assert_not_awaited()

    async def test_reprise_d_un_envoi_accepte_par_discord_sans_accuse_de_reception(self):
        self.embed["_pn"]["reprise"] = "2026-11-23T00:01:00+01:00"

        async def historique(**kwargs):
            yield SimpleNamespace(author=self.moi, embeds=[discord.Embed.from_dict(self.embed)])

        self.salon.history.side_effect = historique
        await pn_bot.PNBot.envoyer_alerte(self.bot, self.embed)
        self.salon.send.assert_not_awaited()
        self.salon.history.assert_called_once()

    async def test_cadet_conserve_sa_destination_son_role_et_son_bouton(self):
        await pn_bot.PNBot.envoyer_alerte(self.bot, {"title": "Cadet", "_pn": {"palier": "cadet", "uid": "123"}})
        self.bot.channel.assert_awaited_once_with(pn_bot.ALERT_CHANNEL_ID)
        appel = self.salon.send.await_args.kwargs
        self.assertEqual(appel["content"], f"<@&{pn_bot.ROLE_HAUT_GRADE_ID}>")
        self.assertEqual(appel["view"].children[0].custom_id, "pn:badge:123")
