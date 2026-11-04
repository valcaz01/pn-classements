"""Simulation du passage de semaine du classement forum, sans API Habbo."""

import ast
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo


SOURCE = Path(__file__).resolve().parents[1] / "build_classement.py"
PARIS = ZoneInfo("Europe/Paris")


def load_functions():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in {"monday_start", "main"}]
    ns = {"ZoneInfo": ZoneInfo, "timedelta": timedelta, "timezone": timezone, "json": json,
          "time": SimpleNamespace(sleep=lambda _: None), "ROOM": "simulation", "KEY": "simulation",
          "VARS": {"xp": "xp", "pr": "pts_recrutement_total"}, "TOP": 50,
          "OUT": "classement.json", "STATE": "classement-state.json"}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), ns)
    ns["read_json"] = lambda path, fallback: (json.loads(Path(path).read_text(encoding="utf-8"))
                                              if Path(path).exists() else fallback)
    return ns


class ClassementWeekTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = Path.cwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, self.old_cwd)
        self.ns = load_functions()
        self.values = {"xp": 10, "pts_recrutement_total": 20}
        self.ns["top_values"] = lambda var: [("u1", "Alice", self.values[var])]
        self.ns["get"] = lambda *a: self.fail("L'API ne doit pas être appelée")

    def run_main(self, when):
        self.ns["datetime"] = SimpleNamespace(now=lambda tz: when.astimezone(tz))
        with contextlib.redirect_stdout(io.StringIO()):
            self.ns["main"]()
        return json.loads(Path("classement.json").read_text(encoding="utf-8"))

    def test_sunday_then_monday_resets_week_but_keeps_totals(self):
        Path("noms.json").write_text(json.dumps({"u1": "Alice"}), encoding="utf-8")
        Path("classement-state.json").write_text(json.dumps({
            "initialized": True, "calendar": "monday", "week_start": "2026-10-26",
            "last_totals": {"xp": {"u1": 10}, "pr": {"u1": 20}},
            "week_values": {"xp": {"u1": {"name": "Alice", "value": 4}},
                            "pr": {"u1": {"name": "Alice", "value": 3}}}}), encoding="utf-8")
        before = self.run_main(datetime(2026, 11, 1, 23, 59, tzinfo=PARIS))
        self.assertEqual(before["week_start"], "2026-10-26")
        self.assertEqual(before["pr"]["week"][0]["value"], 3)
        after = self.run_main(datetime(2026, 11, 2, 0, 0, tzinfo=PARIS))
        self.assertEqual(after["week_start"], "2026-11-02")
        self.assertEqual(after["xp"]["week"], [])
        self.assertEqual(after["pr"]["week"], [])
        self.assertEqual(after["pr"]["total"][0]["value"], 20)
        self.values["pts_recrutement_total"] = 21
        gain = self.run_main(datetime(2026, 11, 2, 0, 15, tzinfo=PARIS))
        self.assertEqual(gain["pr"]["week"][0]["value"], 1)

    def test_existing_sunday_cumul_is_preserved_during_migration(self):
        Path("noms.json").write_text(json.dumps({"u1": "Alice"}), encoding="utf-8")
        Path("classement-state.json").write_text(json.dumps({
            "initialized": True, "week_start": "2026-10-25",
            "last_totals": {"xp": {"u1": 10}, "pr": {"u1": 20}},
            "week_values": {"xp": {"u1": {"name": "Alice", "value": 4}},
                            "pr": {"u1": {"name": "Alice", "value": 3}}}}), encoding="utf-8")
        result = self.run_main(datetime(2026, 10, 28, 15, 0, tzinfo=PARIS))
        state = json.loads(Path("classement-state.json").read_text(encoding="utf-8"))
        self.assertEqual(result["week_start"], "2026-10-26")
        self.assertEqual(result["xp"]["week"][0]["value"], 4)
        self.assertEqual(result["pr"]["week"][0]["value"], 3)
        self.assertEqual(state["calendar"], "monday")

    def test_monday_start_handles_dst_change(self):
        before = self.ns["monday_start"](datetime(2026, 3, 29, 23, 59, tzinfo=PARIS))
        after = self.ns["monday_start"](datetime(2026, 3, 30, 0, 0, tzinfo=PARIS))
        self.assertEqual(before, "2026-03-23")
        self.assertEqual(after, "2026-03-30")

    def test_success_updates_collection_time_without_new_points(self):
        before = self.run_main(datetime(2026, 11, 1, 12, 0, tzinfo=PARIS))
        after = self.run_main(datetime(2026, 11, 1, 12, 2, tzinfo=PARIS))
        self.assertEqual(before["xp"], after["xp"])
        self.assertEqual(before["pr"], after["pr"])
        self.assertEqual(after["updated"], "2026-11-01T11:02:00+00:00")

    def test_failed_collection_keeps_last_values_and_timestamp(self):
        before = self.run_main(datetime(2026, 11, 1, 12, 0, tzinfo=PARIS))
        def unavailable(var):
            raise RuntimeError("API indisponible")
        self.ns["top_values"] = unavailable
        with self.assertRaises(RuntimeError):
            self.run_main(datetime(2026, 11, 1, 12, 2, tzinfo=PARIS))
        self.assertEqual(json.loads(Path("classement.json").read_text(encoding="utf-8")), before)

    def test_failure_of_second_variable_keeps_all_files_and_catches_up_once(self):
        self.run_main(datetime(2026, 11, 4, 12, 0, tzinfo=PARIS))
        files = ("classement.json", "classement-state.json", "noms.json")
        before = {name: Path(name).read_bytes() for name in files}
        original = self.ns["top_values"]
        self.values["xp"] += 2
        self.values["pts_recrutement_total"] += 3

        def partial_failure(var):
            if var == "pts_recrutement_total":
                raise TimeoutError("API timeout")
            return original(var)

        self.ns["top_values"] = partial_failure
        with self.assertRaises(TimeoutError):
            self.run_main(datetime(2026, 11, 4, 12, 2, tzinfo=PARIS))
        self.assertEqual({name: Path(name).read_bytes() for name in files}, before)
        self.ns["top_values"] = original
        after = self.run_main(datetime(2026, 11, 4, 12, 4, tzinfo=PARIS))
        self.assertEqual(after["xp"]["week"][0]["value"], 2)
        self.assertEqual(after["pr"]["week"][0]["value"], 3)
        again = self.run_main(datetime(2026, 11, 4, 12, 6, tzinfo=PARIS))
        self.assertEqual(again["xp"], after["xp"])
        self.assertEqual(again["pr"], after["pr"])


if __name__ == "__main__":
    unittest.main()
