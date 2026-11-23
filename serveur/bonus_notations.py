"""Bonus des notations : file du message officiel p13, crédit fixe de 5 xp_paie.

Le message p13 est modifiable uniquement par l'encadrement sur Forumactif.
Les autres messages, URL, montants et variables fournis par un visiteur ne
constituent jamais une instruction. Aucun endpoint d'écriture public.
Un journal SQLite durable réserve chaque crédit AVANT le seul appel d'écriture.
Une réponse perdue ou un arrêt pendant l'écriture exige un contrôle humain.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from contextlib import contextmanager

LOG = logging.getLogger("pn.bonus")
ROSTER_URL = "https://www.pn-rpg.fr/t13-liste-des-grades-de-la-police-nationale"
VARIABLE = "xp_paie"
AMOUNT = 5


class OfficialPost(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.post_depth = None
        self.posts = 0
        self.blocks = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag != "div":
            return
        self.depth += 1
        if attrs.get("id") == "p13":
            self.posts += 1
            if self.posts != 1 or "post--13" not in attrs.get("class", "").split():
                raise ValueError("Message officiel ambigu")
            self.post_depth = self.depth
        if self.post_depth and attrs.get("id") in {"pn-roster-store", "pn-bonus-store"}:
            key = attrs["id"]
            if key in self.blocks:
                raise ValueError("Registre officiel ambigu")
            field = "data-pn-roster" if key == "pn-roster-store" else "data-pn-bonus"
            self.blocks[key] = json.loads(urllib.parse.unquote(attrs[field]))

    def handle_endtag(self, tag):
        if tag == "div":
            if self.depth == self.post_depth:
                self.post_depth = None
            self.depth -= 1


def parse_official(source):
    parser = OfficialPost()
    parser.feed(source)
    if parser.posts != 1 or "pn-roster-store" not in parser.blocks:
        raise ValueError("Registre officiel absent")
    roster = parser.blocks["pn-roster-store"]
    queue = parser.blocks.get("pn-bonus-store", {"version": 1, "requests": []})
    if queue.get("version") != 1 or not isinstance(queue.get("requests"), list):
        raise ValueError("File de bonus invalide")
    if len(queue["requests"]) > 200:
        raise ValueError("File de bonus trop grande")
    return roster, queue["requests"]


def read_official():
    request = urllib.request.Request(ROSTER_URL, headers={"User-Agent": "PN-notations/1.0", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=20) as response:
        if urllib.parse.urlsplit(response.url).hostname != "www.pn-rpg.fr":
            raise ValueError("Redirection du registre refusée")
        body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise ValueError("Registre trop volumineux")
        return parse_official(body.decode("utf-8"))


def validate_request(row, roster):
    uid = str(row.get("uid", ""))
    week = str(row.get("week", ""))
    if not re.fullmatch(r"[1-9][0-9]{0,9}", uid) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", week):
        raise ValueError("Identité ou semaine invalide")
    end = date.fromisoformat(week)
    if end.weekday() != 4 or end > date.today():
        raise ValueError("La période doit se terminer un vendredi passé")
    pseudo = str(row.get("pseudo", "")).strip()
    if not pseudo or len(pseudo) > 32:
        raise ValueError("Pseudo invalide")
    members = [m for m in roster.get("members", [])
               if urllib.parse.urlsplit(m.get("profileUrl", "")).path.rstrip("/") == "/u" + uid]
    if len(members) != 1 or members[0].get("pseudo", "").casefold() != pseudo.casefold():
        raise ValueError("Le compte ne correspond plus à la liste officielle")
    return f"u{uid}:{week}", pseudo, week


class BonusNotations:
    def __init__(self, database, public_file, api, holders, xp_lock, reader=read_official):
        self.database, self.public_file = Path(database), Path(public_file)
        self.api, self.holders, self.xp_lock, self.reader = api, holders, xp_lock, reader
        self.health = {"status": "pending"}
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS credits (
                request_key TEXT PRIMARY KEY, habbo_id TEXT NOT NULL, week TEXT NOT NULL,
                pseudo TEXT NOT NULL, author TEXT NOT NULL, notation TEXT NOT NULL,
                status TEXT NOT NULL, before_value INTEGER, after_value INTEGER,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(habbo_id, week))""")
            # Un crash après réservation ne doit jamais rejouer la même écriture.
            db.execute("UPDATE credits SET status='uncertain' WHERE status='writing'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def cycle(self):
        roster, requests = self.reader()
        if self.health["status"] != "ready":
            self.probe_read()
        problems = {}
        with self.connect() as db:
            known = {row[0] for row in db.execute("SELECT request_key FROM credits")}
        pending = []
        for row in requests:
            key = f"u{row.get('uid', '')}:{row.get('week', '')}" if isinstance(row, dict) else "invalid"
            if key in known:
                continue
            try:
                key, pseudo, week = validate_request(row, roster)
                pending.append((row, key, pseudo, week))
            except (ValueError, TypeError):
                problems[key] = "identity_check"
        if pending:
            directory = {}
            for hid, name, _ in self.holders("matricule"):
                directory.setdefault(name.casefold(), set()).add(str(hid))
            for row, key, pseudo, week in pending:
                ids = directory.get(pseudo.casefold(), set())
                if len(ids) != 1:
                    problems[key] = "identity_check"
                    continue
                hid = next(iter(ids))
                try:
                    duplicate_status = self.credit(row, key, pseudo, week, hid)
                    if duplicate_status:
                        problems[key] = duplicate_status
                except Exception:
                    # Une lecture échouée n'envoie rien et pourra être retentée.
                    LOG.exception("Bonus différé pour %s", key)
                    problems[key] = "waiting"
        self.export(problems)

    def probe_read(self):
        """Vérifie la lecture individuelle sans aucune écriture ni valeur publique."""
        try:
            listing = self.api(f"/variables/user/{VARIABLE}/users", params={"size": 1})
            hid = str(listing["items"][0]["user"]["id"])
            current = self.api(f"/variables/user/{VARIABLE}/users/{urllib.parse.quote(hid, safe='')}")
            if not isinstance(current, dict) or not re.fullmatch(r"\d+", str(current.get("value", ""))):
                raise ValueError("Lecture individuelle indisponible")
            self.health = {"status": "ready", "checked_at": datetime.now(timezone.utc).isoformat()}
        except Exception:
            self.health = {"status": "waiting"}
            LOG.exception("Contrôle de lecture du bonus différé ; aucune écriture de test")

    def credit(self, row, key, pseudo, week, hid):
        path = f"/variables/user/{VARIABLE}/users/{urllib.parse.quote(hid, safe='')}"
        with self.xp_lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT request_key,status FROM credits WHERE request_key=? OR (habbo_id=? AND week=?)", (key, hid, week)).fetchone()
            if previous:
                return previous["status"]
            catalog = self.api("/variables")
            if not isinstance(catalog, dict) or VARIABLE not in catalog.get("users", []):
                raise RuntimeError("xp_paie n'est pas accessible ; aucun remplacement par xp")
            current = self.api(path)
            if current is None:
                raise RuntimeError("Valeur xp_paie inaccessible ; ne jamais supposer zéro")
            else:
                value = current.get("value") if isinstance(current, dict) else None
                if value is None or not re.fullmatch(r"\d+", str(value)):
                    raise RuntimeError("Valeur xp_paie illisible")
                before = int(value)
                method = "PATCH"
            after = before + AMOUNT
            if after > 2**63 - 1:
                raise ValueError("Valeur hors limite")
            now = datetime.now(timezone.utc).isoformat()
            db.execute("INSERT INTO credits VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                key, hid, week, pseudo, str(row.get("author", ""))[:80], str(row.get("notation", ""))[:100],
                "writing", before, after, now, now))
            db.commit()  # durable avant la transmission ; ne pas déplacer après l'appel
            try:
                result = self.api(path, method=method, write=True, body={"value": str(after)})
                status = "credited" if result is not None else "rejected"
            except Exception:
                LOG.exception("Écriture du bonus incertaine pour %s ; aucun nouvel essai automatique", key)
                status = "uncertain"
            db.execute("UPDATE credits SET status=?, updated_at=? WHERE request_key=?", (
                status, datetime.now(timezone.utc).isoformat(), key))
            db.commit()

    def export(self, problems):
        with self.connect() as db:
            rows = list(db.execute("SELECT request_key,status,updated_at FROM credits ORDER BY created_at DESC"))
        receipts = {row["request_key"]: {"status": row["status"], "at": row["updated_at"]} for row in rows}
        for key, status in problems.items():
            receipts.setdefault(key, {"status": status})
        payload = {"version": 1, "variable": VARIABLE, "amount": AMOUNT, "health": self.health, "receipts": receipts}
        text = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
        if self.public_file.exists() and self.public_file.read_text(encoding="utf-8") == text:
            return
        temp = self.public_file.with_suffix(".tmp")
        temp.write_text(text, encoding="utf-8")
        temp.replace(self.public_file)
