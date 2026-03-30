from __future__ import annotations

import asyncio
import importlib
import os
import pickle
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, TypeVar
from weakref import WeakKeyDictionary

T = TypeVar("T")

_CPU_CONCURRENCY_LIMIT = max(1, int(os.getenv("SIF_CPU_CONCURRENCY", "4")))
_CPU_SEMAPHORES: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = WeakKeyDictionary()
_CPU_EXECUTORS: WeakKeyDictionary[asyncio.AbstractEventLoop, ProcessPoolExecutor] = WeakKeyDictionary()
_CPU_EXECUTOR_LOCKS: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = WeakKeyDictionary()


@dataclass(frozen=True)
class _CpuCallSpec:
    module: str
    qualname: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


def _get_cpu_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _CPU_SEMAPHORES.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(_CPU_CONCURRENCY_LIMIT)
        _CPU_SEMAPHORES[loop] = semaphore
    return semaphore


def _get_executor_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _CPU_EXECUTOR_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _CPU_EXECUTOR_LOCKS[loop] = lock
    return lock


def _executor_worker_count() -> int:
    return max(1, int(os.getenv("SIF_CPU_MAX_WORKERS", str(_CPU_CONCURRENCY_LIMIT))))


async def start_cpu_executor() -> None:
    loop = asyncio.get_running_loop()
    if _CPU_EXECUTORS.get(loop) is not None:
        return
    async with _get_executor_lock():
        if _CPU_EXECUTORS.get(loop) is not None:
            return
        _CPU_EXECUTORS[loop] = ProcessPoolExecutor(
            max_workers=_executor_worker_count(),
        )


def _resolve_callable(spec: _CpuCallSpec) -> Callable[..., Any]:
    target: Any = importlib.import_module(spec.module)
    for attribute in spec.qualname.split("."):
        target = getattr(target, attribute)
    if not callable(target):
        raise TypeError(f"Resolved target '{spec.module}.{spec.qualname}' is not callable")
    return target


def _cpu_worker_dispatch(spec: _CpuCallSpec) -> Any:
    return _resolve_callable(spec)(*spec.args, **spec.kwargs)


def _shutdown_executor_sync(executor: ProcessPoolExecutor) -> None:
    executor.shutdown(wait=True, cancel_futures=True)


def _build_call_spec(func: Callable[..., T], args: tuple[Any, ...], kwargs: dict[str, Any]) -> _CpuCallSpec:
    module = getattr(func, "__module__", None)
    qualname = getattr(func, "__qualname__", None)
    name = getattr(func, "__name__", None)
    if not isinstance(module, str) or not module:
        raise TypeError(f"Unsupported callable type {type(func)!r}: missing __module__")
    if not isinstance(qualname, str) or not qualname:
        raise TypeError(f"Unsupported callable type {type(func)!r}: missing __qualname__")
    if name == "<lambda>" or "<locals>" in qualname:
        raise TypeError(
            "run_cpu supports only top-level or static functions; lambda/local functions are not supported"
        )

    spec = _CpuCallSpec(module=module, qualname=qualname, args=args, kwargs=dict(kwargs))
    try:
        resolved = _resolve_callable(spec)
    except Exception as exc:
        raise TypeError(
            f"Unsupported callable '{module}.{qualname}': cannot resolve import path ({exc})"
        ) from exc
    if resolved is not func:
        raise TypeError(
            f"Unsupported callable '{module}.{qualname}': only top-level/static functions are allowed"
        )

    try:
        pickle.dumps(spec)
    except Exception as exc:
        raise TypeError(
            f"run_cpu arguments are not serializable for '{module}.{qualname}': {exc}"
        ) from exc
    return spec


async def shutdown_cpu_executor() -> None:
    loop = asyncio.get_running_loop()
    async with _get_executor_lock():
        executor = _CPU_EXECUTORS.pop(loop, None)
        _CPU_SEMAPHORES.pop(loop, None)
        _CPU_EXECUTOR_LOCKS.pop(loop, None)
    if executor is None:
        return
    shutdown_task = asyncio.ensure_future(loop.run_in_executor(None, _shutdown_executor_sync, executor))
    cancelled = False
    try:
        await asyncio.shield(shutdown_task)
    except asyncio.CancelledError:
        cancelled = True
        await asyncio.shield(shutdown_task)
    if cancelled:
        raise asyncio.CancelledError


async def run_cpu(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run CPU-bound work in a process pool bound to the current event loop.

    Supported callables are importable top-level functions and static functions
    addressable by module + qualname. ``args``/``kwargs`` must be pickle-serializable.
    Lambda functions, local/nested callables, bound methods, and non-serializable
    arguments are rejected with a fail-fast ``TypeError``.
    """

    await start_cpu_executor()
    loop = asyncio.get_running_loop()
    executor = _CPU_EXECUTORS[loop]
    call_spec = _build_call_spec(func, args, kwargs)
    async with _get_cpu_semaphore():
        return await loop.run_in_executor(executor, _cpu_worker_dispatch, call_spec)
