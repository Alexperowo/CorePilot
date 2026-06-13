from __future__ import annotations
import json
import logging
import re
from typing import Optional
from crewai import Agent, Crew, LLM, Task
from utils import SessionState

logger = logging.getLogger("MANAGER_AGENTS")

_PO_BACKSTORY = """Ты — Product Owner. Изучи проект и сформулируй концепцию. Не пиши код. Не пиши JSON. Без тегов <think>."""
_SM_BACKSTORY = """Ты — Scrum Master. Преврати концепцию в ОРИЕНТИРОВАННЫЙ АЦИКЛИЧЕСКИЙ ГРАФ задач (DAG).
Верни ТОЛЬКО валидный JSON-массив. Поля каждой задачи:
- task_id: уникальный строковый идентификатор (например "T1", "T2", ...);
- title: краткое название;
- description: что сделать;
- target_files: список файлов;
- context_notes: важные заметки;
- depends_on: список task_id задач, которые ОБЯЗАНЫ быть выполнены ДО этой (родители).
ПРАВИЛА ГРАФА:
1. Если задача требует результат другой — укажи её id в depends_on. Иначе depends_on = [].
2. Базовые/инфраструктурные задачи идут первыми и не зависят ни от чего.
3. Не создавай циклов (A зависит от B, B от A — запрещено).
4. Ссылайся только на существующие task_id.
Пример: задача "написать тесты" зависит от задачи "реализовать модуль"."""

def make_manager_crew(state: SessionState, llm: LLM) -> tuple[Agent, Agent]:
    import tools as toolset
    tools = [toolset.get_project_tree, toolset.read_file_content]
    if getattr(state, "web_search_enabled", False):
        tools.append(toolset.web_search)
        tools.append(toolset.read_web_page)
    po = Agent(
        role="Product Owner", goal="Сформировать детальную концепцию.",
        backstory=_PO_BACKSTORY, llm=llm, tools=tools,
        max_iter=getattr(state, "ui_max_iter", 8), allow_delegation=False
    )
    sm = Agent(
        role="Scrum Master", goal="Преобразовать концепцию в строгий JSON-бэклог.",
        backstory=_SM_BACKSTORY, llm=llm, tools=[], max_iter=3, allow_delegation=False
    )
    return po, sm

def parse_backlog(raw: str) -> Optional[list[dict]]:
    from utils import extract_agent_reasoning
    raw, _ = extract_agent_reasoning(raw)
    cln = re.sub(r"```(json)?\s*", "", raw, flags=re.I).strip()
    cln = re.sub(r"```", "", cln).strip()
    if not (m := re.search(r"(\[.*\])", cln, re.DOTALL)): return None
    try: data = json.loads(m.group(1))
    except json.JSONDecodeError: return None
    if not isinstance(data, list) or not data: return None

    val = []
    for i, it in enumerate(data):
        if not isinstance(it, dict): continue
        # task_id нормализуем в СТРОКУ — единый тип для сопоставления зависимостей.
        it["task_id"] = str(it.get("task_id", i + 1))
        it.setdefault("title", f"Задача {it['task_id']}")
        it.setdefault("description", "")
        it.setdefault("target_files", [])
        it.setdefault("context_notes", "")
        if not isinstance(it["target_files"], list): it["target_files"] = [str(it["target_files"])]
        # depends_on: список ID родительских задач (строки). По умолчанию — пусто.
        dep = it.get("depends_on", [])
        if isinstance(dep, (str, int)): dep = [dep]
        it["depends_on"] = [str(d) for d in dep if str(d).strip()] if isinstance(dep, list) else []
        val.append(it)

    # Валидация графа: убираем ссылки на несуществующие задачи и самоссылки,
    # затем разрываем циклы (DAG обязан быть ацикличным).
    known = {it["task_id"] for it in val}
    for it in val:
        it["depends_on"] = [d for d in it["depends_on"] if d in known and d != it["task_id"]]
    _break_cycles(val)
    return val if val else None


def _break_cycles(tasks: list[dict]) -> None:
    """Удаляет рёбра, образующие циклы, оставляя корректный DAG (на случай, если
    модель выдала циклическую зависимость). Изменяет depends_on на месте."""
    by_id = {t["task_id"]: t for t in tasks}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {t["task_id"]: WHITE for t in tasks}

    def visit(tid: str):
        color[tid] = GRAY
        kept = []
        for dep in by_id[tid]["depends_on"]:
            c = color.get(dep, BLACK)
            if c == GRAY:
                continue  # ребро замыкает цикл — отбрасываем
            if c == WHITE:
                visit(dep)
            kept.append(dep)
        by_id[tid]["depends_on"] = kept
        color[tid] = BLACK

    for t in tasks:
        if color[t["task_id"]] == WHITE:
            visit(t["task_id"])
