from __future__ import annotations

import argparse
import asyncio
import dataclasses
from datetime import datetime
import json
import sys
from pprint import pformat
from typing import Any

from sif.core.async_cpu import shutdown_cpu_executor
from sif.core.async_fs import shutdown_fs_executor
from sif.core.events import shutdown_event_writer
from sif.core.kernel import Kernel
from sif.core.spiral_engine import SpiralCycleResult, SpiralEngine
from sif.core.state_store import load_state, save_state
from sif.core.time_utils import utc_now_iso
from sif.core.versioning import latest_version_async, restore_version_async


def _replace_datetimes(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _replace_datetimes(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_datetimes(item) for item in value]
    return value


def serialize_spiral_result(result: SpiralCycleResult) -> dict[str, Any]:
    return _replace_datetimes(dataclasses.asdict(result))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Run the Spiral Autonomy Kernel runtime.'
    )
    parser.add_argument('--rollback', metavar='VERSION_ID', help='Restore repository files from a saved snapshot.')
    parser.add_argument('--hard-restore', action='store_true', help='When used with --rollback, remove files not present in the snapshot.')
    parser.add_argument('--cycles', type=int, default=1, help='Number of cycles to execute (minimum 1).')
    parser.add_argument('--state-path', default='.sif/state.json', help='Path to the state file.')
    parser.add_argument('--json', action='store_true', help='Emit structured JSON output.')
    parser.add_argument('--continuous', action='store_true', help='Run cycles continuously until stopped or --max-cycles is reached.')
    parser.add_argument('--max-cycles', type=int, default=0, help='Maximum cycles in continuous mode (0 = unlimited).')
    parser.add_argument('--sleep-seconds', type=float, default=0.0, help='Delay between cycles in continuous mode.')
    parser.add_argument('--continue-on-error', action='store_true', help='Continue the unattended loop after cycle errors.')
    parser.add_argument('--max-consecutive-errors', type=int, default=3, help='Stop after this many consecutive cycle errors.')
    parser.add_argument('--error-backoff-seconds', type=float, default=1.0, help='Delay after a failed cycle before retry.')
    parser.add_argument('--restart-on-fatal', action='store_true', help='Restart the unattended session after fatal cycle errors.')
    parser.add_argument('--max-restarts', type=int, default=3, help='Maximum unattended session restarts after fatal errors (0 = unlimited).')
    parser.add_argument('--restart-backoff-seconds', type=float, default=2.0, help='Delay before unattended session restart after fatal errors.')
    return parser


async def _run_main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.cycles < 1:
        raise SystemExit('--cycles must be >= 1')
    if args.max_cycles < 0:
        raise SystemExit('--max-cycles must be >= 0')
    if args.sleep_seconds < 0:
        raise SystemExit('--sleep-seconds must be >= 0')
    if args.max_consecutive_errors < 1:
        raise SystemExit('--max-consecutive-errors must be >= 1')
    if args.error_backoff_seconds < 0:
        raise SystemExit('--error-backoff-seconds must be >= 0')
    if args.max_restarts < 0:
        raise SystemExit('--max-restarts must be >= 0')
    if args.restart_backoff_seconds < 0:
        raise SystemExit('--restart-backoff-seconds must be >= 0')

    try:
        state = await load_state(args.state_path)
        kernel = Kernel(state=state)

        if args.rollback:
            version_id = args.rollback
            if version_id == 'latest':
                version_id = await latest_version_async()
                if not version_id:
                    print('No saved versions found to rollback.')
                    return
            restore_mode = 'hard' if args.hard_restore else 'soft'
            restored = await restore_version_async(version_id, mode=restore_mode)
            if not restored:
                print(f'No snapshot found for version: {version_id}')
                sys.exit(1)
            kernel.update_memory('last_cli_rollback', version_id)
            await save_state(args.state_path, kernel.state)
            print(f'Rollback completed for version: {version_id}')
            return

        result: SpiralCycleResult | None = None
        cycle_target = args.cycles
        if args.continuous:
            cycle_target = args.max_cycles if args.max_cycles > 0 else None
            kernel.update_memory('unattended_mode', 'running')
            kernel.update_memory('unattended_started_at', utc_now_iso(timespec='seconds'))
            await save_state(args.state_path, kernel.state)
            if not args.json:
                print('Unattended mode started.')

        completed_cycles = 0
        consecutive_errors = 0
        restart_count = 0
        loop_error: Exception | None = None

        while True:
            session_error: Exception | None = None
            try:
                async with SpiralEngine(kernel=kernel) as engine:
                    while cycle_target is None or completed_cycles < cycle_target:
                        try:
                            result = await engine.step()
                            consecutive_errors = 0
                            completed_cycles += 1
                            if args.continuous:
                                kernel.update_memory('unattended_last_success_at', utc_now_iso(timespec='seconds'))
                                kernel.update_memory('unattended_success_cycles', str(completed_cycles))
                            await save_state(args.state_path, kernel.state)
                        except Exception as exc:
                            if not args.continuous or not args.continue_on_error:
                                raise
                            consecutive_errors += 1
                            total_errors = int(kernel.state.memory.get('unattended_total_errors', '0') or '0') + 1
                            kernel.update_memory('unattended_total_errors', str(total_errors))
                            kernel.update_memory('unattended_consecutive_errors', str(consecutive_errors))
                            kernel.update_memory(
                                'last_unattended_error',
                                json.dumps(
                                    {
                                        'type': type(exc).__name__,
                                        'message': str(exc),
                                        'consecutive_errors': consecutive_errors,
                                        'total_errors': total_errors,
                                    },
                                    ensure_ascii=False,
                                ),
                            )
                            await save_state(args.state_path, kernel.state)
                            if consecutive_errors >= args.max_consecutive_errors:
                                raise
                            if args.error_backoff_seconds > 0:
                                await asyncio.sleep(args.error_backoff_seconds)
                            continue

                        if args.continuous and (cycle_target is None or completed_cycles < cycle_target):
                            if args.sleep_seconds > 0:
                                await asyncio.sleep(args.sleep_seconds)
            except Exception as exc:
                session_error = exc

            if session_error is None:
                break
            if not (args.continuous and args.restart_on_fatal):
                loop_error = session_error
                break
            if args.max_restarts > 0 and restart_count >= args.max_restarts:
                loop_error = session_error
                break
            restart_count += 1
            kernel.update_memory('unattended_restart_count', str(restart_count))
            kernel.update_memory('unattended_last_restart_at', utc_now_iso(timespec='seconds'))
            await save_state(args.state_path, kernel.state)
            if not args.json:
                print('Fatal error detected; restarting unattended session.')
            consecutive_errors = 0
            if args.restart_backoff_seconds > 0:
                await asyncio.sleep(args.restart_backoff_seconds)

        if args.continuous:
            if loop_error is None:
                kernel.update_memory('unattended_mode', 'stopped')
            else:
                kernel.update_memory('unattended_mode', 'error')
                kernel.update_memory(
                    'unattended_last_fatal_error',
                    json.dumps({'type': type(loop_error).__name__, 'message': str(loop_error)}, ensure_ascii=False),
                )
                if not args.json:
                    print('Unattended mode stopped by fatal cycle error.')
            kernel.update_memory('unattended_completed_at', utc_now_iso(timespec='seconds'))
            kernel.update_memory('unattended_success_cycles', str(completed_cycles))
            kernel.update_memory('unattended_restart_count', str(restart_count))
            await save_state(args.state_path, kernel.state)

        if loop_error is not None:
            raise loop_error

        if result is not None:
            if args.json:
                print(json.dumps(serialize_spiral_result(result), ensure_ascii=False, indent=2))
                return
            print('Observations:')
            print(pformat(result.observations, indent=2, width=88))
            print('Plan:')
            print(pformat(result.plan, indent=2, width=88))
            print('Evaluation:')
            print(pformat(result.evaluation, indent=2, width=88))
            if result.reflection:
                print('Reflection Summary:')
                print(result.reflection.summary)
    finally:
        await shutdown_event_writer()
        await shutdown_fs_executor()
        await shutdown_cpu_executor()


def main() -> None:
    asyncio.run(_run_main())


if __name__ == '__main__':
    main()
