# AGENTS.md — rules for how the coding agent works in the repository

## Priority
1) The specification / issue / PR description and acceptance criteria
2) This file
3) Local rules in affected folders (`*/AGENTS.md`):
    - Local `AGENTS.md` files may only clarify scope and make requirements stricter.

##

## Rules
- Do not change contracts / external behavior unless there is an explicit necessity and/or requirement.
- Keep the diff optimal in size: no incidental refactors, mass renames, or mechanical formatting.
- Follow the repository’s existing patterns, including UI/UX.
- Propose the unambiguously best repo-compatible solution for the current situation.
- For a new module: provide the necessary structure + a short description nearby (how to use / verify it).
- Do not invent facts (paths / commands / API / fields / formats / flags / dependency behavior).
- For third-party libs / SDKs / tools, rely on:
  - the actual versions and interfaces in the repository, and/or
  - the vendor’s official documentation / specs (in disputed cases, cite the source in the report), and/or
  - applicable and confirmed industry standards and practices.
  If there is no confirmation, request the specific artifact or choose the conservative option without assumptions.
- Change dependencies / versions / lockfiles only if necessary for the task.
- Suppressing / ignoring errors is allowed only with an explicit explanation of why it is safe.
- Write / update tests only for the implemented code: never “fit” the code to the tests.
- Before presenting the final solution, actually verify usages / call-sites for the affected symbols / modules / contracts.

## Checks and honesty
- If behavior changed, add / update documentation / tests.\
- Run checks via the repository’s existing commands / scripts (do not invent commands).
- Do not claim “passed / works” unless you actually verified it; explicitly state what was run and what was not.

## DoD
- [ ] Acceptance criteria are met; there are no unnecessary behavior changes.
- [ ] The diff matches the repository’s patterns.
- [ ] Contracts are preserved, or the change is formalized according to project practice.
- [ ] The requirements from the “Checks and honesty” section are fulfilled.
- [ ] Tests and static analysis have confirmed the implementation is successful and ready for final delivery.
- [ ] The response (in RU) includes: what was done, key files, what was verified / what should be checked manually, assumptions / risks (if any), tests performed.
