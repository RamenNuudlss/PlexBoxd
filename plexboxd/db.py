"""SQLite state: settings, Plex watches and their sync status, Letterboxd film cache."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_SETTINGS: dict[str, Any] = {
    "poll_seconds": 300,          # how often to check Plex history
    "grace_minutes": 60,          # wait this long after a watch so you can rate/review first
    "late_review_days": 7,        # keep checking for a review this many days after logging
    "sync_reviews": True,
    "sync_ratings": True,
    "sync_rewatches": True,
    "preview_mode": True,         # queue entries for approval instead of posting them
    "plex_account_id": 1,         # 1 = the server owner on your Plex server
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS watches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    history_key     TEXT UNIQUE NOT NULL,
    rating_key      TEXT NOT NULL,
    title           TEXT,
    year            INTEGER,
    viewed_at       INTEGER NOT NULL,
    due_at          INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'waiting',
    tmdb_id         TEXT,
    imdb_id         TEXT,
    plex_guid       TEXT,
    rating          INTEGER,
    review          TEXT,
    spoilers        INTEGER DEFAULT 0,
    rewatch         INTEGER DEFAULT 0,
    lb_film_id      TEXT,
    lb_slug         TEXT,
    lb_viewing_id   TEXT,
    review_synced   INTEGER DEFAULT 0,
    rating_synced   INTEGER,
    attempts        INTEGER DEFAULT 0,
    error           TEXT,
    synced_at       INTEGER,
    last_checked    INTEGER,
    created_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_watches_status ON watches(status);
CREATE TABLE IF NOT EXISTS films (
    tmdb_id   TEXT,
    imdb_id   TEXT,
    film_id   TEXT NOT NULL,
    slug      TEXT,
    PRIMARY KEY (film_id)
);
"""

# Statuses a watch moves through:
#   waiting  -> inside the grace window, or due and about to be processed
#   preview  -> ready, held for your approval (preview mode)
#   synced   -> logged to Letterboxd
#   skipped  -> deliberately not logged (already in diary, you skipped it, no film match)
#   failed   -> gave up after repeated errors (can be retried from the dashboard)


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ---- key/value -------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        return json.loads(row["value"])

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
            self._conn.commit()

    def delete(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM kv WHERE key=?", (key,))
            self._conn.commit()

    def settings(self) -> dict[str, Any]:
        merged = dict(DEFAULT_SETTINGS)
        merged.update(self.get("settings", {}) or {})
        return merged

    def update_settings(self, **changes: Any) -> dict[str, Any]:
        current = self.get("settings", {}) or {}
        current.update({k: v for k, v in changes.items() if k in DEFAULT_SETTINGS})
        self.set("settings", current)
        return self.settings()

    # ---- watches ---------------------------------------------------------
    def add_watch(self, *, history_key: str, rating_key: str, title: str | None,
                  year: int | None, viewed_at: int, due_at: int) -> bool:
        """Insert a new watch. Returns False if it was already known."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO watches(history_key, rating_key, title, year, viewed_at, due_at, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (history_key, rating_key, title, year, viewed_at, due_at, int(time.time())),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def update_watch(self, watch_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE watches SET {cols} WHERE id=?", (*fields.values(), watch_id))
            self._conn.commit()

    def watch(self, watch_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM watches WHERE id=?", (watch_id,)).fetchone()
        return dict(row) if row else None

    def due_watches(self, now: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM watches WHERE status='waiting' AND due_at<=? ORDER BY viewed_at",
                (now,),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_watches(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM watches ORDER BY viewed_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def late_review_candidates(self, since: int, checked_before: int) -> list[dict]:
        """Synced entries still young enough that a review or rating might show up later."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM watches WHERE status='synced' AND viewed_at>=? "
                "AND (last_checked IS NULL OR last_checked<?) ORDER BY viewed_at",
                (since, checked_before),
            ).fetchall()
        return [dict(r) for r in rows]

    def earlier_synced(self, tmdb_id: str | None, imdb_id: str | None, before: int, exclude_id: int) -> bool:
        """Did we already log this film to Letterboxd from an earlier watch?"""
        if not tmdb_id and not imdb_id:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM watches WHERE id<>? AND viewed_at<? AND status='synced' "
                "AND ((tmdb_id IS NOT NULL AND tmdb_id=?) OR (imdb_id IS NOT NULL AND imdb_id=?)) LIMIT 1",
                (exclude_id, before, tmdb_id, imdb_id),
            ).fetchone()
        return row is not None

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM watches GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    # ---- Letterboxd film cache --------------------------------------------
    def cached_film(self, tmdb_id: str | None, imdb_id: str | None) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM films WHERE (tmdb_id IS NOT NULL AND tmdb_id=?) OR (imdb_id IS NOT NULL AND imdb_id=?) LIMIT 1",
                (tmdb_id, imdb_id),
            ).fetchone()
        return dict(row) if row else None

    def cache_film(self, tmdb_id: str | None, imdb_id: str | None, film_id: str, slug: str | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO films(tmdb_id, imdb_id, film_id, slug) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(film_id) DO UPDATE SET tmdb_id=COALESCE(excluded.tmdb_id, tmdb_id), "
                "imdb_id=COALESCE(excluded.imdb_id, imdb_id), slug=COALESCE(excluded.slug, slug)",
                (tmdb_id, imdb_id, film_id, slug),
            )
            self._conn.commit()
