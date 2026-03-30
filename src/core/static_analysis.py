# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import ast
import asyncio
from sif.core.code_intelligence import CodeIntelligence

from sif.core import async_cpu, async_fs
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List

from sif.core import policy


@dataclass
class StaticAnalysisReport:
    repo_root: str
    src_file_count: int
    test_file_count: int
    component_count: int
    generated_component_count: int
    core_module_count: int
    symbol_counts: Dict[str, int]
    entrypoints: List[str]
    risks: List[str]
    notes: List[str]
    summary: str


def _extract_imports_from_source(source: str) -> List[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    imports: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name:
                    imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
    return imports


async def build_python_repository_snapshot_async(
    repo_root: Path,
    *,
    ignore_paths: Iterable[str] | None = None,
) -> Dict[str, Any]:
    """Build repository snapshot metadata using asynchronous filesystem operations.

    Cancellation contract: on ``asyncio.CancelledError`` all worker tasks are
    cancelled and awaited before propagating cancellation.
    """

    ignored = {
        repo_root / path
        for path in (ignore_paths or [])
        if isinstance(path, str)
    }
    files: Dict[str, Dict[str, int]] = {}
    src_files: List[str] = []
    test_files: List[str] = []
    component_files: List[str] = []
    generated_files: List[str] = []
    core_files: List[str] = []
    self_map_files: List[str] = []

    paths = await async_fs.rglob(repo_root, "*.py")
    queue: asyncio.Queue[Path | None] = asyncio.Queue()
    for path in paths:
        if any(path == ignore or ignore in path.parents for ignore in ignored):
            continue
        queue.put_nowait(path)

    worker_count = min(len(paths), 32) if paths else 0
    for _ in range(worker_count):
        queue.put_nowait(None)

    async def _worker() -> None:
        while True:
            path = await queue.get()
            try:
                if path is None:
                    return
                relative = str(path.relative_to(repo_root))
                try:
                    stat = await async_fs.run_fs(path.stat)
                except OSError:
                    continue
                files[relative] = {
                    "mtime_ns": int(stat.st_mtime_ns),
                    "size": int(stat.st_size),
                }

                if relative.startswith("src/"):
                    src_files.append(relative)
                if relative.startswith("tests/"):
                    test_files.append(relative)
                if relative.startswith("src/components/") and path.name != "__init__.py":
                    component_files.append(relative)
                if relative.startswith("src/components/generated/") and path.name != "__init__.py":
                    generated_files.append(relative)
                if relative.startswith("src/core/") and path.name != "__init__.py":
                    core_files.append(relative)
                if (
                    relative.startswith("src/core/")
                    or relative.startswith("src/components/")
                    or relative.startswith("src/evolvable/")
                ):
                    self_map_files.append(relative)
            finally:
                queue.task_done()

    workers = [
        asyncio.create_task(_worker(), name=f"snapshot-stat-worker-{index}")
        for index in range(worker_count)
    ]
    try:
        if workers:
            await queue.join()
            await asyncio.gather(*workers)
    except asyncio.CancelledError:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        raise

    return {
        "repo_root": str(repo_root),
        "files": files,
        "src_files": sorted(src_files),
        "test_files": sorted(test_files),
        "component_files": sorted(component_files),
        "generated_files": sorted(generated_files),
        "core_files": sorted(core_files),
        "self_map_files": sorted(self_map_files),
        "ignore_paths": sorted(str(path.relative_to(repo_root)) for path in ignored),
    }


def calculate_changed_python_files(
    previous_snapshot: Dict[str, Any] | None,
    current_snapshot: Dict[str, Any],
) -> set[str]:
    prev_files = previous_snapshot.get("files", {}) if isinstance(previous_snapshot, dict) else {}
    curr_files = current_snapshot.get("files", {}) if isinstance(current_snapshot, dict) else {}
    prev = prev_files if isinstance(prev_files, dict) else {}
    curr = curr_files if isinstance(curr_files, dict) else {}

    changed: set[str] = set()
    all_paths = set(prev) | set(curr)
    for path in all_paths:
        if path not in prev or path not in curr:
            changed.add(path)
            continue
        if prev[path] != curr[path]:
            changed.add(path)
    return changed


async def _extract_imports_async(
    repo_root: Path,
    relative_paths: List[str],
    *,
    io_max_concurrency: int,
    ast_max_workers: int,
) -> Dict[str, List[str]]:
    imports_by_file: Dict[str, List[str]] = {}
    if not relative_paths:
        return imports_by_file

    io_semaphore = asyncio.Semaphore(max(1, int(io_max_concurrency)))
    worker_count = min(
        len(relative_paths),
        max(1, int(ast_max_workers)),
        max(1, int(io_max_concurrency)),
    )
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    for relative_path in relative_paths:
        queue.put_nowait(relative_path)
    for _ in range(worker_count):
        queue.put_nowait(None)

    async def _worker() -> None:
        while True:
            relative_path = await queue.get()
            try:
                if relative_path is None:
                    return
                try:
                    async with io_semaphore:
                        source = await async_fs.read_text(repo_root / relative_path, encoding="utf-8")
                except OSError:
                    imports_by_file[relative_path] = []
                    continue
                imports = await async_cpu.run_cpu(_extract_imports_from_source, source)
                imports_by_file[relative_path] = sorted(set(imports))
            finally:
                queue.task_done()

    workers = [
        asyncio.create_task(_worker(), name=f"self-map-import-worker-{index}")
        for index in range(worker_count)
    ]
    try:
        await queue.join()
        await asyncio.gather(*workers)
    except asyncio.CancelledError:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        raise
    return imports_by_file

async def build_self_map_from_snapshot_async(
    repo_root: Path,
    snapshot: Dict[str, Any],
    *,
    config: Dict[str, Any] | None = None,
    previous_self_map: Dict[str, Any] | None = None,
    changed_files: set[str] | None = None,
    io_max_concurrency: int = 16,
    ast_max_workers: int = 4,
) -> Dict[str, Any]:
    """Build self-map incrementally with async I/O and bounded AST parsing.

    Cancellation contract: on ``asyncio.CancelledError`` all workers are cancelled,
    awaited, and the cancellation is propagated to the caller.
    """
    _ = config  # snapshot already respects ignore paths
    tracked_files = list(snapshot.get("self_map_files", [])) if isinstance(snapshot, dict) else []

    files: Dict[str, Dict[str, Any]] = {}
    if isinstance(previous_self_map, dict):
        previous_files = previous_self_map.get("files")
        if isinstance(previous_files, dict):
            for path, payload in previous_files.items():
                if path in tracked_files and isinstance(payload, dict):
                    files[path] = dict(payload)

    changed = set(changed_files or set())
    targets = list(tracked_files) if changed_files is None else [path for path in tracked_files if path in changed]
    imports_by_file: Dict[str, List[str]] = {}
    if targets:
        imports_by_file = await _extract_imports_async(
            repo_root,
            targets,
            io_max_concurrency=io_max_concurrency,
            ast_max_workers=ast_max_workers,
        )

    for relative in targets:
        if relative.startswith("src/core/"):
            category = "core"
        elif relative.startswith("src/components/"):
            category = "components"
        elif relative.startswith("src/evolvable/"):
            category = "evolvable"
        else:
            continue
        files[relative] = {
            "category": category,
            "imports": imports_by_file.get(relative, []),
            "allowed": policy.is_path_allowed(Path(relative)),
        }

    for removed_path in changed:
        if removed_path not in tracked_files:
            files.pop(removed_path, None)

    immutable_paths = [str(path) for path in policy.IMMUTABLE_PATHS]
    evolvable_paths = [str(path) for path in policy.EVOLVABLE_PATHS]
    return {
        "repo_root": str(repo_root),
        "file_count": len(files),
        "files": files,
        "immutable_paths": immutable_paths,
        "evolvable_paths": evolvable_paths,
        "summary": f"Self-map includes {len(files)} tracked python files.",
    }


def _analyze_repository_from_snapshot_payload(
    repo_root: Path,
    snapshot: Dict[str, Any],
    *,
    config: Dict[str, Any] | None = None,
    code_index_payload: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    config = config or {}

    notes = [
        "Static analysis uses AST symbol counts for classes/functions.",
        "Component inventory includes generated and first-class modules.",
        "Thresholds and entrypoints can be customized via static_analysis_config.",
    ]
    risks: List[str] = []

    def _coerce_int(
        value: Any,
        default: int,
        min_value: int,
        field_name: str,
        notes: List[str],
        risks: List[str],
    ) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            notes.append(
                f"Invalid static_analysis_config.{field_name}={value!r}; using fallback {default}."
            )
            risks.append(
                f"static_analysis_config.{field_name} is invalid; threshold fallback may reduce signal reliability."
            )
            parsed = default

        if parsed < min_value:
            notes.append(
                f"static_analysis_config.{field_name}={parsed} is below minimum {min_value}; clamped to {min_value}."
            )
            parsed = min_value
        return parsed

    src_files = list(snapshot.get("src_files", []))
    test_files = list(snapshot.get("test_files", []))
    component_files = list(snapshot.get("component_files", []))
    generated_files = list(snapshot.get("generated_files", []))
    core_files = list(snapshot.get("core_files", []))

    symbol_counts = {"classes": 0, "functions": 0}
    if src_files and not config.get("skip_symbol_index"):
        if isinstance(code_index_payload, dict):
            index_files = code_index_payload.get("files", {})
            if isinstance(index_files, dict):
                for symbols in index_files.values():
                    if not isinstance(symbols, list):
                        continue
                    for symbol in symbols:
                        if not isinstance(symbol, dict):
                            continue
                        kind = symbol.get("kind")
                        if kind == "class":
                            symbol_counts["classes"] += 1
                        elif kind == "function":
                            symbol_counts["functions"] += 1
        else:
            raise ValueError(
                "code_index_payload is required when static analysis symbol index is enabled"
            )

    entrypoints: List[str] = [
        entrypoint
        for entrypoint in config.get("entrypoints", [])
        if isinstance(entrypoint, str)
    ]
    if not entrypoints:
        if "src/cli.py" in src_files:
            entrypoints.append("cli.py")
        if "src/__init__.py" in src_files:
            entrypoints.append("package_init")

    min_test_files = _coerce_int(
        config.get("min_test_files", 1), 1, 0, "min_test_files", notes, risks
    )
    min_generated_components = _coerce_int(
        config.get("min_generated_components", 1),
        1,
        0,
        "min_generated_components",
        notes,
        risks,
    )
    min_core_modules = _coerce_int(
        config.get("min_core_modules", 1), 1, 0, "min_core_modules", notes, risks
    )
    if len(test_files) < min_test_files:
        risks.append("No automated tests detected; autonomy safety net is thin.")
    if len(generated_files) < min_generated_components:
        risks.append("No generated components detected; self-evolution path is narrow.")
    if len(core_files) < min_core_modules:
        risks.append("Core modules missing; logic orchestration is undefined.")

    summary = (
        f"Static analysis: {len(src_files)} src files, {len(test_files)} tests, "
        f"{len(component_files)} components, {len(generated_files)} generated components."
    )

    report = StaticAnalysisReport(
        repo_root=str(repo_root),
        src_file_count=len(src_files),
        test_file_count=len(test_files),
        component_count=len(component_files),
        generated_component_count=len(generated_files),
        core_module_count=len(core_files),
        symbol_counts=symbol_counts,
        entrypoints=entrypoints,
        risks=risks,
        notes=notes,
        summary=summary,
    )
    payload = report.__dict__
    payload["config_applied"] = {
        "min_test_files": min_test_files,
        "min_generated_components": min_generated_components,
        "min_core_modules": min_core_modules,
        "ignore_paths": list(snapshot.get("ignore_paths", [])),
        "skip_symbol_index": bool(config.get("skip_symbol_index", False)),
        "entrypoints": entrypoints,
    }
    return payload


async def _build_code_index_payload_async(
    repo_root: Path,
    snapshot: Dict[str, Any],
    *,
    config: Dict[str, Any],
) -> Dict[str, Any] | None:
    if not snapshot.get("src_files") or config.get("skip_symbol_index"):
        return None

    src_root = repo_root / "src"
    src_files = [
        path.removeprefix("src/")
        for path in snapshot.get("src_files", [])
        if isinstance(path, str) and path.startswith("src/")
    ]
    index = await CodeIntelligence(src_root).build_index_incremental_async(snapshot_files=src_files)
    return _serialize_code_index(index)


def _serialize_code_index(index: Any) -> Dict[str, Any]:
    return {
        "root": index.root,
        "file_count": len(index.files),
        "files": {
            name: [
                {"name": symbol.name, "kind": symbol.kind, "line": symbol.line}
                for symbol in symbols
            ]
            for name, symbols in index.files.items()
        },
    }


async def analyze_repository_from_snapshot_async(
    repo_root: Path,
    snapshot: Dict[str, Any],
    *,
    config: Dict[str, Any] | None = None,
    code_index_payload: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Analyze a repository snapshot with async code-index orchestration.

    Cancellation contract: if this coroutine is cancelled while building the
    code index, cancellation is propagated after worker shutdown handled by
    ``CodeIntelligence.build_index_incremental_async``.
    """

    resolved_config = config or {}
    if code_index_payload is None:
        code_index_payload = await _build_code_index_payload_async(
            repo_root,
            snapshot,
            config=resolved_config,
        )
    return _analyze_repository_from_snapshot_payload(
        repo_root,
        snapshot,
        config=resolved_config,
        code_index_payload=code_index_payload,
    )


async def analyze_repository_async(
    repo_root: Path,
    config: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Run static analysis using asynchronous snapshot and symbol indexing stages."""

    resolved_config = config or {}
    snapshot = await build_python_repository_snapshot_async(
        repo_root,
        ignore_paths=resolved_config.get("ignore_paths", []),
    )
    return await analyze_repository_from_snapshot_async(
        repo_root,
        snapshot,
        config=resolved_config,
    )
