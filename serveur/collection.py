"""Une notification persistante pour les lectures de classements et promotions."""
from __future__ import annotations

import json
import math
from pathlib import Path


COLLECTION_REVISION = "2026-11-04.1"
JOBS = {"rankings": "Classements du forum", "promotions": "Alertes de promotion"}
REASONS = {
    "timeout": "Habbo n'a pas répondu dans le délai prévu.",
    "network": "La connexion à Habbo a été interrompue.",
    "unavailable": "Habbo renvoie une erreur de service.",
    "rate_limit": "Habbo limite temporairement les lectures.",
    "access": "Habbo refuse l'accès : la configuration doit être vérifiée.",
    "response": "Habbo renvoie une réponse incomplète ou invalide.",
    "http": "Habbo refuse la requête : une vérification est nécessaire.",
    "internal": "La vérification rencontre une erreur ; le détail est conservé dans les logs.",
}


class CollectionFailure(RuntimeError):
    def __init__(self, kind="internal", service="Habbo"):
        self.kind, self.service = kind, service
        super().__init__(REASONS.get(kind, REASONS["internal"]))


def subprocess_failure(output):
    """Récupère la cause de build_classement.py sans afficher sa trace sur Discord."""
    for line in reversed(output.splitlines()):
        if line.startswith("PN_API_FAILURE:"):
            try:
                detail = json.loads(line.split(":", 1)[1])
                if isinstance(detail, dict) and detail.get("kind") in REASONS and detail.get("service") == "Habbo":
                    return CollectionFailure(detail["kind"])
            except (TypeError, ValueError):
                pass
    return CollectionFailure()


def timestamp(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


class CollectionHealth:
    """Suit chaque travail séparément ; un succès ne masque pas l'autre en panne.

    Une seule alerte groupée après 10 min ET 3 échecs du même travail, rappel
    après 24 h, puis rétablissement quand les deux travaux ont réussi. L'état
    et les notifications non délivrées survivent au redémarrage du service.
    """
    def __init__(self, path):
        self.path = Path(path)
        self.state = {"jobs": {job: {"first_failure": None, "failures": 0,
                                    "last_success": None, "kind": None} for job in JOBS},
                      "alerted": False, "last_notice": None, "incident_since": None, "recovery": None}
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            for job, row in self.state["jobs"].items():
                old = saved.get("jobs", {}).get(job, {})
                row.update(first_failure=timestamp(old.get("first_failure")),
                           last_success=timestamp(old.get("last_success")),
                           failures=max(0, int(old.get("failures", 0))),
                           kind=old.get("kind") if old.get("kind") in REASONS else "internal")
            self.state.update(alerted=saved.get("alerted") is True,
                              **{key: timestamp(saved.get(key)) for key in
                                 ("last_notice", "incident_since", "recovery")})
            if self.state["incident_since"] is None:
                self.state["alerted"] = False
        except (OSError, ValueError, TypeError, AttributeError, OverflowError):
            pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(self.state, sort_keys=True), encoding="utf-8")
        temp.replace(self.path)

    def record(self, job, error, now):
        row = self.state["jobs"][job]
        if error is None:
            row.update(first_failure=None, failures=0, kind=None, last_success=now)
        else:
            if row["first_failure"] is None:
                row["first_failure"] = now
            row["failures"] += 1
            kind = getattr(error, "kind", "internal") if getattr(error, "service", "Habbo") == "Habbo" else "internal"
            row["kind"] = kind if kind in REASONS else "internal"
        self.save()

    def failed_jobs(self):
        return {job: row for job, row in self.state["jobs"].items() if row["first_failure"] is not None}

    def finish_cycle(self, now):
        # Décider après les deux résultats : pas de faux rétablissement entre deux étapes.
        failed = self.failed_jobs()
        if failed:
            self.state["recovery"] = None
        elif self.state["alerted"]:
            self.state["recovery"] = max(0, now - self.state["incident_since"])
            self.state.update(alerted=False, last_notice=None, incident_since=None)
        self.save()

    def alert_due(self, now):
        last = self.state["last_notice"]
        return (any(now - row["first_failure"] >= 600 and row["failures"] >= 3
                    for row in self.failed_jobs().values())
                and (last is None or now - last >= 86400))

    def alert_sent(self, now):
        if self.state["incident_since"] is None:
            self.state["incident_since"] = min(row["first_failure"] for row in self.failed_jobs().values())
        self.state.update(alerted=True, last_notice=now)
        self.save()

    def recovery_sent(self):
        self.state["recovery"] = None
        self.save()
