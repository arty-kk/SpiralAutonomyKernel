# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import asyncio
from dataclasses import dataclass, field
import time
from typing import Any, Awaitable, Callable, TypeAlias

from sif.core.events import append_event


JsonPrimitive: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]
ToolHandler: TypeAlias = Callable[[JsonObject], Awaitable[JsonValue]]


def _validate_json_serializable(value: JsonValue | JsonObject, label: str) -> None:
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Tool {label} must be JSON-serializable.") from exc


def _is_async_callable(handler: ToolHandler) -> bool:
    if asyncio.iscoroutinefunction(handler):
        return True
    return asyncio.iscoroutinefunction(getattr(handler, "__call__", None))


@dataclass
class ToolCall:
    name: str
    args: JsonObject
    requested_at: float = field(default_factory=time.time)


@dataclass
class ToolResult:
    name: str
    ok: bool
    output: Any = None
    error: str | None = None
    duration_sec: float | None = None


@dataclass
class ToolPolicy:
    enabled_tools: dict[str, str] = field(default_factory=dict)
    max_calls_per_cycle: int = 0
    max_runtime_sec: float = 0.0

    def is_enabled(self, tool_name: str) -> bool:
        return tool_name in self.enabled_tools


@dataclass
class ToolManager:
    policy: ToolPolicy = field(default_factory=ToolPolicy)
    tools: dict[str, ToolHandler] = field(default_factory=dict)
    call_count: int = 0

    def register_tool(self, name: str, handler: ToolHandler) -> None:
        if not _is_async_callable(handler):
            raise ValueError(
                f"Tool '{name}' handler must be an async callable accepting JsonObject and returning JsonValue."
            )
        self.tools[name] = handler

    def reset_cycle(self) -> None:
        self.call_count = 0

    async def _execute_handler(self, handler: ToolHandler, args: JsonObject) -> JsonValue:
        return await handler(args)

    async def call_tool(self, call: ToolCall, cycle_index: int | None = None) -> ToolResult:
        if not self.policy.is_enabled(call.name):
            await append_event(
                "tool_denied",
                {"cycle_index": cycle_index, "tool": call.name, "reason": "disabled"},
            )
            return ToolResult(
                name=call.name,
                ok=False,
                error="Tool is disabled by policy.",
            )
        if (
            self.policy.max_calls_per_cycle
            and self.call_count >= self.policy.max_calls_per_cycle
        ):
            await append_event(
                "tool_denied",
                {
                    "cycle_index": cycle_index,
                    "tool": call.name,
                    "reason": "budget_exhausted",
                },
            )
            return ToolResult(
                name=call.name,
                ok=False,
                error="Tool call budget exhausted.",
            )
        handler = self.tools.get(call.name)
        if handler is None:
            await append_event(
                "tool_denied",
                {
                    "cycle_index": cycle_index,
                    "tool": call.name,
                    "reason": "not_registered",
                },
            )
            return ToolResult(
                name=call.name,
                ok=False,
                error="Tool not registered.",
            )
        self.call_count += 1
        start = time.monotonic()
        try:
            _validate_json_serializable(call.args, "call args")
            execution = self._execute_handler(handler, call.args)
            if self.policy.max_runtime_sec:
                output = await asyncio.wait_for(
                    execution,
                    timeout=self.policy.max_runtime_sec,
                )
            else:
                output = await execution
            _validate_json_serializable(output, "output")
        except asyncio.TimeoutError:
            duration = time.monotonic() - start
            await append_event(
                "tool_denied",
                {
                    "cycle_index": cycle_index,
                    "tool": call.name,
                    "reason": "runtime_exceeded_terminated",
                },
            )
            return ToolResult(
                name=call.name,
                ok=False,
                error="Tool runtime exceeded.",
                output=None,
                duration_sec=duration,
            )
        except asyncio.CancelledError:
            await append_event(
                "tool_cancelled",
                {"cycle_index": cycle_index, "tool": call.name},
            )
            raise
        except Exception as exc:
            duration = time.monotonic() - start
            await append_event(
                "tool_failed",
                {"cycle_index": cycle_index, "tool": call.name, "error": str(exc)},
            )
            return ToolResult(
                name=call.name,
                ok=False,
                error=str(exc),
                duration_sec=duration,
            )
        duration = time.monotonic() - start
        await append_event(
            "tool_executed",
            {"cycle_index": cycle_index, "tool": call.name, "duration_sec": duration},
        )
        return ToolResult(
            name=call.name,
            ok=True,
            output=output,
            duration_sec=duration,
        )
