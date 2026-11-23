import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import alertes_grade as bot
from serveur.habbo_http import ApiRequestError


class PromotionsCadets(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dossier = Path(tmp.name)
        self.suivi = dossier / "badges_cadets.json"
        self.formations = dossier / "formations-pa.json"
        self.etat = dossier / "etat_grades.json"
        self.suivi.write_text("{}", encoding="utf-8")
        self.nom = "Cadette"
        self.humeur = "[PN] Cadette"
        self.points = 200
        self.badge = True
        self.superieur = None
        self.autre_groupe = None
        self.groupes_profil = []
        self.profil_visible = True
        self.envois = []
        self.liste = []
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for k, v in {"ROOM": "salle", "KEY": "test", "ETAT": self.etat, "BADGES_CADETS": self.suivi,
                     "req": self.requete, "motto_of": lambda name: self.humeur,
                     "all_values": lambda var: {"1": (self.nom, self.points if var == bot.VAR_PR else 0)},
                     "post": self.envoyer}.items():
            self.stack.enter_context(patch.object(bot, k, v))
        self.stack.enter_context(patch.dict(bot.os.environ, {"TEST": "0"}))

    def envoyer(self, embed):
        self.envois.append(embed)
        return True

    def requete(self, url):
        if url.endswith("/users/hhfr-test"):
            return {"profileVisible": self.profil_visible}
        if url.endswith("/users/hhfr-test/groups"):
            if isinstance(self.groupes_profil, Exception):
                raise self.groupes_profil
            return self.groupes_profil
        groupe = url.split("/")[-2]
        if groupe not in (bot.GROUPE_CADET, *bot.GROUPES_SUPERIEURS):
            self.fail(f"Appel inattendu : {url}")
        rows = [{"uniqueId": "owner", "name": "Direction", "motto": ""}]
        if (groupe == bot.GROUPE_CADET and self.badge) or groupe in (self.superieur, self.autre_groupe):
            rows.append({"uniqueId": "hhfr-test", "name": self.nom, "motto": self.humeur,
                         "memberSince": "2004-01-01T00:00:00Z"})
        return rows

    def passage(self, heure):
        with patch.object(bot, "NOW", datetime.fromisoformat(heure).astimezone(timezone.utc)):
            with contextlib.redirect_stdout(io.StringIO()):
                self.liste = bot.main(suivi_formations=self.formations)

    def registre(self):
        return json.loads(self.suivi.read_text(encoding="utf-8"))

    def test_adhesion_mardi_soir_eligible_jeudi_minuit_meme_apres_changement_humeur_ou_pseudo(self):
        self.passage("2026-11-17T23:59:00+01:00")
        self.nom, self.humeur = "AutrePseudo", "En pause"
        self.passage("2026-11-18T23:59:59+01:00")
        self.assertFalse(self.envois)
        self.passage("2026-11-19T00:00:00+01:00")
        self.passage("2026-11-19T00:02:00+01:00")
        self.assertEqual(len(self.envois), 1)
        self.assertIn("AutrePseudo", self.envois[0]["title"])
        self.assertEqual(self.envois[0]["footer"]["text"], "Badge Cadet depuis le 17/11/2026")
        self.assertIn({"name": "Ancienneté", "value": "Jour 3 (adhésion incluse)", "inline": True},
                      self.envois[0]["fields"])

    def test_registre_attributions_existantes_remplace_compteur_humeur_et_respecte_200_pr(self):
        bot.enregistrer(self.suivi, {"hhfr-test": {"pseudo": self.nom, "depuis": "2026-11-17"}})
        bot.enregistrer(self.etat, {"1": {"pseudo": self.nom, "grade": "cadet", "humeur": "X",
                                           "depuis": "2026-11-20T11:10:35+00:00", "alertes": {}}})
        self.points = 199
        self.passage("2026-11-20T12:15:00+01:00")
        self.assertFalse(self.envois)
        self.points = 200
        self.passage("2026-11-20T12:17:00+01:00")
        self.assertEqual(len(self.envois), 1)
        self.assertIn({"name": "Ancienneté", "value": "Jour 4 (adhésion incluse)", "inline": True},
                      self.envois[0]["fields"])

    def test_une_alerte_ancienne_ou_un_badge_pa_confirme_ne_repart_pas(self):
        for marque in ({"alertes": {"cadet": "2026-11-18"}}, {"badge": {"cadet": {"par": "HG"}}}):
            with self.subTest(marque=marque):
                bot.enregistrer(self.etat, {"1": {"pseudo": self.nom, **marque}})
                bot.enregistrer(self.suivi, {"hhfr-test": {"pseudo": self.nom, "depuis": "2026-11-17"}})
                self.passage("2026-11-20T12:17:00+01:00")
                self.assertFalse(self.envois)

    def test_un_bon_libelle_seul_ne_donne_aucun_droit_et_retrait_puis_retour_redemarre(self):
        self.passage("2026-11-17T08:00:00+01:00")
        self.badge = False
        self.passage("2026-11-20T00:00:00+01:00")
        self.assertNotIn("hhfr-test", self.registre())
        self.assertFalse(self.envois)
        self.badge = True
        self.passage("2026-11-20T10:00:00+01:00")
        self.assertEqual(self.registre()["hhfr-test"]["depuis"], "2026-11-20")
        self.passage("2026-11-21T23:59:59+01:00")
        self.assertFalse(self.envois)
        self.passage("2026-11-22T00:00:00+01:00")
        self.assertEqual(len(self.envois), 1)

    def test_omission_et_profil_prive_conservent_le_jour_sans_envoyer(self):
        self.passage("2026-11-17T08:00:00+01:00")
        self.badge = False
        self.groupes_profil = ApiRequestError("access", status=403)
        self.passage("2026-11-20T00:00:00+01:00")
        self.assertFalse(self.envois)
        self.assertEqual(self.registre()["hhfr-test"]["depuis"], "2026-11-17")
        self.groupes_profil, self.profil_visible = [], False
        self.passage("2026-11-20T00:01:00+01:00")
        self.assertFalse(self.envois)
        self.assertEqual(self.registre()["hhfr-test"]["depuis"], "2026-11-17")
        self.groupes_profil = [{"id": bot.GROUPE_CADET}]
        self.passage("2026-11-20T00:02:00+01:00")
        self.assertEqual(len(self.envois), 1)

    def test_badge_pa_remplace_cadet_et_ouvre_le_troisieme_jour_a_300_points(self):
        self.passage("2026-11-17T08:00:00+01:00")
        self.superieur, self.humeur, self.points = bot.GROUPE_PA, "En pause", 300
        self.passage("2026-11-20T23:59:00+01:00")
        self.passage("2026-11-21T23:59:59+01:00")
        self.assertFalse(self.liste)
        self.points = 299
        self.passage("2026-11-22T00:00:00+01:00")
        self.assertFalse(self.liste)
        self.nom, self.points = "NouveauPseudo", 300
        self.passage("2026-11-22T01:00:00+01:00")
        self.passage("2026-11-23T18:00:00+01:00")
        self.assertEqual([(f["pseudo"], f["uid"], f["pr"], f["depuis"]) for f in self.liste],
                         [("NouveauPseudo", "1", 300, "2026-11-20")])
        self.assertFalse(self.envois)

    def test_pa_attribution_confirmee_avant_demarrage_refus_grade_superieur_et_retrait(self):
        self.badge = False
        self.superieur, self.autre_groupe, self.points = bot.GROUPE_PA, bot.GROUPES_SUPERIEURS[1], 310
        bot.enregistrer(self.etat, {"1": {"pseudo": self.nom, "badge": {"cadet": {"le": "2026-11-17T23:59:00+01:00"}}}})
        self.passage("2026-11-20T00:00:00+01:00")
        self.assertFalse(self.liste)
        self.superieur, self.autre_groupe = None, None
        self.passage("2026-11-20T08:00:00+01:00")
        self.humeur = "[PN] Policier adjoint"
        self.passage("2026-11-21T12:00:00+01:00")
        self.assertFalse(self.liste)
        self.superieur = bot.GROUPE_PA
        self.passage("2026-11-21T23:57:00+01:00")
        self.passage("2026-11-22T23:59:59+01:00")
        self.assertFalse(self.liste)
        self.passage("2026-11-23T00:00:00+01:00")
        self.assertEqual([f["depuis"] for f in self.liste], ["2026-11-21"])

    def test_migration_reprend_la_date_confirmee_sans_poster_d_alerte_individuelle(self):
        self.badge, self.superieur, self.points = False, bot.GROUPE_PA, 300
        bot.enregistrer(self.etat, {"1": {"pseudo": self.nom, "alertes": {"pa": "ancien salon"},
                                          "badge": {"cadet": {"le": "2026-11-20T22:59:00+00:00"}}}})
        self.passage("2026-11-22T00:00:00+01:00")
        self.passage("2026-11-22T00:02:00+01:00")
        self.assertEqual([f["depuis"] for f in self.liste], ["2026-11-20"])
        self.assertFalse(self.envois)

    def test_pa_omission_en_profil_prive_differe_sans_perdre_la_date(self):
        self.badge, self.superieur, self.points = False, bot.GROUPE_PA, 300
        self.passage("2026-11-20T23:59:00+01:00")
        self.superieur, self.profil_visible = None, False
        self.passage("2026-11-22T00:00:00+01:00")
        self.assertFalse(self.liste)
        self.groupes_profil = [{"id": bot.GROUPE_PA}]
        self.passage("2026-11-22T00:02:00+01:00")
        self.assertEqual([f["depuis"] for f in self.liste], ["2026-11-20"])

    def test_gardien_qualifiante_troisieme_jour_sans_pr_ni_matricule(self):
        self.superieur, self.badge, self.points = bot.GROUPE_GDP, False, 0
        with patch.object(bot, "all_values", return_value={}):
            self.passage("2027-03-27T23:59:00+01:00")
            self.passage("2027-03-28T23:59:59+02:00")
            self.assertFalse(self.liste)
            self.passage("2027-03-29T00:00:00+02:00")
            self.assertEqual([(f["type"],f["pseudo"],f["depuis"]) for f in self.liste],
                             [("qualifiante",self.nom,"2027-03-27")])
            self.autre_groupe = bot.GROUPES_GRADES[0]
            self.passage("2027-03-29T00:02:00+02:00")
            self.assertFalse(self.liste)

    def test_exclusion_par_identifiant_stable_pour_les_deux_formations(self):
        def remplace_id(url):
            return [{**r,"uniqueId":next(iter(bot.EXCLUS_FORMATIONS)) if r["uniqueId"]=="hhfr-test" else r["uniqueId"]}
                    for r in self.requete(url)]
        self.badge, self.superieur, self.points = False, bot.GROUPE_PA, 350
        with patch.object(bot,"req",side_effect=remplace_id):
            self.passage("2026-11-20T00:00:00+01:00")
            self.passage("2026-11-23T00:00:00+01:00")
            self.assertFalse(self.liste)
            self.superieur, self.nom = bot.GROUPE_GDP, "NouveauPseudo"
            self.passage("2026-11-23T20:00:00+01:00")
            self.passage("2026-11-26T20:00:00+01:00")
            self.assertFalse(self.liste)

    def test_changement_heure_ete(self):
        self.passage("2027-03-27T23:59:00+01:00")
        self.passage("2027-03-28T23:59:59+02:00")
        self.assertFalse(self.envois)
        self.passage("2027-03-29T00:00:00+02:00")
        self.assertEqual(len(self.envois), 1)

    def test_reponse_groupe_incomplete_ne_modifie_pas_les_registres(self):
        self.passage("2026-11-17T18:00:00+01:00")
        avant = (self.suivi.read_bytes(), self.etat.read_bytes())
        with patch.object(bot, "req", return_value=[]), self.assertRaises(ApiRequestError):
            self.passage("2026-11-20T13:00:00+01:00")
        self.assertEqual((self.suivi.read_bytes(), self.etat.read_bytes()), avant)
        self.assertFalse(self.envois)
