# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass
import json
from pathlib import Path
from typing import List

from sif.core.async_fs import exists as async_exists
from sif.core.async_fs import mkdir as async_mkdir
from sif.core.async_fs import read_text as async_read_text
from sif.core.async_fs import unlink as async_unlink
from sif.core.async_fs import write_text as async_write_text
from sif.core import policy
from sif.core.kernel import Kernel

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class KernelUpdate:
    action: str
    target: str
    value: str
    notes: str = ""


@dataclass
class CodeChange:
    path: str
    content: str
    notes: str = ""


@dataclass
class BlockedCodeChange:
    path: str
    reason: str
    requested_path: str


@dataclass
class CodeApplicationResult:
    applied_changes: List[CodeChange]
    blocked_changes: List[BlockedCodeChange]
    out_of_policy: bool = False
    outside_root: bool = False
    no_op: bool = False


class CodeApplicationError(Exception):
    """Raised when an atomic code-application batch fails and is rolled back."""


def apply_kernel_updates(kernel: Kernel, updates: List[KernelUpdate]) -> List[KernelUpdate]:
    applied: List[KernelUpdate] = []
    for update in updates:
        if policy.violates_invariants(update.action, update.target, update.value):
            kernel.update_memory(
                "invariant_violation",
                f"blocked:{update.target}",
            )
            continue
        if update.action == "add_goal":
            if update.value not in kernel.state.goals:
                kernel.state.goals.append(update.value)
                applied.append(update)
        elif update.action == "remove_goal":
            if update.value in kernel.state.goals:
                kernel.state.goals.remove(update.value)
                applied.append(update)
        elif update.action == "add_constraint":
            if update.value not in kernel.state.constraints:
                kernel.state.constraints.append(update.value)
                applied.append(update)
        elif update.action == "remove_constraint":
            if update.value in kernel.state.constraints and policy.can_remove_constraint(update.value):
                kernel.state.constraints.remove(update.value)
                applied.append(update)
            elif update.value in kernel.state.constraints:
                kernel.update_memory(
                    "constraint_removal_blocked",
                    f"blocked:{update.value}",
                )
        elif update.action == "update_memory":
            kernel.update_memory(update.target, update.value)
            applied.append(update)
    return applied


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        return path.is_relative_to(other)
    except AttributeError:
        try:
            path.relative_to(other)
            return True
        except ValueError:
            return False


async def apply_code_changes_to_root_async(
    root: Path,
    changes: List[CodeChange],
    kernel: Kernel | None = None,
) -> CodeApplicationResult:
    applied: List[CodeChange] = []
    blocked_by_policy = False
    blocked_changes: List[BlockedCodeChange] = []
    blocked_outside_root = False
    eligible_changes: List[tuple[CodeChange, Path]] = []
    write_plan: List[tuple[CodeChange, Path]] = []
    backups_by_path: dict[str, dict] = {}
    resolved_root = root.resolve()

    # Preflight phase (no mutations): resolve + policy checks + backup metadata.
    for change in changes:
        target = Path(change.path)
        if target.is_absolute():
            resolved_target = target.resolve()
        else:
            resolved_target = (root / target).resolve()
        if not _is_relative_to(resolved_target, resolved_root):
            if _is_relative_to(resolved_target, REPO_ROOT) and resolved_root != REPO_ROOT:
                relative_target = resolved_target.relative_to(REPO_ROOT)
                resolved_target = (root / relative_target).resolve()
            else:
                blocked_by_policy = True
                blocked_outside_root = True
                blocked_changes.append(
                    BlockedCodeChange(
                        path=str(resolved_target),
                        requested_path=change.path,
                        reason="outside_root",
                    )
                )
                continue
        try:
            relative_target = resolved_target.relative_to(resolved_root)
        except ValueError:
            relative_target = resolved_target
        if not policy.is_path_allowed(relative_target):
            blocked_by_policy = True
            blocked_changes.append(
                BlockedCodeChange(
                    path=str(relative_target),
                    requested_path=change.path,
                    reason="out_of_policy",
                )
            )
            continue

        eligible_changes.append((change, resolved_target))

    virtual_content_by_path: dict[str, str] = {}
    exists_by_path: dict[str, bool] = {}
    for change, resolved_target in eligible_changes:
        backup_key = str(resolved_target)

        if backup_key in exists_by_path:
            target_exists = exists_by_path[backup_key]
        else:
            target_exists = await async_exists(resolved_target)
            exists_by_path[backup_key] = target_exists

        if target_exists:
            if backup_key in virtual_content_by_path:
                existing = virtual_content_by_path[backup_key]
            else:
                existing = await async_read_text(resolved_target, encoding="utf-8")
                virtual_content_by_path[backup_key] = existing
            if existing == change.content:
                continue
            if backup_key not in backups_by_path:
                backups_by_path[backup_key] = {"path": backup_key, "content": existing}
        else:
            if backup_key not in backups_by_path:
                backups_by_path[backup_key] = {"path": backup_key, "created": True}
        virtual_content_by_path[backup_key] = change.content
        exists_by_path[backup_key] = True
        applied.append(change)
        write_plan.append((change, resolved_target))

    # Mutation phase (atomic batch writes + rollback on failure).
    written_targets: List[Path] = []
    current_target: Path | None = None
    try:
        for change, resolved_target in write_plan:
            current_target = resolved_target
            await async_mkdir(resolved_target.parent, parents=True, exist_ok=True)
            await async_write_text(resolved_target, change.content, encoding="utf-8")
            written_targets.append(resolved_target)
        current_target = None
    except Exception as exc:
        rollback_error: Exception | None = None
        rollback_targets: List[Path] = list(written_targets)
        if current_target is not None and current_target not in rollback_targets:
            rollback_targets.append(current_target)
        try:
            for rollback_target in reversed(rollback_targets):
                rollback_key = str(rollback_target)
                rollback_entry = backups_by_path.get(rollback_key)
                if not rollback_entry:
                    continue
                if rollback_entry.get("created"):
                    if await async_exists(rollback_target):
                        await async_unlink(rollback_target)
                else:
                    await async_mkdir(rollback_target.parent, parents=True, exist_ok=True)
                    await async_write_text(
                        rollback_target,
                        rollback_entry["content"],
                        encoding="utf-8",
                    )
        except Exception as rollback_exc:  # pragma: no cover - rare double-failure path
            rollback_error = rollback_exc

        if rollback_error is not None:
            raise CodeApplicationError(
                "Failed to apply code changes atomically and rollback was unsuccessful"
            ) from rollback_error
        raise CodeApplicationError("Failed to apply code changes atomically") from exc

    if kernel:
        if blocked_by_policy:
            kernel.update_memory("code_change_status", "blocked_by_policy")
        elif changes and not applied:
            kernel.update_memory("code_change_status", "no-op (content already applied)")
        if backups_by_path:
            backup_entries = [
                backups_by_path[path_key] for path_key in sorted(backups_by_path.keys())
            ]
            kernel.update_memory(
                "code_change_backups",
                json.dumps(backup_entries, ensure_ascii=False),
            )
    return CodeApplicationResult(
        applied_changes=applied,
        blocked_changes=blocked_changes,
        out_of_policy=any(item.reason == "out_of_policy" for item in blocked_changes),
        outside_root=blocked_outside_root,
        no_op=bool(changes) and not applied and not blocked_changes,
    )


async def rollback_code_changes_async(kernel: Kernel) -> List[CodeChange]:
    raw_backups = kernel.state.memory.get("code_change_backups")
    if not raw_backups:
        return []
    try:
        backups = json.loads(raw_backups)
    except json.JSONDecodeError:
        return []
    if not isinstance(backups, list) or not backups:
        return []
    applied: List[CodeChange] = []
    resolved_repo_root = REPO_ROOT.resolve()
    backups_by_path: dict[str, dict] = {}
    for entry in backups:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        created = entry.get("created") is True
        content = entry.get("content")
        if not isinstance(path, str):
            continue
        if not created and not isinstance(content, str):
            continue
        target = Path(path)
        if target.is_absolute():
            resolved_target = target.resolve()
        else:
            resolved_target = (REPO_ROOT / target).resolve()
        if not _is_relative_to(resolved_target, resolved_repo_root):
            continue
        try:
            relative_target = resolved_target.relative_to(resolved_repo_root)
        except ValueError:
            relative_target = resolved_target
        if not policy.is_path_allowed(relative_target):
            continue
        entry_key = str(resolved_target)
        if entry_key in backups_by_path:
            continue
        backups_by_path[entry_key] = {
            "resolved_target": resolved_target,
            "created": created,
            "content": content,
        }

    for entry_key in sorted(backups_by_path.keys()):
        dedup_entry = backups_by_path[entry_key]
        resolved_target = dedup_entry["resolved_target"]
        created = dedup_entry["created"]
        content = dedup_entry["content"]
        if created:
            if await async_exists(resolved_target):
                await async_unlink(resolved_target)
            applied.append(
                CodeChange(path=str(resolved_target), content="", notes="rollback_delete")
            )
        else:
            await async_mkdir(resolved_target.parent, parents=True, exist_ok=True)
            await async_write_text(resolved_target, content, encoding="utf-8")
            applied.append(
                CodeChange(path=str(resolved_target), content=content, notes="rollback")
            )
    if applied:
        kernel.update_memory("code_change_backups", json.dumps([], ensure_ascii=False))
    return applied


def validate_code_changes(changes: List[CodeChange]) -> List[str]:
    errors: List[str] = []
    for change in changes:
        if Path(change.path).suffix != ".py":
            continue
        try:
            compile(change.content, change.path, "exec")
        except SyntaxError as exc:
            errors.append(f"{change.path}: {exc.msg} (line {exc.lineno})")
    return errors
