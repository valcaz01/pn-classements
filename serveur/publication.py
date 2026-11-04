"""Reprise des échanges Git et suivi persistant des interruptions de publication."""
from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path


PUBLICATION_REVISION = "2026-11-04.1"
RETRY_DELAYS = (2, 8, 20)
LOG = logging.getLogger("pn.publication")


class GitPublicationError(RuntimeError):
    def __init__(self, operation, detail):
        self.operation = operation
        self.detail = detail.strip()
        super().__init__(f"git {operation} : {self.detail[:800]}")


def retryable(detail):
    text = detail.casefold()
    return any(fragment in text for fragment in (
        "internal server error", "service unavailable", "bad gateway", "gateway timeout",
        "returned error: 500", "returned error: 502", "returned error: 503", "returned error: 504",
        "returned error: 429", "connection closed", "connection reset", "connection timed out",
        "connection refused", "timed out", "could not resolve", "unable to resolve",
        "temporary failure in name resolution", "remote end hung up", "unexpected disconnect",
        "kex_exchange_identification", "permission denied (publickey)",
    ))


def network_git(run_git, *args, sleep=time.sleep):
    """Réessaie seulement fetch/push : aucun clic Discord ni appel d'écriture Habbo."""
    if not args or args[0] not in {"fetch", "push"}:
        raise ValueError("Les reprises réseau sont réservées à git fetch et git push")
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            result = run_git(*args, check=False, timeout=30)
        except subprocess.TimeoutExpired:
            error = GitPublicationError(args[0], "Délai réseau dépassé (30 secondes)")
            can_retry = True
        else:
            if result.returncode == 0:
                return result
            detail = result.stderr or result.stdout or f"code de sortie {result.returncode}"
            error = GitPublicationError(args[0], detail)
            can_retry = retryable(detail)
        if not can_retry or attempt == len(RETRY_DELAYS):
            raise error
        delay = RETRY_DELAYS[attempt]
        LOG.warning("%s ; nouvelle tentative %s/4 dans %ss", error, attempt + 2, delay)
        sleep(delay)


def public_reason(error):
    """Une cause lisible pour Discord ; les détails techniques restent dans les logs."""
    text = str(error).casefold()
    if "permission denied (publickey)" in text:
        return "GitHub refuse la connexion SSH du serveur."
    if any(x in text for x in ("internal server error", "service unavailable", "bad gateway", "gateway timeout")):
        return "GitHub renvoie une erreur de service."
    if "rebase" in text or "non-fast-forward" in text or "fetch first" in text:
        return "Le dépôt nécessite une vérification de synchronisation."
    return "La connexion ou la publication vers GitHub échoue encore."


class PublicationHealth:
    """Une alerte après 10 min et 3 échecs, rappel après 24 h, puis un rétablissement.

    Cet état est privé, hors du dépôt. Un redémarrage ne recommence pas une série
    d'alertes et un message Discord non délivré reste à envoyer au cycle suivant.
    """
    def __init__(self, path):
        self.path = Path(path)
        self.state = {"first_failure": None, "failures": 0, "last_success": None,
                      "last_notice": None, "alerted": False, "recovery": None}
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                for key in ("first_failure", "last_success", "last_notice"):
                    value = saved.get(key)
                    if isinstance(value, (int, float)) and value >= 0:
                        self.state[key] = value
                self.state["failures"] = max(0, int(saved.get("failures", 0)))
                self.state["alerted"] = saved.get("alerted") is True
                recovery = saved.get("recovery")
                if isinstance(recovery, (int, float)) and recovery >= 0:
                    self.state["recovery"] = recovery
        except (OSError, ValueError, TypeError, OverflowError):
            pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.state, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)

    def failure(self, now):
        if self.state["first_failure"] is None:
            self.state["first_failure"] = now
        self.state["failures"] += 1
        self.state["recovery"] = None
        self.save()

    def alert_due(self, now):
        first, last = self.state["first_failure"], self.state["last_notice"]
        return (first is not None and now - first >= 600 and self.state["failures"] >= 3
                and (last is None or now - last >= 86400))

    def alert_sent(self, now):
        self.state.update(alerted=True, last_notice=now)
        self.save()

    def success(self, now):
        first = self.state["first_failure"]
        if first is not None and self.state["alerted"]:
            self.state["recovery"] = max(0, now - first)
        self.state.update(first_failure=None, failures=0, last_notice=None,
                          alerted=False, last_success=now)
        self.save()
        return self.state["recovery"]

    def recovery_sent(self):
        self.state["recovery"] = None
        self.save()
