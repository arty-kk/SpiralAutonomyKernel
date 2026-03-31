# SPDX-FileCopyrightText: 2026 Сацук Артём Венедиктович (Satsuk Artem)
# SPDX-License-Identifier: Apache-2.0

import inspect
import re

from sif.core.kernel import Kernel, KernelState
from sif.core.llm import LLMOrchestrator
from sif.core.spiral_engine import SpiralEngine


def test_llm_directive_is_grounded() -> None:
    directive = LLMOrchestrator.build_self_evolution_directive()
    assert directive['role'] == 'bounded_autonomy_controller'
    assert 'policy boundary' in directive['runtime_contract']
    assert directive['output_contract'].startswith('Return machine-readable JSON')


def test_llm_fallback_plan_and_response_storage() -> None:
    kernel = Kernel(state=KernelState(goals=['g'], constraints=['c'], memory={}))
    llm = LLMOrchestrator()
    response = llm.build_fallback('plan', {'observations': {'goals': 'g', 'constraints': 'c'}})
    assert 'plan' in response
    assert isinstance(response['plan'], list)
    llm._store_response(kernel, 'plan', response)
    assert llm.load_response(kernel, 'plan') == response


def test_llm_supported_tasks_match_fallback_handlers() -> None:
    llm = LLMOrchestrator()
    assert 'self_evolve' not in llm.supported_tasks
    for task in llm.supported_tasks:
        payload = llm.build_fallback(task, {})
        assert isinstance(payload, dict)


def test_llm_supported_tasks_have_spiral_engine_call_sites() -> None:
    llm = LLMOrchestrator()
    source = inspect.getsource(SpiralEngine)
    used_tasks = set(re.findall(r'_run_llm_request_limited\("([a-z_]+)"', source))
    assert set(llm.supported_tasks) == used_tasks
