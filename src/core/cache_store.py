# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import aiosqlite

from sif.core.async_fs import run_fs

CACHE_QUEUE_MAXSIZE = max(1, int(os.getenv("SIF_CACHE_QUEUE_MAXSIZE", "1024")))
DB_OPEN_MAX_ATTEMPTS = max(1, int(os.getenv("SIF_CACHE_DB_OPEN_MAX_ATTEMPTS", "5")))
DB_OPEN_BASE_BACKOFF_S = max(0.0, float(os.getenv("SIF_CACHE_DB_OPEN_BASE_BACKOFF_S", "0.05")))

logger = logging.getLogger(__name__)
_STOP = object()
_MISSING = object()


@dataclass
class _PendingWrite:
    updates: Dict[str, Any]
    ready: asyncio.Event
    accepted: bool = False




def _load_legacy_cache_sync(cache_path: Path) -> Dict[str, Any]:
    if not cache_path.exists():
        return {}
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


class AsyncCacheStore:
    def __init__(
        self,
        cache_path: Path,
        *,
        lock_timeout_s: float = 0.0,
        max_attempts: int = 1,
        base_backoff_s: float = 0.0,
    ) -> None:
        self.cache_path = cache_path
        # Deprecated constructor args are kept for backward compatibility.
        self.lock_timeout_s = lock_timeout_s
        self.max_attempts = max_attempts
        self.base_backoff_s = base_backoff_s

        self._db_path = cache_path.with_suffix(cache_path.suffix + ".sqlite3")
        self._cache: Dict[str, Any] = {}
        self._cache_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._queue: asyncio.Queue[_PendingWrite | object] = asyncio.Queue(maxsize=CACHE_QUEUE_MAXSIZE)
        self._writer_task: asyncio.Task[None] | None = None
        self._db: aiosqlite.Connection | None = None
        self._started = False
        self._stopping = False
        self._state_epoch = 0

    async def start(self) -> None:
        async with self._state_lock:
            if self._started:
                return
            self._started = True
            self._stopping = False

        try:
            await self._open_db()
            self._cache = await self._load_cache_from_db()
            if not self._cache:
                legacy_cache = await self._load_legacy_cache_file()
                if legacy_cache:
                    await self._flush_updates(legacy_cache)
                    self._cache = legacy_cache
        except Exception:
            await self._close_db()
            async with self._state_lock:
                self._started = False
                self._stopping = False
            raise

        async with self._state_lock:
            if not self._started or self._stopping:
                await self._close_db()
                return
            self._writer_task = asyncio.create_task(self._writer_loop())

    async def stop(self) -> None:
        should_enqueue_stop = False
        async with self._state_lock:
            if not self._started:
                return
            writer_task = self._writer_task
            if writer_task is None:
                self._started = False
                self._stopping = False
                self._state_epoch += 1
                await self._close_db()
                return
            if not self._stopping:
                self._stopping = True
                self._state_epoch += 1
                should_enqueue_stop = True

        cancelled = False
        if should_enqueue_stop:
            enqueue_stop_task = asyncio.create_task(self._queue.put(_STOP))
            try:
                await asyncio.shield(enqueue_stop_task)
            except asyncio.CancelledError:
                cancelled = True
                while not enqueue_stop_task.done():
                    try:
                        await asyncio.shield(enqueue_stop_task)
                    except asyncio.CancelledError:
                        continue

        try:
            if writer_task is not None:
                await asyncio.shield(writer_task)
        except asyncio.CancelledError:
            cancelled = True
            if writer_task is not None:
                while not writer_task.done():
                    try:
                        await asyncio.shield(writer_task)
                    except asyncio.CancelledError:
                        continue
        finally:
            await self._close_db()
            async with self._state_lock:
                self._writer_task = None
                self._started = False
                self._stopping = False

        if cancelled:
            raise asyncio.CancelledError

    async def get(self, key: str) -> Any:
        async with self._cache_lock:
            return self._cache.get(key)

    async def put_many(self, updates: Dict[str, Any]) -> None:
        if not updates:
            return

        normalized_updates = dict(updates)

        async with self._state_lock:
            if not self._started or self._stopping:
                raise RuntimeError("AsyncCacheStore is not accepting writes")
            state_epoch = self._state_epoch

        previous_values: dict[str, Any] = {}
        async with self._cache_lock:
            for key in normalized_updates:
                previous_values[key] = self._cache.get(key, _MISSING)
            self._cache.update(normalized_updates)

        pending_write = _PendingWrite(updates=normalized_updates, ready=asyncio.Event())

        should_reject = False
        async with self._state_lock:
            if not self._started or self._stopping or self._state_epoch != state_epoch:
                should_reject = True
        if should_reject:
            await self._rollback_cache_updates(previous_values, normalized_updates)
            raise RuntimeError("AsyncCacheStore is not accepting writes")

        try:
            await self._queue.put(pending_write)
            should_cancel = False
            async with self._state_lock:
                if not self._started or self._stopping or self._state_epoch != state_epoch:
                    should_cancel = True
            if should_cancel:
                await self._rollback_cache_updates(previous_values, normalized_updates)
                raise RuntimeError("AsyncCacheStore is not accepting writes")
            pending_write.accepted = True
        except asyncio.CancelledError:
            await self._rollback_cache_updates(previous_values, normalized_updates)
            raise
        finally:
            pending_write.ready.set()

    async def _open_db(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        last_error: Exception | None = None
        for attempt in range(1, DB_OPEN_MAX_ATTEMPTS + 1):
            db: aiosqlite.Connection | None = None
            try:
                db = await aiosqlite.connect(self._db_path, timeout=5.0)
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA busy_timeout=5000;")
                await db.execute("PRAGMA journal_mode=WAL;")
                await db.execute("PRAGMA synchronous=NORMAL;")
                await db.execute("PRAGMA temp_store=MEMORY;")
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cache_entries (
                        cache_key TEXT PRIMARY KEY,
                        value_json TEXT NOT NULL,
                        updated_at REAL NOT NULL DEFAULT (unixepoch('subsec'))
                    )
                    """
                )
                await db.commit()
                self._db = db
                return
            except sqlite3.OperationalError as exc:
                last_error = exc
                if db is not None:
                    await db.close()
                if "database is locked" not in str(exc).lower() or attempt >= DB_OPEN_MAX_ATTEMPTS:
                    raise
                await asyncio.sleep(DB_OPEN_BASE_BACKOFF_S * attempt)
            except Exception:
                if db is not None:
                    await db.close()
                raise
        if last_error is not None:
            raise last_error

    async def _close_db(self) -> None:
        db = self._db
        self._db = None
        if db is None:
            return
        await db.close()

    async def _load_cache_from_db(self) -> Dict[str, Any]:
        db = self._require_db()
        payload: Dict[str, Any] = {}
        async with db.execute("SELECT cache_key, value_json FROM cache_entries") as cursor:
            async for row in cursor:
                try:
                    payload[row["cache_key"]] = json.loads(row["value_json"])
                except json.JSONDecodeError:
                    logger.warning("Skipping invalid cache row for key %s", row["cache_key"])
        return payload

    async def _load_legacy_cache_file(self) -> Dict[str, Any]:
        return await run_fs(_load_legacy_cache_sync, self.cache_path)

    async def _flush_updates(self, updates: Dict[str, Any]) -> None:
        db = self._require_db()
        rows = [(key, json.dumps(value, ensure_ascii=False)) for key, value in updates.items()]
        await db.executemany(
            """
            INSERT INTO cache_entries(cache_key, value_json, updated_at)
            VALUES(?, ?, unixepoch('subsec'))
            ON CONFLICT(cache_key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            rows,
        )
        await db.commit()

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("AsyncCacheStore database is not available")
        return self._db

    async def _rollback_cache_updates(self, previous_values: dict[str, Any], rejected_updates: Dict[str, Any]) -> None:
        async with self._cache_lock:
            for key, value in previous_values.items():
                if self._cache.get(key, _MISSING) != rejected_updates.get(key, _MISSING):
                    continue
                if value is _MISSING:
                    self._cache.pop(key, None)
                else:
                    self._cache[key] = value

    async def _writer_loop(self) -> None:
        try:
            while True:
                item = await self._queue.get()
                if item is _STOP:
                    self._queue.task_done()
                    break

                pending_write = item
                await pending_write.ready.wait()
                if not pending_write.accepted:
                    self._queue.task_done()
                    continue

                updates = dict(pending_write.updates)
                self._queue.task_done()

                while True:
                    try:
                        queued = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if queued is _STOP:
                        self._queue.task_done()
                        await self._flush_updates(updates)
                        return
                    queued_write = queued
                    await queued_write.ready.wait()
                    if not queued_write.accepted:
                        self._queue.task_done()
                        continue
                    updates.update(queued_write.updates)
                    self._queue.task_done()

                await self._flush_updates(updates)
        except asyncio.CancelledError:
            raise
