# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, TypeVar
from weakref import WeakKeyDictionary

T = TypeVar("T")

_FILE_IO_CONCURRENCY_LIMIT = max(1, int(os.getenv("SIF_FILE_IO_CONCURRENCY", "16")))
_FILE_IO_SEMAPHORES: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = WeakKeyDictionary()
_FILE_IO_EXECUTORS: WeakKeyDictionary[asyncio.AbstractEventLoop, ThreadPoolExecutor] = WeakKeyDictionary()
_FILE_IO_EXECUTOR_LOCKS: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = WeakKeyDictionary()


def _get_file_io_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _FILE_IO_SEMAPHORES.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(_FILE_IO_CONCURRENCY_LIMIT)
        _FILE_IO_SEMAPHORES[loop] = semaphore
    return semaphore


def _get_executor_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _FILE_IO_EXECUTOR_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _FILE_IO_EXECUTOR_LOCKS[loop] = lock
    return lock


def _executor_worker_count() -> int:
    return max(1, int(os.getenv("SIF_FILE_IO_MAX_WORKERS", str(_FILE_IO_CONCURRENCY_LIMIT))))


async def start_fs_executor() -> None:
    loop = asyncio.get_running_loop()
    if _FILE_IO_EXECUTORS.get(loop) is not None:
        return
    async with _get_executor_lock():
        if _FILE_IO_EXECUTORS.get(loop) is not None:
            return
        _FILE_IO_EXECUTORS[loop] = ThreadPoolExecutor(
            max_workers=_executor_worker_count(),
            thread_name_prefix="sif-fs",
        )


async def shutdown_fs_executor() -> None:
    loop = asyncio.get_running_loop()
    async with _get_executor_lock():
        executor = _FILE_IO_EXECUTORS.pop(loop, None)
        _FILE_IO_SEMAPHORES.pop(loop, None)
        _FILE_IO_EXECUTOR_LOCKS.pop(loop, None)
    if executor is None:
        return
    shutdown_task = asyncio.ensure_future(loop.run_in_executor(None, lambda: executor.shutdown(wait=True, cancel_futures=True)))
    cancelled = False
    try:
        await asyncio.shield(shutdown_task)
    except asyncio.CancelledError:
        cancelled = True
        await asyncio.shield(shutdown_task)
    if cancelled:
        raise asyncio.CancelledError


async def run_fs(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    await start_fs_executor()
    loop = asyncio.get_running_loop()
    executor = _FILE_IO_EXECUTORS[loop]
    async with _get_file_io_semaphore():
        return await loop.run_in_executor(executor, lambda: func(*args, **kwargs))


async def read_bytes(path: Path) -> bytes:
    return await run_fs(path.read_bytes)


async def write_bytes(path: Path, payload: bytes) -> None:
    await run_fs(path.write_bytes, payload)


async def read_text(path: Path, encoding: str = "utf-8") -> str:
    return await run_fs(path.read_text, encoding=encoding)


async def write_text(path: Path, payload: str, encoding: str = "utf-8") -> None:
    await run_fs(path.write_text, payload, encoding=encoding)


async def mkdir(path: Path, parents: bool = False, exist_ok: bool = False) -> None:
    await run_fs(path.mkdir, parents=parents, exist_ok=exist_ok)


async def rename(path: Path, target: Path) -> Path:
    return await run_fs(path.rename, target)


async def rmtree(path: Path, ignore_errors: bool = False) -> None:
    await run_fs(shutil.rmtree, path, ignore_errors=ignore_errors)


async def copytree(source: Path, destination: Path, **kwargs: Any) -> Path:
    return await run_fs(shutil.copytree, source, destination, **kwargs)


async def copy_file(
    source: Path,
    destination: Path,
    *,
    preserve_metadata: bool = True,
    buffer_size: int = 1024 * 1024,
) -> Path | None:
    def _copy() -> Path | None:
        if preserve_metadata:
            return shutil.copy2(source, destination)
        with source.open("rb") as src, destination.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=buffer_size)
        return destination

    return await run_fs(_copy)


async def rglob(root: Path, pattern: str) -> list[Path]:
    return await run_fs(lambda: list(root.rglob(pattern)))


async def exists(path: Path) -> bool:
    return await run_fs(path.exists)


async def is_file(path: Path) -> bool:
    return await run_fs(path.is_file)


async def is_dir(path: Path) -> bool:
    return await run_fs(path.is_dir)


async def stat(path: Path) -> os.stat_result:
    return await run_fs(path.stat)


async def unlink(path: Path) -> None:
    await run_fs(path.unlink)


async def unlink_missing_ok(path: Path) -> None:
    await run_fs(path.unlink, True)


async def replace(path: Path, target: Path) -> Path:
    return await run_fs(path.replace, target)


async def glob(root: Path, pattern: str) -> list[Path]:
    return await run_fs(lambda: list(root.glob(pattern)))


async def mkdtemp() -> str:
    return await run_fs(tempfile.mkdtemp)
