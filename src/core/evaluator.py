# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Literal

from sif.core.benchmarks import run_benchmarks_async

DEFAULT_COMPILE_TIMEOUT_S = 60.0
DEFAULT_TEST_TIMEOUT_S = 120.0


async def _run_subprocess(
    cmd: list[str],
    *,
    timeout_s: float,
    cwd: Path | None = None,
    env: Dict[str, str] | None = None,
) -> tuple[int, str, str, bool]:
    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    timed_out = False
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=timeout_s
        )
    except asyncio.TimeoutError:
        timed_out = True
        process.kill()
        stdout_bytes, stderr_bytes = await process.communicate()
    except asyncio.CancelledError:
        process.kill()
        await process.communicate()
        raise
    return (
        process.returncode if process.returncode is not None else -9,
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
        timed_out,
    )


async def evaluate_async(
    workspace_path: Path,
    *,
    compile_timeout_s: float = DEFAULT_COMPILE_TIMEOUT_S,
    test_timeout_s: float = DEFAULT_TEST_TIMEOUT_S,
    benchmark_mode: Literal["always", "never", "auto"] = "auto",
) -> Dict[str, Any]:
    if benchmark_mode not in {"always", "never", "auto"}:
        raise ValueError("benchmark_mode must be one of: always, never, auto")

    start_time = time.monotonic()
    compile_target = workspace_path / "src"
    compile_cmd = [sys.executable, "-m", "compileall", str(compile_target)]
    (
        compile_returncode,
        compile_stdout,
        compile_stderr,
        compile_timed_out,
    ) = await _run_subprocess(compile_cmd, timeout_s=compile_timeout_s)

    tests_timed_out = False
    tests_skip_reason = ""
    if compile_returncode != 0 or compile_timed_out:
        tests_returncode = -1
        tests_stdout = "skipped (compile failed)\n"
        tests_stderr = ""
        tests_skipped = True
        tests_skip_reason = "compile_timeout" if compile_timed_out else "compile_failed"
    elif os.getenv("SIF_EVALUATION_CONTEXT") == "1":
        tests_returncode = 0
        tests_stdout = "skipped (nested evaluation context)\n"
        tests_stderr = ""
        tests_skipped = True
        tests_skip_reason = "evaluation_context"
    else:
        env = dict(os.environ)
        env["SIF_EVALUATION_CONTEXT"] = "1"
        tests_cmd = [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            str(workspace_path / "tests"),
            "-t",
            str(workspace_path),
        ]
        (
            tests_returncode,
            tests_stdout,
            tests_stderr,
            tests_timed_out,
        ) = await _run_subprocess(
            tests_cmd,
            timeout_s=test_timeout_s,
            cwd=workspace_path,
            env=env,
        )
        tests_skipped = False

    benchmarks_skipped = False
    benchmarks_skip_reason = ""
    should_run_benchmarks = benchmark_mode == "always"
    if benchmark_mode == "never":
        benchmarks_skipped = True
        benchmarks_skip_reason = "disabled_by_policy"
    elif benchmark_mode == "auto":
        in_parallel_evaluation_context = os.getenv("SIF_EVALUATION_CONTEXT") == "1"
        benchmark_env_disabled = os.getenv("SIF_DISABLE_BENCHMARKS") == "1"
        if in_parallel_evaluation_context:
            benchmarks_skipped = True
            benchmarks_skip_reason = "evaluation_context"
        elif benchmark_env_disabled:
            benchmarks_skipped = True
            benchmarks_skip_reason = "disabled_by_env"
        else:
            should_run_benchmarks = True

    benchmarks: Dict[str, Any] = {}
    if should_run_benchmarks and not benchmarks_skipped:
        benchmarks = await run_benchmarks_async(workspace_path)

    duration_sec = time.monotonic() - start_time
    tests_success = tests_returncode == 0 and not tests_skipped and not tests_timed_out
    tests_status = "skipped" if tests_skipped else ("passed" if tests_success else "failed")
    timed_out = compile_timed_out or tests_timed_out
    reason = "compile_timeout" if compile_timed_out else ("test_timeout" if tests_timed_out else "")

    return {
        "compile_success": compile_returncode == 0 and not compile_timed_out,
        "tests_success": tests_success,
        "tests_skipped": tests_skipped,
        "tests_skip_reason": tests_skip_reason,
        "tests_status": tests_status,
        "duration_sec": duration_sec,
        "compile_returncode": compile_returncode,
        "tests_returncode": tests_returncode,
        "tests_timed_out": tests_timed_out,
        "compile_stdout": compile_stdout,
        "compile_stderr": compile_stderr,
        "tests_stdout": tests_stdout,
        "tests_stderr": tests_stderr,
        "benchmarks": benchmarks,
        "benchmarks_skipped": benchmarks_skipped,
        "benchmarks_skip_reason": benchmarks_skip_reason,
        "benchmark_mode": benchmark_mode,
        "timed_out": timed_out,
        "reason": reason,
        "network_isolation": False,
        "network_isolation_note": "network isolation not available in this environment",
    }
