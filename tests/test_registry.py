# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from sif.components.registry import ComponentRegistry


def test_registry_contains_release_components() -> None:
    registry = ComponentRegistry()
    names = {component.name for component in registry.components}
    assert 'governance' in names
    assert 'improvement_protocol' in names
    assert 'adaptation' in names
    assert 'constraint_explorer' in names
    assert 'mission_alignment' in names
    assert 'autonomy_scope' in names
    assert 'code_mutation' in names
