"""Call history: every call the phone reports, kept in SQLite on the Pi.

SQLite rather than the cluster's Postgres: this service runs on the Pi's
host, and Postgres only admits the backend namespace (NetworkPolicy,
docs/kubernetes.md) - opening that for a few rows a day isn't worth it.

The phone's call object paths (/org/pipewire/Telephony/ag1/callN) get
reused, so a row is tied to a path only while that call is live: a path
that disappears closes its row, and the next time it appears is a new
call. Direction comes from the first state seen: incoming/waiting is
"in", dialing/alerting is "out". Missed = incoming and never active.
"""
import sqlite3
import time
from pathlib import Path

from app.telephony import Call

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY,
    number TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    direction TEXT NOT NULL,          -- in / out / unknown
    started REAL NOT NULL,
    answered REAL,                    -- first time it was active
    ended REAL,
    on_pc INTEGER NOT NULL DEFAULT 0  -- its audio was bridged to a PC page
)
"""


def direction_of(state: str) -> str:
    if state in ("incoming", "waiting"):
        return "in"
    if state in ("dialing", "alerting"):
        return "out"
    return "unknown"  # first seen mid-call, e.g. the service restarted


class CallLog:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute(SCHEMA)
        self.db.commit()
        self.live: dict[str, int] = {}  # call path -> row id, while the call lasts
        self.changed = False  # set whenever a row is added or closed

    def observe(self, calls: list[Call], bridged: bool, now: float | None = None) -> list[dict]:
        """Update rows from one poll of the phone's calls. Returns calls that just ended missed."""
        now = time.time() if now is None else now
        seen = set()
        for call in calls:
            if call.state == "disconnected":
                continue
            seen.add(call.path)
            row = self.live.get(call.path)
            if row is None:
                cur = self.db.execute(
                    "INSERT INTO calls (number, name, direction, started) VALUES (?, ?, ?, ?)",
                    (call.number, call.name, direction_of(call.state), now),
                )
                row = self.live[call.path] = cur.lastrowid
                self.changed = True
            if call.state == "active":
                self.db.execute("UPDATE calls SET answered = COALESCE(answered, ?) WHERE id = ?", (now, row))
            if call.name:
                self.db.execute("UPDATE calls SET name = ? WHERE id = ? AND name = ''", (call.name, row))
            if bridged and call.state == "active":
                self.db.execute("UPDATE calls SET on_pc = 1 WHERE id = ?", (row,))
        missed = []
        for path in [p for p in self.live if p not in seen]:
            row = self.live.pop(path)
            self.db.execute("UPDATE calls SET ended = ? WHERE id = ?", (now, row))
            self.changed = True
            rec = self._row(row)
            if rec and rec["missed"]:
                missed.append(rec)
        self.db.commit()
        return missed

    def _row(self, row_id: int) -> dict | None:
        r = self.db.execute("SELECT * FROM calls WHERE id = ?", (row_id,)).fetchone()
        return self._dict(r) if r else None

    @staticmethod
    def _dict(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["missed"] = d["direction"] == "in" and d["answered"] is None and d["ended"] is not None
        d["seconds"] = int(d["ended"] - d["answered"]) if d["answered"] and d["ended"] else None
        return d

    def recent(self, limit: int = 30) -> list[dict]:
        rows = self.db.execute("SELECT * FROM calls ORDER BY started DESC LIMIT ?", (limit,)).fetchall()
        return [self._dict(r) for r in rows]

    def last_missed(self) -> dict | None:
        r = self.db.execute(
            "SELECT * FROM calls WHERE direction = 'in' AND answered IS NULL AND ended IS NOT NULL ORDER BY ended DESC LIMIT 1"
        ).fetchone()
        return self._dict(r) if r else None

    def counts(self) -> dict[tuple[str, str], int]:
        """(direction, outcome) -> total, for metrics. outcome: answered / missed / unanswered."""
        out = {}
        for r in self.db.execute(
            "SELECT direction, CASE WHEN answered IS NOT NULL THEN 'answered'"
            " WHEN direction = 'in' THEN 'missed' ELSE 'unanswered' END AS outcome, COUNT(*) AS n"
            " FROM calls WHERE ended IS NOT NULL GROUP BY 1, 2"
        ):
            out[(r["direction"], r["outcome"])] = r["n"]
        return out
