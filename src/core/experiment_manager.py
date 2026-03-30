from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Tuple

from sif.core.cache_store import AsyncCacheStore
from sif.core.candidates import Candidate
from sif.core.evaluator import evaluate_async
from sif.core.evolution import CodeChange, REPO_ROOT, apply_code_changes_to_root_async
from sif.core.events import append_event
from sif.core.selector import should_accept
from sif.core.versioning import get_repo_hash_async
from sif.core.workspace import create_seed_workspace_async, create_selective_workspace_async


def _baseline_hash(baseline_metrics: Dict[str, Any] | None) -> str:
    payload = "__none__"
    if baseline_metrics is not None:
        payload = json.dumps(baseline_metrics, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _evaluator_id(evaluator: Callable[[Path], Awaitable[Dict[str, Any]]]) -> str:
    module = getattr(evaluator, "__module__", None)
    qualname = getattr(evaluator, "__qualname__", None)
    if module and qualname:
        return f"{module}.{qualname}"
    evaluator_type = type(evaluator)
    module = getattr(evaluator_type, "__module__", None)
    qualname = getattr(evaluator_type, "__qualname__", None)
    if module and qualname:
        return f"{module}.{qualname}"
    return repr(evaluator)


def _code_changes_hash(code_changes: List[CodeChange]) -> str:
    payload = [
        {
            "path": getattr(change, "path", ""),
            "content": getattr(change, "content", ""),
            "notes": getattr(change, "notes", ""),
        }
        for change in sorted(code_changes, key=lambda item: getattr(item, "path", ""))
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()




def _normalize_code_changes_paths(code_changes: List[CodeChange], repo_root: Path) -> list[CodeChange]:
    resolved_repo_root = repo_root.resolve()
    normalized: list[CodeChange] = []
    for change in code_changes:
        path = getattr(change, "path", None)
        if not isinstance(path, str) or not path:
            normalized.append(change)
            continue
        candidate_path = Path(path)
        if not candidate_path.is_absolute():
            normalized.append(change)
            continue
        resolved_candidate = candidate_path.resolve()
        try:
            relative_candidate = resolved_candidate.relative_to(resolved_repo_root)
        except ValueError:
            normalized.append(change)
            continue
        normalized.append(CodeChange(path=str(relative_candidate), content=change.content, notes=change.notes))
    return normalized



_DEFAULT_EVALUATOR_REQUIRED_PATHS = ("src", "tests")


def _materialization_paths_for_candidate(
    code_changes: List[CodeChange],
    repo_root: Path,
    evaluator: Callable[[Path], Awaitable[Dict[str, Any]]],
) -> list[str]:
    candidate_paths = _candidate_paths(code_changes, repo_root)
    required_paths: list[str] = []
    if _evaluator_id(evaluator) == _evaluator_id(evaluate_async):
        required_paths.extend(_DEFAULT_EVALUATOR_REQUIRED_PATHS)
    seen: set[str] = set()
    ordered: list[str] = []
    for path in [*required_paths, *candidate_paths]:
        if path in seen:
            continue
        seen.add(path)
        ordered.append(path)
    return ordered

def _candidate_paths(code_changes: List[CodeChange], repo_root: Path) -> list[str]:
    resolved_repo_root = repo_root.resolve()
    candidate_paths: list[str] = []
    for change in code_changes:
        path = getattr(change, "path", None)
        if not isinstance(path, str) or not path:
            continue
        candidate_path = Path(path)
        if candidate_path.is_absolute():
            resolved_candidate = candidate_path.resolve()
            try:
                relative_candidate = resolved_candidate.relative_to(resolved_repo_root)
            except ValueError:
                continue
            candidate_paths.append(str(relative_candidate))
            continue
        candidate_paths.append(path)
    return candidate_paths


@dataclass
class ExperimentManager:
    repo_root: Path = REPO_ROOT
    evaluator: Callable[[Path], Awaitable[Dict[str, Any]]] = evaluate_async
    cache_path: Path | None = None
    max_candidates: int = 5
    timeout_per_candidate: float = 120.0
    max_parallel_evaluations: int = 2
    _cache_store: AsyncCacheStore | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.cache_path is None:
            self.cache_path = self.repo_root / ".sif" / "cache" / "evals.json"
        self._validate_evaluator()

    def _validate_evaluator(self) -> None:
        evaluator = self.evaluator
        is_async_callable = inspect.iscoroutinefunction(evaluator)
        if not is_async_callable and callable(evaluator):
            is_async_callable = inspect.iscoroutinefunction(getattr(evaluator, "__call__", None))
        if not is_async_callable:
            raise TypeError(
                "ExperimentManager evaluator must be an async callable and return a metrics dict."
            )

    async def run_async(
        self,
        candidates: List[Candidate],
        baseline_metrics: Dict[str, Any] | None = None,
    ) -> Tuple[Candidate | None, Dict[str, Any]]:
        cache_store = AsyncCacheStore(self.cache_path)
        results: Dict[str, Any] = {}
        try:
            await cache_store.start()
            self._cache_store = cache_store
            id_counts: Dict[str, int] = {}
            for candidate in candidates:
                id_counts[candidate.id] = id_counts.get(candidate.id, 0) + 1
            duplicate_ids = sorted(candidate_id for candidate_id, count in id_counts.items() if count > 1)
            if duplicate_ids:
                raise ValueError(f"Duplicate candidate ids are not allowed: {', '.join(duplicate_ids)}")

            baseline_hash = _baseline_hash(baseline_metrics)
            evaluator_id = _evaluator_id(self.evaluator)
            repo_hash = await get_repo_hash_async(self.repo_root)
            truncated_candidates = candidates[: self.max_candidates]
            semaphore = asyncio.Semaphore(max(1, self.max_parallel_evaluations))

            async def _evaluate_candidate(
                candidate: Candidate,
                seed_workspace_root: Path,
            ) -> tuple[str, Dict[str, Any], str | None, Dict[str, Any] | None]:
                normalized_code_changes = _normalize_code_changes_paths(candidate.code_changes, self.repo_root)
                code_hash = _code_changes_hash(normalized_code_changes)
                cache_key = f"{candidate.id}:{code_hash}"
                cached = await cache_store.get(cache_key)
                if isinstance(cached, dict):
                    metrics = cached.get("metrics")
                    if (
                        isinstance(metrics, dict)
                        and cached.get("baseline_hash") == baseline_hash
                        and cached.get("evaluator_id") == evaluator_id
                        and cached.get("repo_hash") == repo_hash
                        and cached.get("code_hash") == code_hash
                    ):
                        accepted, reason = should_accept(metrics, baseline_metrics)
                        return candidate.id, {
                            "metrics": metrics,
                            "accepted": accepted,
                            "reason": reason,
                            "cached": True,
                        }, None, None

                async with semaphore:
                    candidate_paths = _materialization_paths_for_candidate(
                        normalized_code_changes, self.repo_root, self.evaluator
                    )
                    async with create_selective_workspace_async(seed_workspace_root, candidate_paths) as workspace_root:
                        application_result = await apply_code_changes_to_root_async(workspace_root, normalized_code_changes)
                        if application_result.blocked_changes:
                            blocked_metadata = [
                                {
                                    "path": blocked.path,
                                    "requested_path": blocked.requested_path,
                                    "reason": blocked.reason,
                                }
                                for blocked in application_result.blocked_changes
                            ]
                            await append_event(
                                "candidate_skipped",
                                {
                                    "candidate_id": candidate.id,
                                    "reason": "partial_application_blocked",
                                    "blocked_changes": blocked_metadata,
                                },
                            )
                            return candidate.id, {
                                "metrics": {"duration_sec": 0.0, "timed_out": False},
                                "accepted": False,
                                "reason": "partial_application_blocked",
                                "cached": False,
                                "blocked_changes": blocked_metadata,
                            }, None, None
                        if not application_result.applied_changes:
                            await append_event("candidate_skipped", {"candidate_id": candidate.id, "reason": "no_changes_applied"})
                            return candidate.id, {
                                "metrics": {"duration_sec": 0.0, "timed_out": False},
                                "accepted": False,
                                "reason": "no_changes_applied",
                                "cached": False,
                            }, None, None

                        started = time.monotonic()
                        try:
                            payload = await asyncio.wait_for(
                                self.evaluator(workspace_root),
                                timeout=self.timeout_per_candidate,
                            )
                            elapsed = time.monotonic() - started
                            if not isinstance(payload, dict):
                                metrics = {"duration_sec": elapsed, "timed_out": False, "tests_success": False}
                                return candidate.id, {
                                    "metrics": metrics,
                                    "accepted": False,
                                    "reason": "evaluation_failed",
                                    "cached": False,
                                    "error": "Evaluator returned non-dict metrics.",
                                }, None, None
                            payload.setdefault("duration_sec", elapsed)
                            payload.setdefault("timed_out", False)
                            accepted, reason = should_accept(payload, baseline_metrics)
                            return candidate.id, {
                                "metrics": payload,
                                "accepted": accepted,
                                "reason": reason,
                                "cached": False,
                            }, cache_key, {
                                "metrics": payload,
                                "baseline_hash": baseline_hash,
                                "evaluator_id": evaluator_id,
                                "repo_hash": repo_hash,
                                "code_hash": code_hash,
                                "timestamp": time.time(),
                            }
                        except asyncio.TimeoutError:
                            elapsed = time.monotonic() - started
                            metrics = {
                                "duration_sec": elapsed,
                                "tests_success": False,
                                "timed_out": True,
                                "reason": "timeout",
                            }
                            return candidate.id, {
                                "metrics": metrics,
                                "accepted": False,
                                "reason": "timeout",
                                "cached": False,
                                "timed_out": True,
                                "error": "evaluation timed out",
                            }, None, None
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            elapsed = time.monotonic() - started
                            metrics = {"duration_sec": elapsed, "tests_success": False, "timed_out": False}
                            return candidate.id, {
                                "metrics": metrics,
                                "accepted": False,
                                "reason": "evaluation_failed",
                                "cached": False,
                                "error": str(exc),
                            }, None, None

            async with create_seed_workspace_async(self.repo_root) as seed_workspace_root:
                tasks = [asyncio.create_task(_evaluate_candidate(candidate, seed_workspace_root)) for candidate in truncated_candidates]
                try:
                    task_results = await asyncio.gather(*tasks)
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

                cache_updates: Dict[str, Any] = {}
                for candidate_id, result_payload, cache_key, cache_entry in task_results:
                    results[candidate_id] = result_payload
                    if cache_key and cache_entry:
                        cache_updates[cache_key] = cache_entry

                if cache_updates:
                    await cache_store.put_many(cache_updates)

                best_candidate = self._select_best(candidates, results)
                return best_candidate, results
        finally:
            try:
                await cache_store.stop()
            finally:
                if self._cache_store is cache_store:
                    self._cache_store = None

    @staticmethod
    def _select_best(candidates: List[Candidate], results: Dict[str, Any]) -> Candidate | None:
        best: Candidate | None = None
        best_score = float("-inf")
        for candidate in candidates:
            result = results.get(candidate.id, {})
            if not isinstance(result, dict) or not result.get("accepted"):
                continue
            metrics = result.get("metrics", {})
            duration = metrics.get("duration_sec")
            score = -float(duration) if isinstance(duration, (int, float)) else 0.0
            if score > best_score:
                best_score = score
                best = candidate
        return best
