"""Test du timer avec commandes Git/Docker factices : aucun conteneur réel lancé."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "serveur" / "maj.sh"


def bash_executable():
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            candidate = Path(git).resolve().parent.parent / "bin" / "bash.exe"
            if candidate.exists():
                return str(candidate)
        return None
    return shutil.which("bash")


def shell_path(path):
    value = Path(path).resolve().as_posix()
    if os.name == "nt" and len(value) > 2 and value[1] == ":":
        return "/" + value[0].lower() + value[2:]
    return value


@unittest.skipUnless(bash_executable(), "Bash non disponible")
class ServiceUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.root / "repo").mkdir()
        (self.root / "lock").mkdir()
        self.marker = self.root / "lock" / "deployed-service-tree"
        self.calls = self.root / "docker-calls"
        self.script = self.root / "maj.sh"
        self.script.write_text(SOURCE.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        scripts = {
            "flock": "exit 0\n",
            "git": 'if [[ "$1" == fetch && "${FAIL_FETCH:-0}" == 1 ]]; then exit 1; fi\n'
                   'if [[ "$1" == rev-parse ]]; then printf "%s\\n" "$FAKE_TREE"; fi\n',
            "docker": 'printf "%s\\n" "$*" >> "$FAKE_CALLS"\n'
                      'if [[ "${FAIL_BUILD:-0}" == 1 ]]; then exit 1; fi\n',
        }
        for name, body in scripts.items():
            path = self.bin / name
            path.write_text("#!/bin/bash\n" + body, encoding="utf-8", newline="\n")
            path.chmod(0o755)

    def run_update(self, tree="new-service", **extra):
        env = dict(os.environ, PN_ROOT=shell_path(self.root), TEST_BIN=shell_path(self.bin),
                   TEST_SCRIPT=shell_path(self.script), FAKE_CALLS=shell_path(self.calls),
                   FAKE_TREE=tree, **extra)
        return subprocess.run([bash_executable(), "-c", 'PATH="$TEST_BIN:$PATH" bash "$TEST_SCRIPT"'],
                              env=env, capture_output=True, text=True, timeout=15)

    def test_code_already_fetched_by_bot_still_restarts_service_once(self):
        self.marker.write_text("old-service", encoding="utf-8")
        self.assertEqual(self.run_update().returncode, 0)
        self.assertEqual(self.marker.read_text().strip(), "new-service")
        self.assertEqual(self.run_update().returncode, 0)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)

    def test_missing_deployed_marker_triggers_first_restart(self):
        self.assertEqual(self.run_update().returncode, 0)
        self.assertEqual(self.marker.read_text().strip(), "new-service")

    def test_failed_build_is_not_marked_deployed_and_is_retried(self):
        self.marker.write_text("old-service", encoding="utf-8")
        self.assertNotEqual(self.run_update(FAIL_BUILD="1").returncode, 0)
        self.assertEqual(self.marker.read_text(), "old-service")
        self.assertEqual(self.run_update().returncode, 0)
        self.assertEqual(self.marker.read_text().strip(), "new-service")

    def test_failed_fetch_does_not_restart_or_update_marker(self):
        self.marker.write_text("old-service", encoding="utf-8")
        self.assertNotEqual(self.run_update(FAIL_FETCH="1").returncode, 0)
        self.assertEqual(self.marker.read_text(), "old-service")
        self.assertFalse(self.calls.exists())
