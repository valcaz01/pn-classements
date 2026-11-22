import base64
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "serveur"))
from suivi_qg import SuiviQG, decoder, inscription, prive, signature


class LecturePrivee(unittest.TestCase):
    def test_inscription_limitee_a_un_tunnel_signe(self):
        jeton = "essai-local"
        url = "https://trois-mots-ici.trycloudflare.com"
        date = "1795368000"
        mac = signature(jeton, f"annonce {date} {url}".encode())
        self.assertEqual(inscription(f"PN-QG/1 {date} {url} {mac}",jeton),(int(date),url))
        for invalide in (url+"/autre",url+".example.org","http://127.0.0.1"):
            self.assertIsNone(inscription(f"PN-QG/1 {date} {invalide} {mac}",jeton))
        self.assertIsNone(inscription(f"PN-QG/1 {date} {url} {mac}","autre"))

    def test_reponse_liee_a_la_demande_et_au_contenu(self):
        nonce, jeton = "nouveau","essai"
        png = b"\x89PNG\r\n\x1a\nillustration"
        raw = json.dumps({"nonce":nonce,"texte":"26 personnes","png":base64.b64encode(png).decode()}).encode()
        mac = signature(jeton, b"reponse nouveau\n"+raw)
        self.assertEqual(decoder(raw,mac,nonce,jeton),("26 personnes",png))
        with self.assertRaises(ValueError):
            decoder(raw,mac,"precedent",jeton)
        with self.assertRaises(ValueError):
            decoder(raw.replace(b"26",b"60"),mac,nonce,jeton)

    def test_salon_prive_sans_acces_individuel_ou_autre_role(self):
        everyone, direction, robot, autre = (Mock(id=i) for i in range(4))
        droits = lambda voir: SimpleNamespace(view_channel=voir)
        canal=Mock(guild=Mock(default_role=everyone),overwrites={everyone:droits(False),direction:droits(True),robot:droits(True)})
        canal.overwrites_for.side_effect=lambda role:canal.overwrites[role]
        canal.permissions_for.return_value=SimpleNamespace(read_message_history=True)
        self.assertTrue(prive(canal,direction,robot))
        canal.overwrites[autre]=droits(True)
        self.assertFalse(prive(canal,direction,robot))
        del canal.overwrites[autre]
        canal.overwrites[everyone]=droits(None)
        self.assertFalse(prive(canal,direction,robot))


class DiscordQG(unittest.IsolatedAsyncioTestCase):
    async def test_meme_panneau_et_ancienne_piece_jointe_retiree_hors_ligne(self):
        import tempfile
        with tempfile.TemporaryDirectory() as rep:
            qg=SuiviQG(Mock(),7,Path(rep)/"etat")
            qg.contexte=AsyncMock(return_value=(Mock(),Mock()))
            msg=Mock(edit=AsyncMock(),jump_url="lien")
            qg.panneau=AsyncMock(return_value=msg)
            qg.obtenir=AsyncMock(side_effect=[("relevé",b"image"),("Habbo hors ligne",None)])
            self.assertEqual(await qg.actualiser(),"lien")
            await qg.actualiser()
            a,b=msg.edit.await_args_list
            self.assertEqual(len(a.kwargs["attachments"]),1)
            self.assertEqual(a.kwargs["embed"].image.url,"attachment://navigateur-habbo.png")
            self.assertEqual(b.kwargs["attachments"],[])
            self.assertIsNone(b.kwargs["embed"].image.url)
            self.assertIn("hors ligne",b.kwargs["embed"].description)
            self.assertEqual(qg.panneau.await_count,2)

    async def test_requete_dans_autre_salon_refusee_avant_lecture(self):
        import tempfile
        with tempfile.TemporaryDirectory() as rep:
            qg=SuiviQG(Mock(),7,Path(rep)/"etat")
            qg.etat["salon"]=9
            inter=SimpleNamespace(guild_id=7,channel_id=10,response=Mock(send_message=AsyncMock()))
            self.assertFalse(await qg.autoriser(inter))
            inter.response.send_message.assert_awaited_once()
