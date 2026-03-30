# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass
import ast
import asyncio
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List

from sif.core import async_cpu
from sif.core.async_fs import read_text


@dataclass
class CodeSymbol:
    name: str
    kind: str
    line: int


@dataclass
class CodeIndex:
    root: str
    files: Dict[str, List[CodeSymbol]]


def _parse_source_to_symbols(source: str) -> List[CodeSymbol]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    symbols: List[CodeSymbol] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            symbols.append(CodeSymbol(name=node.name, kind="class", line=node.lineno))
        elif isinstance(node, ast.FunctionDef):
            symbols.append(CodeSymbol(name=node.name, kind="function", line=node.lineno))
    return symbols


class CodeIntelligence:
    def __init__(self, root: Path) -> None:
        self.root = root

    async def build_index_incremental_async(
        self,
        snapshot_files: Iterable[str],
        *,
        changed_files: set[str] | None = None,
        previous_index: CodeIndex | Dict[str, Any] | None = None,
        ast_max_workers: int = 4,
        ast_batch_size: int = 64,
        io_max_concurrency: int | None = None,
        parse_max_concurrency: int | None = None,
    ) -> CodeIndex:
        """Build incremental code index without blocking the event loop.

        Cancellation contract: if cancelled, this coroutine cancels all worker tasks,
        waits for their completion, and re-raises ``asyncio.CancelledError``.
        """

        snapshot_set = {path for path in snapshot_files}
        previous_files = self._coerce_previous_files(previous_index)

        files: Dict[str, List[CodeSymbol]] = {
            path: symbols
            for path, symbols in previous_files.items()
            if path in snapshot_set
        }

        paths_to_reindex = sorted(snapshot_set)
        if changed_files is not None:
            paths_to_reindex = sorted(path for path in changed_files if path in snapshot_set)
        if not paths_to_reindex:
            return CodeIndex(root=str(self.root), files=files)

        if io_max_concurrency is not None or parse_max_concurrency is not None:
            parse_limit = parse_max_concurrency
            if parse_limit is None:
                parse_limit = max(1, int(ast_max_workers))
            indexed = await self._index_files_async(
                paths_to_reindex,
                io_max_concurrency=io_max_concurrency,
                parse_max_concurrency=parse_limit,
            )
            files.update(indexed)
            return CodeIndex(root=str(self.root), files=files)

        worker_count = min(
            len(paths_to_reindex),
            max(1, int(ast_max_workers)),
            max(1, int(ast_batch_size)),
        )
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        for relative_path in paths_to_reindex:
            queue.put_nowait(relative_path)
        for _ in range(worker_count):
            queue.put_nowait(None)

        indexed: Dict[str, List[CodeSymbol]] = {}

        async def _worker() -> None:
            while True:
                relative_path = await queue.get()
                try:
                    if relative_path is None:
                        return
                    indexed_path, symbols = await self._index_file_by_relative_path_async(
                        relative_path,
                    )
                    indexed[indexed_path] = symbols
                finally:
                    queue.task_done()

        workers = [
            asyncio.create_task(_worker(), name=f"code-index-worker-{index}")
            for index in range(worker_count)
        ]
        try:
            await asyncio.gather(*workers)
        except asyncio.CancelledError:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise

        files.update(indexed)
        return CodeIndex(root=str(self.root), files=files)

    async def _index_file_by_relative_path_async(
        self,
        relative_path: str,
    ) -> tuple[str, List[CodeSymbol]]:
        path = self.root / relative_path
        try:
            source = await read_text(path, encoding="utf-8")
        except OSError:
            return relative_path, []
        symbols = await async_cpu.run_cpu(_parse_source_to_symbols, source)
        return relative_path, symbols

    async def _index_files_async(
        self,
        relative_paths: List[str],
        *,
        io_max_concurrency: int | None,
        parse_max_concurrency: int,
    ) -> Dict[str, List[CodeSymbol]]:
        if not relative_paths:
            return {}

        io_limit = io_max_concurrency
        if io_limit is None:
            io_limit = max(1, int(os.getenv("SIF_CODE_INDEX_IO_CONCURRENCY", "16")))
        io_semaphore = asyncio.Semaphore(max(1, int(io_limit)))
        parse_limit = max(1, int(parse_max_concurrency))
        parse_semaphore = asyncio.Semaphore(parse_limit)
        worker_count = min(len(relative_paths), max(1, int(io_limit)), parse_limit)
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        for relative_path in relative_paths:
            queue.put_nowait(relative_path)
        for _ in range(worker_count):
            queue.put_nowait(None)

        indexed: Dict[str, List[CodeSymbol]] = {}

        async def index_one(relative_path: str) -> tuple[str, List[CodeSymbol]]:
            path = self.root / relative_path
            try:
                async with io_semaphore:
                    source = await read_text(path, encoding="utf-8")
            except OSError:
                return relative_path, []

            async with parse_semaphore:
                symbols = await async_cpu.run_cpu(_parse_source_to_symbols, source)
            return relative_path, symbols

        async def _worker() -> None:
            while True:
                relative_path = await queue.get()
                try:
                    if relative_path is None:
                        return
                    indexed_path, symbols = await index_one(relative_path)
                    indexed[indexed_path] = symbols
                finally:
                    queue.task_done()

        workers = [
            asyncio.create_task(_worker(), name=f"code-index-async-worker-{index}")
            for index in range(worker_count)
        ]
        try:
            await asyncio.gather(*workers)
        except asyncio.CancelledError:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise
        except BaseException:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise

        return {path: indexed[path] for path in relative_paths}

    @staticmethod
    def _coerce_previous_files(previous_index: CodeIndex | Dict[str, Any] | None) -> Dict[str, List[CodeSymbol]]:
        if isinstance(previous_index, CodeIndex):
            return dict(previous_index.files)
        if not isinstance(previous_index, dict):
            return {}
        raw_files = previous_index.get("files")
        if not isinstance(raw_files, dict):
            return {}
        files: Dict[str, List[CodeSymbol]] = {}
        for path, raw_symbols in raw_files.items():
            if not isinstance(path, str) or not isinstance(raw_symbols, list):
                continue
            symbols: List[CodeSymbol] = []
            for raw_symbol in raw_symbols:
                if not isinstance(raw_symbol, dict):
                    continue
                name = raw_symbol.get("name")
                kind = raw_symbol.get("kind")
                line = raw_symbol.get("line")
                if isinstance(name, str) and isinstance(kind, str) and isinstance(line, int):
                    symbols.append(CodeSymbol(name=name, kind=kind, line=line))
            files[path] = symbols
        return files

    _parse_source_to_symbols = staticmethod(_parse_source_to_symbols)
