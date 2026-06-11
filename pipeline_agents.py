from __future__ import annotations
from crewai import Agent, LLM
from utils import SessionState
from agents import build_role_llm
import tools as toolset

def make_code_gatherer(llm: LLM, max_iter: int, max_rpm: int | None, spec: str | None, spd: str) -> Agent:
    bs = "Ты CodeGatherer. Верни строго валидный JSON по схеме GathererManifest."
    if spec == 'security': bs += " Ищи уязвимости."
    elif spec == 'test': bs += " Ищи тесты."
    return Agent(role="CodeGatherer", goal="Собрать JSON-манифест. НЕ писать код.", backstory=bs+spd, llm=llm, tools=[toolset.get_project_tree, toolset.read_file_content, toolset.search_code], max_iter=max_iter, max_rpm=max_rpm, allow_delegation=False)

def make_system_architect(llm: LLM, max_iter: int, max_rpm: int | None, spec: str | None, spd: str) -> Agent:
    bs = "Ты SystemArchitect. Верни строго валидный JSON ArchitectPlan. НЕ пиши код."
    return Agent(role="SystemArchitect", goal="Построить JSON-план. НЕ писать код.", backstory=bs+spd, llm=llm, tools=[], max_iter=max(3, max_iter//2), max_rpm=max_rpm, allow_delegation=False)

def make_code_fixer(llm: LLM, max_iter: int, max_rpm: int | None, spec: str | None, spd: str) -> Agent:
    bs = "Ты CodeFixer. Выполни план. Верни валидный JSON FixerOutput с полным кодом изменённых файлов."
    return Agent(role="CodeFixer", goal="Реализовать план. Вернуть JSON с кодом.", backstory=bs+spd, llm=llm, tools=[toolset.read_file_content, toolset.run_terminal_command, toolset.search_code], max_iter=max_iter, max_rpm=max_rpm, allow_delegation=False)

def make_qa_auditor(llm: LLM, max_iter: int, max_rpm: int | None, spec: str | None, spd: str) -> Agent:
    bs = "Ты QAAuditor. Сравни план с патчами. Завершись фразой «Вердикт: ОК» или «Вердикт: ОТКЛОНЕНО»."
    return Agent(role="QAAuditor", goal="Сравнить план с патчами.", backstory=bs+spd, llm=llm, tools=[toolset.read_file_content], max_iter=max(3, max_iter//2), max_rpm=max_rpm, allow_delegation=False)

def make_pipeline_agents(state: SessionState, specialist_role: str | None = None) -> tuple[Agent, Agent, Agent, Agent]:
    spd = "\nРЕЖИМ: СКОРОСТЬ" if state.speed == "fast" else "\nРЕЖИМ: КАЧЕСТВО" if state.speed == "slow" else ""
    from agents import cot_suffix
    def _rl(rn: str):
        return build_role_llm(state, rn)

    mi = state.ui_max_iter
    grpm = None if getattr(state, "mode_gatherer", "local") == "local" else state.ui_max_rpm
    arpm = None if getattr(state, "mode_architect", "local") == "local" else state.ui_max_rpm
    frpm = None if getattr(state, "mode_coder", "local") == "local" else state.ui_max_rpm
    qrpm = None if getattr(state, "mode_auditor", "local") == "local" else state.ui_max_rpm

    # CoT-приставка к backstory локальных ролей (тумблер force_local_reasoning).
    return (make_code_gatherer(_rl("gatherer"), mi, grpm, specialist_role, spd + cot_suffix(state, "gatherer")),
            make_system_architect(_rl("architect"), mi, arpm, specialist_role, spd + cot_suffix(state, "architect")),
            make_code_fixer(_rl("coder"), mi, frpm, specialist_role, spd + cot_suffix(state, "coder")),
            make_qa_auditor(_rl("auditor"), mi, qrpm, specialist_role, spd + cot_suffix(state, "auditor")))
