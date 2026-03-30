# Evolution Plan

## 0. Baseline (from audit)
- Architecture map:
  - CLI entrypoint orchestrates rollback and cycle execution in `sif.cli` (`src/sif/cli.py:39-57#build_parser`, `src/sif/cli.py:60-217#_run_main`).
  - Core autonomy loop is centered in `SpiralEngine.step()` and evolve/apply stages (`src/core/spiral_engine.py:1160-1300#SpiralEngine`, `src/core/spiral_engine.py:760-1025#SpiralEngine.evolve`).
  - Candidate evaluation pipeline is `ExperimentManager -> evaluate_async -> selector.should_accept` (`src/core/experiment_manager.py:124-323#ExperimentManager.run_async`, `src/core/evaluator.py:54-164#evaluate_async`, `src/core/selector.py:9-29#should_accept`).
  - Persistence and recovery are split between state store and version snapshots (`src/core/state_store.py:203-255#load_state`, `src/core/versioning.py:106-325#create_version_async`, `src/core/versioning.py:198-325#restore_version_async`).
  - Mutation boundary is policy-gated to `src/components` and `src/evolvable` (`src/core/policy.py:17-58#is_path_allowed`).
- Critical flows:
  1. `sif.cli` single-cycle run with state load/save (`src/sif/cli.py:79-127#_run_main`).
  2. Continuous unattended run with restart/error counters (`src/sif/cli.py:115-193#_run_main`).
  3. Plan/evaluate/reflect/code-change decision inside spiral cycle (`src/core/spiral_engine.py:1160-1268#SpiralEngine.step`).
  4. Candidate experiment selection and application (`src/core/spiral_engine.py:743-855#SpiralEngine.evolve`, `src/core/experiment_manager.py:172-323#ExperimentManager.run_async`).
  5. Post-apply degradation detection and rollback to LKG/latest version (`src/core/spiral_engine.py:866-980#SpiralEngine.evolve`).
  6. Snapshot creation/restore for rollback (`src/core/versioning.py:106-195#create_version_async`, `src/core/versioning.py:198-325#restore_version_async`).
  7. Policy enforcement for file changes (`src/core/evolution.py:116-151#apply_code_changes_to_root_async`, `src/core/policy.py:52-58#is_path_allowed`).
  8. Event durability via async writer and fail-safe queue (`src/core/events.py:244-280#AsyncEventWriter`, `src/core/events.py:142-160#_enqueue_fail_safe_lines`).
  9. LLM fallback path without API key (`src/core/llm.py:31-44#LLMOrchestrator.start`, `src/core/llm.py:84-89#LLMOrchestrator.request_response`).
  10. Repository-native validation path (`Makefile:3-13`, `scripts/check.sh:5-10`, `docs/evaluation.md:14-21`).
- Current pain points:
  - **P0** Evaluation command in runtime uses `unittest discover`, but repository test contract is pytest-based. Runtime evaluator executes `python -m unittest discover ...` (`src/core/evaluator.py:91-100#evaluate_async`), while repo-native checks are `pytest -q` (`Makefile:3-4`, `scripts/check.sh:7-8`, `docs/evaluation.md:16-19`), and tests use pytest fixtures/functions (`tests/test_cli.py:13-29`, `tests/test_versioning.py:11-25`). This causes evaluation mismatch and candidate rejection risk.
  - **P0** Candidate acceptance hard-requires `tests_success=True` (`src/core/selector.py:13-29#should_accept`) and `ExperimentManager` relies on evaluator output for accept/reject (`src/core/experiment_manager.py:254-260#ExperimentManager.run_async`). With the evaluator mismatch above, valid candidates can be consistently rejected.
  - **P1** Critical evolution/rollback paths are effectively untested in automated suite: tests currently cover only CLI smoke, policy, state store, version smoke, and LLM fallback (`tests/test_cli.py:13-29`, `tests/test_policy.py:1-31`, `tests/test_state_store.py:12-27`, `tests/test_versioning.py:11-25`, `tests/test_llm.py:8-22`), but there is no direct test for `ExperimentManager.run_async` (`src/core/experiment_manager.py:149-323`) and no direct test for post-apply rollback branch in `SpiralEngine.evolve` (`src/core/spiral_engine.py:866-980`).
  - **P2** LLM task contract is misleading: `self_evolve` is declared as supported (`src/core/llm.py:27#LLMOrchestrator`) but no `self_evolve` branch exists in fallback builder (`src/core/llm.py:145-155#LLMOrchestrator.build_fallback`), while call-sites in the core loop explicitly request only `plan`, `evaluate`, `reflect`, and `code_changes` (`src/core/spiral_engine.py:1941-2108#SpiralEngine._resolve_llm_plan`, `src/core/spiral_engine.py:1976-2108#SpiralEngine._resolve_llm_evaluation`).
- Constraints: 
  - Mutation policy intentionally restricts writable code to evolvable/component areas (`src/core/policy.py:17-58#is_path_allowed`).
  - Safety and rollback are first-class contracts in docs and runtime (`README.md:27-39`, `docs/guarantees.md:5-10`, `src/core/spiral_engine.py:866-980#SpiralEngine.evolve`).
  - Validation commands already standardized in repository scripts/docs (`Makefile:3-13`, `scripts/check.sh:5-10`, `CONTRIBUTING.md:11-17`).

## 1. North Star
- UX outcomes:
  - Candidate evaluation outcomes become predictable: same result from runtime evaluator and contributor local checks (proxy metric: zero command-set drift between evaluator and repo check commands).
  - Failed candidate explanations map to actionable reasons (proxy metric: each rejection reason corresponds to a tested branch in evaluator/selector).
- Domain outcomes:
  - Single source of truth for “candidate is valid” contract across evaluator and selector.
  - Rollback invariants remain verifiable for post-apply degradation path.
- Engineering outcomes:
  - Reduced false-negative candidate rejection rate (proxy: accepted candidates after evaluator fix under unchanged baseline).
  - Higher regression resistance by direct tests for ExperimentManager + rollback branch.

## 2. Roadmap (incremental)
### Phase 1 (Stabilize Core)
- Goal
  - Remove correctness defects that currently distort candidate acceptance.
- Scope (what we touch / what we don’t)
  - Touch: evaluator command path, acceptance contract tests, experiment-manager evaluation tests.
  - Don’t touch: mutation policy boundary, component strategy logic, LLM prompt design.
- Deliverables (concrete changes)
  1. Replace `unittest discover` invocation with repo-native pytest invocation in evaluator.
  2. Add regression tests proving evaluator executes pytest-compatible suites in workspace mode.
  3. Add acceptance-flow tests: evaluator output -> `should_accept` behavior.
  4. Add `ExperimentManager.run_async` test for accepted candidate under passing evaluator.
- Dependencies
  - Existing repo command contract (`Makefile:3-4`, `scripts/check.sh:7-8`).
- Risk & Rollback strategy (if migration/contract changes are required)
  - Risk: evaluator runtime cost increase when running pytest.
  - Rollback: keep a feature flag to switch evaluator test command back only if CI/runtime regressions appear; revert evaluator command change commit.
- Validation (how to verify: tests/linter/commands from the repo)
  - `pytest -q`
  - `python -m compileall -q src`
  - `PYTHONPATH=src python -m sif.cli --cycles 1 --json`

### Phase 2 (UX & Domain Consolidation)
- Goal
  - Make rejection/rollback behavior transparent and directly testable.
- Scope (what we touch / what we don’t)
  - Touch: SpiralEngine rollback branch observability and tests.
  - Don’t touch: snapshot file format, version id format.
- Deliverables (concrete changes)
  1. Add deterministic test for post-apply degradation branch and rollback metadata persistence.
  2. Add contract test for fallback restore path (`lkg_version_id` absent -> latest version restore attempt).
  3. Normalize rejection reason taxonomy (selector + experiment manager) and test branch mapping.
- Dependencies
  - Phase 1 evaluator reliability.
- Risk & Rollback strategy (if migration/contract changes are required)
  - Risk: tests may require controlled stubs/mocks for versioning + evaluator.
  - Rollback: keep behavior unchanged, add tests first; if behavior adjustments needed, gate behind explicit branch conditions with backward-compatible defaults.
- Validation (how to verify: tests/linter/commands from the repo)
  - `pytest -q`
  - `python -m compileall -q src`
  - `python scripts/build_proof_pack.py`

### Phase 3 (Scale & Maintainability)
- Goal
  - Remove misleading contracts and reduce future decision drift.
- Scope (what we touch / what we don’t)
  - Touch: LLM orchestrator task registry contract and related tests/docs.
  - Don’t touch: model provider integration mechanics.
- Deliverables (concrete changes)
  1. Remove or implement `self_evolve` task end-to-end so supported task list matches actual behavior.
  2. Add tests asserting every `supported_tasks` entry has runtime handling/fallback and at least one call-site or explicit “reserved” marker.
  3. Update docs describing supported model tasks and expected payload contracts.
- Dependencies
  - None blocking core correctness; can proceed after Phase 1.
- Risk & Rollback strategy (if migration/contract changes are required)
  - Risk: external prompts/scripts depending on old task list.
  - Rollback: retain compatibility alias for one release cycle if task key removal is required.
- Validation (how to verify: tests/linter/commands from the repo)
  - `pytest -q`
  - `python -m compileall -q src`

## 3. Task Specs (atomic, single-strategy)
- ID: EVO-001
- Priority: P0
- Theme: Reliability
- Problem:
  - Runtime evaluator uses non-repo-native test runner and fails to evaluate actual suite consistently.
- Evidence: `src/core/evaluator.py:91-100#evaluate_async`, `Makefile:3-4`, `scripts/check.sh:7-8`, `docs/evaluation.md:16-19`, `tests/test_cli.py:13-29`.
- Root Cause
  - Evaluator hardcodes `unittest discover` while project standard and tests are pytest-based.
- Impact
  - False negatives during candidate validation; valid candidates rejected.
- Fix (single solution)
  - Switch evaluator test command to `pytest -q` against workspace root.
- Steps
  1. Update evaluator subprocess command construction.
  2. Preserve timeout and output capture contract.
  3. Add regression test for evaluator command behavior.
- Acceptance Criteria (verifiable)
  - Evaluator passes when pytest suite passes in workspace.
  - Evaluator marks tests failed when pytest exits non-zero.
- Validation Commands (if visible in the project)
  - `pytest -q`
  - `python -m compileall -q src`
- Migration/Rollback (if needed)
  - Revert evaluator command change if execution time becomes unacceptable and add explicit documented flag before reattempt.

- ID: EVO-002
- Priority: P0
- Theme: Domain
- Problem:
  - Acceptance gate can reject all candidates when evaluator output is systematically wrong.
- Evidence: `src/core/selector.py:13-29#should_accept`, `src/core/experiment_manager.py:254-260#ExperimentManager.run_async`, `src/core/evaluator.py:74-113#evaluate_async`.
- Root Cause
  - Strict acceptance depends entirely on evaluator fields without contract tests for evaluator-selector coupling.
- Impact
  - Self-evolution loop stalls despite valid code changes.
- Fix (single solution)
  - Add contract tests for evaluator→selector coupling and enforce required metric fields before decision.
- Steps
  1. Add tests with synthetic evaluator payloads for accept/reject branches.
  2. Validate required fields (`compile_success`, `tests_success`, `tests_skipped`) before `should_accept` call.
  3. Reject malformed payloads with explicit reason.
- Acceptance Criteria (verifiable)
  - Accepted candidate path reachable with valid metrics.
  - Malformed payload yields deterministic rejection reason.
- Validation Commands (if visible in the project)
  - `pytest -q`
- Migration/Rollback (if needed)
  - No data migration; rollback by reverting coupling checks.

- ID: EVO-003
- Priority: P1
- Theme: Reliability
- Problem:
  - Post-apply rollback branch is complex but not covered by direct tests.
- Evidence: `src/core/spiral_engine.py:866-980#SpiralEngine.evolve`, `tests/test_versioning.py:11-25`, `tests/test_cli.py:13-29`.
- Root Cause
  - Existing tests validate only snapshot smoke and CLI smoke, not degradation-triggered rollback path.
- Impact
  - High regression risk in safety-critical recovery path.
- Fix (single solution)
  - Add deterministic unit/integration tests for degradation-triggered rollback metadata and restore path selection.
- Steps
  1. Stub evaluator metrics to trigger degradation.
  2. Stub versioning outcomes for LKG success/fallback/failure.
  3. Assert memory keys/events set as specified.
- Acceptance Criteria (verifiable)
  - Rollback keys (`rollback_triggered`, `rollback_reason`, `rollback_version_id|rollback_failed`) are correct for each branch.
- Validation Commands (if visible in the project)
  - `pytest -q`
- Migration/Rollback (if needed)
  - No migration.

- ID: EVO-004
- Priority: P1
- Theme: Platform
- Problem:
  - Candidate evaluation behavior is under-tested despite being a critical integration point.
- Evidence: `src/core/experiment_manager.py:149-323#ExperimentManager.run_async`, `tests/test_code_mutation.py:11-38`, `tests/test_llm.py:15-22`.
- Root Cause
  - Current tests focus on isolated modules; integration path (workspace materialization + code apply + evaluator + cache) lacks direct coverage.
- Impact
  - Refactors can silently break candidate throughput and selection.
- Fix (single solution)
  - Add integration tests for `ExperimentManager.run_async` covering accepted, blocked, timeout, and cached outcomes.
- Steps
  1. Use temporary repo fixture with synthetic candidates.
  2. Inject deterministic evaluator coroutine.
  3. Assert result payload and `best_candidate` selection.
- Acceptance Criteria (verifiable)
  - All documented result reasons are covered by tests.
- Validation Commands (if visible in the project)
  - `pytest -q`
- Migration/Rollback (if needed)
  - No migration.

- ID: EVO-005
- Priority: P2
- Theme: Domain
- Problem:
  - `LLMOrchestrator.supported_tasks` advertises `self_evolve` without actual runtime handling.
- Evidence: `src/core/llm.py:27#LLMOrchestrator`, `src/core/llm.py:145-155#LLMOrchestrator.build_fallback`, `src/core/spiral_engine.py:2072-2111#SpiralEngine._resolve_llm_code_changes`.
- Root Cause
  - Task registry grew without end-to-end implementation/use.
- Impact
  - Misleading contract for maintainers and future integrations.
- Fix (single solution)
  - Remove `self_evolve` from supported list until a concrete call path and schema exist.
- Steps
  1. Update supported task list.
  2. Update tests/docs for supported tasks.
  3. Add guard test that supported tasks map to explicit handlers.
- Acceptance Criteria (verifiable)
  - No orphan task entries in supported list.
- Validation Commands (if visible in the project)
  - `pytest -q`
- Migration/Rollback (if needed)
  - If external dependency exists, keep temporary compatibility alias in one release.

## 4. Explicit Non-Goals
- Do not broaden mutation policy boundaries beyond current allowed paths (`src/core/policy.py:17-58#is_path_allowed`).
- Do not redesign component strategy heuristics or prompt content in this plan.
- Do not introduce new external dependencies for validation tooling.
- Do not refactor unrelated modules for style-only reasons.

Stopping rule:
- This audit found actionable items in whitelist categories (P0/P1/P2), therefore no “no-op” conclusion is applicable.
