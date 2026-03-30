# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
import os
import time
import warnings
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

import portalocker

try:
    import fcntl
except ImportError:  # pragma: no cover - platform dependent
    fcntl = None

from sif.core.evolution import REPO_ROOT
from sif.core.time_utils import utc_now_iso
from sif.core import async_fs


EVENTS_PATH = REPO_ROOT / ".sif" / "events.jsonl"
FAIL_SAFE_QUEUE_DIR = REPO_ROOT / ".sif" / "events.queue"
EVENT_WRITER_QUEUE_MAXSIZE = 512
FAIL_SAFE_WRITER_QUEUE_MAXSIZE = 2048
EVENT_WRITER_MAX_BATCH_SIZE = 128
EVENT_WRITER_MAX_FLUSH_INTERVAL_S = 0.05
FAIL_SAFE_WRITER_OVERFLOW_POLICY = "drop_oldest"
# Contract:
# - EVENT_WRITER_OVERFLOW_POLICY supports: "block", "drop_new", "drop_oldest".
# - With "block", append_event applies queue backpressure via await queue.put(...).
# - With "drop_new", append_event uses put_nowait and drops the incoming event if full.
# - With "drop_oldest", append_event evicts exactly one oldest queued event if full,
#   then inserts the incoming event with put_nowait.
# - Every dropped event emits an explicit warning and is appended to fail-safe storage
#   with reason="overflow_<policy>", so overflow is observable and measurable.
# Production defaults prioritize hot-path non-blocking ingest:
# - drop_oldest keeps ingress responsive under bursts;
# - replay durability is preserved by writing dropped/unwritten events into events.queue.
EVENT_WRITER_OVERFLOW_POLICY = "drop_oldest"
EVENTS_FSYNC_ENABLED = False
EVENT_FILE_LOCK_TIMEOUT_S = 1.0
EVENT_FILE_LOCK_RETRY_BACKOFF_S = 0.05


_EVENT_WRITER_METRICS: dict[str, float] = {
    "queue_depth": 0,
    "fail_safe_queue_depth": 0,
    "dropped_overflow_total": 0,
    "fail_safe_dropped_total": 0,
    "flush_latency_ms": 0.0,
    "lock_timeout_total": 0,
}


def _event_writer_metrics_snapshot(*, queue_depth: int | None = None) -> dict[str, float]:
    snapshot = dict(_EVENT_WRITER_METRICS)
    if queue_depth is not None:
        snapshot["queue_depth"] = queue_depth
    return snapshot


def _record_metric_increment(metric: str, value: int = 1) -> None:
    _EVENT_WRITER_METRICS[metric] = _EVENT_WRITER_METRICS.get(metric, 0) + value


def _record_metric_set(metric: str, value: float) -> None:
    _EVENT_WRITER_METRICS[metric] = value


async def _mkdir(path: Path) -> None:
    await async_fs.mkdir(path, parents=True, exist_ok=True)


async def _unlink(path: Path) -> None:
    await async_fs.unlink_missing_ok(path)


async def _replace(src: Path, dst: Path) -> None:
    await async_fs.replace(src, dst)


async def _read_text(path: Path) -> str:
    return await async_fs.read_text(path, encoding="utf-8")


async def _write_lines(path: Path, lines: list[str]) -> None:
    def _write() -> None:
        with path.open("w", encoding="utf-8") as handle:
            handle.writelines(lines)
            handle.flush()

    await async_fs.run_fs(_write)


def _acquire_lock_single_try_sync(handle) -> bool:
    if fcntl is None:
        warnings.warn("fcntl unavailable; using portalocker fallback lock.")
        try:
            portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except Exception:  # pragma: no cover - platform dependent
            return False
        return True
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


async def _acquire_lock_with_retry_async(single_try_acquire_async: Callable[[], Awaitable[bool]]) -> bool:
    lock_deadline = time.monotonic() + EVENT_FILE_LOCK_TIMEOUT_S
    while True:
        lock_acquired = await single_try_acquire_async()
        if lock_acquired:
            return True
        if time.monotonic() >= lock_deadline:
            _record_metric_increment("lock_timeout_total")
            warnings.warn("Skipping event write due to lock acquisition timeout.")
            return False
        await asyncio.sleep(EVENT_FILE_LOCK_RETRY_BACKOFF_S)


def _release_lock_sync(handle) -> None:
    if fcntl is None:
        try:
            portalocker.unlock(handle)
        except Exception as exc:  # pragma: no cover - platform dependent
            warnings.warn(f"Unable to release portalocker lock. ({exc})")
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        warnings.warn(f"Unable to release events file lock. ({exc})")


async def _enqueue_fail_safe_lines(lines: list[str]) -> None:
    if not lines:
        return
    await _mkdir(FAIL_SAFE_QUEUE_DIR)
    normalized_lines = [
        line if line.endswith("\n") else f"{line}\n" for line in lines if line.strip()
    ]
    if not normalized_lines:
        return
    queue_id = uuid4().hex
    temp_path = FAIL_SAFE_QUEUE_DIR / f".{queue_id}.tmp"
    final_path = FAIL_SAFE_QUEUE_DIR / f"{queue_id}.jsonl"
    await _write_lines(temp_path, normalized_lines)
    await _replace(temp_path, final_path)


async def _append_fail_safe_event(event: dict[str, Any], reason: str, original_event_type: str) -> None:
    fail_safe_event = _build_fail_safe_event(event, reason, original_event_type)
    await _enqueue_fail_safe_lines([json.dumps(fail_safe_event, ensure_ascii=False) + "\n"])


def _build_fail_safe_event(event: dict[str, Any], reason: str, original_event_type: str) -> dict[str, Any]:
    fail_safe_event = dict(event)
    fail_safe_metadata = {
        "reason": reason,
        "original_event_type": original_event_type,
    }
    original_payload = event.get("payload")
    if isinstance(original_payload, dict):
        fail_safe_payload = dict(original_payload)
        fail_safe_payload["_fail_safe"] = fail_safe_metadata
    else:
        fail_safe_payload = {
            "value": original_payload,
            "_fail_safe": fail_safe_metadata,
        }
    fail_safe_event["payload"] = fail_safe_payload
    return fail_safe_event


def _serialize_events_to_jsonl(values: list[dict[str, Any]]) -> list[str]:
    return [json.dumps(item, ensure_ascii=False) + "\n" for item in values]


def _to_fail_safe_line_sync(event: dict[str, Any], reason: str) -> str:
    event_type = str(event.get("type", "unknown"))
    fail_safe_event = _build_fail_safe_event(event, reason, event_type)
    return json.dumps(fail_safe_event, ensure_ascii=False) + "\n"


async def _write_lines_to_events(lines: list[str]) -> list[str]:
    if not lines:
        return []

    await _mkdir(EVENTS_PATH.parent)
    normalized_lines = [line if line.endswith("\n") else f"{line}\n" for line in lines if line.strip()]
    if not normalized_lines:
        return []
    batch_payload = "".join(normalized_lines)
    line_lengths = [len(line) for line in normalized_lines]

    def _remaining_lines(written_chars: int) -> list[str]:
        consumed = 0
        for index, line_len in enumerate(line_lengths):
            consumed += line_len
            if written_chars < consumed:
                return normalized_lines[index:]
        return []

    def _write_batch_single_try_sync() -> tuple[bool, int]:
        with EVENTS_PATH.open("a", encoding="utf-8") as handle:
            if not _acquire_lock_single_try_sync(handle):
                return False, 0
            try:
                written_chars = handle.write(batch_payload) or 0
                handle.flush()
                if EVENTS_FSYNC_ENABLED:
                    os.fsync(handle.fileno())
                return True, written_chars
            except OSError:
                return True, 0
            finally:
                _release_lock_sync(handle)

    write_attempt_result: tuple[bool, int] = (False, 0)

    async def _attempt_write_once_async() -> bool:
        nonlocal write_attempt_result
        write_attempt_result = await async_fs.run_fs(_write_batch_single_try_sync)
        lock_acquired, _written_chars = write_attempt_result
        return lock_acquired

    lock_acquired = await _acquire_lock_with_retry_async(_attempt_write_once_async)
    if not lock_acquired:
        return list(normalized_lines)

    _lock_acquired, written_chars = write_attempt_result
    if written_chars < len(batch_payload):
        return _remaining_lines(written_chars)
    return []


class AsyncEventWriter:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        max_batch_size: int = EVENT_WRITER_MAX_BATCH_SIZE,
        max_flush_interval_s: float = EVENT_WRITER_MAX_FLUSH_INTERVAL_S,
    ) -> None:
        self._loop = loop
        self._max_batch_size = max(1, max_batch_size)
        self._max_flush_interval_s = max(0.0, max_flush_interval_s)
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=EVENT_WRITER_QUEUE_MAXSIZE)
        self._fail_safe_queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=FAIL_SAFE_WRITER_QUEUE_MAXSIZE)
        self._task: asyncio.Task[None] | None = None
        self._fail_safe_task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    async def start(self) -> None:
        if (self._task is not None and self._task.done()) or (
            self._fail_safe_task is not None and self._fail_safe_task.done()
        ):
            await self._handle_unexpected_task_exit()
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._writer_loop(), name="sif-event-writer")
        self._fail_safe_task = asyncio.create_task(
            self._fail_safe_writer_loop(),
            name="sif-fail-safe-event-writer",
        )

    async def append_event(self, event: dict[str, Any]) -> None:
        """Append event using EVENT_WRITER_OVERFLOW_POLICY queue semantics."""
        await self.start()
        policy = EVENT_WRITER_OVERFLOW_POLICY
        queued = event
        if policy == "block":
            await self._queue.put(queued)
            _record_metric_set("queue_depth", self._queue.qsize())
            return

        if policy == "drop_new":
            try:
                self._queue.put_nowait(queued)
                _record_metric_set("queue_depth", self._queue.qsize())
            except asyncio.QueueFull:
                await self._handle_dropped_overflow_event(
                    event=event,
                    policy=policy,
                )
            return

        if policy == "drop_oldest":
            try:
                self._queue.put_nowait(queued)
                _record_metric_set("queue_depth", self._queue.qsize())
            except asyncio.QueueFull:
                dropped = self._queue.get_nowait()
                self._queue.task_done()
                if dropped is None:
                    self._queue.put_nowait(None)
                    await self._handle_dropped_overflow_event(
                        event=event,
                        policy=policy,
                    )
                    return

                self._queue.put_nowait(queued)
                _record_metric_set("queue_depth", self._queue.qsize())
                await self._handle_dropped_overflow_event(
                    event=dropped,
                    policy=policy,
                )
            return

        raise ValueError(
            "Unsupported EVENT_WRITER_OVERFLOW_POLICY "
            f"{policy!r}; supported values are: block, drop_new, drop_oldest"
        )

    async def _serialize_events(self, events: list[dict[str, Any]]) -> list[str]:
        return _serialize_events_to_jsonl(events)

    async def _to_fail_safe_line(self, event: dict[str, Any], reason: str) -> str:
        return _to_fail_safe_line_sync(event, reason)

    async def _handle_dropped_overflow_event(
        self,
        event: dict[str, Any],
        policy: str,
    ) -> None:
        event_type = str(event.get("type", "unknown"))
        warnings.warn(
            "Dropping event due to full writer queue "
            f"(policy={policy}, event_type={event_type})."
        )
        _record_metric_increment("dropped_overflow_total")
        fail_safe_line = await self._to_fail_safe_line(event, f"overflow_{policy}")
        self._enqueue_fail_safe_line_nonblocking(fail_safe_line)

    def _enqueue_fail_safe_line_nonblocking(self, line: str) -> None:
        policy = FAIL_SAFE_WRITER_OVERFLOW_POLICY
        try:
            self._fail_safe_queue.put_nowait(line)
            _record_metric_set("fail_safe_queue_depth", self._fail_safe_queue.qsize())
            return
        except asyncio.QueueFull:
            pass

        _record_metric_increment("fail_safe_dropped_total")
        warnings.warn(f"Dropping fail-safe line due to full fail-safe queue (policy={policy}).")
        if policy == "drop_new":
            return
        if policy == "drop_oldest":
            try:
                dropped = self._fail_safe_queue.get_nowait()
                self._fail_safe_queue.task_done()
            except asyncio.QueueEmpty:
                return
            if dropped is None:
                self._fail_safe_queue.put_nowait(None)
                return
            try:
                self._fail_safe_queue.put_nowait(line)
            except asyncio.QueueFull:
                return
            _record_metric_set("fail_safe_queue_depth", self._fail_safe_queue.qsize())
            return

        raise ValueError(
            "Unsupported FAIL_SAFE_WRITER_OVERFLOW_POLICY "
            f"{policy!r}; supported values are: drop_new, drop_oldest"
        )

    async def _writer_loop(self) -> None:
        try:
            while True:
                first_item = await self._queue.get()
                _record_metric_set("queue_depth", self._queue.qsize())
                if first_item is None:
                    self._queue.task_done()
                    break

                batch = [first_item]
                flush_deadline = time.monotonic() + self._max_flush_interval_s
                while True:
                    if len(batch) >= self._max_batch_size:
                        break
                    try:
                        queued = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        remaining_s = flush_deadline - time.monotonic()
                        if remaining_s <= 0:
                            break
                        try:
                            queued = await asyncio.wait_for(self._queue.get(), timeout=remaining_s)
                            _record_metric_set("queue_depth", self._queue.qsize())
                        except asyncio.TimeoutError:
                            break
                    if queued is None:
                        self._queue.task_done()
                        self._queue.put_nowait(None)
                        break
                    batch.append(queued)

                try:
                    write_lines = await self._serialize_events(batch)
                    flush_started = time.monotonic()
                    unwritten = await _write_lines_to_events(write_lines)
                    _record_metric_set("flush_latency_ms", (time.monotonic() - flush_started) * 1000.0)
                    if unwritten:
                        start_index = len(write_lines) - len(unwritten)
                        for event in batch[start_index:]:
                            line = await self._to_fail_safe_line(event, "lock_timeout")
                            self._enqueue_fail_safe_line_nonblocking(line)
                except asyncio.CancelledError:
                    for event in batch:
                        line = _to_fail_safe_line_sync(event, "writer_error")
                        self._enqueue_fail_safe_line_nonblocking(line)
                    raise
                except Exception as exc:
                    warnings.warn(f"Event writer loop failed to write batch; enqueuing fail-safe. ({exc})")
                    for event in batch:
                        line = await self._to_fail_safe_line(event, "writer_error")
                        self._enqueue_fail_safe_line_nonblocking(line)
                finally:
                    for _ in batch:
                        self._queue.task_done()
                    _record_metric_set("queue_depth", self._queue.qsize())
        except asyncio.CancelledError:
            pending_lines = self.drain_pending_main_queue_lines()
            if pending_lines:
                await _enqueue_fail_safe_lines(pending_lines)
            raise

    async def _fail_safe_writer_loop(self) -> None:
        try:
            while True:
                first_item = await self._fail_safe_queue.get()
                _record_metric_set("fail_safe_queue_depth", self._fail_safe_queue.qsize())
                if first_item is None:
                    self._fail_safe_queue.task_done()
                    break

                batch = [first_item]
                flush_deadline = time.monotonic() + self._max_flush_interval_s
                while True:
                    if len(batch) >= self._max_batch_size:
                        break
                    try:
                        queued = self._fail_safe_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        remaining_s = flush_deadline - time.monotonic()
                        if remaining_s <= 0:
                            break
                        try:
                            queued = await asyncio.wait_for(self._fail_safe_queue.get(), timeout=remaining_s)
                            _record_metric_set("fail_safe_queue_depth", self._fail_safe_queue.qsize())
                        except asyncio.TimeoutError:
                            break
                    if queued is None:
                        self._fail_safe_queue.task_done()
                        self._fail_safe_queue.put_nowait(None)
                        break
                    batch.append(queued)

                try:
                    await _enqueue_fail_safe_lines(batch)
                except asyncio.CancelledError:
                    await _enqueue_fail_safe_lines(batch)
                    raise
                except Exception as exc:
                    warnings.warn(f"Fail-safe writer loop failed to flush batch. ({exc})")
                    await _enqueue_fail_safe_lines(batch)
                finally:
                    for _ in batch:
                        self._fail_safe_queue.task_done()
                    _record_metric_set("fail_safe_queue_depth", self._fail_safe_queue.qsize())
        except asyncio.CancelledError:
            pending_lines = self.drain_pending_fail_safe_queue_lines()
            if pending_lines:
                await _enqueue_fail_safe_lines(pending_lines)
            raise

    async def stop(self) -> None:
        if not self._running:
            return
        if (self._task is not None and self._task.done()) or (
            self._fail_safe_task is not None and self._fail_safe_task.done()
        ):
            await self._handle_unexpected_task_exit()
            self._running = False
            return
        await self._queue.join()
        await self._fail_safe_queue.join()
        await self._queue.put(None)
        await self._fail_safe_queue.put(None)
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pending_lines = self.drain_pending_main_queue_lines()
                if pending_lines:
                    await _enqueue_fail_safe_lines(pending_lines)
        if self._fail_safe_task is not None:
            try:
                await self._fail_safe_task
            except asyncio.CancelledError:
                pending_lines = self.drain_pending_fail_safe_queue_lines()
                if pending_lines:
                    await _enqueue_fail_safe_lines(pending_lines)
        self._task = None
        self._fail_safe_task = None
        self._running = False

    async def _handle_unexpected_task_exit(self) -> None:
        pending_lines = self.drain_pending_fail_safe_lines()
        if pending_lines:
            await _enqueue_fail_safe_lines(pending_lines)
        tasks_to_cancel: list[asyncio.Task[None]] = []
        if self._task is not None and not self._task.done():
            self._task.cancel()
            tasks_to_cancel.append(self._task)
        if self._fail_safe_task is not None and not self._fail_safe_task.done():
            self._fail_safe_task.cancel()
            tasks_to_cancel.append(self._fail_safe_task)
        for task in tasks_to_cancel:
            try:
                await task
            except asyncio.CancelledError:
                continue
        for task, label in ((self._task, "Event writer task"), (self._fail_safe_task, "Fail-safe writer task")):
            if task is None or not task.done():
                continue
            try:
                task_error = task.exception()
            except asyncio.CancelledError:
                task_error = None
            if task_error is not None:
                warnings.warn(f"{label} exited unexpectedly; restarting writer loop. ({task_error})")
        self._task = None
        self._fail_safe_task = None
        self._running = False

    def drain_pending_main_queue_lines(self) -> list[str]:
        pending_lines: list[str] = []
        while True:
            try:
                queued = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if queued is None:
                self._queue.task_done()
                continue
            pending_lines.append(_to_fail_safe_line_sync(queued, "writer_error"))
            self._queue.task_done()
        _record_metric_set("queue_depth", self._queue.qsize())
        return pending_lines

    def drain_pending_fail_safe_queue_lines(self) -> list[str]:
        pending_lines: list[str] = []
        while True:
            try:
                queued = self._fail_safe_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if queued is None:
                self._fail_safe_queue.task_done()
                continue
            pending_lines.append(queued)
            self._fail_safe_queue.task_done()
        _record_metric_set("fail_safe_queue_depth", self._fail_safe_queue.qsize())
        return pending_lines

    def drain_pending_fail_safe_lines(self) -> list[str]:
        pending_lines = self.drain_pending_main_queue_lines()
        pending_lines.extend(self.drain_pending_fail_safe_queue_lines())
        return pending_lines


_EVENT_WRITER: AsyncEventWriter | None = None
_EVENT_WRITERS: dict[asyncio.AbstractEventLoop, AsyncEventWriter] = {}


async def _get_event_writer() -> AsyncEventWriter:
    current_loop = asyncio.get_running_loop()
    writer = _EVENT_WRITERS.get(current_loop)
    if writer is None:
        writer = AsyncEventWriter(loop=current_loop)
        _EVENT_WRITERS[current_loop] = writer
    global _EVENT_WRITER
    _EVENT_WRITER = writer
    return writer


async def start_event_writer(loop: asyncio.AbstractEventLoop | None = None) -> AsyncEventWriter:
    """Start (or reuse) the async durable writer loop for the target event loop."""
    target_loop = loop or asyncio.get_running_loop()
    writer = _EVENT_WRITERS.get(target_loop)
    if writer is None:
        writer = AsyncEventWriter(loop=target_loop)
        _EVENT_WRITERS[target_loop] = writer
    await writer.start()
    return writer


async def stop_event_writer(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Stop writer loop after draining accepted queue items; spill unwritten to fail-safe."""
    target_loop = loop or asyncio.get_running_loop()
    writer = _EVENT_WRITERS.pop(target_loop, None)
    if writer is None:
        return
    await writer.stop()


async def append_event(event_type: str, payload: Any) -> None:
    payload_with_telemetry: Any
    writer = _EVENT_WRITER
    telemetry = _event_writer_metrics_snapshot(queue_depth=writer.queue_depth if writer is not None else 0)
    if isinstance(payload, dict):
        payload_with_telemetry = dict(payload)
        payload_with_telemetry["_telemetry"] = telemetry
    else:
        payload_with_telemetry = {
            "value": payload,
            "_telemetry": telemetry,
        }

    event: dict[str, Any] = {
        "timestamp": utc_now_iso(timespec="seconds"),
        "type": event_type,
        "payload": payload_with_telemetry,
    }
    cycle_index = None
    if isinstance(payload_with_telemetry, dict):
        cycle_index = payload_with_telemetry.get("cycle_index")
    if cycle_index is not None:
        event["cycle_index"] = cycle_index

    writer = await _get_event_writer()
    try:
        await writer.append_event(event=event)
    except Exception:
        await _append_fail_safe_event(
            event=event,
            reason="writer_error",
            original_event_type=event_type,
        )


async def shutdown_event_writer() -> None:
    """Stop all active writer loops and wait for full queue drain before shutdown.

    Lifecycle contract:
    - `stop_event_writer` awaits queue drain and flushes accepted events to disk.
    - any unwritten accepted entries are moved to fail-safe `events.queue`.
    - `drain_fail_safe_events` can be called afterwards to replay durable fail-safe data.
    """
    loops = list(_EVENT_WRITERS.keys())
    for writer_loop in loops:
        await stop_event_writer(loop=writer_loop)
    global _EVENT_WRITER
    _EVENT_WRITER = None


async def drain_fail_safe_events() -> None:
    replay_paths = await _claim_fail_safe_replay_files()
    if not replay_paths:
        return

    pending_lines = await _read_replay_lines(replay_paths)
    if not pending_lines:
        for replay_path in replay_paths:
            await _unlink(replay_path)
        return

    valid_lines_to_write: list[str] = []
    for line in pending_lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            warnings.warn(f"Discarding fail-safe replay line (reason=parse_error): {exc}")
            continue
        valid_lines_to_write.append(json.dumps(event, ensure_ascii=False) + "\n")

    unwritten_valid_lines = await _write_lines_to_events(valid_lines_to_write)
    if unwritten_valid_lines:
        await _enqueue_fail_safe_lines(unwritten_valid_lines)
    for replay_path in replay_paths:
        await _unlink(replay_path)


async def _claim_fail_safe_replay_files() -> list[Path]:
    replay_paths: list[Path] = []
    exists = await async_fs.exists(FAIL_SAFE_QUEUE_DIR)
    if exists:
        queue_paths = sorted(await async_fs.glob(FAIL_SAFE_QUEUE_DIR, "*.jsonl"))
        for queue_path in queue_paths:
            replay_path = queue_path.with_name(f"{queue_path.name}.replay.{uuid4().hex}")
            try:
                await _replace(queue_path, replay_path)
                replay_paths.append(replay_path)
            except FileNotFoundError:
                continue
    return replay_paths


async def _read_replay_lines(replay_paths: list[Path]) -> list[str]:
    pending_lines: list[str] = []
    for replay_path in replay_paths:
        content = await _read_text(replay_path)
        pending_lines.extend(line for line in content.splitlines() if line.strip())
    return pending_lines
