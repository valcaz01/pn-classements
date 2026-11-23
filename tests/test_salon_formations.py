import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "serveur"))
import formations as form
# Le verrou Git du serveur utilise fcntl sous Linux. Ces tests ne touchent pas à Git.
if sys.platform == "win32":
    with patch.dict(sys.modules, {"fcntl": Mock()}):
        import pn_bot
else:
    import pn_bot


class SalonFormations(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.formateur = Mock(spec=discord.Role)
        self.formateur.id = form.FORMATEUR
        self.moi = Mock(id=12)
        self.everyone = Mock(spec=discord.Role)
        self.role_bot = Mock(spec=discord.Role)
        self.role_bot.id = 42
        self.role_bot.permissions.administrator = False
        self.role_bot.tags.bot_id = self.moi.id
        self.guild = Mock(id=pn_bot.GUILD_ID, default_role=self.everyone, me=self.moi, owner_id=99)
        self.guild.get_role.return_value = self.formateur
        self.salon = Mock(id=form.SALON, guild=self.guild)
        self.salon.overwrites = {
            self.everyone: discord.PermissionOverwrite(view_channel=False),
            self.formateur: discord.PermissionOverwrite(view_channel=True),
            self.role_bot: discord.PermissionOverwrite(view_channel=True),
        }
        self.salon.overwrites_for.side_effect = self.salon.overwrites.__getitem__
        def permissions(cible):
            droits = discord.Permissions.all()
            if cible is self.formateur:
                droits.send_messages = False
            return droits
        self.salon.permissions_for.side_effect = permissions
        self.bot = SimpleNamespace(user=self.moi, channel=AsyncMock(return_value=self.salon))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.chemin = Path(tmp.name) / "tableau.json"
        self.tableau = form.TableauFormations(self.bot, self.guild.id, self.chemin)
        self.a = {"uid": "1", "hid": "hhfr-1", "pseudo": "Alpha", "depuis": "2026-11-21", "pr": 321}
        self.b = {**self.a, "uid": "2", "hid": "hhfr-2", "pseudo": "Bravo"}
        self.messages = {}
        self.compteur = 100
        async def envoyer(**options):
            self.compteur += 1
            ident = self.compteur
            vue = options.get("view")
            msg = SimpleNamespace(id=ident, author=self.moi, embeds=[options["embed"]] if "embed" in options else [],
                                  components=vue.children if vue else [], vue=vue)
            async def modifier(**kw):
                msg.embeds = [kw["embed"]] if kw.get("embed") else []
                msg.vue = kw["view"]
                msg.components = msg.vue.children
            async def effacer():
                del self.messages[ident]
            msg.edit, msg.delete = AsyncMock(side_effect=modifier), AsyncMock(side_effect=effacer)
            self.messages[ident] = msg
            return msg
        async def historique(**kw):
            for msg in reversed(list(self.messages.values())):
                yield msg
        self.salon.send = AsyncMock(side_effect=envoyer)
        self.salon.fetch_message = AsyncMock(side_effect=self.messages.__getitem__)
        self.salon.history.side_effect = historique

    async def test_seul_le_salon_prive_recoit_la_notification_et_seul_formateur_est_mentionne(self):
        await self.tableau.actualiser([self.a, self.b])
        self.bot.channel.assert_awaited_once_with(form.SALON)
        appel = self.salon.send.await_args.kwargs
        textes = [c.content for c in appel["view"].walk_children() if isinstance(c,discord.ui.TextDisplay)]
        self.assertEqual("\n".join(textes).count(f"<@&{self.formateur.id}>"), 1)
        self.assertEqual([r.id for r in appel["allowed_mentions"].roles], [self.formateur.id])
        self.assertFalse(appel["allowed_mentions"].everyone)
        self.assertEqual([b.label for b in appel["view"].walk_children() if isinstance(b, discord.ui.Button)],
                         ["Formation faite", "Historique des formations"])
        self.assertEqual(appel["view"].carte.description, "• Alpha\n• Bravo")
        self.assertTrue(appel["view"].is_persistent())
        await self.tableau.actualiser([self.a, self.b])
        self.salon.send.assert_awaited_once()

    async def test_aucun_repli_public_si_une_permission_permet_un_acces_non_prevu(self):
        self.salon.overwrites[self.everyone] = discord.PermissionOverwrite(view_channel=None)
        with self.assertRaises(RuntimeError):
            await self.tableau.actualiser([self.a])
        self.salon.overwrites[self.everyone] = discord.PermissionOverwrite(view_channel=False)
        autre = Mock(spec=discord.Role)
        autre.id, autre.tags, autre.permissions.administrator = 890, None, False
        self.salon.overwrites[autre] = discord.PermissionOverwrite(view_channel=True)
        with self.assertRaises(RuntimeError):
            await self.tableau.actualiser([self.a])
        self.salon.send.assert_not_awaited()

    async def test_reprise_apres_reponse_discord_perdue_et_une_seule_liste(self):
        await self.tableau.actualiser([self.a])
        envoyer = self.salon.send.side_effect
        async def sans_reponse(**kw):
            await envoyer(**kw)
            raise TimeoutError()
        self.salon.send.side_effect = sans_reponse
        with self.assertRaises(TimeoutError):
            await self.tableau.actualiser([self.a, self.b])
        self.salon.send.side_effect = envoyer
        reprise = form.TableauFormations(self.bot, self.guild.id, self.chemin)
        await reprise.actualiser([self.a, self.b])
        self.assertEqual(self.salon.send.await_count, 2)
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(next(iter(self.messages.values())).vue.carte.description, "• Alpha\n• Bravo")

    async def test_confirmation_historique_concurrence_et_persistance(self):
        await self.tableau.actualiser([self.a, self.b])
        hg = SimpleNamespace(display_name="Formatrice", id=800)
        inter = Mock(user=hg, channel=self.salon, response=Mock(defer=AsyncMock()), edit_original_response=AsyncMock())
        menu = form.Confirmation(self.tableau, hg.id, form.cle(self.a))
        await menu.confirmer.callback(inter)
        await menu.confirmer.callback(inter)
        self.assertIn("déjà", inter.edit_original_response.await_args.kwargs["content"])
        self.salon.send.assert_awaited_once()
        nouveau = form.TableauFormations(self.bot, self.guild.id, self.chemin)
        await nouveau.actualiser([self.a, self.b])
        self.assertEqual([f["pseudo"] for f in nouveau.liste()], ["Bravo"])
        archive = list(nouveau.etat["historique"].values())
        self.assertEqual([(f["pseudo"], f["par"]) for f in archive], [("Alpha", "Formatrice")])
        historique = form.PagesFormations(nouveau, hg.id, historique=True)
        self.assertIn("Formatrice", historique.carte.description)
        self.assertIn("Alpha", historique.carte.description)
        self.assertTrue(archive[0]["le"])
        self.salon.send.assert_awaited_once()

    async def test_migration_des_messages_du_bot_sans_nouvelle_mention(self):
        ancien = discord.Embed(title="🎓 Alpha peut passer la formation Gardien de la paix")
        ancien.set_footer(text="Suivi de formation · 1/20261121")
        ancien.add_field(name="Points de recrutement cumulés", value="321 PR")
        await self.salon.send(embed=ancien)
        humain = SimpleNamespace(id=10, author=Mock(id=17), embeds=[ancien])
        self.messages[10] = humain
        await self.tableau.actualiser([])
        self.assertEqual(self.salon.send.await_args.kwargs["allowed_mentions"].to_dict()["parse"], [])
        self.assertIn(10, self.messages)
        self.assertEqual(len(self.messages), 2)
        self.assertEqual([f["pseudo"] for f in self.tableau.liste()], ["Alpha"])

    async def test_presence_disponible_hors_ligne_ou_indisponible_sans_mention(self):
        await self.tableau.actualiser([self.a, self.b])
        with patch.object(form, "presences_habbo", return_value={form.cle(self.a):True, form.cle(self.b):False}):
            await self.tableau.actualiser_presences()
        msg = next(iter(self.messages.values()))
        self.assertEqual(msg.vue.carte.description, "• Alpha 🟢\n• Bravo")
        self.assertEqual(msg.edit.await_args.kwargs["allowed_mentions"].to_dict()["parse"], [])
        self.tableau.presences[form.cle(self.a)] = (0, True)
        with patch.object(form, "presences_habbo", side_effect=form.ApiRequestError("unavailable")):
            await self.tableau.actualiser_presences()
        self.assertEqual(msg.vue.carte.description, "• Alpha\n• Bravo")
        self.salon.send.assert_awaited_once()
        self.assertEqual(self.tableau.suivre_presences.minutes, 2)

    async def test_presence_publique_et_profil_masque_et_changement_de_pseudo(self):
        groupe = [{"uniqueId":"hhfr-1","name":"PseudoModifié","online":True}]
        with patch.object(form, "request_json", side_effect=[groupe, {"online":True,"profileVisible":False}]):
            lecture = form.presences_habbo([self.a, self.b])
        self.assertEqual(lecture, {form.cle(self.a):True, form.cle(self.b):False})
        instant = time.time()
        await self.tableau.actualiser([{**self.a,"en_ligne":True,"observation":instant}])
        self.tableau.noter_presence(form.cle(self.a), instant - 60, False)
        self.assertEqual(self.tableau.presences[form.cle(self.a)], (instant, True))

    async def test_menu_page_deux_et_nom_absent_du_dernier_releve(self):
        candidats = [{**self.a, "pseudo":f"Police{i:02}", "uid":str(i)} for i in range(28)]
        await self.tableau.actualiser(candidats)
        await self.tableau.actualiser([])
        vue = form.PagesFormations(self.tableau, 200, historique=False, page=1)
        select = next(c for c in vue.children if isinstance(c, discord.ui.Select))
        self.assertEqual([o.label for o in select.options], ["Police25", "Police26", "Police27"])
        self.assertEqual(len(self.messages), 1)

    async def test_formateur_peut_utiliser_les_boutons_en_lecture_seule(self):
        membre = Mock(spec=discord.Member)
        membre.id, membre.roles, membre.guild_permissions.administrator = 88, [self.formateur], False
        inter = SimpleNamespace(user=membre, guild_id=self.guild.id, channel_id=form.SALON, channel=self.salon,
                                response=Mock(send_message=AsyncMock()))
        self.assertTrue(await self.tableau.autoriser(inter))
        membre.roles = []
        self.assertFalse(await self.tableau.autoriser(inter))
        inter.response.send_message.assert_awaited_once()

    async def test_menu_qualifiante_distinct_du_tableau_public_et_historique_type(self):
        qualif = {**self.a, "type":"qualifiante", "uid":"hhfr-1", "pseudo":"Charlie"}
        await self.tableau.actualiser([self.a, qualif])
        public = next(iter(self.messages.values())).vue
        self.assertEqual(public.carte.description, "• Alpha")
        menu = next(c for c in public.walk_children() if isinstance(c, discord.ui.Select))
        self.assertEqual([(o.label, o.default) for o in menu.options],
                         [("Formation Gardien de la paix",True),("Formation qualifiante",False)])
        inter = Mock(user=Mock(id=888), response=Mock(send_message=AsyncMock()), is_expired=Mock(return_value=False),
                     edit_original_response=AsyncMock())
        menu._values = ["qualifiante"]
        await menu.callback(inter)
        opts = inter.response.send_message.await_args.kwargs
        self.assertTrue(opts["ephemeral"])
        self.assertEqual(opts["view"].carte.description, "• Charlie")
        self.tableau.noter_presence(form.cle(qualif), time.time(), True)
        await self.tableau.actualiser([])
        self.assertEqual(inter.edit_original_response.await_args.kwargs["view"].carte.description,"• Charlie 🟢")
        self.assertEqual(len(self.messages), 1)
        self.tableau.terminer(form.cle(qualif), SimpleNamespace(id=888, display_name="Formateur"))
        archive = form.PagesFormations(self.tableau, 888, historique=True)
        self.assertIn("Formation qualifiante", archive.carte.description)
        self.assertEqual([f["pseudo"] for f in self.tableau.liste()], ["Alpha"])

    async def test_cadet_conserve_sa_destination_son_role_et_son_bouton(self):
        await pn_bot.PNBot.envoyer_alerte(self.bot, {"title": "Cadet", "_pn": {"palier": "cadet", "uid": "123"}})
        self.bot.channel.assert_awaited_once_with(pn_bot.ALERT_CHANNEL_ID)
        appel = self.salon.send.await_args.kwargs
        self.assertEqual(appel["content"], f"<@&{pn_bot.ROLE_HAUT_GRADE_ID}>")
        self.assertEqual(appel["view"].children[0].custom_id, "pn:badge:123")
