import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'serveur'))
from bonus_notations import BonusNotations, parse_official


class BonusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.request = dict(uid='17', week='2026-11-20', pseudo='Alice', author='Sami', notation='n1')
        self.roster = {'members': [{'profileUrl': 'https://www.pn-rpg.fr/u17', 'pseudo': 'Alice'}]}
        self.requests = [self.request]
        self.writes = []
        self.value = 32
        self.fail = None
        self.read_fail = False
        self.missing_value = False
        self.catalog = {'users': ['xp_paie', 'xp']}

    def api(self, path, **kwargs):
        if path == '/variables':
            return self.catalog
        if path == '/variables/user/xp_paie/users':
            return {'items': [{'user': {'id': 42}}]}
        self.assertEqual(path, '/variables/user/xp_paie/users/42')
        if kwargs.get('write'):
            self.writes.append(kwargs)
            self.value = int(kwargs['body']['value'])
            if self.fail:
                raise self.fail
            return {'value': str(self.value)}
        if self.read_fail:
            raise TimeoutError()
        if self.missing_value:
            return None
        return {'value': str(self.value)}

    def service(self):
        return BonusNotations(self.root / 'db.sqlite', self.root / 'status.json', self.api,
                              lambda var: [('42', 'Alice', 1)], threading.Lock(),
                              lambda: (self.roster, self.requests))

    def test_double_click_correction_and_restart_credit_once(self):
        bot = self.service()
        bot.cycle()
        self.request['notation'] = 'another-note-same-week'
        bot.cycle()
        self.service().cycle()
        self.assertEqual(self.value, 37)
        self.assertEqual(len(self.writes), 1)
        status = json.loads((self.root / 'status.json').read_text())
        self.assertEqual(status['receipts']['u17:2026-11-20']['status'], 'credited')

    def test_health_probe_does_not_credit_anyone(self):
        self.requests.clear()
        self.service().cycle()
        self.assertEqual(self.writes, [])
        status = json.loads((self.root / 'status.json').read_text())
        self.assertEqual(status['health']['status'], 'ready')
        self.assertNotIn('value', status['health'])

    def test_timeout_after_remote_write_never_replayed(self):
        self.fail = TimeoutError('response lost after write')
        bot = self.service()
        bot.cycle()
        self.fail = None
        self.service().cycle()
        self.assertEqual(len(self.writes), 1)
        status = json.loads((self.root / 'status.json').read_text())
        self.assertEqual(status['receipts']['u17:2026-11-20']['status'], 'uncertain')

    def test_read_timeout_is_retryable_without_writing_zero(self):
        self.read_fail = True
        bot = self.service()
        bot.cycle()
        self.assertEqual(self.writes, [])
        self.read_fail = False
        bot.cycle()
        self.assertEqual(self.value, 37)

    def test_missing_variable_does_not_fall_back_to_xp(self):
        self.catalog = {'users': ['xp']}
        self.service().cycle()
        self.assertEqual(self.writes, [])

    def test_missing_member_value_is_never_assumed_zero(self):
        self.missing_value = True
        self.service().cycle()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.value, 32)

    def test_changed_forum_profile_cannot_pay_same_habbo_twice(self):
        bot = self.service()
        bot.cycle()
        self.roster['members'][0]['profileUrl'] = 'https://www.pn-rpg.fr/u18'
        self.request['uid'] = '18'
        bot.cycle()
        self.assertEqual(len(self.writes), 1)
        status = json.loads((self.root / 'status.json').read_text())
        self.assertEqual(status['receipts']['u18:2026-11-20']['status'], 'credited')

    def test_wrong_profile_does_not_credit_another_member(self):
        self.request['pseudo'] = 'Bob'
        self.service().cycle()
        self.assertEqual(self.writes, [])

    def test_request_content_cannot_choose_amount_or_variable(self):
        self.request.update(amount=500, variable='xp', habbo_id='999')
        self.service().cycle()
        self.assertEqual(self.value, 37)

    def test_new_week_can_receive_a_new_bonus(self):
        self.request['week'] = '2026-11-13'
        bot = self.service()
        bot.cycle()
        self.request['week'] = '2026-11-20'
        bot.cycle()
        self.assertEqual(self.value, 42)

    def test_crash_before_confirm_is_uncertain_not_replayed(self):
        bot = self.service()
        bot.cycle()
        with bot.connect() as db:
            db.execute("UPDATE credits SET status='writing'")
        self.service().cycle()
        self.assertEqual(len(self.writes), 1)

    def test_only_the_official_post_queue_is_read(self):
        roster = quote(json.dumps(self.roster))
        queue = quote(json.dumps({'version': 1, 'requests': self.requests}))
        s = f'<div id="p13" class="post post--13"><div id="pn-roster-store" data-pn-roster="{roster}"></div></div>'
        s += f'<div id="p25"><div id="pn-bonus-store" data-pn-bonus="{queue}"></div></div>'
        self.assertEqual(parse_official(s)[1], [])
        with self.assertRaises(ValueError):
            parse_official(s + '<div id="p13" class="post--13"></div>')


if __name__ == '__main__':
    unittest.main()
