"""What gets written into the mock EHR: referral orders, filed documents and the audit log.

Kept in a local SQLite file (var/ehr.db), never in the Synthea CSVs, so the source chart is
never changed and deleting the file resets the demo.

The write limits live here, not in the screens, so no screen or request can get around them:
- The agent can only propose a referral order or file a document, and what it enters is
  always pending until a person approves it. It can never approve or reject.
- People (the coordinator and the physician) approve or reject; what they enter themselves
  takes effect at once, because they are the ones accountable for it.
- There is no way to change problems, medications, allergies, results or demographics.
- Every write and every decision is recorded in the audit log, with who did it.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "var" / "ehr.db"

USERS = {
    "fable": {"name": "Fable operator", "role": "agent", "title": "AI agent"},
    "dana": {"name": "Dana", "role": "coordinator", "title": "Referral coordinator"},
    "patel": {"name": "Dr. Patel", "role": "physician", "title": "Primary care physician"},
}
SPECIALTIES = ["Endocrinology", "Nephrology", "Cardiology", "Orthopedic surgery", "Other"]
URGENCIES = ["Routine", "Urgent", "Emergent"]
DOC_KINDS = {"referral_letter": "Referral letter", "consult_note": "Consult note",
             "tracker_note": "Referral tracker note"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS referrals (
  id INTEGER PRIMARY KEY, patient_id TEXT NOT NULL, specialty TEXT NOT NULL, urgency TEXT NOT NULL,
  question TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,              -- pending | ordered | rejected | closed
  entered_by TEXT NOT NULL, entered_at TEXT NOT NULL,
  decided_by TEXT, decided_at TEXT, decision_note TEXT, closed_at TEXT);
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY, patient_id TEXT NOT NULL, referral_id INTEGER, kind TEXT NOT NULL,
  title TEXT NOT NULL, body TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,              -- pending | filed | rejected
  entered_by TEXT NOT NULL, entered_at TEXT NOT NULL,
  decided_by TEXT, decided_at TEXT, decision_note TEXT);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  patient_id TEXT, item TEXT, detail TEXT NOT NULL DEFAULT '');
"""


class NotAllowed(Exception):
    """A write the limits above refuse. The message is shown to whoever tried it."""


class Store:
    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    # ---- helpers ------------------------------------------------------------------

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _user(uid: str | None) -> dict:
        if uid not in USERS:
            raise NotAllowed("Sign in to make changes.")
        return USERS[uid]

    def _audit(self, actor: str, action: str, pid: str | None, item: str, detail: str = "") -> None:
        self.db.execute("INSERT INTO audit (at, actor, action, patient_id, item, detail) VALUES (?,?,?,?,?,?)",
                        (self._now(), actor, action, pid, item, detail))

    # ---- writes -------------------------------------------------------------------

    def propose_referral(self, uid: str | None, pid: str, specialty: str, urgency: str, question: str,
                         reason: str = "") -> int:
        user = self._user(uid)
        question, reason = question.strip(), reason.strip()
        if specialty not in SPECIALTIES or urgency not in URGENCIES:
            raise NotAllowed("Choose a specialty and an urgency from the list.")
        if not question:
            raise NotAllowed("A referral needs the clinical question for the specialist.")
        agent = user["role"] == "agent"
        with self.lock, self.db:
            cur = self.db.execute(
                "INSERT INTO referrals (patient_id, specialty, urgency, question, reason, status, entered_by, entered_at,"
                " decided_by, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (pid, specialty, urgency, question[:2000], reason[:4000], "pending" if agent else "ordered", uid,
                 self._now(), None if agent else uid, None if agent else self._now()))
            rid = cur.lastrowid
            self._audit(uid, "proposed referral order" if agent else "placed referral order", pid, f"referral {rid}",
                        f"{specialty}, {urgency.lower()}: {question[:200]}")
        return rid

    def file_document(self, uid: str | None, pid: str, kind: str, title: str, body: str,
                      referral_id: int | None = None, source: str = "") -> int:
        user = self._user(uid)
        title, body, source = title.strip(), body.strip(), source.strip()
        if kind not in DOC_KINDS:
            raise NotAllowed("Choose a document type from the list.")
        if not title or not body:
            raise NotAllowed("A document needs a title and its text.")
        if referral_id is not None:
            ref = self.referral(referral_id)
            if not ref or ref["patient_id"] != pid:
                raise NotAllowed("That referral is not on this patient's chart.")
        agent = user["role"] == "agent"
        with self.lock, self.db:
            cur = self.db.execute(
                "INSERT INTO documents (patient_id, referral_id, kind, title, body, source, status, entered_by, entered_at,"
                " decided_by, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (pid, referral_id, kind, title[:300], body[:50000], source[:500], "pending" if agent else "filed", uid,
                 self._now(), None if agent else uid, None if agent else self._now()))
            did = cur.lastrowid
            self._audit(uid, f"{'proposed' if agent else 'filed'} {DOC_KINDS[kind].lower()}", pid, f"document {did}",
                        title[:200] + (f" (source: {source[:120]})" if source else ""))
            if not agent:
                self._after_filed(uid, did)
        return did

    def decide(self, uid: str | None, item: str, item_id: int, approve: bool, note: str = "") -> None:
        user = self._user(uid)
        if user["role"] == "agent":
            raise NotAllowed("The agent cannot approve or reject. A person decides.")
        table = {"referral": "referrals", "document": "documents"}.get(item)
        if not table:
            raise NotAllowed("Unknown item.")
        with self.lock, self.db:
            row = self.db.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
            if not row or row["status"] != "pending":
                raise NotAllowed("This item is not waiting for a decision.")
            status = ("ordered" if item == "referral" else "filed") if approve else "rejected"
            self.db.execute(f"UPDATE {table} SET status=?, decided_by=?, decided_at=?, decision_note=? WHERE id=?",
                            (status, uid, self._now(), note.strip()[:1000], item_id))
            self._audit(uid, f"{'approved' if approve else 'rejected'} {item}", row["patient_id"], f"{item} {item_id}",
                        note.strip()[:300])
            if item == "document" and approve:
                self._after_filed(uid, item_id)

    def _after_filed(self, uid: str, did: int) -> None:
        """A filed consult note for an open referral closes the loop on that referral."""
        doc = self.db.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone()
        if doc["kind"] != "consult_note" or not doc["referral_id"]:
            return
        ref = self.db.execute("SELECT * FROM referrals WHERE id=?", (doc["referral_id"],)).fetchone()
        if ref and ref["status"] == "ordered":
            self.db.execute("UPDATE referrals SET status='closed', closed_at=? WHERE id=?", (self._now(), ref["id"]))
            self._audit(uid, "closed referral", ref["patient_id"], f"referral {ref['id']}",
                        f"consult note filed (document {did})")

    def reset(self, uid: str | None) -> None:
        user = self._user(uid)
        if user["role"] == "agent":
            raise NotAllowed("The agent cannot reset the EHR.")
        with self.lock, self.db:
            self.db.executescript("DELETE FROM referrals; DELETE FROM documents; DELETE FROM audit;")
            self._audit(uid, "reset demo data", None, "", "")

    # ---- reads --------------------------------------------------------------------

    def referral(self, rid: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM referrals WHERE id=?", (rid,)).fetchone()

    def document(self, did: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone()

    def referrals(self, pid: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM referrals WHERE patient_id=? ORDER BY id DESC", (pid,)).fetchall()

    def documents(self, pid: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM documents WHERE patient_id=? ORDER BY id DESC", (pid,)).fetchall()

    def pending(self) -> list[tuple[str, sqlite3.Row]]:
        refs = [("referral", r) for r in self.db.execute("SELECT * FROM referrals WHERE status='pending' ORDER BY id")]
        docs = [("document", d) for d in self.db.execute("SELECT * FROM documents WHERE status='pending' ORDER BY id")]
        return sorted(refs + docs, key=lambda x: x[1]["entered_at"])

    def pending_count(self) -> int:
        return self.db.execute("SELECT (SELECT COUNT(*) FROM referrals WHERE status='pending')"
                               " + (SELECT COUNT(*) FROM documents WHERE status='pending')").fetchone()[0]

    def audit(self, pid: str | None = None, limit: int = 500) -> list[sqlite3.Row]:
        if pid:
            return self.db.execute("SELECT * FROM audit WHERE patient_id=? ORDER BY id DESC LIMIT ?", (pid, limit)).fetchall()
        return self.db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
