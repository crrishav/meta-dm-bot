"""SQLite-backed conversation state.

Deliberately boring: one file, no ORM, no migrations. Swap for Postgres by
replacing the four helpers at the bottom if you outgrow it.
"""

import sqlite3
import threading
import time
from typing import Optional

from .config import DB_PATH, MAX_HISTORY

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def init() -> None:
    global _conn
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS seen_mids (
            mid TEXT PRIMARY KEY,
            ts  INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sent_mids (
            mid TEXT PRIMARY KEY,
            ts  INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            convo TEXT NOT NULL,
            role  TEXT NOT NULL,
            text  TEXT NOT NULL,
            ts    INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS messages_convo_idx ON messages (convo, id);
        CREATE TABLE IF NOT EXISTS threads (
            convo       TEXT PRIMARY KEY,
            muted_until INTEGER NOT NULL DEFAULT 0,
            referral    TEXT
        );
        """
    )
    try:
        _conn.execute("ALTER TABLE threads ADD COLUMN referral TEXT")
        _conn.commit()
    except sqlite3.OperationalError:
        pass  # already there - CREATE TABLE above only runs on a fresh db
    _conn.commit()


def _now() -> int:
    return int(time.time())


def claim_mid(mid: str) -> bool:
    """Return True the first time we see a message id, False on redelivery.

    Meta retries webhooks it thinks failed, so without this the customer gets
    the same reply two or three times.
    """
    with _lock:
        try:
            _conn.execute("INSERT INTO seen_mids (mid, ts) VALUES (?, ?)", (mid, _now()))
            _conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def record_sent_mid(mid: str) -> None:
    with _lock:
        _conn.execute(
            "INSERT OR IGNORE INTO sent_mids (mid, ts) VALUES (?, ?)", (mid, _now())
        )
        _conn.commit()


def was_sent_by_us(mid: str) -> bool:
    with _lock:
        row = _conn.execute("SELECT 1 FROM sent_mids WHERE mid = ?", (mid,)).fetchone()
    return row is not None


def add_message(convo: str, role: str, text: str) -> None:
    with _lock:
        _conn.execute(
            "INSERT INTO messages (convo, role, text, ts) VALUES (?, ?, ?, ?)",
            (convo, role, text, _now()),
        )
        _conn.commit()


def history(convo: str) -> list[dict]:
    """Recent turns, oldest first, as {role, content} dicts."""
    with _lock:
        rows = _conn.execute(
            "SELECT role, text FROM messages WHERE convo = ? ORDER BY id DESC LIMIT ?",
            (convo, MAX_HISTORY),
        ).fetchall()
    return [{"role": role, "content": text} for role, text in reversed(rows)]


def mute(convo: str, hours: float) -> None:
    until = _now() + int(hours * 3600)
    with _lock:
        _conn.execute(
            "INSERT INTO threads (convo, muted_until) VALUES (?, ?) "
            "ON CONFLICT(convo) DO UPDATE SET muted_until = excluded.muted_until",
            (convo, until),
        )
        _conn.commit()


def is_muted(convo: str) -> bool:
    with _lock:
        row = _conn.execute(
            "SELECT muted_until FROM threads WHERE convo = ?", (convo,)
        ).fetchone()
    return bool(row) and row[0] > _now()


def set_referral(convo: str, referral: str) -> None:
    """Record which ad started this conversation - first-touch only, so a
    later message from an unrelated ad click mid-thread does not overwrite
    the story of how the lead actually arrived."""
    with _lock:
        _conn.execute(
            "INSERT INTO threads (convo, referral) VALUES (?, ?) "
            "ON CONFLICT(convo) DO UPDATE SET referral = excluded.referral "
            "WHERE threads.referral IS NULL",
            (convo, referral),
        )
        _conn.commit()


def get_referral(convo: str) -> Optional[str]:
    with _lock:
        row = _conn.execute(
            "SELECT referral FROM threads WHERE convo = ?", (convo,)
        ).fetchone()
    return row[0] if row else None
