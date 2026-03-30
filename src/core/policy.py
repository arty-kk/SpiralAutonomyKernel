from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

IMMUTABLE_PATHS: list[Path] = [
    Path("tests"),
    Path("src/core/policy.py"),
    Path("src/core/workspace.py"),
    Path("src/core/evaluator.py"),
    Path("src/core/selector.py"),
]
EVOLVABLE_PATHS: list[Path] = [
    Path("src/components"),
    Path("src/evolvable"),
]
PROTECTED_CONSTRAINT_PREFIXES: tuple[str, ...] = (
    "safety:",
    "security:",
    "policy:",
)
INVARIANT_KEYS: tuple[str, ...] = (
    "self_recovery_enabled",
    "observability_enabled",
    "cycle_integrity_enabled",
)
INVARIANT_DEFAULTS: dict[str, str] = {
    "self_recovery_enabled": "true",
    "observability_enabled": "true",
    "cycle_integrity_enabled": "true",
}


def _resolve_path(path: Path) -> Path:
    if path.is_absolute():
        return path.resolve()
    return (REPO_ROOT / path).resolve()


def _is_within(path: Path, base: Path) -> bool:
    resolved_base = _resolve_path(base)
    try:
        return path.is_relative_to(resolved_base)
    except AttributeError:
        return resolved_base == path or resolved_base in path.parents


def is_path_allowed(path: Path) -> bool:
    resolved = _resolve_path(path)
    if not any(_is_within(resolved, evolvable) for evolvable in EVOLVABLE_PATHS):
        return False
    if any(_is_within(resolved, immutable) for immutable in IMMUTABLE_PATHS):
        return False
    return True


def can_remove_constraint(constraint: str) -> bool:
    normalized_constraint = constraint.strip().lower()
    return not normalized_constraint.startswith(PROTECTED_CONSTRAINT_PREFIXES)


def violates_invariants(action: str, target: str, value: str) -> bool:
    if action != "update_memory":
        return False
    if target in INVARIANT_KEYS and str(value).lower() in {"false", "0", "disabled"}:
        return True
    return False
