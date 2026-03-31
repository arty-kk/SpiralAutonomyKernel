# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from sif.core.selector import should_accept


def test_should_accept_accepts_when_compile_and_tests_pass() -> None:
    accepted, reason = should_accept(
        {
            'compile_success': True,
            'tests_success': True,
            'tests_skipped': False,
        }
    )
    assert accepted is True
    assert reason == 'accepted'


def test_should_accept_rejects_when_tests_skipped() -> None:
    accepted, reason = should_accept(
        {
            'compile_success': True,
            'tests_success': True,
            'tests_skipped': True,
        }
    )
    assert accepted is False
    assert reason == 'tests_skipped'
