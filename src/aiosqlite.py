# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

"""Lightweight local subset of the aiosqlite API used by this repository.

It supports:
- connect(...)
- Connection.execute / executemany / commit / close
- async context manager on Connection and Cursor
- async iteration over Cursor
- Row alias

This shim keeps the project runnable in offline environments without pulling the
external dependency during validation.
"""

import asyncio
import sqlite3
from pathlib import Path
from typing import Any, Iterable

Row = sqlite3.Row


class Cursor:
    def __init__(self, connection: 'Connection', sql: str, parameters: Iterable[Any] | None = None) -> None:
        self._connection = connection
        self._sql = sql
        self._parameters = tuple(parameters or ())
        self._rows: list[Any] | None = None
        self._index = 0

    async def _ensure_rows(self) -> list[Any]:
        if self._rows is None:
            async with self._connection._lock:
                def _run() -> list[Any]:
                    assert self._connection._conn is not None
                    cursor = self._connection._conn.execute(self._sql, self._parameters)
                    return list(cursor.fetchall())
                self._rows = await asyncio.to_thread(_run)
        return self._rows

    async def fetchall(self) -> list[Any]:
        return list(await self._ensure_rows())

    async def __aenter__(self) -> 'Cursor':
        await self._ensure_rows()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    def __aiter__(self) -> 'Cursor':
        return self

    async def __anext__(self) -> Any:
        rows = await self._ensure_rows()
        if self._index >= len(rows):
            raise StopAsyncIteration
        item = rows[self._index]
        self._index += 1
        return item


class Connection:
    def __init__(self, database: str | Path, timeout: float = 5.0) -> None:
        self._database = str(database)
        self._timeout = timeout
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._row_factory: Any = None

    def __await__(self):
        return self._ensure_open().__await__()

    async def _ensure_open(self) -> 'Connection':
        if self._conn is None:
            def _open() -> sqlite3.Connection:
                conn = sqlite3.connect(self._database, timeout=self._timeout)
                if self._row_factory is not None:
                    conn.row_factory = self._row_factory
                return conn
            self._conn = await asyncio.to_thread(_open)
        return self

    @property
    def row_factory(self):
        return self._row_factory

    @row_factory.setter
    def row_factory(self, value) -> None:
        self._row_factory = value
        if self._conn is not None:
            self._conn.row_factory = value

    async def __aenter__(self) -> 'Connection':
        return await self._ensure_open()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def execute(self, sql: str, parameters: Iterable[Any] | None = None) -> Cursor:
        await self._ensure_open()
        return Cursor(self, sql, parameters)

    async def executemany(self, sql: str, seq_of_parameters: Iterable[Iterable[Any]]) -> None:
        await self._ensure_open()
        async with self._lock:
            def _run() -> None:
                assert self._conn is not None
                self._conn.executemany(sql, list(seq_of_parameters))
            await asyncio.to_thread(_run)

    async def commit(self) -> None:
        if self._conn is None:
            return
        async with self._lock:
            await asyncio.to_thread(self._conn.commit)

    async def close(self) -> None:
        conn = self._conn
        self._conn = None
        if conn is not None:
            await asyncio.to_thread(conn.close)


def connect(database: str | Path, timeout: float = 5.0) -> Connection:
    return Connection(database, timeout=timeout)
