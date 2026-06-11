from __future__ import annotations
from crewai import Agent, LLM
from utils import SessionState
from agents import build_role_llm

def make_risk_analyzer(llm: LLM, max_iter: int = 10) -> Agent:
    return Agent(
        role="RiskAnalyzer", goal="Проанализировать собранные данные и классифицировать риски.",
        backstory="КЛАССИФИЦИРУЙ каждый объект: SAFE, WARN, DANGER. Опирайся на size_mb, category, risk_hint и путь.",
        llm=llm, tools=[],
        max_iter=max_iter, allow_delegation=False
    )

def make_cleaner_analyzer(state: SessionState) -> Agent:
    # Сбор данных вынесен из LLM: сканеры вызываются напрямую в cleaner_flow.
    # Здесь остаётся единственная LLM-роль — аналитик рисков (минус одна модель в VRAM).
    return make_risk_analyzer(build_role_llm(state, "architect"), state.ui_max_iter)
