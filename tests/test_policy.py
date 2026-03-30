from pathlib import Path

from sif.core.policy import can_remove_constraint, is_path_allowed


def test_policy_respects_allowed_paths() -> None:
    assert is_path_allowed(Path('src/components/example.py')) is True
    assert is_path_allowed(Path('src/evolvable/example.py')) is True
    assert is_path_allowed(Path('src/core/kernel.py')) is False
    assert is_path_allowed(Path('tests/test_policy.py')) is False


def test_policy_protects_prefixed_constraints() -> None:
    assert can_remove_constraint('temporary: relax later') is True
    assert can_remove_constraint('security: keep audit trail') is False
