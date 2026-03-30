import asyncio
from dataclasses import dataclass, field
import json
import os
from typing import Any, Dict, List

from sif.core.kernel import Kernel
from sif.core.openai_client import OpenAIClient
from sif.core.time_utils import utc_now_iso


@dataclass
class LLMOrchestrator:
    """Coordinates model requests for the autonomy loop.

    Error contract for ``request_response``:
    - Offline mode (no OPENAI_API_KEY): a deterministic fallback payload is returned.
    - Network mode failures: a structured error dict is returned and written to ``llm_last_error``.
    """

    memory_key_requests: str = 'llm_requests'
    memory_key_responses: str = 'llm_responses'
    memory_key_status: str = 'llm_status'
    supported_tasks: List[str] = field(default_factory=lambda: ['plan', 'evaluate', 'reflect', 'self_evolve', 'code_changes'])
    _client: OpenAIClient | None = field(default=None, init=False, repr=False)
    _lifecycle_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def start(self) -> None:
        api_key = os.getenv('OPENAI_API_KEY')
        if not api_key:
            async with self._lifecycle_lock:
                client = self._client
                self._client = None
            if client is not None:
                await client.aclose()
            return
        async with self._lifecycle_lock:
            if self._client is None:
                self._client = OpenAIClient(api_key=api_key)
            await self._client.start()

    async def aclose(self) -> None:
        async with self._lifecycle_lock:
            client = self._client
            self._client = None
            if client is not None:
                await client.aclose()

    async def __aenter__(self) -> 'LLMOrchestrator':
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    @staticmethod
    def build_self_evolution_directive() -> Dict[str, Any]:
        return {
            'role': 'bounded_autonomy_controller',
            'model': 'gpt-5.2',
            'identity': 'You are the model-side controller for a persistent self-improvement runtime.',
            'runtime_contract': (
                'The system runs repeated improvement cycles, keeps durable memory, and may propose '
                'bounded state or code changes inside an explicit policy boundary.'
            ),
            'operating_rules': [
                'Respect goals, constraints, rollback readiness, and observability requirements.',
                'Prefer small, testable, reversible changes over broad speculative rewrites.',
                'Return structured JSON only for each task.',
                'Do not claim capabilities not evidenced by the payload.',
                'When uncertainty is high, preserve system stability and reduce scope.',
            ],
            'output_contract': 'Return machine-readable JSON that matches the requested task schema.',
        }

    async def request_response(self, kernel: Kernel, task: str, payload: Dict[str, Any]) -> Dict[str, Any] | None:
        if task not in self.supported_tasks:
            refusal = self._unsupported_task_response(task)
            kernel.update_memory('llm_last_error', json.dumps(refusal, ensure_ascii=False))
            return refusal
        api_key = os.getenv('OPENAI_API_KEY')
        if not api_key:
            fallback = self.build_fallback(task, payload)
            self._store_response(kernel, task, fallback)
            return fallback
        directive = self.build_self_evolution_directive()
        message_payload = json.dumps(payload, ensure_ascii=False)
        if self._client is not None:
            try:
                response = await self._client.responses_json(
                    model=directive.get('model', 'gpt-5.2'),
                    system_prompt=json.dumps(directive, ensure_ascii=False),
                    user_prompt=message_payload,
                )
            except asyncio.CancelledError:
                await self.aclose()
                raise
        else:
            error_payload = {
                'type': 'llm_client_not_initialized',
                'message': 'LLM client is not initialized. Call start() before request_response() in network mode.',
                'task': task,
            }
            kernel.update_memory('llm_last_error', json.dumps(error_payload, ensure_ascii=False))
            return error_payload
        if not response.ok or response.text is None:
            error_payload = {
                'type': response.error_type or 'unknown_error',
                'message': response.error_message or 'Unknown LLM request error.',
                'task': task,
            }
            kernel.update_memory('llm_last_error', json.dumps(error_payload, ensure_ascii=False))
            return error_payload
        parsed = self._parse_json_response(response.text)
        if parsed is None:
            error_payload = {
                'type': 'invalid_json_response',
                'message': 'LLM returned a non-JSON payload that failed to parse.',
                'task': task,
            }
            kernel.update_memory('llm_last_error', json.dumps(error_payload, ensure_ascii=False))
            return error_payload
        self._store_response(kernel, task, parsed)
        return parsed

    def queue_request(self, kernel: Kernel, task: str, payload: Dict[str, Any]) -> Dict[str, Any] | None:
        if task not in self.supported_tasks:
            refusal = self._unsupported_task_response(task)
            kernel.update_memory('llm_last_error', json.dumps(refusal, ensure_ascii=False))
            return refusal
        requests = self._load_json_list(kernel.state.memory.get(self.memory_key_requests))
        requests.append({'task': task, 'timestamp': utc_now_iso(timespec='seconds'), 'payload': payload})
        kernel.update_memory(self.memory_key_requests, json.dumps(requests, ensure_ascii=False))
        kernel.update_memory(self.memory_key_status, 'awaiting_response')
        return None

    def load_response(self, kernel: Kernel, task: str) -> Dict[str, Any] | None:
        responses = self._load_json_dict(kernel.state.memory.get(self.memory_key_responses))
        response = responses.get(task)
        return response if isinstance(response, dict) else None

    def build_fallback(self, task: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if task == 'plan':
            return {'plan': self._fallback_plan(payload)}
        if task == 'evaluate':
            return {'evaluation': self._fallback_evaluation(payload)}
        if task == 'reflect':
            return {'reflection': self._fallback_reflection(payload)}
        if task == 'code_changes':
            return self._fallback_code_changes(payload)
        return {'notes': 'No fallback available.'}

    def _unsupported_task_response(self, task: str) -> Dict[str, Any]:
        return {'type': 'unsupported_task', 'message': f"Unsupported task '{task}'", 'task': task, 'supported_tasks': list(self.supported_tasks)}

    @staticmethod
    def _fallback_plan(payload: Dict[str, Any]) -> List[str]:
        observations = payload.get('observations', {}) if isinstance(payload, dict) else {}
        goals = observations.get('goals', 'unspecified goals')
        constraints = observations.get('constraints', 'unspecified constraints')
        internal_constraints = observations.get('internal_constraints', 'unknown')
        external_constraints = observations.get('external_constraints', 'unknown')
        return [
            f'Review observations (goals: {goals}; constraints: {constraints}).',
            f'Identify risks and gaps given internal={internal_constraints}, external={external_constraints}.',
            'Prioritize bounded improvements that preserve rollback readiness.',
            'Select one or two near-term experiments for the next cycle.',
            'Define evaluation signals and logging for the next iteration.',
        ]

    @staticmethod
    def _fallback_evaluation(payload: Dict[str, Any]) -> Dict[str, Any]:
        fallback = payload.get('fallback') if isinstance(payload, dict) else None
        evaluation = dict(fallback) if isinstance(fallback, dict) else {}
        evaluation.setdefault('alignment', 'unknown')
        evaluation.setdefault('coverage', 'partial')
        evaluation.setdefault('notes', 'Fallback evaluation generated due to missing LLM response.')
        evaluation.setdefault('errors', [])
        evaluation.setdefault('metrics', {'source': 'fallback', 'signals_present': False})
        return evaluation

    @staticmethod
    def _fallback_reflection(payload: Dict[str, Any]) -> Dict[str, Any]:
        observations = payload.get('observations', {}) if isinstance(payload, dict) else {}
        evaluation = payload.get('evaluation') if isinstance(payload, dict) else {}
        reflection_summary = payload.get('reflection_summary') if isinstance(payload, dict) else None
        summary = reflection_summary or 'Fallback reflection generated.'
        opportunities = payload.get('opportunities') if isinstance(payload, dict) else None
        if opportunities is None and isinstance(payload, dict):
            opportunities = payload.get('freedoms')
        if not isinstance(opportunities, list):
            opportunities = [
                'Reframe at least one internal constraint into an explicit opportunity.',
                'Capture missing signals from the current observations.',
            ]
        assumptions = payload.get('assumptions') if isinstance(payload, dict) else None
        if not isinstance(assumptions, list):
            assumptions = [
                'Assumption: current observations capture all relevant constraints.',
                'Assumption: evaluation reflects system health accurately enough for the next cycle.',
            ]
        dod = payload.get('dod') if isinstance(payload, dict) else None
        dod_check = payload.get('dod_check') if isinstance(payload, dict) else None
        if dod_check is None:
            dod_check = {
                'signals': {},
                'rollback_triggered': bool(evaluation.get('errors')) if isinstance(evaluation, dict) else False,
                'notes': 'Fallback DoD check generated.',
            }
        return {
            'summary': summary,
            'opportunities': opportunities,
            'assumptions': assumptions,
            'ignored_directives': [],
            'dod': dod,
            'dod_check': dod_check,
            'observations': observations,
        }

    @staticmethod
    def _fallback_code_changes(payload: Dict[str, Any]) -> Dict[str, Any]:
        _ = payload
        return {
            'code_changes': [],
            'notes': 'Fallback code_changes used; no changes proposed.',
            'rationale': 'LLM response unavailable; preserve the current code state.',
        }

    def _store_response(self, kernel: Kernel, task: str, parsed: Dict[str, Any]) -> None:
        responses = self._load_json_dict(kernel.state.memory.get(self.memory_key_responses))
        responses[task] = parsed
        kernel.update_memory(self.memory_key_responses, json.dumps(responses, ensure_ascii=False))
        kernel.update_memory(self.memory_key_status, 'response_received')

    @staticmethod
    def _parse_json_response(response_text: str) -> Dict[str, Any] | None:
        try:
            parsed = json.loads(response_text)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _load_json_list(raw: str | None) -> List[Dict[str, Any]]:
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []

    @staticmethod
    def _load_json_dict(raw: str | None) -> Dict[str, Any]:
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}
