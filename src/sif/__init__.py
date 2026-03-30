"""Canonical Spiral Autonomy Kernel package namespace.

This package maps sibling source packages (``core``, ``components``,
``evolvable``) into ``sif.*`` using explicit local file paths so imports stay
bound to this repository source tree.
"""

from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys


def _load_local_package_alias(alias: str) -> None:
    source_root = Path(__file__).resolve().parent.parent
    package_dir = source_root / alias
    init_file = package_dir / "__init__.py"
    spec = spec_from_file_location(
        f"{__name__}.{alias}",
        init_file,
        submodule_search_locations=[str(package_dir)],
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive guard
        raise ImportError(f"Unable to create module spec for local package '{alias}'.")

    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    globals()[alias] = module


for _alias in ("core", "components", "evolvable"):
    _load_local_package_alias(_alias)
