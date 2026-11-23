import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'serveur'))
from habbo_http import ApiRequestError
from presence_formations_qg import CONTROLE, PRESENCES, ControlePresenceQG


def joueur(uid, valeur, hid=None):
    return {'user': {'id': uid, 'unique_id': hid}, 'variable': {'value': str(valeur)}}


class SondesWIRED(unittest.TestCase):
    def setUp(self):
        p = patch('presence_formations_qg.time.sleep')
        p.start()
        self.addCleanup(p.stop)
        self.api = Mock()
        self.sonde = ControlePresenceQG(self.api)

    def test_deux_joueurs_et_echos_perimes_seule_la_globale_est_ecrite(self):
        a = {'uid':'1', 'hid':'hhfr-1'}
        b = {'uid':'hhfr-2','hid':'hhfr-2'}
        self.api.side_effect = [
            {'value':'103'}, {'items':[joueur(1,103)]}, {'value':'104'},
            {'items':[joueur(1,104),joueur(2,103)]},
            {'user':{'id':2,'unique_id':'hhfr-2'}},
            {'value':'105'}, {'items':[joueur(1,105),joueur(2,105)]},
        ]
        self.assertEqual(self.sonde.relever([a,b]), (104,1,{'hhfr-1'}))
        self.assertEqual(self.sonde.relever([a,b]), (105,2,{'hhfr-1','hhfr-2'}))
        ecritures = [c for c in self.api.call_args_list if c.kwargs.get('write')]
        self.assertEqual([(c.args, c.kwargs['method'], c.kwargs['body']) for c in ecritures],
                         [((CONTROLE,), 'PATCH', {'value':'104'}), ((CONTROLE,), 'PATCH', {'value':'105'})])

    def test_sortie_de_page_echos_tardifs_et_204_sans_nouvelle_ecriture(self):
        self.api.side_effect = [
            {'value':'99'}, {'items':[joueur(1,103)]}, {}, {'value':'104'},
            {'items':[joueur(1,103)]}, {'items':[joueur(i,104) for i in range(1,101)]},
            {'items':[joueur(101,104,'hhfr-test'),joueur(102,98)]},
        ]
        self.assertEqual(self.sonde.relever([{'uid':'hhfr-test','hid':'hhfr-test'}]), (104,101,{'hhfr-test'}))
        self.assertEqual(self.api.call_args.args, (PRESENCES, {'order_by':'value','order_dir':'desc','page':2,'size':100}))
        self.assertEqual(sum(bool(c.kwargs.get('write')) for c in self.api.call_args_list), 1)

    def test_pas_d_absence_sur_erreur_ou_page_incomplete(self):
        self.api.side_effect = [{'value':'103'}, {'items':[]}, {'value':'104'}, {'erreur':'aucun tableau'}]
        with self.assertRaises(ApiRequestError):
            self.sonde.relever([])
        self.api.side_effect = [{'value':'105'}, {'items':[joueur(1,106)]}]
        with self.assertRaises(ApiRequestError):
            self.sonde.relever([])
        self.assertIsNone(self.sonde.numero)

    def test_numero_non_reutilise_si_le_patch_a_perdu_sa_reponse(self):
        self.api.side_effect = [{'value':'103'}, {'items':[]}, ApiRequestError('timeout'),
                                {'value':'105'}, {'items':[joueur(1,105)]}]
        with self.assertRaises(ApiRequestError):
            self.sonde.relever([])
        self.assertEqual(self.sonde.relever([]), (105,1,set()))

    def test_acquittement_sans_valeur_et_identifiant_a_la_racine(self):
        self.api.side_effect = [{'value':'10'}, [], {'status':'ok'}, {'value':'11'},
                                [{'entity_id':5, 'value':'11'}]]
        self.assertEqual(self.sonde.relever([{'uid':'5'}]), (11,1,{'5'}))

    def test_localise_un_404_avant_de_toucher_au_compteur(self):
        self.api.side_effect = [{'value':'103'}, None]
        with self.assertRaises(ApiRequestError) as erreur:
            self.sonde.relever([])
        self.assertEqual((erreur.exception.status, erreur.exception.indication),
                         (404, 'variable de présence introuvable'))
        self.assertFalse(any(c.kwargs.get('write') for c in self.api.call_args_list))
