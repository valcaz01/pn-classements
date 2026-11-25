import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "serveur"))
import promotions_qg as promotions
from formations import TableauFormations


class PromotionsQG(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.chemin = self.repo / "rappels.json"
        self.role = 567
        self.client = Mock()
        self.client.user.id = 123
        self.ch = Mock()
        self.client.channel = AsyncMock(return_value=self.ch)
        self.ch.guild.get_role.return_value.mentionable = True
        self.alerte = Mock(id=234, author=self.client.user, components=[
            SimpleNamespace(children=[SimpleNamespace(custom_id="pn:badge:1", disabled=False)])])
        self.messages = [self.alerte]
        async def historique(**options):
            for msg in reversed(self.messages):
                yield msg
        self.ch.history.side_effect = historique
        self.ch.fetch_message = AsyncMock(return_value=self.alerte)
        async def envoyer(texte, **options):
            msg = SimpleNamespace(author=self.client.user, reference=SimpleNamespace(message_id=options["reference"].id),
                                  id=345, created_at=datetime.fromtimestamp(time.time(), timezone.utc), content=texte,
                                  components=[])
            self.messages.append(msg)
            return msg
        self.ch.send = AsyncMock(side_effect=envoyer)
        self.initial = {"grade": "cadet", "pseudo": "Alpha", "alertes": {"cadet": "2026-11-23T00:02:00+01:00"}}
        (self.repo / "etat_grades.json").write_text(json.dumps({"1": self.initial}), encoding="utf-8")
        (self.repo / "badges_cadets.json").write_text(json.dumps({
            "hhfr-1": {"pseudo": "Alpha", "depuis": "2026-11-21"}}), encoding="utf-8")
        self.rappel = self.ouvrir()
        self.instant = 0
        self.horloge_qg = patch("presence_formations_qg.monotonic", side_effect=lambda: self.instant)
        self.horloge_qg.start()
        self.addCleanup(self.horloge_qg.stop)
        self.disponibles = {"hhfr-1"}
        self.api = patch.object(promotions, "cadets_en_ligne", side_effect=lambda host: self.disponibles)
        self.api.start()
        self.addCleanup(self.api.stop)

    def ouvrir(self):
        return promotions.AlertesBadgesQG(self.client, 456, self.role, self.repo, self.chemin, "www.habbo.fr", 3600)

    async def sonde(self, presents):
        self.rappel.observer(self.rappel.demandes(), presents)
        if self.rappel.tache is not None:
            await self.rappel.tache
        self.instant += 30

    async def sejour(self):
        for _ in range(7):
            await self.sonde({"hhfr-1"})

    async def test_depart_avant_trois_minutes_repart_de_zero(self):
        for _ in range(6):
            await self.sonde({"hhfr-1"})
        self.ch.send.assert_not_awaited()
        await self.sonde(set())
        for _ in range(6):
            await self.sonde({"hhfr-1"})
        self.ch.send.assert_not_awaited()
        await self.sonde({"hhfr-1"})
        self.ch.send.assert_awaited_once()

    async def test_les_deux_presences_et_le_bouton_actif_declenchent_une_seule_reponse(self):
        await self.sonde(set())
        self.disponibles.clear()
        await self.sejour()
        self.disponibles.add("hhfr-1")
        bouton = self.alerte.components[0].children[0]
        bouton.disabled = True
        await self.sonde({"hhfr-1"})
        self.ch.send.assert_not_awaited()
        bouton.disabled = False
        self.rappel.recherches.clear()
        await self.sonde({"hhfr-1"})
        await self.sonde({"hhfr-1"})
        self.ch.send.assert_awaited_once()
        self.assertEqual(self.ch.send.await_args.kwargs["allowed_mentions"].to_dict(), {"roles": [self.role], "parse": []})
        self.assertIs(self.ch.send.await_args.kwargs["reference"], self.alerte)
        (self.repo / "etat_grades.json").write_text(json.dumps({"1": {**self.initial, "badge": {"cadet": {"le": "remis"}}}}))
        self.assertEqual(self.rappel.demandes(), [])

    @patch.object(time, "time", return_value=1_800_000_000)
    async def test_reponse_perdue_redemarrage_et_retour_avant_une_heure(self, horloge):
        debut = horloge.return_value
        envoi = self.ch.send.side_effect
        async def reponse_perdue(*args, **options):
            await envoi(*args, **options)
            raise TimeoutError()
        self.ch.send.side_effect = reponse_perdue
        with self.assertLogs("pn.promotions.qg", level="ERROR"):
            await self.sejour()
        self.assertTrue(self.rappel.etat["1"]["envoi"])
        self.rappel = self.ouvrir()
        self.ch.send.side_effect = envoi
        await self.sejour()
        self.assertEqual(self.rappel.etat["1"]["dernier"], debut)
        await self.sonde(set())
        await self.sonde(set())
        horloge.return_value = debut + 3599
        await self.sonde({"hhfr-1"})
        horloge.return_value = debut + 7200
        await self.sonde({"hhfr-1"})
        self.ch.send.assert_awaited_once()
        await self.sonde(set())
        await self.sonde({"hhfr-1"})
        self.ch.send.assert_awaited_once()
        await self.sonde(set())
        await self.sonde(set())
        await self.sejour()
        self.assertEqual(self.ch.send.await_count, 2)

    async def test_badge_deja_attribue_pendant_verification(self):
        self.ch.fetch_message.return_value = Mock(components=[SimpleNamespace(children=[
            SimpleNamespace(custom_id="pn:badge-fait:1", disabled=True)])])
        await self.sejour()
        self.ch.send.assert_not_awaited()
        self.assertNotIn("dernier", self.rappel.etat["1"])

    async def test_une_sonde_partagee_fonctionne_sans_stagiaire(self):
        formateurs = TableauFormations(self.client, 12, self.repo / "formations.json", promotions_qg=self.rappel)
        formateurs.controle_qg = Mock()
        formateurs.controle_qg.relever.return_value = (107, 4, {"hhfr-1"})
        formateurs.actualiser_rendu = AsyncMock()
        with patch("formations.verifier_salon"):
            await formateurs.actualiser_qg()
            await self.rappel.tache
            await formateurs.actualiser_qg()
        formateurs.controle_qg.relever.assert_called_once_with(self.rappel.demandes())
        self.assertEqual(formateurs.au_qg(), set())
        self.ch.send.assert_not_awaited()

    def test_autre_adhesion_et_humeur_seule_ne_rejouent_pas_l_alerte(self):
        (self.repo / "badges_cadets.json").write_text(json.dumps({"hhfr-1": {"pseudo": "Alpha", "depuis": "2026-11-24"}}))
        self.assertEqual(self.rappel.demandes(), [])
        (self.repo / "badges_cadets.json").write_text("{}")
        self.assertEqual(self.rappel.demandes(), [])

    def test_statut_public_et_exclusion_du_grade_deja_recu(self):
        self.api.stop()
        with patch.object(promotions, "request_json", side_effect=[
            [{"uniqueId": "a", "online": True}, {"uniqueId": "b", "online": False}, {"uniqueId": "c", "online": True}],
            [{"uniqueId": "c"}],
        ]):
            self.assertEqual(promotions.cadets_en_ligne("www.habbo.fr"), {"a"})
