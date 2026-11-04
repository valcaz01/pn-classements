"""Pannes Git simulées, dépôts Git locaux et notifications ; aucun appel Habbo/Discord/GitHub."""
import ast
import asyncio
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from serveur.publication import (
    GitPublicationError, PUBLICATION_REVISION, PublicationHealth, network_git, public_reason,
)


SOURCE = Path(__file__).resolve().parents[1] / "serveur" / "pn_bot.py"
PARIS = ZoneInfo("Europe/Paris")


def result(code=0, err="", out=""):
    return subprocess.CompletedProcess([], code, out, err)


def load_bot_functions():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in {"git", "publish"}]
    bot = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PNBot")
    nodes.append(next(n for n in bot.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "publish_data"))
    ns = {"asyncio": asyncio, "datetime": datetime, "PARIS": PARIS, "os": os, "Path": Path,
          "subprocess": subprocess, "PUBLICATION_REVISION": PUBLICATION_REVISION,
          "network_git": network_git, "public_reason": public_reason, "LOG": Mock(),
          "fcntl": SimpleNamespace(flock=lambda *_: None, LOCK_EX=1), "ROUGE": 1, "VERT": 2}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), ns)
    return ns


class NetworkRetryTests(unittest.TestCase):
    def test_server_and_authentication_errors_are_retried_then_recover(self):
        run = Mock(side_effect=[result(1, "remote: Internal Server Error"),
                                result(128, "git@github.com: Permission denied (publickey)."), result()])
        sleep = Mock()
        network_git(run, "push", "origin", "HEAD:main", sleep=sleep)
        self.assertEqual(run.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2, 8])
        self.assertTrue(all(c.kwargs == {"check": False, "timeout": 30} for c in run.call_args_list))

    def test_persistent_failure_is_bounded_and_keeps_last_error(self):
        run = Mock(return_value=result(128, "Permission denied (publickey)."))
        sleep = Mock()
        with self.assertRaisesRegex(GitPublicationError, "publickey"):
            network_git(run, "push", "origin", "HEAD:main", sleep=sleep)
        self.assertEqual(run.call_count, 4)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2, 8, 20])

    def test_network_timeout_recovers(self):
        run = Mock(side_effect=[subprocess.TimeoutExpired("git", 30), result()])
        network_git(run, "fetch", "origin", "main", sleep=lambda _: None)
        self.assertEqual(run.call_count, 2)

    def test_conflict_does_not_retry_or_force_push(self):
        run = Mock(return_value=result(1, "rejected: non-fast-forward"))
        with self.assertRaises(GitPublicationError):
            network_git(run, "push", "origin", "HEAD:main", sleep=Mock())
        self.assertEqual(run.call_count, 1)
        with self.assertRaises(ValueError):
            network_git(run, "commit", "-m", "must not run")


class PublicationHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "private" / "publication-health.json"
        self.health = PublicationHealth(self.path)

    def test_brief_outage_is_silent_and_state_resets(self):
        self.health.failure(0)
        self.health.failure(120)
        self.assertFalse(self.health.alert_due(599))
        self.assertIsNone(self.health.success(300))
        self.assertFalse(self.health.alert_due(3600))
        self.assertEqual(self.health.state["last_success"], 300)

    def test_persistent_outage_alerts_once_across_restart_then_daily(self):
        for now in (0, 120, 600):
            self.health.failure(now)
        self.assertTrue(self.health.alert_due(600))
        # Pas d'accusé de réception : une notification Discord ratée est retentée.
        self.assertTrue(self.health.alert_due(720))
        self.health.alert_sent(720)
        restarted = PublicationHealth(self.path)
        restarted.failure(3600)
        self.assertFalse(restarted.alert_due(3600))
        self.assertFalse(restarted.alert_due(720 + 86399))
        self.assertTrue(restarted.alert_due(720 + 86400))
        self.assertEqual(restarted.state["first_failure"], 0)

    def test_recovery_is_persisted_until_notification_delivered(self):
        for now in (10, 130, 610):
            self.health.failure(now)
        self.health.alert_sent(610)
        self.assertEqual(self.health.success(730), 720)
        restarted = PublicationHealth(self.path)
        self.assertEqual(restarted.success(850), 720)
        restarted.recovery_sent()
        self.assertIsNone(PublicationHealth(self.path).success(970))

    def test_corrupt_state_does_not_stop_publication(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("incomplete JSON", encoding="utf-8")
        health = PublicationHealth(self.path)
        health.failure(100)
        self.assertEqual(json.loads(self.path.read_text())["first_failure"], 100)


class GitPublicationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.remote, self.repo, self.peer = [self.root / n for n in ("remote.git", "bot", "peer")]
        self.git_at(self.root, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.git_at(self.root, "clone", str(self.remote), str(self.repo))
        self.configure(self.repo)
        (self.repo / "classement.json").write_text('{"points": 1}', encoding="utf-8")
        (self.repo / "code.txt").write_text("initial", encoding="utf-8")
        self.git_at(self.repo, "add", ".")
        self.git_at(self.repo, "commit", "-m", "initial")
        self.git_at(self.repo, "push", "origin", "main")
        self.git_at(self.root, "clone", str(self.remote), str(self.peer))
        self.configure(self.peer)
        self.ns = load_bot_functions()
        self.ns.update(REPO=self.repo, SSH_DIR=self.root, LOCK_FILE=str(self.root / "git.lock"),
                       DATA_FILES=["classement.json"])
        actual_git = self.ns["git"]
        self.fail_pushes = 0
        self.lose_response = False
        self.push_calls = 0

        def controlled_git(*args, **kwargs):
            args = list(args)
            if args[0] == "fetch":
                args[2] = str(self.remote)  # Le vrai code utilise HTTPS ; ce test reste strictement local.
            if args[0] == "push":
                self.push_calls += 1
                if self.fail_pushes:
                    self.fail_pushes -= 1
                    return result(1, "remote: Internal Server Error")
                response = actual_git(*args, **kwargs)
                if self.lose_response:
                    self.lose_response = False
                    return result(1, "Connection closed by remote host")
                return response
            return actual_git(*args, **kwargs)

        self.ns["git"] = controlled_git
        self.ns["network_git"] = lambda run, *args: network_git(run, *args, sleep=lambda _: None)

    def git_at(self, cwd, *args):
        return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def configure(self, repo):
        self.git_at(repo, "config", "user.name", "Test PN")
        self.git_at(repo, "config", "user.email", "test@example.invalid")

    def write_score(self, points):
        (self.repo / "classement.json").write_text(json.dumps({"points": points}), encoding="utf-8")

    def remote_score(self):
        return json.loads(self.git_at(self.remote, "show", "main:classement.json"))["points"]

    def test_three_failed_pushes_then_success_commit_data_only_once(self):
        self.write_score(7)
        self.fail_pushes = 3
        self.ns["publish"]("new scores")
        self.assertEqual(self.remote_score(), 7)
        self.assertEqual(self.push_calls, 4)
        self.assertEqual(self.git_at(self.remote, "rev-list", "--count", "main"), "2")
        self.assertIn(f"PN-Publisher: {PUBLICATION_REVISION}", self.git_at(self.remote, "log", "-1", "--format=%B"))

    def test_failed_cycle_keeps_data_then_catches_up_without_new_data(self):
        self.write_score(8)
        self.fail_pushes = 4
        with self.assertRaises(GitPublicationError):
            self.ns["publish"]("offline scores")
        self.assertEqual(self.remote_score(), 1)
        self.assertEqual(json.loads((self.repo / "classement.json").read_text())["points"], 8)
        self.ns["publish"]("retry")
        self.assertEqual(self.remote_score(), 8)
        self.assertEqual(self.git_at(self.remote, "rev-list", "--count", "main"), "2")

    def test_uncertain_push_response_does_not_duplicate_commits(self):
        self.write_score(9)
        self.lose_response = True
        self.ns["publish"]("scores")
        self.assertEqual(self.remote_score(), 9)
        self.assertEqual(self.push_calls, 2)
        self.assertEqual(self.git_at(self.remote, "rev-list", "--count", "main"), "2")

    def test_catchup_preserves_upstream_code_and_latest_local_score(self):
        self.write_score(10)
        self.fail_pushes = 4
        with self.assertRaises(GitPublicationError):
            self.ns["publish"]("offline scores")
        (self.peer / "code.txt").write_text("upstream correction", encoding="utf-8")
        self.git_at(self.peer, "add", "code.txt")
        self.git_at(self.peer, "commit", "-m", "upstream")
        self.git_at(self.peer, "push", "origin", "main")
        self.write_score(11)
        self.ns["publish"]("latest scores")
        self.assertEqual(self.remote_score(), 11)
        self.assertEqual(self.git_at(self.remote, "show", "main:code.txt"), "upstream correction")

    def test_conflict_aborts_rebase_without_discarding_local_score(self):
        self.write_score(20)
        self.fail_pushes = 4
        with self.assertRaises(GitPublicationError):
            self.ns["publish"]("offline scores")
        (self.peer / "classement.json").write_text('{"points": 99}', encoding="utf-8")
        self.git_at(self.peer, "add", "classement.json")
        self.git_at(self.peer, "commit", "-m", "conflicting score")
        self.git_at(self.peer, "push", "origin", "main")
        with self.assertRaisesRegex(RuntimeError, "rebase impossible"):
            self.ns["publish"]("retry")
        self.assertEqual(json.loads((self.repo / "classement.json").read_text())["points"], 20)
        self.assertEqual(self.remote_score(), 99)
        self.assertFalse((self.repo / ".git" / "rebase-merge").exists())


class BotPublicationTests(unittest.TestCase):
    def test_only_sustained_outage_is_reported_then_one_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            ns = load_bot_functions()
            clock = [1000]
            ns["time"] = SimpleNamespace(time=lambda: clock[0])
            failing = [True]

            def publish(_message):
                if failing[0]:
                    raise GitPublicationError("push", "remote: Internal Server Error")
                return "1"

            ns["publish"] = publish
            notices = []

            async def notice(*args):
                notices.append(args)
                return True

            async def scenario():
                bot = SimpleNamespace(publication_lock=asyncio.Lock(),
                                      publication_health=PublicationHealth(Path(directory) / "health.json"),
                                      publication_notice=notice)
                for now in (1000, 1120, 1600, 4600):
                    clock[0] = now
                    await ns["publish_data"](bot, "scores")
                self.assertEqual(len(notices), 1)
                self.assertIn("interrompue", notices[0][0])
                self.assertNotIn("Internal Server Error", notices[0][1])
                failing[0] = False
                clock[0] = 4720
                await ns["publish_data"](bot, "scores")
                await ns["publish_data"](bot, "scores")
                self.assertEqual(len(notices), 2)
                self.assertIn("rétablie", notices[1][0])

            asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
