"""Pannes Habbo simulées, aucune lecture ni écriture sur un service réel."""
import ast
import asyncio
import contextlib
import http.client
import io
import json
import os
import ssl
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

from serveur.collection import (
    CollectionFailure, CollectionHealth, JOBS, REASONS, subprocess_failure,
)
from serveur.habbo_http import ApiRequestError, request_json, retry_after


ROOT = Path(__file__).resolve().parents[1]
PARIS = ZoneInfo("Europe/Paris")


def response(value=None, raw=None):
    body = json.dumps(value if value is not None else {"items": []}).encode() if raw is None else raw
    return contextlib.closing(io.BytesIO(body))


def http_error(code, headers=None):
    return urllib.error.HTTPError("https://www.habbo.fr/api/public/rooms/1/variables", code,
                                  "failure", headers or {}, io.BytesIO(b"private response"))


class HttpReadTests(unittest.TestCase):
    def setUp(self):
        self.req = urllib.request.Request("https://www.habbo.fr/api/public/rooms/1/variables",
                                          headers={"X-Wired-Read-Key": "test-secret"})
        self.sleep = Mock()

    def test_timeout_then_503_then_success(self):
        opener = Mock(side_effect=[urllib.error.URLError(TimeoutError("timed out")),
                                   http_error(503), response({"value": "14"})])
        self.assertEqual(request_json(self.req, opener=opener, sleep=self.sleep), {"value": "14"})
        self.assertEqual(opener.call_count, 3)
        self.assertTrue(all(call.kwargs["timeout"] == 20 for call in opener.call_args_list))
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [2, 5])

    def test_persistent_timeout_is_bounded_and_safe(self):
        opener = Mock(side_effect=TimeoutError("contains test-secret"))
        with self.assertRaises(ApiRequestError) as caught:
            request_json(self.req, opener=opener, sleep=self.sleep)
        self.assertEqual(caught.exception.kind, "timeout")
        self.assertEqual(opener.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)
        self.assertNotIn("test-secret", str(caught.exception))

    def test_partial_body_and_connection_reset_are_retried(self):
        for failure in (http.client.IncompleteRead(b"{", 100), ConnectionResetError("reset"),
                        urllib.error.URLError("Temporary failure in name resolution")):
            with self.subTest(failure=type(failure).__name__):
                opener = Mock(side_effect=[failure, response({"value": "10"})])
                self.assertEqual(request_json(self.req, opener=opener, sleep=self.sleep), {"value": "10"})
                self.assertEqual(opener.call_count, 2)

    def test_429_honors_retry_after(self):
        opener = Mock(side_effect=[http_error(429, {"Retry-After": "8"}), response()])
        request_json(self.req, opener=opener, sleep=self.sleep)
        self.sleep.assert_called_once_with(8)
        now = datetime(2026, 11, 4, 21, 0, tzinfo=timezone.utc)
        self.assertEqual(retry_after({"Retry-After": "Wed, 04 Nov 2026 21:00:10 GMT"}, now), 10)
        self.assertIsNone(retry_after({"Retry-After": "invalid"}))

    def test_long_retry_after_stops_instead_of_retrying_early(self):
        opener = Mock(side_effect=http_error(429, {"Retry-After": "90"}))
        with self.assertRaises(ApiRequestError) as caught:
            request_json(self.req, opener=opener, sleep=self.sleep)
        self.assertEqual(caught.exception.kind, "rate_limit")
        self.assertEqual(opener.call_count, 1)
        self.sleep.assert_not_called()

    def test_configuration_errors_and_404_are_not_retried(self):
        for code in (400, 401, 403, 404):
            with self.subTest(code=code):
                opener = Mock(side_effect=http_error(code))
                if code == 404:
                    self.assertIsNone(request_json(self.req, opener=opener, sleep=self.sleep))
                else:
                    with self.assertRaises(ApiRequestError):
                        request_json(self.req, opener=opener, sleep=self.sleep)
                self.assertEqual(opener.call_count, 1)
        self.sleep.assert_not_called()

    def test_certificate_verification_is_never_bypassed_or_retried(self):
        opener = Mock(side_effect=urllib.error.URLError(ssl.SSLCertVerificationError("invalid cert")))
        with self.assertRaises(ApiRequestError):
            request_json(self.req, opener=opener, sleep=self.sleep)
        self.assertEqual(opener.call_count, 1)
        self.sleep.assert_not_called()

    def test_bad_json_or_empty_response_never_becomes_zero_values(self):
        for raw in (b"", b"null", b"<html>outage</html>", b"{broken"):
            with self.subTest(raw=raw):
                opener = Mock(side_effect=lambda *_a, **_k: response(raw=raw))
                with self.assertRaises(ApiRequestError) as caught:
                    request_json(self.req, opener=opener, sleep=self.sleep)
                self.assertEqual(caught.exception.kind, "response")
                self.assertEqual(opener.call_count, 3)

    def test_writes_are_never_replayed_after_uncertain_response(self):
        for method in ("PATCH", "PUT", "POST", "DELETE"):
            for error in (TimeoutError(), http_error(503), http_error(429)):
                with self.subTest(method=method, error=type(error).__name__):
                    req = urllib.request.Request(self.req.full_url, data=b'{"value":"1"}', method=method)
                    opener, sleep = Mock(side_effect=error), Mock()
                    with self.assertRaises(ApiRequestError):
                        request_json(req, opener=opener, sleep=sleep)
                    self.assertEqual(opener.call_count, 1)
                    sleep.assert_not_called()

    def test_successful_empty_write_is_not_mistaken_for_a_missing_variable(self):
        req = urllib.request.Request(self.req.full_url, data=b"{}", method="PATCH")
        self.assertEqual(request_json(req, opener=Mock(return_value=response(raw=b""))), {})

    def test_webhook_secret_never_appears_in_errors(self):
        req = urllib.request.Request("https://discord.com/api/webhooks/123/webhook-secret", data=b"{}", method="POST")
        with self.assertRaises(ApiRequestError) as caught:
            request_json(req, service="Discord", opener=Mock(side_effect=http_error(503)))
        self.assertNotIn("webhook-secret", str(caught.exception))
        self.assertNotIn("private response", str(caught.exception))


class CollectionHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "private" / "collection-health.json"
        self.health = CollectionHealth(self.path)
        self.error = ApiRequestError("timeout")

    def cycle(self, now, fail_rank=True, fail_prom=True):
        self.health.record("rankings", self.error if fail_rank else None, now)
        self.health.record("promotions", self.error if fail_prom else None, now)
        self.health.finish_cycle(now)

    def test_transient_failure_is_silent(self):
        self.cycle(1000)
        self.assertFalse(self.health.alert_due(1000))
        self.cycle(1120, False, False)
        self.assertFalse(self.health.alert_due(2000))
        self.assertIsNone(self.health.state["recovery"])

    def test_needs_three_failures_and_ten_minutes_of_same_job(self):
        self.cycle(0)
        self.cycle(600)
        self.assertFalse(self.health.alert_due(600))
        self.cycle(601)
        self.assertTrue(self.health.alert_due(601))

    def test_success_of_rankings_does_not_hide_promotion_failure(self):
        for now in (0, 120, 600):
            self.cycle(now, False, True)
        self.assertTrue(self.health.alert_due(600))
        self.assertEqual(list(self.health.failed_jobs()), ["promotions"])
        self.health.alert_sent(600)
        self.cycle(720, False, True)
        self.assertIsNone(self.health.state["recovery"])

    def test_alternating_failure_does_not_accumulate_into_a_false_outage(self):
        for i, now in enumerate(range(0, 1201, 120)):
            self.cycle(now, i % 2 == 0, i % 2 != 0)
        self.assertFalse(self.health.alert_due(1200))

    def test_one_notice_across_restart_then_daily_reminder(self):
        for now in (0, 120, 600):
            self.cycle(now)
        self.assertTrue(self.health.alert_due(600))
        # Notification non délivrée : elle reste à envoyer.
        self.health = CollectionHealth(self.path)
        self.assertTrue(self.health.alert_due(720))
        self.health.alert_sent(720)
        self.health = CollectionHealth(self.path)
        self.cycle(3600)
        self.assertFalse(self.health.alert_due(3600))
        self.assertFalse(self.health.alert_due(720 + 86399))
        self.assertTrue(self.health.alert_due(720 + 86400))

    def test_one_recovery_only_after_both_jobs_succeed_and_delivery_survives_restart(self):
        for now in (0, 120, 600):
            self.cycle(now)
        self.health.alert_sent(600)
        self.cycle(720, False, True)
        self.assertIsNone(self.health.state["recovery"])
        self.cycle(840, False, False)
        self.assertEqual(self.health.state["recovery"], 840)
        self.health = CollectionHealth(self.path)
        self.cycle(960, False, False)
        self.assertEqual(self.health.state["recovery"], 840)
        self.health.recovery_sent()
        self.cycle(1080, False, False)
        self.assertIsNone(self.health.state["recovery"])

    def test_subprocess_error_is_structured_without_exposing_trace(self):
        exc = subprocess_failure('log\nPN_API_FAILURE:{"kind":"timeout","service":"Habbo","status":null}')
        self.assertEqual(exc.kind, "timeout")
        self.assertNotIn("urlopen", str(exc))
        self.assertEqual(subprocess_failure("Traceback: secret").kind, "internal")
        self.assertEqual(subprocess_failure("PN_API_FAILURE:broken").kind, "internal")

    def test_corrupt_or_incomplete_state_does_not_stop_the_bot(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for saved in ("broken", "null", "[]", '{"alerted":true}',
                      '{"jobs":{"rankings":{"failures":"oops"}}}'):
            with self.subTest(saved=saved):
                self.path.write_text(saved, encoding="utf-8")
                self.health = CollectionHealth(self.path)
                self.cycle(1000, False, False)
                self.assertFalse(self.health.alert_due(1000))


class ApiWrapperTests(unittest.TestCase):
    def test_all_three_clients_use_the_shared_transport_and_keep_headers_and_methods(self):
        for file, function, kind in (("build_classement.py", "get", "rankings"),
                                     ("alertes_grade.py", "req", "promotions"),
                                     ("serveur/pn_bot.py", "api", "bot")):
            with self.subTest(file=file):
                tree = ast.parse((ROOT / file).read_text(encoding="utf-8"))
                node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function)
                transport = Mock(return_value={"value": "17"})
                ns = {"urllib": urllib, "json": json, "HOST": "www.habbo.fr", "ROOM": "1",
                      "KEY": "read-only-secret", "READ_KEY": "read-only-secret",
                      "WRITE_KEY": "write-secret", "request_json": transport}
                exec(compile(ast.Module(body=[node], type_ignores=[]), file, "exec"), ns)
                if kind == "promotions":
                    result = ns[function]("https://www.habbo.fr/api/public/users?name=Alice")
                else:
                    result = ns[function]("/variables/user/xp/users", {"page": 1})
                self.assertEqual(result, {"value": "17"})
                self.assertEqual(transport.call_count, 1)
                request = transport.call_args.args[0]
                self.assertEqual(request.get_method(), "GET")
                if kind != "promotions":
                    self.assertEqual(request.get_header("X-wired-read-key"), "read-only-secret")
                    self.assertIsNone(request.get_header("X-wired-write-key"))
                if kind == "bot":
                    ns[function]("/variables/user/xp/users/1", method="PATCH", write=True, body={"value": "18"})
                    write_request = transport.call_args.args[0]
                    self.assertEqual(write_request.get_method(), "PATCH")
                    self.assertEqual(write_request.get_header("X-wired-write-key"), "write-secret")
                    self.assertEqual(json.loads(write_request.data), {"value": "18"})


def load_cycle_functions():
    source = ROOT / "serveur" / "pn_bot.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    bot = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PNBot")
    nodes = [n for n in bot.body if isinstance(n, ast.AsyncFunctionDef)
             and n.name in {"cycle", "notify_collection_health"}]
    ns = {"asyncio": asyncio, "os": os, "sys": SimpleNamespace(executable="python"), "REPO": ROOT,
          "LOG": Mock(), "datetime": datetime, "timezone": timezone, "PARIS": PARIS,
          "ROUGE": 1, "VERT": 2, "JOBS": JOBS, "REASONS": REASONS,
          "subprocess_failure": subprocess_failure, "refresh_habbo": Mock()}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), ns)
    return ns


class BotCollectionTests(unittest.TestCase):
    def test_both_stages_fail_repeatedly_one_grouped_notice_then_recover(self):
        with tempfile.TemporaryDirectory() as directory:
            ns = load_cycle_functions()
            clock, failing = [1000], [True]
            ns["time"] = SimpleNamespace(time=lambda: clock[0])

            def run(*_args, **_kwargs):
                return subprocess.CompletedProcess([], 1 if failing[0] else 0, "collection OK",
                           'PN_API_FAILURE:{"kind":"timeout","service":"Habbo"}' if failing[0] else "")

            ns["subprocess"] = SimpleNamespace(run=run)

            async def alerts():
                if failing[0]:
                    raise ApiRequestError("timeout")

            async def scenario():
                bot = SimpleNamespace(cycle_lock=asyncio.Lock(), alerts=alerts,
                      collection_health=CollectionHealth(Path(directory) / "health.json"),
                      publication_notice=AsyncMock(return_value=True), publish_data=AsyncMock())
                bot.notify_collection_health = lambda: ns["notify_collection_health"](bot)
                for now in (1000, 1120, 1600, 4600):
                    clock[0] = now
                    await ns["cycle"](bot)
                self.assertEqual(bot.publication_notice.await_count, 1)
                notice = bot.publication_notice.await_args.args
                self.assertIn("Classements du forum", notice[1])
                self.assertIn("Alertes de promotion", notice[1])
                self.assertNotIn("urlopen", notice[1])
                self.assertNotIn("Traceback", notice[1])
                ns["refresh_habbo"].assert_not_called()
                self.assertEqual(bot.publish_data.await_count, 4)
                failing[0] = False
                clock[0] = 4720
                await ns["cycle"](bot)
                await ns["cycle"](bot)
                self.assertEqual(bot.publication_notice.await_count, 2)
                self.assertIn("rétablies", bot.publication_notice.await_args.args[0])
                self.assertEqual(ns["refresh_habbo"].call_count, 2)

            asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
