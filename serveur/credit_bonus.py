"""Attribution de 5 crédits à retirer au QG. Seul le WIRED débite le coffre."""
import logging
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone

from habbo_http import ApiRequestError


LOG = logging.getLogger("pn.bonus.credits")
AMOUNT = 5
PENDING = "bonus_credits_qg"
READY = "bonus_credits_qg_actif"


def value(data):
    raw = data.get("value") if isinstance(data, dict) else None
    if type(raw) is int or (isinstance(raw, str) and raw.isascii() and raw.isdecimal()):
        return int(raw)
    raise ValueError("Valeur WIRED illisible")


class CreditBonus:
    def __init__(self, api, path):
        self.api, self.path = api, path
        self.lock = threading.Lock()

    def issue(self, request_id, author_id, public_id, name):
        try:
            return self._issue(str(request_id), str(author_id), public_id, name)
        except (ApiRequestError, sqlite3.Error, ValueError):
            LOG.exception("Bonus crédits indisponible pour la demande %s", request_id)
            return "unavailable"

    def _issue(self, request_id, author_id, public_id, name):
        catalog = self.api("/variables")
        if not isinstance(catalog, dict):
            return "unavailable"
        if PENDING not in catalog.get("users", []):
            return "preview"
        enabled = self.api(f"/variables/global/{READY}")
        if enabled is None or value(enabled) != 1:
            return "preview"

        profile = self.api("/variables_profile/user/users", {"unique_id": public_id})
        user = profile.get("user") if isinstance(profile, dict) else None
        if not isinstance(user, dict):
            return "unregistered"
        user_id = str(user.get("id", ""))
        if (not user_id.isascii() or not user_id.isdecimal()
                or (user.get("unique_id") or user.get("uniqueId")) != public_id):
            return "unavailable"

        path = f"/variables/user/{PENDING}/users/{user_id}"
        with self.lock, closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS grants (
                request_id TEXT PRIMARY KEY, habbo_id TEXT NOT NULL, name TEXT NOT NULL,
                author_id TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)""")
            same = db.execute("SELECT status FROM grants WHERE request_id = ?", (request_id,)).fetchone()
            if same:
                return "duplicate" if same[0] == "issued" else same[0]
            uncertain = db.execute("""SELECT 1 FROM grants WHERE habbo_id = ?
                AND status IN ('writing', 'uncertain') LIMIT 1""", (user_id,)).fetchone()
            if uncertain:
                return "uncertain"

            pending = self.api(path)
            if pending is not None:
                current = value(pending)
                if current == AMOUNT:
                    return "pending"
                if current != 0:
                    return "unavailable"

            db.execute("INSERT INTO grants VALUES (?, ?, ?, ?, 'writing', ?)",
                       (request_id, user_id, name, author_id, datetime.now(timezone.utc).isoformat()))
            db.commit()
            try:
                result = self.api(path, method="PUT" if pending is None else "PATCH",
                                  write=True, body={"value": str(AMOUNT)})
                status = "issued" if result is not None else "rejected"
            except Exception:
                LOG.exception("Résultat du bonus crédits incertain pour la demande %s", request_id)
                status = "uncertain"
            db.execute("UPDATE grants SET status = ? WHERE request_id = ?", (status, request_id))
            return status
