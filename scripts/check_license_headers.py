# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

ROOTS = ("src", "tests", "scripts", "examples")
SUFFIXES = {".py", ".sh"}
COPYRIGHT_TAG = "SPDX-FileCopyrightText:"
LICENSE_TAG = "SPDX-License-Identifier: Apache-2.0"


def main() -> int:
    missing: list[str] = []

    for root in ROOTS:
        for path in sorted(Path(root).rglob("*")):
            if path.suffix not in SUFFIXES:
                continue
            header = path.read_text().splitlines()[:8]
            has_copyright = any(COPYRIGHT_TAG in line for line in header)
            has_license = any(LICENSE_TAG in line for line in header)
            if not (has_copyright and has_license):
                missing.append(str(path))

    if missing:
        print("Missing SPDX header tags in:")
        for path in missing:
            print(f" - {path}")
        return 1

    print("All checked code files include SPDX copyright and license tags.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
