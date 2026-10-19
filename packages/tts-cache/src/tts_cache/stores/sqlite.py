"""SQLite: the shared cache for a single process (4.2, 4.2a, 4.2c, 4.4, 4.5).

All database work runs on one dedicated thread, so the event loop never
blocks on disk and the connection is only ever used from the thread that
opened it.
"""

import asyncio
import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TypeVar

from tts_cache.key import split_key
from tts_cache.scope import Scope, scope_base_hash, scope_hash
from tts_cache.store import HOT_HALF_LIFE_S, Entry, TouchMeta, decayed

T = TypeVar("T")

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL,
    hash TEXT NOT NULL,
    audio BLOB NOT NULL,
    codec TEXT NOT NULL,
    sample_rate INTEGER NOT NULL,
    channels INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    bytes INTEGER NOT NULL,
    text TEXT NOT NULL,
    canonical_text TEXT NOT NULL,
    language TEXT NOT NULL,
    tags TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_hit_at REAL,
    hit_count INTEGER NOT NULL DEFAULT 0,
    expires_at REAL,
    verify_score REAL,
    ingest_version INTEGER NOT NULL DEFAULT 1,
    hot_score REAL NOT NULL DEFAULT 0,
    UNIQUE (scope, hash)
);
CREATE INDEX IF NOT EXISTS entries_expiry ON entries (expires_at);
CREATE TABLE IF NOT EXISTS tags (
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    scope TEXT NOT NULL,
    hash TEXT NOT NULL,
    PRIMARY KEY (key, value, scope, hash)
);
CREATE INDEX IF NOT EXISTS tags_entry ON tags (scope, hash);
CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(text);
CREATE TABLE IF NOT EXISTS generation (
    scope TEXT PRIMARY KEY,
    gen INTEGER NOT NULL
);
"""

_COLUMNS = (
    "scope, hash, audio, codec, sample_rate, channels, duration_ms, bytes, text, canonical_text, language, "
    "tags, created_at, last_hit_at, hit_count, expires_at, verify_score, ingest_version, hot_score"
)


def _entry(row: sqlite3.Row) -> Entry:
    return Entry(
        key=f"tts:{row['scope']}:{row['hash']}",
        audio=row["audio"],
        codec=row["codec"],
        sample_rate=row["sample_rate"],
        channels=row["channels"],
        duration_ms=row["duration_ms"],
        text=row["text"],
        canonical_text=row["canonical_text"],
        language=row["language"],
        tags=json.loads(row["tags"]),
        created_at=row["created_at"],
        last_hit_at=row["last_hit_at"],
        hit_count=row["hit_count"],
        expires_at=row["expires_at"],
        verify_score=row["verify_score"],
        ingest_version=row["ingest_version"],
        hot_score=row["hot_score"],
    )


def _fts_query(match: str) -> str:
    # Match the words as a phrase, so user text cannot inject FTS5 syntax.
    return '"' + match.replace('"', '""') + '"'


class SQLiteStore:
    """One file on local disk with the section 4.2c schema.

    Expired entries are invisible to :meth:`get` at once and deleted by the
    sweep, which also trims the file to ``max_bytes`` of audio by deleting the
    lowest-scoring entries (decayed hit count times characters saved, divided
    by bytes).
    """

    def __init__(
        self,
        path: str | Path = "tts-cache.db",
        *,
        max_bytes: int | None = None,
        sweep_interval_s: float = 300.0,
    ) -> None:
        self.path = str(path)
        self.max_bytes = max_bytes
        self.sweep_interval_s = sweep_interval_s
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts-cache-sqlite")
        self._conn: sqlite3.Connection | None = None
        self._sweeper: asyncio.Task[None] | None = None
        self._closed = False

    # Plumbing.

    def _connect(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("SQLiteStore is closed")
        if self._conn is None:
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.create_function("decayed", 3, decayed, deterministic=True)
            conn.executescript(SCHEMA)
            self._conn = conn
        return self._conn

    async def _run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, lambda: fn(self._connect()))

    async def start(self) -> None:
        """Open the database and start the sweep task."""
        await self._run(lambda c: None)
        if self._sweeper is None and self.sweep_interval_s > 0:
            self._sweeper = asyncio.create_task(self._sweep_loop())

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self.sweep_interval_s)
            try:
                await self.sweep()
            except Exception:  # the sweep retries on its next tick
                pass

    async def close(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            self._sweeper = None

        def shut(c: sqlite3.Connection) -> None:
            c.close()
            self._conn = None

        if self._conn is not None and not self._closed:
            await self._run(shut)
        self._closed = True
        self._executor.shutdown(wait=False)

    # CacheStore.

    async def get(self, key: str) -> Entry | None:
        scope, digest = split_key(key)
        now = time.time()

        def q(c: sqlite3.Connection) -> Entry | None:
            row = c.execute(
                f"SELECT {_COLUMNS} FROM entries WHERE scope = ? AND hash = ? "
                "AND (expires_at IS NULL OR expires_at > ?)",
                (scope, digest, now),
            ).fetchone()
            return _entry(row) if row else None

        entry = await self._run(q)
        if entry is not None:
            entry.tier = "sqlite"
        return entry

    async def put(self, key: str, entry: Entry) -> None:
        scope, digest = split_key(key)

        def q(c: sqlite3.Connection) -> None:
            with c:
                old = c.execute(
                    "SELECT id FROM entries WHERE scope = ? AND hash = ?", (scope, digest)
                ).fetchone()
                if old:
                    c.execute("DELETE FROM entries_fts WHERE rowid = ?", (old["id"],))
                    c.execute("DELETE FROM entries WHERE id = ?", (old["id"],))
                    c.execute("DELETE FROM tags WHERE scope = ? AND hash = ?", (scope, digest))
                cur = c.execute(
                    f"INSERT INTO entries ({_COLUMNS}) VALUES ({', '.join('?' * 19)})",
                    (
                        scope,
                        digest,
                        entry.audio,
                        entry.codec,
                        entry.sample_rate,
                        entry.channels,
                        entry.duration_ms,
                        entry.nbytes,
                        entry.text,
                        entry.canonical_text,
                        entry.language,
                        json.dumps(dict(entry.tags), sort_keys=True),
                        entry.created_at,
                        entry.last_hit_at,
                        entry.hit_count,
                        entry.expires_at,
                        entry.verify_score,
                        entry.ingest_version,
                        entry.hot_score,
                    ),
                )
                c.execute("INSERT INTO entries_fts (rowid, text) VALUES (?, ?)", (cur.lastrowid, entry.text))
                c.executemany(
                    "INSERT OR IGNORE INTO tags (key, value, scope, hash) VALUES (?, ?, ?, ?)",
                    [(k, v, scope, digest) for k, v in entry.tags.items()],
                )

        await self._run(q)

    async def touch(self, key: str, meta: TouchMeta) -> None:
        scope, digest = split_key(key)

        def q(c: sqlite3.Connection) -> None:
            with c:
                c.execute(
                    "UPDATE entries SET hot_score = decayed(hot_score, last_hit_at, ?) + ?, last_hit_at = ?, "
                    "hit_count = hit_count + ?, expires_at = ? WHERE scope = ? AND hash = ?",
                    (meta.hit_at, meta.hits, meta.hit_at, meta.hits, meta.expires_at, scope, digest),
                )

        await self._run(q)

    def _delete_ids(self, c: sqlite3.Connection, rows: list[sqlite3.Row]) -> int:
        for row in rows:
            c.execute("DELETE FROM entries_fts WHERE rowid = ?", (row["id"],))
            c.execute("DELETE FROM tags WHERE scope = ? AND hash = ?", (row["scope"], row["hash"]))
            c.execute("DELETE FROM entries WHERE id = ?", (row["id"],))
        return len(rows)

    async def delete(self, key: str) -> None:
        scope, digest = split_key(key)

        def q(c: sqlite3.Connection) -> None:
            with c:
                rows = c.execute(
                    "SELECT id, scope, hash FROM entries WHERE scope = ? AND hash = ?", (scope, digest)
                ).fetchall()
                self._delete_ids(c, rows)

        await self._run(q)

    def _matching(
        self, c: sqlite3.Connection, tags: Mapping[str, str] | None, match: str | None
    ) -> list[sqlite3.Row]:
        sql = "SELECT e.id, e.scope, e.hash FROM entries e WHERE 1"
        args: list[Any] = []
        for k, v in (tags or {}).items():
            sql += " AND EXISTS (SELECT 1 FROM tags t WHERE t.scope = e.scope AND t.hash = e.hash "
            sql += "AND t.key = ? AND t.value = ?)"
            args += [k, v]
        if match:
            sql += " AND e.id IN (SELECT rowid FROM entries_fts WHERE entries_fts MATCH ?)"
            args.append(_fts_query(match))
        return c.execute(sql, args).fetchall()

    async def purge(self, *, tags: Mapping[str, str] | None = None, match: str | None = None) -> int:
        if not tags and not match:
            raise ValueError("purge needs tags or match")

        def q(c: sqlite3.Connection) -> int:
            with c:
                return self._delete_ids(c, self._matching(c, tags, match))

        return await self._run(q)

    async def inspect(self, match: str | None = None, *, limit: int = 50) -> list[dict[str, Any]]:
        """Entries whose text matches ``match`` (all when ``None``), most hit first."""

        def q(c: sqlite3.Connection) -> list[dict[str, Any]]:
            ids = [r["id"] for r in self._matching(c, None, match)]
            if not ids:
                return []
            rows = c.execute(
                "SELECT scope, hash, text, language, codec, bytes, duration_ms, hit_count, created_at, "
                f"last_hit_at, expires_at FROM entries WHERE id IN ({', '.join('?' * len(ids))}) "
                "ORDER BY hit_count DESC, created_at LIMIT ?",
                [*ids, limit],
            ).fetchall()
            return [{**dict(r), "key": f"tts:{r['scope']}:{r['hash']}"} for r in rows]

        return await self._run(q)

    async def hot(self, scope: Scope, limit_bytes: int) -> list[Entry]:
        sh = scope_hash(scope)
        now = time.time()

        def q(c: sqlite3.Connection) -> list[Entry]:
            out, used = [], 0
            cur = c.execute(
                f"SELECT {_COLUMNS} FROM entries WHERE scope = ? AND (expires_at IS NULL OR expires_at > ?) "
                "ORDER BY decayed(hot_score, last_hit_at, ?) DESC, hit_count DESC",
                (sh, now, now),
            )
            for row in cur:
                if used + row["bytes"] > limit_bytes:
                    break
                used += row["bytes"]
                out.append(_entry(row))
            return out

        return await self._run(q)

    async def hot_texts(self, scope: Scope, limit: int) -> list[tuple[str, str]]:
        """``(key, canonical_text)`` for the top ``limit`` hot entries, for the prefix prefetch."""
        sh = scope_hash(scope)
        now = time.time()

        def q(c: sqlite3.Connection) -> list[tuple[str, str]]:
            rows = c.execute(
                "SELECT scope, hash, canonical_text FROM entries WHERE scope = ? "
                "AND (expires_at IS NULL OR expires_at > ?) "
                "ORDER BY decayed(hot_score, last_hit_at, ?) DESC, hit_count DESC LIMIT ?",
                (sh, now, now, limit),
            ).fetchall()
            return [(f"tts:{r['scope']}:{r['hash']}", r["canonical_text"]) for r in rows]

        return await self._run(q)

    async def generation(self, scope: Scope) -> int:
        base = scope_base_hash(scope)

        def q(c: sqlite3.Connection) -> int:
            row = c.execute("SELECT gen FROM generation WHERE scope = ?", (base,)).fetchone()
            return int(row["gen"]) if row else 0

        return await self._run(q)

    async def bump_generation(self, scope: Scope) -> int:
        base = scope_base_hash(scope)

        def q(c: sqlite3.Connection) -> int:
            with c:
                c.execute(
                    "INSERT INTO generation (scope, gen) VALUES (?, 1) "
                    "ON CONFLICT (scope) DO UPDATE SET gen = gen + 1",
                    (base,),
                )
                return int(c.execute("SELECT gen FROM generation WHERE scope = ?", (base,)).fetchone()["gen"])

        return await self._run(q)

    # Maintenance.

    async def sweep(self) -> dict[str, int]:
        """Delete expired entries, then trim to ``max_bytes``; returns counts."""
        now = time.time()
        max_bytes = self.max_bytes

        def q(c: sqlite3.Connection) -> dict[str, int]:
            with c:
                expired = self._delete_ids(
                    c,
                    c.execute(
                        "SELECT id, scope, hash FROM entries "
                        "WHERE expires_at IS NOT NULL AND expires_at <= ?",
                        (now,),
                    ).fetchall(),
                )
                evicted = 0
                if max_bytes is not None:
                    total = c.execute("SELECT COALESCE(SUM(bytes), 0) AS b FROM entries").fetchone()["b"]
                    if total > max_bytes:
                        rows = c.execute(
                            "SELECT id, scope, hash, bytes FROM entries ORDER BY "
                            "(decayed(hot_score, last_hit_at, ?) + 1) * length(canonical_text) / bytes ASC",
                            (now,),
                        ).fetchall()
                        doomed = []
                        for row in rows:
                            if total <= max_bytes:
                                break
                            doomed.append(row)
                            total -= row["bytes"]
                        evicted = self._delete_ids(c, doomed)
                return {"expired": expired, "evicted": evicted}

        return await self._run(q)

    async def stats(self) -> dict[str, int]:
        def q(c: sqlite3.Connection) -> dict[str, int]:
            row = c.execute("SELECT COUNT(*) AS n, COALESCE(SUM(bytes), 0) AS b FROM entries").fetchone()
            return {"entries": int(row["n"]), "bytes": int(row["b"])}

        return await self._run(q)


__all__ = ["HOT_HALF_LIFE_S", "SQLiteStore"]
