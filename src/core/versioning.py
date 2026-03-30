from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal, TypeVar
from weakref import WeakKeyDictionary

from sif.core.async_fs import (
    copy_file as fs_copy_file,
    exists as fs_exists,
    is_dir as fs_is_dir,
    is_file as fs_is_file,
    mkdir as fs_mkdir,
    read_bytes as fs_read_bytes,
    read_text as fs_read_text,
    replace as fs_replace,
    rename as fs_rename,
    run_fs as fs_run,
    rmtree as fs_rmtree,
    unlink as fs_unlink,
    write_text as fs_write_text,
)
from sif.core.time_utils import utc_now_iso
from sif.core.workspace import DEFAULT_IGNORE_PATTERNS

logger = logging.getLogger(__name__)

DEFAULT_VERSION_IGNORE_PATTERNS = DEFAULT_IGNORE_PATTERNS
AUTO_VERSION_ID_MAX_ATTEMPTS = 8
_VERSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_BATCH_SIZE = max(1, int(os.getenv("SIF_FILE_IO_BATCH_SIZE", "256")))
_VERSION_COPY_CONCURRENCY = max(1, int(os.getenv("SIF_VERSION_COPY_CONCURRENCY", "8")))
_RESTORE_COPY_CONCURRENCY = max(1, int(os.getenv("SIF_RESTORE_COPY_CONCURRENCY", "8")))
_REPO_HASH_READ_CONCURRENCY = max(1, int(os.getenv("SIF_REPO_HASH_READ_CONCURRENCY", "8")))
_REPO_HASH_STATE_FILE = Path(".sif") / "repo_hash_state.json"
T = TypeVar("T")

_REPO_HASH_IN_FLIGHT: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[tuple[str, tuple[str, ...]], asyncio.Task[str]]] = (
    WeakKeyDictionary()
)
_REPO_HASH_LOCKS: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = WeakKeyDictionary()


def _cleanup_repo_hash_entry_local(
    loop: asyncio.AbstractEventLoop,
    key: tuple[str, tuple[str, ...]],
    task: asyncio.Task[str],
) -> None:
    registry = _REPO_HASH_IN_FLIGHT.get(loop)
    if registry is None:
        return
    if registry.get(key) is task:
        registry.pop(key, None)
    if not registry:
        _REPO_HASH_IN_FLIGHT.pop(loop, None)
        _REPO_HASH_LOCKS.pop(loop, None)


def _register_repo_hash_task_cleanup(
    loop: asyncio.AbstractEventLoop,
    key: tuple[str, tuple[str, ...]],
    task: asyncio.Task[str],
) -> None:
    task.add_done_callback(
        lambda done_task, *, current_loop=loop, current_key=key: _cleanup_repo_hash_entry_local(
            current_loop,
            current_key,
            done_task,
        )
    )


@dataclass(frozen=True)
class _AsyncGitResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    failed: bool


@dataclass(frozen=True)
class _RepoHashFileState:
    mtime_ns: int
    size: int
    content_hash: str


@dataclass(frozen=True)
class _RepoHashComputation:
    repo_hash: str
    paths: list[Path]


async def create_version_async(
    snapshot_id: str | None = None,
    ignore_patterns: Iterable[str] | None = None,
) -> str:
    repo_root = _resolve_repo_root()
    resolved_root = repo_root.resolve()
    versions_root = resolved_root / ".sif" / "versions"

    validated_snapshot_id: str | None = None
    if snapshot_id is not None:
        validated_snapshot_id = _validate_version_id(snapshot_id, versions_root)

    await fs_mkdir(versions_root, parents=True, exist_ok=True)
    repo_hash_result = await _compute_repo_hash_incremental_async(
        repo_root=resolved_root,
        ignore_patterns=ignore_patterns,
    )
    changed_paths = repo_hash_result.paths
    candidate_hash = repo_hash_result.repo_hash

    if validated_snapshot_id is not None:
        timestamp = utc_now_iso(timespec="microseconds")
        version_id = validated_snapshot_id
        snapshot_root = versions_root / version_id
        if await fs_exists(snapshot_root):
            raise FileExistsError(f"Version directory already exists: {snapshot_root}")
    else:
        timestamp = ""
        version_id = ""
        snapshot_root = versions_root
        for _ in range(AUTO_VERSION_ID_MAX_ATTEMPTS):
            timestamp = utc_now_iso(timespec="microseconds")
            version_id = _build_version_id(timestamp, candidate_hash)
            snapshot_root = versions_root / version_id
            if not await fs_exists(snapshot_root):
                break
        else:
            raise FileExistsError(
                "Failed to generate a unique version id due to repeated collisions "
                f"after {AUTO_VERSION_ID_MAX_ATTEMPTS} attempts"
            )

    temp_root = versions_root / f".tmp-{version_id}"
    if await fs_exists(temp_root):
        await fs_rmtree(temp_root)

    files_root = temp_root / "files"
    copied_paths: list[str] = []
    try:
        await fs_mkdir(files_root, parents=True, exist_ok=True)
        semaphore = asyncio.Semaphore(_VERSION_COPY_CONCURRENCY)

        async def _copy_single(relative_path: Path) -> str | None:
            async with semaphore:
                source_path = resolved_root / relative_path
                if not await fs_is_file(source_path):
                    return None
                destination = files_root / relative_path
                await fs_mkdir(destination.parent, parents=True, exist_ok=True)
                await fs_copy_file(source_path, destination)
                return relative_path.as_posix()

        copied_items: list[str | None] = []
        for offset in range(0, len(changed_paths), _BATCH_SIZE):
            batch = changed_paths[offset : offset + _BATCH_SIZE]
            copied_items.extend(await asyncio.gather(*[_copy_single(path) for path in batch]))

        copied_set = {item for item in copied_items if item is not None}
        copied_paths = [path.as_posix() for path in changed_paths if path.as_posix() in copied_set]

        commit_hash = await _git_commit_hash_async(repo_root)
        snapshot_metadata = {
            "version_id": version_id,
            "timestamp": timestamp,
            "hash": candidate_hash,
            "paths": copied_paths,
            "commit_hash": commit_hash,
        }
        await fs_write_text(
            temp_root / "metadata.json",
            json.dumps(snapshot_metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        await fs_rename(temp_root, snapshot_root)
    except Exception:
        if await fs_exists(temp_root):
            await fs_rmtree(temp_root)
        raise

    return version_id


async def restore_version_async(version_id: str, mode: Literal["soft", "hard"] = "soft") -> bool:
    repo_root = _resolve_repo_root()
    resolved_root = repo_root.resolve()
    versions_root = resolved_root / ".sif" / "versions"
    try:
        version_id = _validate_version_id(version_id, versions_root)
    except ValueError:
        logger.error("Restore failed: rejected version id %r", version_id)
        return False

    snapshot_root = versions_root / version_id
    files_root = snapshot_root / "files"
    if not await fs_is_dir(files_root):
        return False

    metadata = await _read_snapshot_metadata(snapshot_root, version_id)
    if metadata is None:
        return False

    raw_paths = metadata.get("paths")
    if not isinstance(raw_paths, list) or not all(isinstance(path, str) for path in raw_paths):
        logger.error(
            "Restore failed for version %s: snapshot metadata format requires migration (missing/invalid paths)",
            version_id,
        )
        return False

    safe_paths = list(_iter_relative_paths(resolved_root, [Path(path) for path in raw_paths]))
    if raw_paths and not safe_paths:
        logger.error(
            "Restore failed for version %s: metadata contains only unsafe paths",
            version_id,
        )
        return False

    if not raw_paths:
        if mode == "hard":
            await _hard_delete_tracked_files_async(resolved_root)
        return True

    allowed_paths = {path.as_posix() for path in safe_paths}
    for relative_path in safe_paths:
        source = files_root / relative_path
        if not await fs_is_file(source):
            logger.error(
                "Restore failed for version %s: missing snapshot file %s",
                version_id,
                relative_path.as_posix(),
            )
            return False

    nonce = secrets.token_hex(8)
    staging_root = versions_root / f".restore-tmp-{version_id}-{nonce}"
    rollback_root = versions_root / f".restore-rollback-{version_id}-{nonce}"
    rollback_manifest: dict[Path, Path | None] = {}
    restore_semaphore = asyncio.Semaphore(_RESTORE_COPY_CONCURRENCY)

    async def _cleanup_temp_artifacts() -> None:
        if await fs_exists(staging_root):
            await fs_rmtree(staging_root, ignore_errors=True)
        if await fs_exists(rollback_root):
            await fs_rmtree(rollback_root, ignore_errors=True)

    async def _restore_from_rollback() -> None:
        for destination, rollback_path in rollback_manifest.items():
            if rollback_path is None:
                if await fs_exists(destination):
                    await fs_unlink(destination)
                continue

            await fs_mkdir(destination.parent, parents=True, exist_ok=True)
            await fs_copy_file(rollback_path, destination)

    async def _stage_single(relative_path: Path) -> None:
        async with restore_semaphore:
            source = files_root / relative_path
            staged = staging_root / relative_path
            await fs_mkdir(staged.parent, parents=True, exist_ok=True)
            await fs_copy_file(source, staged)

    async def _backup_single(relative_path: Path) -> None:
        async with restore_semaphore:
            destination = resolved_root / relative_path
            rollback_path: Path | None = None
            if await fs_exists(destination):
                rollback_path = rollback_root / relative_path
                await fs_mkdir(rollback_path.parent, parents=True, exist_ok=True)
                await fs_copy_file(destination, rollback_path)
            rollback_manifest[destination] = rollback_path

    async def _apply_single(relative_path: Path) -> None:
        async with restore_semaphore:
            destination = resolved_root / relative_path
            staged = staging_root / relative_path
            await fs_mkdir(destination.parent, parents=True, exist_ok=True)
            await fs_replace(staged, destination)

    try:
        await fs_mkdir(staging_root, parents=True, exist_ok=True)
        await fs_mkdir(rollback_root, parents=True, exist_ok=True)

        for offset in range(0, len(safe_paths), _BATCH_SIZE):
            batch = safe_paths[offset : offset + _BATCH_SIZE]
            await asyncio.gather(*[_stage_single(relative_path) for relative_path in batch])

        for offset in range(0, len(safe_paths), _BATCH_SIZE):
            batch = safe_paths[offset : offset + _BATCH_SIZE]
            await asyncio.gather(*[_backup_single(relative_path) for relative_path in batch])

        expected_destinations = {resolved_root / relative_path for relative_path in safe_paths}
        if rollback_manifest.keys() != expected_destinations:
            raise RuntimeError("Restore rollback manifest incomplete before apply phase")

        for offset in range(0, len(safe_paths), _BATCH_SIZE):
            batch = safe_paths[offset : offset + _BATCH_SIZE]
            await asyncio.gather(*[_apply_single(relative_path) for relative_path in batch])
    except Exception:
        try:
            await _restore_from_rollback()
        except Exception:
            logger.exception("Rollback failed while restoring version %s", version_id)
        return False
    finally:
        await _cleanup_temp_artifacts()

    if mode == "hard" and allowed_paths:
        await _hard_delete_tracked_files_async(resolved_root, allowed_paths)
    return True


async def _read_snapshot_metadata(snapshot_root: Path, version_id: str) -> dict[str, Any] | None:
    metadata_path = snapshot_root / "metadata.json"
    try:
        payload = await fs_read_text(metadata_path, encoding="utf-8")
        metadata = json.loads(payload)
    except FileNotFoundError:
        logger.error(
            "Restore failed for version %s: snapshot metadata format requires migration (missing metadata.json)",
            version_id,
        )
        return None
    except json.JSONDecodeError:
        logger.error(
            "Restore failed for version %s: snapshot metadata format requires migration (invalid JSON)",
            version_id,
        )
        return None
    if not isinstance(metadata, dict):
        logger.error(
            "Restore failed for version %s: snapshot metadata format requires migration (payload is not an object)",
            version_id,
        )
        return None
    return metadata


async def _hard_delete_tracked_files_async(
    resolved_root: Path,
    allowed_paths: set[str] | None = None,
) -> None:
    permission_errors: list[Path] = []
    for relative_path in await _collect_repo_files_async(resolved_root):
        if allowed_paths is not None and relative_path.as_posix() in allowed_paths:
            continue
        target = resolved_root / relative_path
        try:
            await fs_unlink(target)
        except FileNotFoundError:
            continue
        except PermissionError as exc:
            logger.warning("Permission error while removing %s: %s", target, exc)
            permission_errors.append(target)
    if permission_errors:
        logger.warning(
            "Encountered %d permission errors while removing files: %s",
            len(permission_errors),
            ", ".join(path.as_posix() for path in permission_errors),
        )


async def list_versions_async() -> list[str]:
    repo_root = _resolve_repo_root()
    resolved_root = repo_root.resolve()
    versions_root = resolved_root / ".sif" / "versions"
    if not await fs_exists(versions_root):
        return []
    versions = await _list_version_ids_async(versions_root)
    if not versions:
        return []
    keyed_versions = await asyncio.gather(*[_version_sort_key_async(versions_root / name) for name in versions])
    return [name for name, _ in sorted(zip(versions, keyed_versions), key=lambda item: item[1])]


def _validate_version_id(name: str, versions_root: Path) -> str:
    if not name.strip():
        raise ValueError("Version id cannot be empty or whitespace-only")
    raw_path = Path(name)
    if raw_path.is_absolute():
        raise ValueError("Version id cannot be an absolute path")
    if ".." in name:
        raise ValueError("Version id cannot contain '..'")
    if "/" in name or "\\" in name:
        raise ValueError("Version id cannot contain path separators")
    if not _VERSION_ID_PATTERN.fullmatch(name):
        raise ValueError("Version id contains unsupported characters")

    resolved_root = versions_root.resolve()
    resolved_candidate = (versions_root / name).resolve()
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("Version id must stay under versions root") from exc
    return name


async def latest_version_async() -> str | None:
    repo_root = _resolve_repo_root()
    resolved_root = repo_root.resolve()
    versions_root = resolved_root / ".sif" / "versions"
    return await _resolve_latest_version_id_async(versions_root)


async def get_repo_hash_async(
    repo_root: Path | None = None,
    ignore_patterns: Iterable[str] | None = None,
) -> str:
    if not _repo_hash_coalesce_enabled():
        return await _compute_repo_hash_async(repo_root=repo_root, ignore_patterns=ignore_patterns)

    resolved_root = (repo_root or _resolve_repo_root()).resolve()
    patterns = tuple(DEFAULT_VERSION_IGNORE_PATTERNS if ignore_patterns is None else ignore_patterns)
    key = (str(resolved_root), patterns)
    loop = asyncio.get_running_loop()
    lock = _REPO_HASH_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _REPO_HASH_LOCKS[loop] = lock

    async with lock:
        registry = _REPO_HASH_IN_FLIGHT.get(loop)
        if registry is None:
            registry = {}
            _REPO_HASH_IN_FLIGHT[loop] = registry
        existing_task = registry.get(key)
        if existing_task is None:
            existing_task = asyncio.create_task(
                _compute_repo_hash_async(repo_root=resolved_root, ignore_patterns=patterns)
            )
            registry[key] = existing_task
            _register_repo_hash_task_cleanup(loop, key, existing_task)

    try:
        return await asyncio.shield(existing_task)
    finally:
        if existing_task.done():
            _cleanup_repo_hash_entry_local(loop, key, existing_task)


def _repo_hash_coalesce_enabled() -> bool:
    return os.getenv("SIF_REPO_HASH_COALESCE", "1") != "0"


async def _compute_repo_hash_async(
    repo_root: Path | None = None,
    ignore_patterns: Iterable[str] | None = None,
) -> str:
    resolved_root = (repo_root or _resolve_repo_root()).resolve()
    commit_hash = await _git_commit_hash_async(resolved_root)
    if commit_hash:
        status_result = await _run_git_async(resolved_root, "status", "--porcelain")
        if status_result.returncode == 0:
            if not status_result.stdout.strip():
                return commit_hash
            workdir_hash = (
                await _compute_repo_hash_incremental_async(
                    repo_root=resolved_root,
                    ignore_patterns=ignore_patterns,
                )
            ).repo_hash
            combined = hashlib.sha256(f"{commit_hash}:{workdir_hash}".encode("utf-8")).hexdigest()
            return combined
    return (
        await _compute_repo_hash_incremental_async(
            repo_root=resolved_root,
            ignore_patterns=ignore_patterns,
        )
    ).repo_hash


def _resolve_repo_root() -> Path:
    env_root = os.getenv("SIF_REPO_ROOT")
    if env_root:
        return Path(env_root)
    try:
        from sif.core.evolution import REPO_ROOT
    except ImportError:
        return Path.cwd()
    return REPO_ROOT


async def _collect_repo_files_async(
    repo_root: Path,
    ignore_patterns: Iterable[str] | None = None,
) -> list[Path]:
    patterns = DEFAULT_VERSION_IGNORE_PATTERNS if ignore_patterns is None else ignore_patterns
    excluded = set(patterns)

    def _collect_files() -> list[Path]:
        paths: list[Path] = []
        for root, dirs, files in os.walk(repo_root, topdown=True):
            root_path = Path(root)
            dirs[:] = [name for name in dirs if name not in excluded]
            for file_name in files:
                if file_name in excluded:
                    continue
                absolute_path = root_path / file_name
                if not absolute_path.is_file():
                    continue
                relative = absolute_path.relative_to(repo_root)
                if relative == _REPO_HASH_STATE_FILE:
                    continue
                if any(part in excluded for part in relative.parts):
                    continue
                paths.append(relative)
        paths.sort(key=lambda item: item.as_posix())
        return paths

    return await fs_run(_collect_files)



def _iter_relative_paths(repo_root: Path, paths: Iterable[Path]) -> Iterable[Path]:
    resolved_root = repo_root.resolve()
    for path in paths:
        resolved_path = path.resolve() if path.is_absolute() else (resolved_root / path).resolve()
        try:
            relative = resolved_path.relative_to(resolved_root)
        except ValueError:
            continue
        yield relative


def _decode_subprocess_output(payload: bytes | None) -> str:
    if not payload:
        return ""
    return payload.decode("utf-8", errors="replace")


async def _run_git_async(
    repo_root: Path,
    *args: str,
    timeout_seconds: float = 3.0,
) -> _AsyncGitResult:
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=repo_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as exc:
        return _AsyncGitResult(
            returncode=None,
            stdout="",
            stderr=f"{type(exc).__name__}: {exc}",
            timed_out=False,
            failed=True,
        )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        return _AsyncGitResult(
            returncode=process.returncode,
            stdout=_decode_subprocess_output(stdout_bytes),
            stderr=_decode_subprocess_output(stderr_bytes),
            timed_out=False,
            failed=False,
        )
    except asyncio.TimeoutError:
        try:
            process.kill()
        except Exception:
            pass
        try:
            stdout_bytes, stderr_bytes = await process.communicate()
        except Exception:
            stdout_bytes, stderr_bytes = b"", b""
        return _AsyncGitResult(
            returncode=process.returncode,
            stdout=_decode_subprocess_output(stdout_bytes),
            stderr=_decode_subprocess_output(stderr_bytes),
            timed_out=True,
            failed=True,
        )
    except Exception as exc:
        return _AsyncGitResult(
            returncode=process.returncode,
            stdout="",
            stderr=f"{type(exc).__name__}: {exc}",
            timed_out=False,
            failed=True,
        )


async def _git_commit_hash_async(repo_root: Path) -> str | None:
    result = await _run_git_async(repo_root, "rev-parse", "HEAD")
    try:
        if result.returncode == 0:
            stdout = result.stdout.strip()
            if stdout:
                return stdout
    except Exception:
        return None
    return None


async def _hash_paths_async(repo_root: Path, paths: Iterable[Path]) -> str:
    hasher = hashlib.sha256()
    sorted_paths = sorted(_iter_relative_paths(repo_root, paths))
    for chunk in _iter_chunks(sorted_paths, _BATCH_SIZE):
        payloads = await asyncio.gather(*[_read_path_payload_async(repo_root, path) for path in chunk])
        for path, content in zip(chunk, payloads):
            hasher.update(path.as_posix().encode("utf-8"))
            if content is not None:
                hasher.update(content)
        await asyncio.sleep(0)
    return hasher.hexdigest()


def _state_snapshot_key(ignore_patterns: Iterable[str], mode: str) -> str:
    patterns = tuple(ignore_patterns)
    payload = {
        "mode": mode,
        "ignore_patterns": list(patterns),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _repo_hash_state_path(repo_root: Path) -> Path:
    return repo_root / _REPO_HASH_STATE_FILE


async def _load_repo_hash_state_async(repo_root: Path) -> dict[str, Any]:
    state_path = _repo_hash_state_path(repo_root)
    try:
        state = json.loads(await fs_read_text(state_path, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if isinstance(state, dict):
        return state
    return {}


async def _save_repo_hash_state_async(repo_root: Path, state: dict[str, Any]) -> None:
    state_path = _repo_hash_state_path(repo_root)
    await fs_mkdir(state_path.parent, parents=True, exist_ok=True)
    temp_path = state_path.with_name(f"{state_path.name}.tmp-{uuid.uuid4().hex}")
    payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    await fs_write_text(temp_path, payload, encoding="utf-8")
    await fs_replace(temp_path, state_path)


async def _scan_repo_file_stats_async(
    repo_root: Path,
    ignore_patterns: Iterable[str] | None = None,
) -> list[tuple[Path, int, int]]:
    patterns = DEFAULT_VERSION_IGNORE_PATTERNS if ignore_patterns is None else ignore_patterns
    excluded = set(patterns)

    def _collect_stats() -> list[tuple[Path, int, int]]:
        file_stats: list[tuple[Path, int, int]] = []
        for root, dirs, files in os.walk(repo_root, topdown=True):
            root_path = Path(root)
            dirs[:] = [name for name in dirs if name not in excluded]
            for file_name in files:
                if file_name in excluded:
                    continue
                absolute_path = root_path / file_name
                try:
                    stat_result = absolute_path.stat()
                except FileNotFoundError:
                    continue
                if not absolute_path.is_file():
                    continue
                relative = absolute_path.relative_to(repo_root)
                if relative == _REPO_HASH_STATE_FILE:
                    continue
                if any(part in excluded for part in relative.parts):
                    continue
                file_stats.append((relative, stat_result.st_mtime_ns, stat_result.st_size))
        file_stats.sort(key=lambda item: item[0].as_posix())
        return file_stats

    return await fs_run(_collect_stats)


async def _compute_repo_hash_incremental_async(
    repo_root: Path,
    ignore_patterns: Iterable[str] | None = None,
    mode: str = "workspace",
) -> _RepoHashComputation:
    normalized_patterns = tuple(DEFAULT_VERSION_IGNORE_PATTERNS if ignore_patterns is None else ignore_patterns)
    snapshot_key = _state_snapshot_key(normalized_patterns, mode)
    persisted_state = await _load_repo_hash_state_async(repo_root)
    persisted_files = persisted_state.get("files") if isinstance(persisted_state, dict) else None
    known_files: dict[str, _RepoHashFileState] = {}
    if persisted_state.get("snapshot_key") == snapshot_key and isinstance(persisted_files, dict):
        for key, value in persisted_files.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            mtime_ns = value.get("mtime_ns")
            size = value.get("size")
            content_hash = value.get("content_hash")
            if isinstance(mtime_ns, int) and isinstance(size, int) and isinstance(content_hash, str):
                known_files[key] = _RepoHashFileState(mtime_ns=mtime_ns, size=size, content_hash=content_hash)

    discovered_files = await _scan_repo_file_stats_async(repo_root, ignore_patterns=normalized_patterns)
    updated_files: dict[str, _RepoHashFileState] = {}
    paths_requiring_reread: list[tuple[Path, int, int]] = []

    for relative_path, mtime_ns, size in discovered_files:
        key = relative_path.as_posix()
        existing = known_files.get(key)
        if existing is not None and existing.mtime_ns == mtime_ns and existing.size == size:
            updated_files[key] = existing
            continue
        paths_requiring_reread.append((relative_path, mtime_ns, size))

    if paths_requiring_reread:
        read_semaphore = asyncio.Semaphore(_REPO_HASH_READ_CONCURRENCY)
        worker_count = min(len(paths_requiring_reread), _REPO_HASH_READ_CONCURRENCY)
        queue: asyncio.Queue[tuple[Path, int, int] | None] = asyncio.Queue()
        for reread_item in paths_requiring_reread:
            queue.put_nowait(reread_item)
        for _ in range(worker_count):
            queue.put_nowait(None)
        reread_by_key: dict[str, _RepoHashFileState] = {}

        async def _reread_file_state(
            relative_path: Path,
            mtime_ns: int,
            size: int,
        ) -> tuple[str, _RepoHashFileState]:
            async with read_semaphore:
                content = await _read_path_payload_async(repo_root, relative_path)
            content_hash = hashlib.sha256(content or b"").hexdigest()
            return (
                relative_path.as_posix(),
                _RepoHashFileState(mtime_ns=mtime_ns, size=size, content_hash=content_hash),
            )

        async def _reread_worker() -> None:
            while True:
                entry = await queue.get()
                try:
                    if entry is None:
                        return
                    relative_path, mtime_ns, size = entry
                    key, state = await _reread_file_state(relative_path, mtime_ns, size)
                    reread_by_key[key] = state
                finally:
                    queue.task_done()

        workers = [
            asyncio.create_task(_reread_worker(), name=f"repo-hash-reread-worker-{index}")
            for index in range(worker_count)
        ]
        try:
            await asyncio.gather(*workers)
        except BaseException:
            for worker in workers:
                if not worker.done():
                    worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise

        for relative_path, _, _ in paths_requiring_reread:
            key = relative_path.as_posix()
            updated_files[key] = reread_by_key[key]

    repo_hasher = hashlib.sha256()
    sorted_paths = sorted(updated_files)
    for key in sorted_paths:
        repo_hasher.update(key.encode("utf-8"))
        repo_hasher.update(updated_files[key].content_hash.encode("utf-8"))

    repo_hash = repo_hasher.hexdigest()
    state_payload = {
        "snapshot_key": snapshot_key,
        "files": {
            key: {
                "mtime_ns": value.mtime_ns,
                "size": value.size,
                "content_hash": value.content_hash,
            }
            for key, value in updated_files.items()
        },
        "repo_hash": repo_hash,
    }
    await _save_repo_hash_state_async(repo_root, state_payload)
    return _RepoHashComputation(repo_hash=repo_hash, paths=[Path(path) for path in sorted_paths])


async def _read_path_payload_async(repo_root: Path, path: Path) -> bytes | None:
    source_path = repo_root / path
    try:
        return await fs_read_bytes(source_path)
    except FileNotFoundError:
        return None



def _build_version_id(timestamp: str, candidate_hash: str) -> str:
    normalized = timestamp.replace(":", "").replace("-", "").replace("+", "")
    short_hash = candidate_hash[:8]
    random_suffix = secrets.token_hex(2)
    return f"{normalized}-{short_hash}-{random_suffix}"


async def _resolve_latest_version_id_async(versions_root: Path) -> str | None:
    versions = await _list_version_ids_async(versions_root)
    if not versions:
        return None
    keyed_versions = await asyncio.gather(*[_version_sort_key_async(versions_root / name) for name in versions])
    return max(zip(versions, keyed_versions), key=lambda item: item[1])[0]


async def _list_version_ids_async(versions_root: Path) -> list[str]:
    def _list_version_dirs() -> list[str]:
        versions: list[str] = []
        for entry in versions_root.iterdir():
            if entry.name.startswith(".tmp-"):
                continue
            if entry.is_dir():
                versions.append(entry.name)
        return versions

    try:
        return await fs_run(_list_version_dirs)
    except FileNotFoundError:
        return []


async def _version_sort_key_async(version_path: Path) -> tuple[int, str, str]:
    metadata_path = version_path / "metadata.json"
    if not await fs_exists(metadata_path):
        return (0, "", version_path.name)
    try:
        payload = json.loads(await fs_read_text(metadata_path, encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError):
        return (0, "", version_path.name)
    timestamp = payload.get("timestamp") if isinstance(payload, dict) else ""
    if not isinstance(timestamp, str):
        timestamp = ""
    return (1, timestamp, version_path.name)


def _iter_chunks(items: list[T], chunk_size: int) -> Iterable[list[T]]:
    for start in range(0, len(items), chunk_size):
        yield items[start : start + chunk_size]
