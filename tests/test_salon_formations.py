import sys
import tempfile
import time
import unittest
from datetime import datetime
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
        self.texte_inconnus = ("**Alpha** (P.Adj depuis 21/11/26)\nPrésence indisponible\n\n"
                               "**Bravo** (P.Adj depuis 21/11/26)\nPrésence indisponible")
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

    async def test_une_seule_mention_everyone_pour_le_lot_dans_le_salon_prive(self):
        await self.tableau.actualiser([self.a, self.b])
        self.bot.channel.assert_awaited_once_with(form.SALON)
        appel = self.salon.send.await_args.kwargs
        textes = [c.content for c in appel["view"].walk_children() if isinstance(c,discord.ui.TextDisplay)]
        self.assertEqual("\n".join(textes).count("@everyone"), 1)
        self.assertNotIn("<@&", "\n".join(textes))
        self.assertEqual(appel["allowed_mentions"].to_dict(), {"parse": ["everyone"]})
        self.assertEqual([b.label for b in appel["view"].walk_children() if isinstance(b, discord.ui.Button)],
                         ["Formation faite", "Historique des formations", "Retirer de la liste"])
        self.assertEqual(appel["view"].carte.description, self.texte_inconnus)
        self.assertTrue(appel["view"].is_persistent())
        await self.tableau.actualiser([self.a, self.b])
        self.salon.send.assert_awaited_once()

    async def test_alerte_everyone_reprise_si_permission_retablie(self):
        droits = self.salon.permissions_for.side_effect
        def priver_de_mention(membre):
            acces = droits(membre)
            acces.mention_everyone = False
            return acces
        self.salon.permissions_for.side_effect = priver_de_mention
        with self.assertRaisesRegex(RuntimeError, "@everyone"):
            await self.tableau.actualiser([self.a])
        self.salon.send.assert_not_awaited()
        self.salon.permissions_for.side_effect = droits
        await self.tableau.actualiser([])
        self.salon.send.assert_awaited_once()
        self.assertEqual(self.salon.send.await_args.kwargs["allowed_mentions"].to_dict()["parse"], ["everyone"])

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
        self.assertEqual(next(iter(self.messages.values())).vue.carte.description, self.texte_inconnus)

    async def test_confirmation_historique_concurrence_et_persistance(self):
        await self.tableau.actualiser([self.a, self.b])
        hg = SimpleNamespace(display_name="Formatrice", id=800)
        inter = Mock(user=hg, channel=self.salon, response=Mock(defer=AsyncMock()), edit_original_response=AsyncMock())
        menu = form.Confirmation(self.tableau, hg.id, form.cle(self.a), sujet="https://www.pn-rpg.fr/viewtopic?p=31")
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
        self.assertEqual(archive[0]["sujet"], "https://www.pn-rpg.fr/viewtopic?p=31#31")
        self.assertIn(f"[Sujet de formation]({archive[0]['sujet']})", historique.carte.description)
        del archive[0]["sujet"]
        ancien = form.PagesFormations(nouveau, hg.id, historique=True)
        self.assertIn("Alpha", ancien.carte.description)
        self.assertNotIn("[Sujet de formation]", ancien.carte.description)
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
        dernier = datetime(2026, 11, 23, 8, 5, tzinfo=form.PARIS)
        lectures = {form.cle(self.a):form.Presence(True), form.cle(self.b):form.Presence(False, dernier)}
        with patch.object(form, "presences_habbo", return_value=lectures):
            await self.tableau.actualiser_presences()
        msg = next(iter(self.messages.values()))
        self.assertEqual(msg.vue.carte.description, "**Alpha** 🟢 en ligne (P.Adj depuis 21/11/26)\n\n"
                         "**Bravo** (P.Adj depuis 21/11/26)\nDernière connexion le 23/11/26 à 08:05")
        self.assertEqual(msg.vue.carte.footer.text, "2 en attente · 1 en ligne")
        self.assertEqual(msg.edit.await_args.kwargs["allowed_mentions"].to_dict()["parse"], [])
        await self.tableau.actualiser([{**self.b, "observation":time.time(), "en_ligne":False}])
        self.assertEqual(self.tableau.presences[form.cle(self.b)][1].dernier_acces, dernier)
        self.tableau.presences[form.cle(self.a)] = (0, form.Presence(True))
        self.tableau.prochain_statut = 0  # passage suivant
        with patch.object(form, "presences_habbo", side_effect=form.ApiRequestError("unavailable")):
            await self.tableau.actualiser_presences()
        self.assertEqual(msg.vue.carte.description, self.texte_inconnus)
        self.salon.send.assert_awaited_once()
        self.assertEqual(self.tableau.suivre_presences.seconds, 30)
        self.assertEqual(self.tableau.suivre_qg.seconds, 30)

    async def test_visite_qg_panne_reprise_puis_depart_et_retour(self):
        await self.tableau.actualiser([self.a, self.b])
        controle = Mock()
        self.tableau.controle_qg = controle

        async def relever(tableau, resultat):
            tableau.prochain_qg = 0
            controle.relever.side_effect = [resultat]
            await tableau.actualiser_qg()

        await relever(self.tableau, (104, 3, {"hhfr-2"}))
        self.assertEqual(self.salon.send.await_count, 2)
        dernier = self.salon.send.await_args.kwargs
        self.assertEqual(dernier['allowed_mentions'].to_dict(), {'roles':[form.FORMATEUR], 'parse':[]})
        self.assertIn('**Bravo** 🟢 en ligne · au QG', dernier['view'].carte.description)
        self.assertEqual(len(self.messages), 1)
        reprise = form.TableauFormations(self.bot, self.guild.id, self.chemin)
        reprise.controle_qg = controle
        await relever(reprise, (105, 3, {"hhfr-2"}))
        await relever(reprise, form.ApiRequestError('rate_limit', retry_delay=150))
        self.assertGreater(reprise.prochain_qg - time.monotonic(), 148)
        await relever(reprise, (106, 2, set()))
        await relever(reprise, (107, 2, {"hhfr-2"}))
        self.assertEqual(self.salon.send.await_count, 2)
        await relever(reprise, (108, 2, set()))
        await relever(reprise, (109, 1, set()))
        await relever(reprise, (110, 2, {"hhfr-2"}))
        self.assertEqual(self.salon.send.await_count, 3)
        self.assertEqual(len(self.messages), 1)

    async def test_mention_qg_reponse_discord_perdue_et_profils_doubles(self):
        await self.tableau.actualiser([self.a, {**self.a, 'type':'qualifiante', 'uid':self.a['hid']}])
        envoi = self.salon.send.side_effect
        async def perdu(**options):
            await envoi(**options)
            raise TimeoutError()
        self.salon.send.side_effect = perdu
        self.tableau.controle_qg = Mock()
        self.tableau.controle_qg.relever.return_value = (104, 10, {self.a['hid']})
        with self.assertRaises(TimeoutError):
            await self.tableau.actualiser_qg()
        self.salon.send.side_effect = envoi
        reprise = form.TableauFormations(self.bot, self.guild.id, self.chemin)
        reprise.controle_qg = self.tableau.controle_qg
        await reprise.actualiser_qg()
        self.assertEqual(self.salon.send.await_count, 2)
        self.assertEqual(reprise.etat['visites_qg'], {self.a['hid']:0})
        self.assertEqual(len(self.messages), 1)

    def test_groupes_relus_et_derniere_connexion_en_cache(self):
        membre = {'uniqueId':self.a['hid'], 'name':'Alpha', 'online':False}
        profil = {'profileVisible':True, 'online':False, 'lastAccessTime':'2026-11-23T08:00:00Z'}
        cache = {}
        with patch.object(form, 'request_json', side_effect=[[membre], profil, [membre], [{**membre,'online':True}], [membre], profil]) as api:
            a = form.presences_habbo([self.a], cache)
            b = form.presences_habbo([self.a], cache)
            c = form.presences_habbo([self.a], cache)
            d = form.presences_habbo([self.a], cache)
        self.assertEqual(a, b)
        self.assertTrue(c[form.cle(self.a)].en_ligne)
        self.assertEqual(a, d)
        self.assertEqual(api.call_count, 6)

    async def test_presence_publique_et_profil_masque_et_changement_de_pseudo(self):
        groupe = [{"uniqueId":"hhfr-1","name":"PseudoModifié","online":True}]
        with patch.object(form, "request_json", side_effect=[groupe, {"online":True,"profileVisible":False}]):
            lecture = form.presences_habbo([self.a, self.b])
        self.assertEqual(lecture, {form.cle(self.a):form.Presence(True), form.cle(self.b):form.Presence(None)})
        instant = time.time()
        await self.tableau.actualiser([{**self.a,"en_ligne":True,"observation":instant}])
        self.tableau.noter_presence(form.cle(self.a), instant - 60, form.Presence(False))
        self.assertEqual(self.tableau.presences[form.cle(self.a)], (instant, form.Presence(True)))

    async def test_derniere_connexion_publique_hiver_ete_masquage_et_deux_listes(self):
        membre = {"uniqueId":"hhfr-1", "name":"Alpha", "online":False}
        qualif = {**self.a, "type":"qualifiante"}
        profil = {"profileVisible":True, "online":False, "lastAccessTime":"2026-11-22T23:03:59.000+0000"}
        with patch.object(form, "request_json", side_effect=[[membre], [membre], profil]) as req:
            lectures = form.presences_habbo([self.a, qualif])
        self.assertEqual(req.call_count, 3)
        for fiche in [self.a, qualif]:
            self.assertEqual(lectures[form.cle(fiche)].dernier_acces.strftime("%d/%m/%y %H:%M"), "23/11/26 00:03")
        ete = form.presence_publique({**profil, "lastAccessTime":"2026-07-25T10:00:00Z"}, membre)
        self.assertEqual(ete.dernier_acces.hour, 12)
        for prive in [{**profil, "profileVisible":False}, {**profil, "lastAccessTime":"invalide"},
                      {**profil, "lastAccessTime":"2026-01-01T09:00:00"}]:
            lecture = form.presence_publique(prive, membre)
            self.assertIsNone(lecture.dernier_acces)
            carte, _ = form.carte_liste([self.a], {form.cle(self.a):lecture}, "gdp")
            self.assertIn("Dernière connexion non communiquée", carte.description)

    async def test_menu_page_deux_et_nom_absent_du_dernier_releve(self):
        candidats = [{**self.a, "pseudo":f"Police{i:02}", "uid":str(i)} for i in range(28)]
        await self.tableau.actualiser(candidats)
        await self.tableau.actualiser([])
        vue = form.PagesFormations(self.tableau, 200, historique=False, page=1)
        select = next(c for c in vue.children if isinstance(c, discord.ui.Select))
        self.assertEqual([o.label for o in select.options], ["Police25", "Police26", "Police27"])
        self.assertEqual(len(self.messages), 1)

    async def test_ordre_identique_du_tableau_et_du_menu_pour_les_deux_formations(self):
        instant = time.time()
        for genre in form.TYPES:
            ancien = {**self.a, "type": genre, "depuis": "2026-11-16", "pseudo": "Ancien"}
            recents = [{**self.b, "type": genre, "uid": str(i), "pseudo": nom} for i, nom in
                       enumerate(["Bravo", "Alpha", "Inconnu"], start=2)]
            await self.tableau.actualiser([ancien, *recents])
            self.tableau.noter_presence(form.cle(ancien), instant, form.Presence(False))
            self.tableau.noter_presence(form.cle(recents[0]), instant, form.Presence(True))
            self.tableau.noter_presence(form.cle(recents[1]), instant, form.Presence(True))
            self.tableau.noter_presence(form.cle(recents[2]), instant - 300, form.Presence(True))
            vue = form.BoutonsFormations(self.tableau, genre)
            attendus = ["Alpha", "Bravo", "Ancien", "Inconnu"]
            self.assertEqual([ligne.split("**")[1] for ligne in vue.carte.description.split("\n\n")], attendus)
            self.assertEqual(vue.carte.footer.text, "4 en attente · 2 en ligne")
            for retrait in (False, True):
                menu = form.PagesFormations(self.tableau, 99, historique=False, genre=genre, retrait=retrait)
                select = next(c for c in menu.children if isinstance(c, discord.ui.Select))
                self.assertEqual([o.label for o in select.options], attendus)

    def test_longue_liste_apercu_et_fichier_complet_en_texte(self):
        fiches = [{**self.a, "uid":str(i), "pseudo": f"Police_{i:03}"} for i in range(100)]
        carte, fichier = form.carte_liste(fiches, {}, "gdp")
        self.assertIn("**Police\\_000**", carte.description)
        self.assertNotIn("Police\\_005", carte.description)
        self.assertEqual(carte.footer.text, "100 en attente")
        self.assertEqual(fichier.decode().split("\n\n"), [f"{f['pseudo']} (P.Adj depuis 21/11/26)\nPrésence indisponible" for f in fiches])

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
        adjoint = {**self.a, "depuis": "2026-11-16"}
        await self.tableau.actualiser([adjoint, qualif])
        public = next(iter(self.messages.values())).vue
        self.assertEqual(public.carte.description, "**Alpha** (P.Adj depuis 16/11/26)\nPrésence indisponible")
        menu = next(c for c in public.walk_children() if isinstance(c, discord.ui.Select))
        self.assertEqual([(o.label, o.default) for o in menu.options],
                         [("Formation Gardien de la paix",False),("Formation qualifiante",False)])
        inter = Mock(user=Mock(id=888), response=Mock(send_message=AsyncMock()), is_expired=Mock(return_value=False),
                     edit_original_response=AsyncMock())
        menu._values = ["qualifiante"]
        await menu.callback(inter)
        opts = inter.response.send_message.await_args.kwargs
        self.assertTrue(opts["ephemeral"])
        self.assertEqual(opts["view"].carte.description, "**Charlie** (GPX depuis 21/11/26)\nPrésence indisponible")
        for vue, titre in [(public, "Formation Gardien de la paix"), (opts["view"], "Formation qualifiante")]:
            self.assertIn(f"### 🎓 {titre}", [c.content for c in vue.walk_children() if isinstance(c, discord.ui.TextDisplay)])
        self.tableau.noter_presence(form.cle(qualif), time.time(), form.Presence(True))
        await self.tableau.actualiser([])
        self.assertEqual(inter.edit_original_response.await_args.kwargs["view"].carte.description,
                         "**Charlie** 🟢 en ligne (GPX depuis 21/11/26)")
        self.assertEqual(len(self.messages), 1)
        self.tableau.terminer(form.cle(qualif), SimpleNamespace(id=888, display_name="Formateur"),
                              sujet="https://www.pn-rpg.fr/t45-formation")
        archive = form.PagesFormations(self.tableau, 888, historique=True)
        self.assertIn("Formation qualifiante", archive.carte.description)
        self.assertEqual([f["pseudo"] for f in self.tableau.liste()], ["Alpha"])

    def test_liens_des_sujets_et_messages_du_forum(self):
        base = "https://www.pn-rpg.fr"
        for entree, attendu in (
            ("https://pn-rpg.fr/t12p25-rapport#104", "/t12p25-#104"),
            ("http://pn-rpg.forumactif.com/t12-formation?sid=test", "/t12-"),
            ("www.pn-rpg.fr/viewtopic?t=12&start=25#104", "/t12p25-#104"),
            (base + "/viewtopic?p=104#104", "/viewtopic?p=104#104"),
        ):
            with self.subTest(entree=entree):
                self.assertEqual(form.lien_sujet_forum(entree), base + attendu)
        for invalide in ("", base, base + "/f12-forum", base + "/viewtopic?p=0",
                         "https://exemple.fr/t12-rapport", "https://pn-rpg.fr@exemple.fr/t12-rapport",
                         "https://www.pn-rpg.fr.exemple.fr/t12-rapport"):
            with self.subTest(invalide=invalide):
                self.assertIsNone(form.lien_sujet_forum(invalide))

    async def test_formation_faite_exige_un_sujet_pour_les_deux_formations(self):
        membre = Mock(spec=discord.Member, id=77, display_name="Formatrice", roles=[self.formateur])
        membre.guild_permissions.administrator = False
        inter = SimpleNamespace(user=membre, guild_id=self.guild.id, channel_id=form.SALON, channel=self.salon,
                                response=Mock(send_modal=AsyncMock(), edit_message=AsyncMock(), send_message=AsyncMock(),
                                              defer=AsyncMock()), edit_original_response=AsyncMock())
        for genre in form.TYPES:
            fiche = {**self.a, "type": genre}
            k = form.cle(fiche)
            await self.tableau.actualiser([fiche])
            def selection(vue):
                choix = next(c for c in vue.children if isinstance(c, discord.ui.Select))
                choix._values = [k]
                return choix.callback(inter)
            await selection(form.PagesFormations(self.tableau, membre.id, historique=False, genre=genre))
            modal = inter.response.send_modal.await_args.args[0]
            self.assertTrue(modal.lien.required)
            with self.assertRaises(ValueError):
                self.tableau.terminer(k, membre)
            modal.lien._value = "https://exemple.fr/t12-formation"
            await modal.on_submit(inter)
            rep = inter.response.edit_message.await_args.kwargs
            self.assertIn("corriger le lien", rep["content"])
            self.assertIn(k, self.tableau.etat["attente"])
            await selection(rep["view"])
            modal = inter.response.send_modal.await_args.args[0]
            modal.lien._value = "https://pn-rpg.fr/t12p25-formation#104"
            await modal.on_submit(inter)
            rep = inter.response.edit_message.await_args.kwargs
            self.assertIn("https://www.pn-rpg.fr/t12p25-#104", rep["content"])
            self.assertIn(k, self.tableau.etat["attente"])
            await rep["view"].confirmer.callback(inter)
            self.assertEqual(self.tableau.etat["historique"][k]["sujet"], "https://www.pn-rpg.fr/t12p25-#104")
            self.assertNotIn(k, self.tableau.etat["attente"])

    async def test_ajout_du_retrait_sur_un_tableau_existant_sans_nouvelle_mention(self):
        await self.tableau.actualiser([self.a])
        msg = next(iter(self.messages.values()))
        # L'ancien tableau commun avait la même signature que la vue sans bouton de retrait.
        sans_retrait = form.BoutonsFormations(self.tableau, auteur=77)
        msg.components = sans_retrait.children
        self.tableau.sauver(rendu=sans_retrait.signature())
        reprise = form.TableauFormations(self.bot, self.guild.id, self.chemin)
        await reprise.actualiser([])
        msg.edit.assert_awaited_once()
        self.assertEqual(msg.edit.await_args.kwargs["allowed_mentions"].to_dict()["parse"], [])
        self.assertIn("pn:formas:retirer", [getattr(c, "custom_id", None) for c in msg.vue.walk_children()])
        self.salon.send.assert_awaited_once()

    async def test_retrait_reserve_aux_trois_roles_et_revalide_apres_selection(self):
        qualif = {**self.a, "type":"qualifiante", "uid":"hhfr-1"}
        await self.tableau.actualiser([self.a, qualif])
        public = next(iter(self.messages.values())).vue
        retirer = next(c for c in public.walk_children() if getattr(c, "custom_id", None) == "pn:formas:retirer")
        menu = next(c for c in public.walk_children() if isinstance(c, discord.ui.Select))
        menu._values = ["qualifiante"]
        membre = Mock(spec=discord.Member)
        membre.id, membre.display_name, membre.guild_permissions.administrator = 77, "Membre", False
        inter = SimpleNamespace(user=membre, guild_id=self.guild.id, guild=self.guild,
                                channel_id=form.SALON, channel=self.salon, is_expired=lambda:False,
                                response=Mock(send_message=AsyncMock(), defer=AsyncMock()), edit_original_response=AsyncMock())
        self.guild.get_member.return_value = membre
        for role in form.ROLES_RETRAIT:
            membre.roles = [Mock(id=role)]
            await retirer.callback(inter)
            reponse = inter.response.send_message.await_args.kwargs
            self.assertTrue(reponse["ephemeral"])
            self.assertTrue(reponse["view"].retrait)
            self.assertEqual(reponse["view"].genre, "gdp")
            await menu.callback(inter)
            vue = inter.response.send_message.await_args.kwargs['view']
            self.assertTrue(any(getattr(c, 'label', None) == 'Retirer de la liste' for c in vue.walk_children()))
            self.assertTrue(await self.tableau.autoriser(inter, retrait=True))
        confirmation = form.Confirmation(self.tableau, membre.id, form.cle(qualif), retrait=True)
        for admin in [False, True]:
            membre.roles, membre.guild_permissions.administrator = [self.formateur], admin
            await retirer.callback(inter)
            refus = inter.response.send_message.await_args
            self.assertIn("réservé aux Hauts gradés", refus.args[0])
            self.assertEqual(refus.kwargs, {"ephemeral": True})
            await menu.callback(inter)
            vue = inter.response.send_message.await_args.kwargs['view']
            self.assertFalse(any(getattr(c, 'label', None) == 'Retirer de la liste' for c in vue.walk_children()))
            await confirmation.confirmer.callback(inter)
            self.assertIn(form.cle(qualif), self.tableau.etat['attente'])
            inter.response.defer.assert_not_awaited()
        membre.roles = [Mock(id=1552758582141784225)]
        await confirmation.confirmer.callback(inter)
        reprise = form.TableauFormations(self.bot, self.guild.id, self.chemin)
        await reprise.actualiser([qualif, self.a])
        self.assertEqual(reprise.liste('qualifiante'), [])
        self.assertEqual(len(reprise.liste('gdp')), 1)
        self.assertEqual(reprise.etat['historique'], {})
        self.assertEqual(reprise.etat['retraits'][form.cle(qualif)]['par_id'], '77')
        self.salon.send.assert_awaited_once()

    async def test_cadet_conserve_sa_destination_son_role_et_son_bouton(self):
        await pn_bot.PNBot.envoyer_alerte(self.bot, {"title": "Cadet", "_pn": {"palier": "cadet", "uid": "123"}})
        self.bot.channel.assert_awaited_once_with(pn_bot.ALERT_CHANNEL_ID)
        appel = self.salon.send.await_args.kwargs
        self.assertEqual(appel["content"], f"<@&{pn_bot.ROLE_HAUT_GRADE_ID}>")
        self.assertEqual(appel["view"].children[0].custom_id, "pn:badge:123")
