# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

"""Small compatibility shim used when the external portalocker package is absent.

The runtime only needs the names and basic lock/unlock functions. On POSIX systems
`fcntl` is used by the caller first, so this module is mainly a fallback import shim.
"""

LOCK_EX = 1
LOCK_NB = 2


def lock(_handle, _flags: int) -> None:
    return None


def unlock(_handle) -> None:
    return None
