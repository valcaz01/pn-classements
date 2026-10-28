"""Simulation hors ligne du reset hebdomadaire : aucune requête Habbo ou Discord."""

import ast
import asyncio
import json
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo


SOURCE = Path(__file__).resolve().parents[1] / "serveur" / "pn_bot.py"
PARIS = ZoneInfo("Europe/Paris")


def load_functions():
    """Charge uniquement les fonctions testées, sans démarrer le bot Discord."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    wanted = {"monday_of", "set_values"}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    bot = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PNBot")
    nodes.append(next(n for n in bot.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "weekly"))
    namespace = {"PARIS": PARIS, "timedelta": timedelta, "asyncio": asyncio, "urllib": urllib,
                 "time": SimpleNamespace(sleep=lambda _: None)}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace


def read_json(path, default):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class WeeklyResetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.ns = load_functions()
        self.batch_set_values = self.ns["set_values"]
        self.ns.update(REPO=self.repo, HEBDO_STATE=self.repo / "hebdo-state.json",
                       RATTRAPAGE=timedelta(hours=6), VAR_SEMAINE="pts_recrutement_semaine",
                       read_json=read_json, write_json=write_json)
        write_json(self.ns["HEBDO_STATE"], {"derniere_remise": "2026-10-26", "publie": True})
        self.rows = [("u1", "Alice", 12), ("u2", "Bob", 0)]
        self.writes = []
        self.posts = []
        self.publishes = []
        self.reports = []
        self.ns["holders"] = lambda var: list(self.rows)
        self.ns["set_values"] = lambda var, values: self.writes.append((var, values)) or []
        self.ns["publish"] = lambda message: self.publishes.append(message)

        async def post_podium(state, week):
            self.posts.append(week)
            state["publie"] = True
            write_json(self.ns["HEBDO_STATE"], state)

        async def report(key, message):
            self.reports.append((key, message))

        self.bot = SimpleNamespace(post_podium=post_podium, report=report)

    def run_weekly(self, when):
        asyncio.run(self.ns["weekly"](self.bot, when))

    def test_sunday_no_reset_monday_once(self):
        self.run_weekly(datetime(2026, 11, 1, 23, 59, tzinfo=PARIS))
        self.assertEqual(self.writes, [])
        self.run_weekly(datetime(2026, 11, 2, 0, 0, tzinfo=PARIS))
        self.assertEqual(self.writes, [("pts_recrutement_semaine", {"u1": 0})])
        self.assertEqual(self.posts, ["2026-11-02"])
        snap = read_json(self.repo / "hebdo" / "2026-10-26.json", {})
        self.assertEqual(snap["classement"], [{"id": "u1", "pseudo": "Alice", "points": 12}])
        self.run_weekly(datetime(2026, 11, 2, 0, 1, tzinfo=PARIS))
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(len(self.posts), 1)

    def test_late_start_warns_without_reset(self):
        self.run_weekly(datetime(2026, 11, 2, 7, 0, tzinfo=PARIS))
        self.assertEqual(self.writes, [])
        self.assertEqual(len(self.reports), 1)
        self.assertEqual(read_json(self.ns["HEBDO_STATE"], {})["derniere_remise"], "2026-10-26")

    def test_existing_snapshot_is_not_overwritten_after_interruption(self):
        snap = self.repo / "hebdo" / "2026-10-26.json"
        write_json(snap, {"classement": [{"id": "u1", "pseudo": "Alice", "points": 12}]})
        self.rows = [("u1", "Alice", 0)]  # une écriture aurait déjà réussi avant l'interruption
        self.run_weekly(datetime(2026, 11, 2, 0, 2, tzinfo=PARIS))
        self.assertEqual(read_json(snap, {})["classement"][0]["points"], 12)

    def test_batch_missing_result_is_reported(self):
        self.ns["api"] = lambda *a, **kw: {"results": [{"op_id": "u1", "status": 200}]}
        failures = self.batch_set_values("pts_recrutement_semaine", {"u1": 0, "u2": 0})
        self.assertTrue(any("u2" in failure for failure in failures))

    def test_batch_exception_is_reported(self):
        def unavailable(*a, **kw):
            raise RuntimeError("API indisponible")

        self.ns["api"] = unavailable
        failures = self.batch_set_values("pts_recrutement_semaine", {"u1": 0})
        self.assertTrue(any("u1" in failure for failure in failures))


if __name__ == "__main__":
    unittest.main()
