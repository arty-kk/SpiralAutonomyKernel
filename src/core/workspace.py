from __future__ import annotations

import asyncio
import errno
import os
import shutil
import stat
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Iterable

from sif.core.async_fs import copytree as fs_copytree
from sif.core.async_fs import mkdir as fs_mkdir
from sif.core.async_fs import mkdtemp as fs_mkdtemp
from sif.core.async_fs import rmtree as fs_rmtree
from sif.core.async_fs import run_fs as fs_run
from sif.core.async_fs import stat as fs_stat

try:
    import fcntl
except ImportError:  # pragma: no cover - unavailable on some platforms.
    fcntl = None

DEFAULT_IGNORE_PATTERNS = (
    ".sif",
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "data",
)

_FICLONE = 0x40049409


def _copy_file_with_reflink_fallback(source: str, destination: str) -> str:
    if fcntl is not None:
        source_fd = destination_fd = None
        try:
            source_fd = os.open(source, os.O_RDONLY)
            destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            fcntl.ioctl(destination_fd, _FICLONE, source_fd)
            shutil.copystat(source, destination, follow_symlinks=True)
            return destination
        except OSError as exc:
            if exc.errno not in {
                errno.EOPNOTSUPP,
                errno.ENOTTY,
                errno.EPERM,
                errno.EXDEV,
                errno.EINVAL,
                errno.ENOSYS,
            }:
                raise
        finally:
            if source_fd is not None:
                os.close(source_fd)
            if destination_fd is not None:
                os.close(destination_fd)

    return shutil.copy2(source, destination)


async def _clone_tree_async(source: Path, destination: Path) -> Path:
    return await fs_copytree(source, destination, copy_function=_copy_file_with_reflink_fallback)


def _resolve_path_in_root(root: Path, raw_path: str | Path) -> Path | None:
    candidate_path = Path(raw_path)
    resolved_path = candidate_path.resolve() if candidate_path.is_absolute() else (root / candidate_path).resolve()
    try:
        resolved_path.relative_to(root)
    except ValueError:
        return None
    return resolved_path


def _materialize_concurrency_limit() -> int:
    raw_limit = os.getenv("SIF_WORKSPACE_MATERIALIZE_CONCURRENCY", "8")
    try:
        return max(1, int(raw_limit))
    except (TypeError, ValueError):
        return 8


async def _materialize_selective_paths_async(
    seed_workspace_root: Path,
    selective_root: Path,
    candidate_paths: Iterable[str | Path],
) -> None:
    semaphore = asyncio.Semaphore(_materialize_concurrency_limit())

    async def _copy_path(raw_path: str | Path) -> None:
        resolved_source = _resolve_path_in_root(seed_workspace_root, raw_path)
        if resolved_source is None:
            return

        relative_path = resolved_source.relative_to(seed_workspace_root)
        destination = selective_root / relative_path

        async with semaphore:
            try:
                source_stat = await fs_stat(resolved_source)
            except FileNotFoundError:
                return

            await fs_mkdir(destination.parent, parents=True, exist_ok=True)

            if stat.S_ISDIR(source_stat.st_mode):
                await fs_copytree(
                    resolved_source,
                    destination,
                    copy_function=_copy_file_with_reflink_fallback,
                    dirs_exist_ok=True,
                )
            else:
                await fs_run(_copy_file_with_reflink_fallback, str(resolved_source), str(destination))

    tasks = [asyncio.create_task(_copy_path(raw_path)) for raw_path in candidate_paths]
    if not tasks:
        return

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    except Exception:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


@asynccontextmanager
async def WorkspaceAsync(root: Path, ignore_patterns: Iterable[str] | None = None) -> AsyncIterator[Path]:
    async with create_seed_workspace_async(root, ignore_patterns=ignore_patterns) as workspace_root:
        yield workspace_root


@asynccontextmanager
async def create_seed_workspace_async(
    repo_root: Path,
    ignore_patterns: Iterable[str] | None = None,
) -> AsyncIterator[Path]:
    root = repo_root.resolve()
    patterns = DEFAULT_IGNORE_PATTERNS if ignore_patterns is None else ignore_patterns
    temp_dir = await fs_mkdtemp()
    temp_root = Path(temp_dir)
    workspace_root = temp_root / root.name

    try:
        await fs_copytree(
            root,
            workspace_root,
            ignore=shutil.ignore_patterns(*patterns),
        )
        yield workspace_root
    finally:
        try:
            await asyncio.shield(fs_rmtree(temp_root, True))
        except Exception:
            pass


@asynccontextmanager
async def create_overlay_workspace_async(seed_workspace_root: Path) -> AsyncIterator[Path]:
    temp_dir = await fs_mkdtemp()
    temp_root = Path(temp_dir)
    overlay_root = temp_root / seed_workspace_root.name
    try:
        await _clone_tree_async(seed_workspace_root, overlay_root)
        yield overlay_root
    finally:
        try:
            await asyncio.shield(fs_rmtree(temp_root, True))
        except Exception:
            pass


@asynccontextmanager
async def create_selective_workspace_async(
    seed_workspace_root: Path,
    candidate_paths: Iterable[str | Path],
) -> AsyncIterator[Path]:
    temp_dir = await fs_mkdtemp()
    temp_root = Path(temp_dir)
    selective_root = temp_root / seed_workspace_root.name
    try:
        await fs_mkdir(selective_root, parents=True, exist_ok=True)
        await _materialize_selective_paths_async(seed_workspace_root, selective_root, candidate_paths)
        yield selective_root
    finally:
        try:
            await asyncio.shield(fs_rmtree(temp_root, True))
        except Exception:
            pass


@asynccontextmanager
async def create_workspace_async(
    repo_root: Path,
    ignore_patterns: Iterable[str] | None = None,
) -> AsyncIterator[Path]:
    async with create_seed_workspace_async(repo_root, ignore_patterns=ignore_patterns) as seed_workspace_root:
        async with create_overlay_workspace_async(seed_workspace_root) as workspace_root:
            yield workspace_root
