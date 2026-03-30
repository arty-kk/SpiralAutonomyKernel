# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import os
import random
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class OpenAIResult:
    """OpenAI client result contract.

    Error type codes are stable for downstream handling:
    - openai_client_not_initialized
    - timeout
    - network_error
    - http_error
    - invalid_json
    - extract_error
    - empty_output
    - unexpected_client_error
    """

    ok: bool
    text: str | None = None
    error_type: str | None = None
    error_message: str | None = None


class OpenAIClient:
    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1") -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._transport_config = self._load_transport_config_from_env()

    @staticmethod
    def _env_int(name: str, default: int) -> int:
        raw = os.getenv(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    @staticmethod
    def _env_float(name: str, default: float) -> float:
        raw = os.getenv(name)
        if raw is None:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    @classmethod
    def _load_transport_config_from_env(cls) -> dict[str, float | int]:
        return {
            "max_connections": cls._env_int("OPENAI_MAX_CONNECTIONS", 100),
            "max_keepalive_connections": cls._env_int("OPENAI_MAX_KEEPALIVE_CONNECTIONS", 20),
            "keepalive_expiry": cls._env_float("OPENAI_KEEPALIVE_EXPIRY", 5.0),
            "timeout_connect": cls._env_float("OPENAI_TIMEOUT_CONNECT", 10.0),
            "timeout_read": cls._env_float("OPENAI_TIMEOUT_READ", 60.0),
            "timeout_write": cls._env_float("OPENAI_TIMEOUT_WRITE", 60.0),
            "timeout_pool": cls._env_float("OPENAI_TIMEOUT_POOL", 10.0),
            "retry_max_attempts": cls._env_int("OPENAI_RETRY_MAX_ATTEMPTS", 3),
            "retry_base_delay": cls._env_float("OPENAI_RETRY_BASE_DELAY", 0.2),
            "retry_max_delay": cls._env_float("OPENAI_RETRY_MAX_DELAY", 2.0),
        }

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._client is None:
                limits = httpx.Limits(
                    max_connections=int(self._transport_config["max_connections"]),
                    max_keepalive_connections=int(self._transport_config["max_keepalive_connections"]),
                    keepalive_expiry=float(self._transport_config["keepalive_expiry"]),
                )
                timeout = httpx.Timeout(
                    connect=float(self._transport_config["timeout_connect"]),
                    read=float(self._transport_config["timeout_read"]),
                    write=float(self._transport_config["timeout_write"]),
                    pool=float(self._transport_config["timeout_pool"]),
                )
                self._client = httpx.AsyncClient(
                    base_url=self.base_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    limits=limits,
                    timeout=timeout,
                )

    async def aclose(self) -> None:
        async with self._lifecycle_lock:
            client = self._client
            self._client = None
            if client is not None:
                await client.aclose()

    async def __aenter__(self) -> "OpenAIClient":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("openai_client_not_initialized")
        return self._client

    async def responses_json(self, model: str, system_prompt: str, user_prompt: str) -> OpenAIResult:
        payload = {
            "model": model,
            "input": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
        }
        try:
            client = self._require_client()
        except RuntimeError as exc:
            return OpenAIResult(ok=False, error_type=str(exc), error_message=str(exc))
        max_attempts = max(int(self._transport_config["retry_max_attempts"]), 1)
        base_delay = max(float(self._transport_config["retry_base_delay"]), 0.0)
        max_delay = max(float(self._transport_config["retry_max_delay"]), base_delay)
        raw: bytes
        for attempt in range(1, max_attempts + 1):
            try:
                resp = await client.post("/responses", json=payload)
                resp.raise_for_status()
                raw = resp.content
                break
            except httpx.HTTPStatusError as exc:
                return OpenAIResult(
                    ok=False,
                    error_type="http_error",
                    error_message=f"{exc.response.status_code}: {exc.response.reason_phrase}",
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                is_timeout = isinstance(exc, httpx.TimeoutException)
                error_type = "timeout" if is_timeout else "network_error"
                if attempt >= max_attempts:
                    return OpenAIResult(ok=False, error_type=error_type, error_message=str(exc))
                backoff_cap = min(base_delay * (2 ** (attempt - 1)), max_delay)
                await asyncio.sleep(random.uniform(0.0, backoff_cap) if backoff_cap > 0 else 0)
            except Exception as exc:
                return OpenAIResult(
                    ok=False,
                    error_type="unexpected_client_error",
                    error_message=str(exc),
                )
        else:
            return OpenAIResult(ok=False, error_type="network_error", error_message="Retry loop exhausted")
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            return OpenAIResult(
                ok=False,
                error_type="invalid_json",
                error_message=str(exc),
            )
        try:
            text = self._extract_text(parsed)
        except Exception as exc:
            return OpenAIResult(
                ok=False,
                error_type="extract_error",
                error_message=str(exc),
            )
        if text is None:
            return OpenAIResult(
                ok=False,
                error_type="empty_output",
                error_message="No output_text content found in response payload.",
            )
        return OpenAIResult(ok=True, text=text)

    @staticmethod
    def _extract_text(payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None

        output = payload.get("output")
        if not isinstance(output, list):
            return None

        for item in output:
            if not isinstance(item, dict):
                continue

            content = item.get("content")
            if not isinstance(content, list):
                continue

            for content_item in content:
                if not isinstance(content_item, dict):
                    continue

                text = content_item.get("text")
                if content_item.get("type") == "output_text" and isinstance(text, str):
                    return text

        return None
