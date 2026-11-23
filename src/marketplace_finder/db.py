from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    price_cents INTEGER,
    location_text TEXT NOT NULL DEFAULT '',
    lat REAL,
    lon REAL,
    distance_mi REAL,
    image_urls TEXT NOT NULL DEFAULT '[]',
    description TEXT NOT NULL DEFAULT '',
    pickup INTEGER,
    posted_at TEXT NOT NULL DEFAULT '',
    extra TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    filter_reason TEXT,
    detail_fetched INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);
CREATE INDEX IF NOT EXISTS listings_status ON listings (status, first_seen_at);

CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    model TEXT NOT NULL,
    verdict TEXT NOT NULL,
    passed INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    sent_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    summary TEXT
);

-- One row per source, plus "evaluator", tracking consecutive failures and alerts.
CREATE TABLE IF NOT EXISTS health (
    name TEXT PRIMARY KEY,
    failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    paused_until TEXT,
    needs_attention INTEGER NOT NULL DEFAULT 0,
    alerted INTEGER NOT NULL DEFAULT 0
);

-- Small named records that outlive a run, such as how far the opening post has got.
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Pages fetched from reference sites, kept so they are fetched rarely.
CREATE TABLE IF NOT EXISTS cache (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
"""

# pending_eval -> rejected | matched -> notified;  filtered = dropped by code;  error = gave up.
# seeded = matched before the opening post and not picked for it, so never posted.
PENDING, FILTERED, REJECTED, MATCHED, NOTIFIED, SEEDED, ERROR = (
    "pending_eval",
    "filtered",
    "rejected",
    "matched",
    "notified",
    "seeded",
    "error",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # -- listings ---------------------------------------------------------

    def get(self, source: str, source_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM listings WHERE source = ? AND source_id = ?", (source, source_id)
        ).fetchone()

    def insert(self, listing: Listing, status: str, filter_reason: str | None) -> None:
        now = now_iso()
        self.conn.execute(
            """INSERT INTO listings (source, source_id, url, title, price_cents, location_text,
                   lat, lon, distance_mi, image_urls, description, pickup, posted_at, extra,
                   status, filter_reason, first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                listing.source,
                listing.source_id,
                listing.url,
                listing.title,
                listing.price_cents,
                listing.location_text,
                listing.lat,
                listing.lon,
                listing.distance_mi,
                json.dumps(listing.image_urls),
                listing.description,
                None if listing.pickup is None else int(listing.pickup),
                listing.posted_at,
                json.dumps(listing.extra),
                status,
                filter_reason,
                now,
                now,
            ),
        )
        self.conn.commit()

    def touch(self, listing: Listing) -> None:
        """Record that a known listing is still up, with its current price."""
        self.conn.execute(
            "UPDATE listings SET last_seen_at = ?, price_cents = ? WHERE source = ? AND source_id = ?",
            (now_iso(), listing.price_cents, listing.source, listing.source_id),
        )
        self.conn.commit()

    def save_detail(self, listing: Listing) -> None:
        self.conn.execute(
            """UPDATE listings SET url = ?, title = ?, price_cents = ?, location_text = ?, lat = ?,
                   lon = ?, distance_mi = ?, image_urls = ?, description = ?, pickup = ?,
                   posted_at = ?, extra = ?, detail_fetched = 1
               WHERE source = ? AND source_id = ?""",
            (
                listing.url,
                listing.title,
                listing.price_cents,
                listing.location_text,
                listing.lat,
                listing.lon,
                listing.distance_mi,
                json.dumps(listing.image_urls),
                listing.description,
                None if listing.pickup is None else int(listing.pickup),
                listing.posted_at,
                json.dumps(listing.extra),
                listing.source,
                listing.source_id,
            ),
        )
        self.conn.commit()

    def set_status(self, listing: Listing, status: str, filter_reason: str | None = None) -> None:
        self.conn.execute(
            "UPDATE listings SET status = ?, filter_reason = ? WHERE source = ? AND source_id = ?",
            (status, filter_reason, listing.source, listing.source_id),
        )
        self.conn.commit()

    def bump_attempts(self, listing: Listing) -> int:
        self.conn.execute(
            "UPDATE listings SET attempts = attempts + 1 WHERE source = ? AND source_id = ?",
            (listing.source, listing.source_id),
        )
        self.conn.commit()
        return self.get(listing.source, listing.source_id)["attempts"]

    def with_status(self, status: str, limit: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM listings WHERE status = ? ORDER BY first_seen_at, source, source_id"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return self.conn.execute(sql, (status,)).fetchall()

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) AS n FROM listings GROUP BY status")
        return {row["status"]: row["n"] for row in rows}

    # -- evaluations and notifications -------------------------------------

    def add_evaluation(self, listing: Listing, model: str, verdict: dict, passed: bool) -> None:
        self.conn.execute(
            "INSERT INTO evaluations (source, source_id, model, verdict, passed, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (listing.source, listing.source_id, model, json.dumps(verdict), int(passed), now_iso()),
        )
        self.conn.commit()

    def latest_verdict(self, source: str, source_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT verdict FROM evaluations WHERE source = ? AND source_id = ?"
            " ORDER BY id DESC LIMIT 1",
            (source, source_id),
        ).fetchone()
        return json.loads(row["verdict"]) if row else None

    def add_notification(self, listing: Listing) -> None:
        self.conn.execute(
            "INSERT INTO notifications (source, source_id, sent_at) VALUES (?, ?, ?)",
            (listing.source, listing.source_id, now_iso()),
        )
        self.conn.commit()

    # -- runs and health ---------------------------------------------------

    def start_run(self) -> int:
        cur = self.conn.execute("INSERT INTO runs (started_at) VALUES (?)", (now_iso(),))
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, summary: dict) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, summary = ? WHERE id = ?",
            (now_iso(), json.dumps(summary), run_id),
        )
        self.conn.commit()

    def last_runs(self, limit: int = 5) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def health(self, name: str) -> sqlite3.Row:
        self.conn.execute("INSERT OR IGNORE INTO health (name) VALUES (?)", (name,))
        self.conn.commit()
        return self.conn.execute("SELECT * FROM health WHERE name = ?", (name,)).fetchone()

    def all_health(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM health ORDER BY name").fetchall()

    def update_health(self, name: str, **fields) -> None:
        self.health(name)
        assignments = ", ".join(f"{column} = ?" for column in fields)
        self.conn.execute(
            f"UPDATE health SET {assignments} WHERE name = ?", (*fields.values(), name)
        )
        self.conn.commit()

    # -- state -------------------------------------------------------------

    def get_state(self, key: str) -> dict | None:
        row = self.conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def set_state(self, key: str, value: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, json.dumps(value))
        )
        self.conn.commit()

    # -- cache -------------------------------------------------------------

    def cache_get(self, key: str) -> tuple[str, str] | None:
        """(value, fetched_at) or None."""
        row = self.conn.execute(
            "SELECT value, fetched_at FROM cache WHERE key = ?", (key,)
        ).fetchone()
        return (row["value"], row["fetched_at"]) if row else None

    def cache_put(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO cache (key, value, fetched_at) VALUES (?, ?, ?)",
            (key, value, now_iso()),
        )
        self.conn.commit()


def row_to_listing(row: sqlite3.Row) -> Listing:
    return Listing(
        source=row["source"],
        source_id=row["source_id"],
        url=row["url"],
        title=row["title"],
        price_cents=row["price_cents"],
        location_text=row["location_text"],
        lat=row["lat"],
        lon=row["lon"],
        distance_mi=row["distance_mi"],
        image_urls=json.loads(row["image_urls"]),
        description=row["description"],
        pickup=None if row["pickup"] is None else bool(row["pickup"]),
        posted_at=row["posted_at"],
        extra=json.loads(row["extra"]),
    )
