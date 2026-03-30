import asyncio
import json
import os
import random
import time
from pathlib import Path

from sif.core.async_fs import run_fs

if os.name == "nt":
    import ctypes

    import msvcrt
else:
    import errno
    import fcntl

from sif.core.kernel import KernelState


DEFAULT_GOALS = ["Sustain bounded autonomous self-improvement"]
DEFAULT_CONSTRAINTS = ["Maintain policy boundaries, observability, and rollback readiness"]
DEFAULT_LOCK_TIMEOUT_S = 2.0
DEFAULT_LOCK_BACKOFF_BASE_S = 0.05
DEFAULT_LOCK_BACKOFF_MAX_S = 1.0
DEFAULT_STALE_LOCK_RETRIES = 2


_IS_WINDOWS = os.name == "nt"


def _default_state(*, memory: dict[str, str] | None = None) -> KernelState:
    return KernelState(
        goals=list(DEFAULT_GOALS),
        constraints=list(DEFAULT_CONSTRAINTS),
        memory=memory or {},
    )


def _sync_open_lock_file(lock_path: Path) -> int:
    return os.open(lock_path, os.O_CREAT | os.O_RDWR)


def _sync_close_fd(lock_fd: int) -> None:
    os.close(lock_fd)


def _sync_try_acquire_lock(lock_fd: int) -> bool:
    if _IS_WINDOWS:
        try:
            os.lseek(lock_fd, 0, os.SEEK_SET)
            msvcrt.locking(lock_fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    except OSError as exc:
        if exc.errno in (errno.EAGAIN, errno.EACCES):
            return False
        raise


def _sync_write_lock_metadata(lock_fd: int) -> None:
    metadata = json.dumps(
        {"pid": os.getpid(), "timestamp": time.time()},
        ensure_ascii=False,
    )
    os.lseek(lock_fd, 0, os.SEEK_SET)
    os.ftruncate(lock_fd, 0)
    os.write(lock_fd, metadata.encode("utf-8"))
    os.fsync(lock_fd)


def _sync_read_lock_pid(lock_path: Path) -> int | None:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if isinstance(payload, dict):
        pid = payload.get("pid")
        if isinstance(pid, int) and pid > 0:
            return pid
    return None


def _sync_is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if _IS_WINDOWS:
        try:
            handle = ctypes.windll.kernel32.OpenProcess(
                0x1000,  # PROCESS_QUERY_LIMITED_INFORMATION
                False,
                pid,
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            error = ctypes.get_last_error()
            if error == 5:  # Access is denied.
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _sync_read_state_json(state_path: Path) -> object:
    return json.loads(state_path.read_text(encoding="utf-8"))


def _sync_write_state_atomically(state_path: Path, tmp_path: Path, json_payload: str) -> None:
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(json_payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, state_path)


def _sync_unlink_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _sync_path_exists(path: Path) -> bool:
    return path.exists()


def _sync_ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


async def _acquire_state_lock(lock_path: Path, state_path: Path) -> int:
    start_time = time.monotonic()
    attempt = 0
    stale_lock_retries = 0

    while True:
        lock_fd = await run_fs(_sync_open_lock_file, lock_path)
        try:
            acquired = await run_fs(_sync_try_acquire_lock, lock_fd)
            if acquired:
                await _write_lock_metadata(lock_fd)
                return lock_fd
        except Exception:
            await run_fs(_sync_close_fd, lock_fd)
            raise

        await run_fs(_sync_close_fd, lock_fd)

        if time.monotonic() - start_time >= DEFAULT_LOCK_TIMEOUT_S:
            pid = await _read_lock_pid(lock_path)
            if pid is not None and await run_fs(_sync_is_pid_alive, pid):
                raise RuntimeError(
                    "Timed out acquiring state lock at "
                    f"{lock_path} after {DEFAULT_LOCK_TIMEOUT_S:.2f}s "
                    f"for state {state_path} (pid {pid} still running)"
                )
            stale_lock_retries += 1
            if stale_lock_retries > DEFAULT_STALE_LOCK_RETRIES:
                raise RuntimeError(
                    "Timed out acquiring state lock at "
                    f"{lock_path} after {DEFAULT_LOCK_TIMEOUT_S * (DEFAULT_STALE_LOCK_RETRIES + 1):.2f}s "
                    f"for state {state_path} (stale lock metadata unresolved)"
                )
            start_time = time.monotonic()
            attempt = 0

        jitter = random.uniform(0, DEFAULT_LOCK_BACKOFF_BASE_S)
        delay = min(DEFAULT_LOCK_BACKOFF_BASE_S * (2**attempt), DEFAULT_LOCK_BACKOFF_MAX_S)
        await asyncio.sleep(delay + jitter)
        attempt += 1


async def _release_state_lock(lock_fd: int | None) -> None:
    if lock_fd is not None:
        await run_fs(_sync_close_fd, lock_fd)


async def _write_lock_metadata(lock_fd: int) -> None:
    await run_fs(_sync_write_lock_metadata, lock_fd)


async def _read_lock_pid(lock_path: Path) -> int | None:
    return await run_fs(_sync_read_lock_pid, lock_path)


async def load_state(path: str | Path) -> KernelState:
    state_path = Path(path)
    if not await run_fs(_sync_path_exists, state_path):
        return _default_state()

    try:
        data = await run_fs(_sync_read_state_json, state_path)
    except json.JSONDecodeError:
        return _default_state(memory={"state_load_error": "invalid_json"})
    except (FileNotFoundError, OSError):
        return _default_state()

    if not isinstance(data, dict):
        return _default_state(memory={"state_load_error": "invalid_state_payload"})

    goals = data.get("goals")
    if not isinstance(goals, list) or not all(isinstance(item, str) for item in goals):
        goals = list(DEFAULT_GOALS)
    constraints = data.get("constraints")
    if not isinstance(constraints, list) or not all(isinstance(item, str) for item in constraints):
        constraints = list(DEFAULT_CONSTRAINTS)
    memory = data.get("memory")
    if isinstance(memory, dict):
        memory = {str(key): str(value) for key, value in memory.items()}
    else:
        memory = {}
    return KernelState(
        goals=goals,
        constraints=constraints,
        memory=memory,
    )


async def save_state(path: str | Path, state: KernelState) -> None:
    state_path = Path(path)
    await run_fs(_sync_ensure_parent, state_path)
    lock_path = state_path.with_name(state_path.name + ".lock")
    tmp_path = state_path.with_name(state_path.name + ".tmp")
    lock_fd: int | None = None

    payload = {
        "goals": state.goals,
        "constraints": state.constraints,
        "memory": state.memory,
    }
    json_payload = json.dumps(payload, ensure_ascii=False, indent=2)

    try:
        lock_fd = await _acquire_state_lock(lock_path, state_path)
        await run_fs(_sync_write_state_atomically, state_path, tmp_path, json_payload)
    finally:
        await _release_state_lock(lock_fd)
        await run_fs(_sync_unlink_if_exists, tmp_path)
