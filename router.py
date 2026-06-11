from __future__ import annotations

import json
import logging
import re
import threading
from typing import Optional, Tuple

from crewai import Agent, LLM

from agents import build_role_llm
from pipeline_agents import make_pipeline_agents
from utils import SessionState, InteractionHandler, DummyInteractionHandler, save_session

logger = logging.getLogger("ROUTER")

VALID_PROFILES: frozenset[str] = frozenset({
    "Универсальный Senior Developer", "Python / Backend", "JavaScript / TypeScript / Frontend",
    "C++ / Системное программирование", "Java / Kotlin / Android", "Swift / iOS",
    "C# / .NET", "PHP / Web", "Go / Микросервисы",
})
VALID_SPECIALIST_ROLES: frozenset[str] = frozenset({"none", "security", "test"})

_DEFAULT_PROFILE = "Универсальный Senior Developer"
_DEFAULT_SPECIALIST_ROLE = "none"

_CLASSIFIER_SYSTEM = """Ты — роутер задач в AI-конвейере. Выбери профиль и специализацию. Верни только JSON. {"selected_profile": "...", "specialist_role": "...", "reason": "..."}"""

def _build_classifier_prompt(task_text: str) -> str: return f"Задача:\n\n{task_text[:2000]}"

def _parse_classifier_response(raw: str) -> Tuple[str, str]:
    cln = re.sub(r"```(json)?", "", raw, flags=re.I).strip()
    if m := re.search(r"\{.*\}", cln, re.DOTALL):
        try:
            data = json.loads(m.group(0))
            p, r = data.get("selected_profile", _DEFAULT_PROFILE), data.get("specialist_role", _DEFAULT_SPECIALIST_ROLE).lower().strip()
            return p if p in VALID_PROFILES else _DEFAULT_PROFILE, r if r in VALID_SPECIALIST_ROLES else _DEFAULT_SPECIALIST_ROLE
        except json.JSONDecodeError: pass
    return _DEFAULT_PROFILE, _DEFAULT_SPECIALIST_ROLE

def _classify_with_llm(task_text: str, state: SessionState) -> Tuple[str, str]:
    try:
        llm = build_role_llm(state, "gatherer")
    except Exception: return _DEFAULT_PROFILE, _DEFAULT_SPECIALIST_ROLE

    _res, _exc = [(_DEFAULT_PROFILE, _DEFAULT_SPECIALIST_ROLE)], [None]
    
    def _call():
        try:
            resp = llm.call(messages=[{"role": "user", "content": f"{_CLASSIFIER_SYSTEM}\n\n{_build_classifier_prompt(task_text)}"}])
            _res[0] = _parse_classifier_response(resp.content if hasattr(resp, "content") else (resp.choices[0].message.content if hasattr(resp, "choices") else str(resp)))
        except Exception as e: _exc[0] = e

    t = threading.Thread(target=_call, daemon=True)
    t.start()
    t.join(timeout=15.0)
    
    if t.is_alive(): return _DEFAULT_PROFILE, _DEFAULT_SPECIALIST_ROLE
    return _res[0] if not _exc[0] else (_DEFAULT_PROFILE, _DEFAULT_SPECIALIST_ROLE)

def route_task(task_text: str, state: SessionState, ui: InteractionHandler) -> Tuple[Agent, Agent, Agent, Agent]:
    p, r = _classify_with_llm(task_text, state)
    r_arg = None if r == "none" else r
    if p != state.agent_profile:
        state.agent_profile = p
        if not isinstance(ui, DummyInteractionHandler): save_session(state)
    return make_pipeline_agents(state, specialist_role=r_arg)
