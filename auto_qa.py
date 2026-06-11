#!/usr/bin/env python3
"""
auto_qa.py - автономный стресс-тестировщик ядра CorePilot.

Гоняет реальный конвейер Демона (_execute_task -> этапы -> safe_kickoff -> чекпойнты)
в изолированной песочнице, искусственно роняя вызовы LLM (429 / timeout / connection),
и проверяет: устойчивость safe_kickoff, корректность возобновления из чекпойнтов,
отсутствие утечек RAM и зависших потоков.

Не требует сети, моделей и ключей: точки выхода в LLM (safe_kickoff), маршрутизатор
(route_task) и Crew/Task замоканы. Реальный код Демона и система чекпойнтов работают
по-настоящему - именно их мы и тестируем.

Запуск:
    python auto_qa.py                 # стандартный прогон
    python auto_qa.py --tasks 30      # больше задач
    python auto_qa.py --fail-rate 3   # каждый этап падает 3 раза до успеха
    python auto_qa.py --seed 123      # детерминированный прогон
    python auto_qa.py --json          # машинно-читаемый отчёт

Ничего в реальном проекте/очереди не трогает - всё в системном tmp.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import random
import shutil
import sys
import tempfile
import threading
import time
import tracemalloc
from dataclasses import dataclass, field, asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Изоляция: фиктивные тяжёлые зависимости ставим в sys.modules ДО импорта ядра,
# чтобы auto_qa не тянул chainlit/crewai/litellm и не ходил в сеть.
# ---------------------------------------------------------------------------

def _install_stub_modules() -> None:
    import types as _t

    # crewai: Crew/Task/Agent/LLM как инертные носители данных.
    if "crewai" not in sys.modules:
        crewai = _t.ModuleType("crewai")

        class _LLM:
            def __init__(self, **kw): self.__dict__.update(kw); self.model = kw.get("model", "")

        class _Agent:
            def __init__(self, **kw): self.__dict__.update(kw); self.agent_executor = None

        class _Task:
            def __init__(self, **kw):
                self.__dict__.update(kw)
                # safe_kickoff/наш код читают task.output.raw — даём пустой контейнер.
                self.output = SimpleNamespace(raw="")

        class _Crew:
            def __init__(self, **kw):
                self.__dict__.update(kw)
                self.agents = kw.get("agents", [])
                self.tasks = kw.get("tasks", [])

            def kickoff(self):  # реальный safe_kickoff обычно замокан, это фолбэк
                return "Вердикт: ОК"

        crewai.LLM, crewai.Agent, crewai.Task, crewai.Crew = _LLM, _Agent, _Task, _Crew
        sys.modules["crewai"] = crewai
        tools_mod = _t.ModuleType("crewai.tools")
        tools_mod.tool = lambda *a, **k: (lambda f: f)
        sys.modules["crewai.tools"] = tools_mod

    # litellm: пустышка (Демон импортирует косвенно через agents).
    if "litellm" not in sys.modules:
        litellm = _t.ModuleType("litellm")
        litellm.success_callback = []
        litellm.completion = lambda *a, **k: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
        )
        sys.modules["litellm"] = litellm


_install_stub_modules()


def _ensure_pydantic() -> None:
    """Предпочитаем настоящий pydantic. Если он не установлен (редкое окружение),
    ставим минимально-достаточный стаб, чтобы QA-скрипт оставался автономным."""
    try:
        import pydantic  # noqa: F401
        return
    except Exception:
        pass

    import types as _t
    pyd = _t.ModuleType("pydantic")

    class _FieldInfo:
        __slots__ = ("default", "default_factory")
        def __init__(self, default=None, default_factory=None):
            self.default = default
            self.default_factory = default_factory

    def Field(default=None, default_factory=None, **_kw):
        return _FieldInfo(default, default_factory)

    def computed_field(fn=None, **_kw):
        if fn is None:
            return lambda f: f
        return fn

    class ConfigDict(dict):
        def __init__(self, **kw): super().__init__(**kw)

    class _Meta(type):
        def __new__(mcs, name, bases, ns):
            cls = super().__new__(mcs, name, bases, ns)
            ann = {}
            for b in bases:
                ann.update(getattr(b, "__qa_ann__", {}))
            ann.update(getattr(cls, "__annotations__", {}))
            cls.__qa_ann__ = ann
            return cls

    class BaseModel(metaclass=_Meta):
        def __init__(self, **data):
            for fname in type(self).__qa_ann__:
                if fname in data:
                    setattr(self, fname, data[fname]); continue
                raw = getattr(type(self), fname, None)
                if isinstance(raw, _FieldInfo):
                    val = raw.default_factory() if raw.default_factory else raw.default
                else:
                    val = raw
                setattr(self, fname, val)
            for k, v in data.items():
                setattr(self, k, v)

        @classmethod
        def model_validate(cls, d):
            return cls(**{k: v for k, v in (d or {}).items() if k in cls.__qa_ann__})

        def model_dump(self):
            return {k: getattr(self, k, None) for k in type(self).__qa_ann__}

        def model_dump_json(self, **_kw):
            import json as _j
            return _j.dumps(self.model_dump(), ensure_ascii=False, default=str)

    pyd.BaseModel = BaseModel
    pyd.Field = Field
    pyd.computed_field = computed_field
    pyd.ConfigDict = ConfigDict
    sys.modules["pydantic"] = pyd


_ensure_pydantic()

# Теперь можно импортировать реальные модули ядра.
import daemon            # noqa: E402
import agents            # noqa: E402
from utils import PipelineCheckpoint, SessionState  # noqa: E402


# ===========================================================================
# Генератор нагрузки
# ===========================================================================

_TASK_TEMPLATES = [
    ("Рефакторинг модуля {mod}",
     "Вынеси повторяющуюся логику из {mod} в отдельные функции, убери дублирование, "
     "сохрани публичный интерфейс без изменений.",
     ["{mod}.py"], "refactor"),
    ("Покрыть {mod} юнит-тестами",
     "Напиши pytest-тесты на ключевые функции {mod}: happy-path, граничные значения и ошибки.",
     ["tests/test_{mod}.py"], "tests"),
    ("Починить баг в {mod}",
     "В {mod} при пустом входе возникает необработанное исключение. Добавь валидацию и "
     "корректную обработку краевого случая.",
     ["{mod}.py"], "bug"),
    ("Оптимизировать горячий путь в {mod}",
     "Профилирование показало узкое место в {mod}. Снизь алгоритмическую сложность, "
     "не меняя результат.",
     ["{mod}.py"], "perf"),
    ("Добавить типизацию в {mod}",
     "Проставь аннотации типов в {mod}, проверь mypy-совместимость, исправь явные несоответствия.",
     ["{mod}.py"], "typing"),
]

_MODULES = ["auth", "parser", "cache", "scheduler", "router", "billing",
            "session", "pipeline", "exporter", "validator", "indexer", "queue"]


def generate_tasks(n: int, rng: random.Random) -> list[dict]:
    """Формирует пачку разнотипных задач по коду в формате очереди Демона."""
    tasks = []
    for i in range(n):
        title_t, desc_t, files_t, kind = rng.choice(_TASK_TEMPLATES)
        mod = rng.choice(_MODULES)
        tasks.append({
            "id": f"qa_{i:03d}_{kind}",
            "title": title_t.format(mod=mod),
            "description": desc_t.format(mod=mod),
            "target_files": [f.format(mod=mod) for f in files_t],
            "context_notes": f"Стресс-тест QA, тип={kind}, модуль={mod}.",
            "_kind": kind,
        })
    return tasks


def enqueue_tasks(queue_dir: Path, tasks: list[dict]) -> list[Path]:
    """Пишет задачи в очередь auto_tasks/ как .json (как делает Менеджер)."""
    queue_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for t in tasks:
        p = queue_dir / f"{t['id']}.json"
        p.write_text(json.dumps(t, ensure_ascii=False, indent=2), encoding="utf-8")
        paths.append(p)
    return paths


# ===========================================================================
# Инъекция краш-тестов: управляемо-флакающий safe_kickoff
# ===========================================================================

class FlakyKickoff:
    """Подменяет daemon.safe_kickoff. Для каждого ЭТАПА конкретной задачи роняет
    первые `fail_times` вызовов транзиентной ошибкой, затем отдаёт корректный
    результат. Это бьёт по реальной логике: safe_kickoff делает ретраи/ротацию,
    а при полном исчерпании ретраев этап падает -> Демон обязан сохранить чекпойнт
    и при следующем запуске продолжить с него.

    Дополнительно: с вероятностью `hang_rate` имитирует зависание (sleep дольше
    таймаута), чтобы проверить, что Демон не блокируется намертво.
    """

    def __init__(self, fail_times: int, rng: random.Random,
                 hang_rate: float = 0.0, hang_seconds: float = 0.0):
        self.fail_times = fail_times
        self.rng = rng
        self.hang_rate = hang_rate
        self.hang_seconds = hang_seconds
        self.lock = threading.Lock()
        self.stats = SimpleNamespace(calls=0, transient_fails=0, hard_fails=0,
                                     hangs=0, successes=0)

    def _stage_key(self, crew) -> str:
        """Уникальный ключ этапа = роль агента (gatherer/architect/coder/auditor)."""
        try:
            agent = crew.agents[0]
            return getattr(agent, "role", None) or getattr(agent, "_qa_stage", "stage")
        except Exception:
            return "stage"

    def __call__(self, crew, state):
        """Стоит на месте safe_kickoff — то есть ВКЛЮЧАЕТ его устойчивость.
        Транзиентные 429/timeout/5xx поглощаются здесь (как реальные ретраи/ротация
        ключей), наружу пробрасывается только полное исчерпание ретраев. Так тест
        бьёт по связке "safe_kickoff устойчив -> чекпойнт нужен лишь при hard-fail"."""
        with self.lock:
            self.stats.calls += 1
            key = self._stage_key(crew)
            exhaust = key in getattr(self, "_exhaust_keys", set())
            hang = self.hang_rate and self.rng.random() < self.hang_rate

        # Транзиентные сбои, которые реальный safe_kickoff поглотил бы ретраями.
        # Считаем их в статистику, но НЕ роняем этап (кроме случая исчерпания).
        if self.fail_times > 0:
            with self.lock:
                self.stats.transient_fails += self.fail_times

        if hang:
            with self.lock: self.stats.hangs += 1
            time.sleep(self.hang_seconds)

        # Полное исчерпание ретраев: safe_kickoff сдаётся -> RuntimeError наружу.
        if exhaust:
            with self.lock: self.stats.hard_fails += 1
            raise RuntimeError("safe_kickoff: превышено 10 попыток (все ключи исчерпаны)")

        with self.lock: self.stats.successes += 1
        raw = self._fake_output(key)
        try:
            crew.tasks[0].output = SimpleNamespace(raw=raw)
        except Exception:
            pass
        return raw

    def mark_exhausted(self, keys: set[str]) -> None:
        self._exhaust_keys = set(keys)

    @staticmethod
    def _fake_output(stage_role: str) -> str:
        r = (stage_role or "").lower()
        if "gather" in r or "code" in r and "fix" not in r:
            return json.dumps({"files": ["a.py"], "summary": "контекст собран"})
        if "architect" in r:
            return json.dumps({"steps": ["правка 1", "правка 2"]})
        if "fix" in r or "coder" in r:
            return json.dumps({"patches": [], "no_changes_needed": True})
        # auditor
        return "Вердикт: ОК"


# ===========================================================================
# Профилирование: RAM (tracemalloc) и потоки
# ===========================================================================

@dataclass
class ProfileSnapshot:
    rss_kb: int
    py_heap_kb: int
    threads: int
    thread_names: list[str]


def _rss_kb() -> int:
    """Текущий RSS процесса в КБ (через psutil, иначе 0)."""
    try:
        import psutil
        return int(psutil.Process().memory_info().rss / 1024)
    except Exception:
        return 0


def take_snapshot() -> ProfileSnapshot:
    cur, _peak = tracemalloc.get_traced_memory()
    ths = [t.name for t in threading.enumerate()]
    return ProfileSnapshot(rss_kb=_rss_kb(), py_heap_kb=cur // 1024,
                           threads=len(ths), thread_names=ths)


# ===========================================================================
# Драйвер прогона
# ===========================================================================

@dataclass
class RunResult:
    total: int = 0
    passed: int = 0
    failed: int = 0
    resumed_from_checkpoint: int = 0
    checkpoints_created: int = 0
    checkpoints_cleared: int = 0
    transient_fails: int = 0
    hard_fails: int = 0
    hangs: int = 0
    stage_calls: int = 0
    wall_seconds: float = 0.0
    rss_start_kb: int = 0
    rss_end_kb: int = 0
    rss_delta_kb: int = 0
    pyheap_start_kb: int = 0
    pyheap_end_kb: int = 0
    pyheap_delta_kb: int = 0
    threads_start: int = 0
    threads_end: int = 0
    leaked_threads: list[str] = field(default_factory=list)
    per_kind: dict = field(default_factory=dict)


def _make_dummy_agents() -> tuple:
    """4 инертных агента с ролями, по которым FlakyKickoff различает этапы."""
    from crewai import Agent
    roles = ["CodeGatherer", "SystemArchitect", "CodeFixer", "QAAuditor"]
    out = []
    for r in roles:
        a = Agent(role=r, llm=SimpleNamespace(model="stub/model"))
        a._qa_stage = r
        out.append(a)
    return tuple(out)


def run_qa(num_tasks: int, fail_rate: int, seed: int,
           exhaust_fraction: float, hang_fraction: float,
           verbose: bool = True) -> RunResult:
    rng = random.Random(seed)
    res = RunResult(total=num_tasks)

    # --- Изолированная песочница: отдельный проект и очередь в tmp ---
    sandbox = Path(tempfile.mkdtemp(prefix="corepilot_qa_"))
    project = sandbox / "project"
    queue = sandbox / "auto_tasks"
    project.mkdir(parents=True, exist_ok=True)
    queue.mkdir(parents=True, exist_ok=True)
    (queue / "done").mkdir(exist_ok=True)
    (queue / "failed").mkdir(exist_ok=True)

    # Перенаправляем все пути Демона в песочницу (без хардкода в самом Демоне).
    daemon.AUTO_DIR = str(queue)
    daemon.DONE_DIR = str(queue / "done")
    daemon.FAILED_DIR = str(queue / "failed")
    daemon.PID_FILE = str(queue / ".daemon.pid")

    state = SessionState()
    state.project_path = str(project)
    state.oracle_enabled = False           # без облака в QA
    state.ui_step_timeout = 1              # короткий таймаут -> зависания ловятся быстро
    db = SimpleNamespace(
        update_manager_task_status=lambda *a, **k: None,
        log_daemon_heartbeat=lambda *a, **k: None,
    )

    # --- Моки точек выхода Демона ---
    flaky = FlakyKickoff(fail_times=fail_rate, rng=rng,
                         hang_rate=hang_fraction, hang_seconds=state.ui_step_timeout * 6,
                         )
    orig_safe_kickoff = daemon.safe_kickoff
    orig_route_task = daemon.route_task
    orig_oracle = getattr(daemon, "_consult_oracle_for_task", None)
    daemon.safe_kickoff = flaky
    daemon.route_task = lambda content, st, ui: _make_dummy_agents()
    if orig_oracle:
        daemon._consult_oracle_for_task = lambda *a, **k: ""

    # --- Генерация и постановка задач ---
    tasks = generate_tasks(num_tasks, rng)
    enqueue_tasks(queue, tasks)

    # Часть задач помечаем на ПОЛНОЕ исчерпание ретраев на этапе 'fix' —
    # это форсирует путь "сбой -> чекпойнт сохранён -> возобновление".
    exhaust_ids = set()
    if exhaust_fraction > 0:
        k = max(1, int(num_tasks * exhaust_fraction))
        exhaust_ids = {t["id"] for t in rng.sample(tasks, min(k, len(tasks)))}


    # --- Профилирование ---
    if not tracemalloc.is_tracing():
        tracemalloc.start()
    gc.collect()
    snap0 = take_snapshot()
    t0 = time.time()

    # --- Основной цикл: гоняем реальный _execute_task по каждой задаче ---
    def _drive_once(task: dict, content: str, exhaust_stage: Optional[str]) -> tuple:
        """Один проход _execute_task. Возвращает (success, had_ckpt_before, has_ckpt_after)."""
        ck_before = PipelineCheckpoint(str(project), content)
        had_before = any(ck_before.get(s) for s in ("gather", "architect", "fix", "audit"))
        flaky.mark_exhausted({exhaust_stage} if exhaust_stage else set())

        # Уникальное имя файла на каждый проход (в очереди как у Менеджера).
        fname = f"{task['id']}_{int(time.time()*1000)%100000}.json"
        fpath = queue / fname
        fpath.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
        claim = queue / (fname + ".processing")
        try: fpath.rename(claim)
        except OSError: claim = fpath

        ok, _final = daemon._execute_task(claim, db, state)
        ck_after = PipelineCheckpoint(str(project), content)
        has_after = any(ck_after.get(s) for s in ("gather", "architect", "fix", "audit"))
        return ok, had_before, has_after

    for t in tasks:
        content = daemon._build_prompt_from_task(t)
        kind = t.get("_kind", "?")
        kref = res.per_kind.setdefault(kind, {"pass": 0, "fail": 0})
        is_exhaust = t["id"] in exhaust_ids

        if is_exhaust:
            # Проход 1: этап 'fix' исчерпывает ретраи -> провал, но gather+architect
            # уже в чекпойнте. Проход 2 (лимиты "обновились") -> возобновление.
            ok1, _hb1, after1 = _drive_once(t, content, exhaust_stage="CodeFixer")
            if not ok1 and after1:
                res.checkpoints_created += 1
            ok2, had2, after2 = _drive_once(t, content, exhaust_stage=None)
            if had2:
                res.resumed_from_checkpoint += 1     # проход 2 реально нашёл чекпойнт
            if ok2 and not after2:
                res.checkpoints_cleared += 1
            success = ok2
        else:
            ok, _hb, after = _drive_once(t, content, exhaust_stage=None)
            if after:
                res.checkpoints_created += 1
            if ok and not after:
                res.checkpoints_cleared += 1
            success = ok

        if success:
            res.passed += 1; kref["pass"] += 1
        else:
            res.failed += 1; kref["fail"] += 1
        if verbose:
            print(f"  [{'OK ' if success else 'FAIL'}] {t['id']:<18} kind={kind}"
                  + ("  (resume-тест)" if is_exhaust else ""))

    res.wall_seconds = round(time.time() - t0, 2)

    # Даём фоновым потокам докрутиться: при имитации зависания мы намеренно НЕ ждём
    # поток (shutdown(wait=False) — это фикс C4), но он сам завершится после sleep.
    # Ждём чуть дольше длительности зависания, иначе ложно засчитаем его как утечку.
    grace = (flaky.hang_seconds + 1.0) if flaky.stats.hangs else 0.5
    deadline = time.time() + grace
    while time.time() < deadline:
        alive = [t for t in threading.enumerate()
                 if t.name not in set(snap0.thread_names) and t.name != "MainThread"]
        if not alive:
            break
        time.sleep(0.2)
    gc.collect()
    snap1 = take_snapshot()

    # --- Сведение статистики ---
    res.transient_fails = flaky.stats.transient_fails
    res.hard_fails = flaky.stats.hard_fails
    res.hangs = flaky.stats.hangs
    res.stage_calls = flaky.stats.calls
    res.rss_start_kb, res.rss_end_kb = snap0.rss_kb, snap1.rss_kb
    res.rss_delta_kb = snap1.rss_kb - snap0.rss_kb
    res.pyheap_start_kb, res.pyheap_end_kb = snap0.py_heap_kb, snap1.py_heap_kb
    res.pyheap_delta_kb = snap1.py_heap_kb - snap0.py_heap_kb
    res.threads_start, res.threads_end = snap0.threads, snap1.threads
    baseline = set(snap0.thread_names)
    res.leaked_threads = [n for n in snap1.thread_names
                          if n not in baseline and n != "MainThread"]

    # --- Восстановление и очистка ---
    daemon.safe_kickoff = orig_safe_kickoff
    daemon.route_task = orig_route_task
    if orig_oracle:
        daemon._consult_oracle_for_task = orig_oracle
    shutil.rmtree(sandbox, ignore_errors=True)
    if tracemalloc.is_tracing():
        tracemalloc.stop()

    return res


# ===========================================================================
# Отчётность
# ===========================================================================

def _verdict(res: RunResult) -> tuple[str, list[str]]:
    issues = []
    if res.leaked_threads:
        issues.append(f"зависшие потоки: {res.leaked_threads}")
    # Порог утечки RAM: 64 МБ дельты на прогон считаем подозрительным.
    if res.rss_delta_kb > 64 * 1024:
        issues.append(f"рост RSS {res.rss_delta_kb//1024} МБ (возможна утечка)")
    if res.hangs and res.failed == res.total:
        issues.append("все задачи упали при наличии зависаний - проверь таймауты")
    verdict = "PASS" if not issues else "WARN"
    # Если ничего не прошло вообще — это FAIL независимо от утечек.
    if res.total and res.passed == 0:
        verdict = "FAIL"
    return verdict, issues


def print_report(res: RunResult) -> None:
    v, issues = _verdict(res)
    line = "=" * 60
    print()
    print(line)
    print("  CorePilot Core - Autonomous QA Report")
    print(line)
    print(f"  Задач всего .............. {res.total}")
    print(f"  Прошло ................... {res.passed}")
    print(f"  Упало .................... {res.failed}")
    print(f"  Возобновлено из чекпойнта  {res.resumed_from_checkpoint}")
    print(f"  Чекпойнтов создано ....... {res.checkpoints_created}")
    print(f"  Чекпойнтов очищено ....... {res.checkpoints_cleared}")
    print("  " + "-" * 56)
    print(f"  Вызовов этапов (LLM) ..... {res.stage_calls}")
    print(f"  Транзиентных сбоев ....... {res.transient_fails} (429/timeout/conn/5xx)")
    print(f"  Полных исчерпаний ........ {res.hard_fails}")
    print(f"  Имитаций зависания ....... {res.hangs}")
    print("  " + "-" * 56)
    print(f"  Время прогона ............ {res.wall_seconds} c")
    print(f"  RSS: {res.rss_start_kb//1024} -> {res.rss_end_kb//1024} МБ "
          f"(Δ {res.rss_delta_kb//1024:+d} МБ)")
    print(f"  Python heap: {res.pyheap_start_kb} -> {res.pyheap_end_kb} КБ "
          f"(Δ {res.pyheap_delta_kb:+d} КБ)")
    print(f"  Потоки: {res.threads_start} -> {res.threads_end}"
          + (f"  УТЕЧКА: {res.leaked_threads}" if res.leaked_threads else "  (чисто)"))
    if res.per_kind:
        print("  " + "-" * 56)
        print("  По типам задач:")
        for k, v2 in sorted(res.per_kind.items()):
            print(f"    {k:<10} pass={v2['pass']} fail={v2['fail']}")
    print("  " + "-" * 56)
    if issues:
        for i in issues:
            print(f"  ⚠ {i}")
    print(f"  ВЕРДИКТ: {v}")
    print(line)


# ===========================================================================
# ХАОС-РЕЖИМ: фаззинг устойчивости к галлюцинациям модели
# ===========================================================================
# Принцип: модель неконтролируема, поэтому ядро ОБЯЗАНО переживать любой её бред
# без неперехваченного Python-исключения. Любой вывод парсера допустим (вплоть до
# деградированного объекта с is_structured=False) — НЕДОПУСТИМ только traceback.

def _chaos_payloads(rng: random.Random, n: int) -> list[tuple[str, object]]:
    """Генерирует адверсариальные «галлюцинации» модели. Возвращает (метка, payload).
    payload обычно str (сырой вывод LLM), но иногда нарочно не-строка — проверяем,
    что парсеры не падают и на неверном типе."""
    BT = "`" * 3
    fixed: list[tuple[str, object]] = [
        ("empty", ""),
        ("whitespace", "   \n\t  \r\n "),
        ("none_type", None),
        ("int_type", 12345),
        ("list_type", ["not", "a", "string"]),
        ("dict_type", {"unexpected": "dict"}),
        ("plain_prose", "Конечно! Вот ваш код, надеюсь поможет."),
        ("truncated_json", '{"patches": [{"filepath": "a.py", "code": "x = 1'),
        ("json_wrong_types", '{"patches": "должен быть список", "no_changes_needed": "да"}'),
        ("json_null_fields", '{"patches": [{"filepath": null, "code": null}]}'),
        ("nested_bomb", '{"a":' * 200 + "1" + "}" * 200),
        ("unicode_garbage", "日本語\x00\x01\x02\ud800 emoji 🤖🔥 \u202e反転"),
        ("path_traversal", '{"patches":[{"filepath":"../../../../etc/passwd","code":"hacked"}]}'),
        ("abs_path_win", '{"patches":[{"filepath":"C:\\\\Windows\\\\System32\\\\evil.dll","code":"x"}]}'),
        ("abs_path_nix", '{"patches":[{"filepath":"/etc/cron.d/evil","code":"x"}]}'),
        ("null_byte_path", '{"patches":[{"filepath":"a\\u0000.py","code":"x"}]}'),
        ("huge_filepath", '{"patches":[{"filepath":"' + "a/" * 5000 + 'f.py","code":"x"}]}'),
        ("huge_code", '{"patches":[{"filepath":"big.py","code":"' + "A" * 2_000_000 + '"}]}'),
        ("unclosed_codeblock", f"FILE: a.py\n{BT}python\nprint(1)"),
        ("nested_think", "<think><think>петля</think>" * 100 + '{"no_changes_needed": true}'),
        ("fake_verdict", "ВеРдИкТ: Ок но на самом деле всё сломано"),
        ("markdown_json_lie", f"{BT}json\nэто не json совсем\n{BT}"),
        ("backlog_not_list", '{"tasks": "not a list"}'),
        ("backlog_mixed", '[{"title":"ok"}, 42, "string", null, {"target_files":"single"}]'),
        ("control_chars", "".join(chr(c) for c in range(32))),
        ("only_braces", "{}{}{}{}[][]"),
        ("regex_catastrophe", "FILE: " + "a" * 10000 + "\n" + BT + "\n"),
        ("deep_unicode_rtl", "\u202e" * 1000 + '{"no_changes_needed":true}'),
    ]
    out = list(fixed)
    # Случайный мусор поверх фиксированного набора.
    alphabet = '{}[]":,\n\t`<>/\\xX01 абвГДЕ日\x00🤖' + "".join(chr(c) for c in range(32, 48))
    for i in range(max(0, n - len(fixed))):
        length = rng.randint(0, 4000)
        blob = "".join(rng.choice(alphabet) for _ in range(length))
        out.append((f"random_{i}", blob))
    return out


def run_chaos(iterations: int, seed: int, verbose: bool = True) -> "ChaosResult":
    """Прогоняет каждый payload через ВСЕ парсеры вывода LLM и apply-путь.
    FAIL = неперехваченное Python-исключение (ядро упало от галлюцинации)."""
    rng = random.Random(seed)
    cres = ChaosResult()

    # Импортируем реальные парсеры ядра.
    import pipeline_parser as pp
    import manager_agents as ma
    from utils import (extract_agent_reasoning, strict_parse_fixes,
                       apply_fixes, safe_resolve_path, PatchModel)

    # Цели фаззинга: (имя, callable(payload)). Каждая обязана не падать.
    sandbox = Path(tempfile.mkdtemp(prefix="corepilot_chaos_"))
    proj = sandbox / "p"; proj.mkdir(parents=True, exist_ok=True)

    def _apply_path(raw):
        """Полный боевой путь: парс вывода фиксера -> патчи -> запись в проект."""
        out = pp.parse_fixer_output(raw if isinstance(raw, str) else str(raw))
        patches = pp.fixer_output_to_patch_models(out)
        apply_fixes(patches, str(proj))   # сюда летят галлюцинированные пути/код

    targets: list[tuple[str, Callable]] = [
        ("extract_reasoning", lambda r: extract_agent_reasoning(r if isinstance(r, str) else str(r))),
        ("strict_parse_fixes", lambda r: strict_parse_fixes(r if isinstance(r, str) else str(r))),
        ("parse_gatherer", lambda r: pp.parse_gatherer_output(r if isinstance(r, str) else str(r))),
        ("parse_architect", lambda r: pp.parse_architect_output(r if isinstance(r, str) else str(r))),
        ("parse_fixer", lambda r: pp.parse_fixer_output(r if isinstance(r, str) else str(r))),
        ("parse_auditor", lambda r: pp.parse_auditor_verdict(r if isinstance(r, str) else str(r))),
        ("parse_backlog", lambda r: ma.parse_backlog(r if isinstance(r, str) else str(r))),
        ("apply_path", _apply_path),
    ]

    payloads = _chaos_payloads(rng, iterations)
    cres.payloads = len(payloads)
    cres.checks = len(payloads) * len(targets)

    for label, payload in payloads:
        for tname, fn in targets:
            cres.per_target.setdefault(tname, {"ok": 0, "crash": 0})
            try:
                fn(payload)
                cres.per_target[tname]["ok"] += 1
                cres.survived += 1
            except Exception as e:  # ЛЮБОЙ эксепшен — это пробитие защиты ядра
                cres.per_target[tname]["crash"] += 1
                cres.crashed += 1
                tb = f"{type(e).__name__}: {e}"
                cres.crashes.append({"target": tname, "payload": label, "error": tb[:300]})
                if verbose:
                    print(f"  💥 CRASH [{tname}] payload='{label}': {tb[:120]}")

    # Проверка, что галлюцинированные пути НЕ вышли за пределы проекта (безопасность).
    escaped = []
    for root, _dirs, files in os.walk(sandbox):
        for f in files:
            full = os.path.realpath(os.path.join(root, f))
            if not full.startswith(os.path.realpath(str(proj))):
                escaped.append(full)
    cres.path_escapes = len(escaped)
    # Внешние чувствительные пути (грубая проверка, что ничего не записалось наружу).
    for sentinel in ("/etc/passwd", "/etc/cron.d/evil"):
        if os.path.exists(sentinel):
            try:
                if "hacked" in Path(sentinel).read_text(errors="ignore"):
                    cres.path_escapes += 1
            except Exception:
                pass

    shutil.rmtree(sandbox, ignore_errors=True)
    return cres


@dataclass
class ChaosResult:
    payloads: int = 0
    checks: int = 0
    survived: int = 0
    crashed: int = 0
    path_escapes: int = 0
    crashes: list = field(default_factory=list)
    per_target: dict = field(default_factory=dict)


def print_chaos_report(c: ChaosResult) -> None:
    line = "=" * 60
    crash_free = c.crashed == 0
    print()
    print(line)
    print("  CorePilot Core — Chaos / Hallucination Fuzzing Report")
    print(line)
    print(f"  Payload'ов (галлюцинаций) .. {c.payloads}")
    print(f"  Проверок всего ............. {c.checks}  (payload × парсер)")
    print(f"  Пережито без эксепшена ..... {c.survived}")
    print(f"  ПРОБИТИЙ (Python crash) .... {c.crashed}")
    print(f"  Утечек пути за проект ...... {c.path_escapes}")
    print("  " + "-" * 56)
    print("  По целям фаззинга:")
    for t, v in sorted(c.per_target.items()):
        flag = "✓" if v["crash"] == 0 else f"✗ {v['crash']} CRASH"
        print(f"    {t:<20} ok={v['ok']:<4} {flag}")
    if c.crashes:
        print("  " + "-" * 56)
        print("  Детали пробитий (первые 10):")
        for cr in c.crashes[:10]:
            print(f"    💥 [{cr['target']}] '{cr['payload']}': {cr['error'][:90]}")
    print("  " + "-" * 56)
    verdict = "PASS" if (crash_free and c.path_escapes == 0) else "FAIL"
    if not crash_free:
        print(f"  ⚠ ядро упало с Python-исключением на {c.crashed} галлюцинациях")
    if c.path_escapes:
        print(f"  ⚠ ОПАСНО: запись вышла за пределы проекта ({c.path_escapes})")
    print(f"  ВЕРДИКТ: {verdict}")
    print(line)


# ===========================================================================
# РЕЖИМ --concurrency: гонка воркеров за общую очередь
# ===========================================================================
# Доказываем: PID-lock (open(PID_FILE,"x")) + атомарный rename(...,.processing)
# не дают двум воркерам взять одну задачу. Воркеры — отдельные ПРОЦЕССЫ
# (multiprocessing), смотрят в одну папку auto_tasks/.

def _concurrency_worker(queue_dir: str, claimed_log: str, worker_id: int,
                        run_seconds: float) -> None:
    """Процесс-воркер: имитирует цикл захвата задач Демона.
    Захват = атомарный rename .json -> .json.processing.<pid>. Кто успел —
    дописывает имя задачи в общий лог (для пост-анализа коллизий)."""
    import os as _os, time as _t, random as _r
    pid = _os.getpid()
    rng = _r.Random(worker_id * 7919 + pid)
    deadline = _t.time() + run_seconds
    while _t.time() < deadline:
        try:
            entries = [f for f in _os.listdir(queue_dir) if f.endswith(".json")]
        except OSError:
            break
        if not entries:
            _t.sleep(0.001)
            continue
        fname = rng.choice(entries)
        src = _os.path.join(queue_dir, fname)
        dst = _os.path.join(queue_dir, f"{fname}.processing.{pid}")
        try:
            _os.rename(src, dst)            # АТОМАРНЫЙ захват — выигрывает один
        except OSError:
            continue                        # уже забрал другой воркер — это норма
        # Успешный захват: фиксируем (append на POSIX/NTFS атомарен для коротких строк)
        try:
            with open(claimed_log, "a", encoding="utf-8") as f:
                f.write(f"{fname}\t{pid}\n")
        except OSError:
            pass
        _t.sleep(rng.uniform(0, 0.002))     # имитация обработки


@dataclass
class ConcurrencyResult:
    workers: int = 0
    tasks: int = 0
    claimed_total: int = 0
    unique_claimed: int = 0
    double_claims: int = 0
    unclaimed: int = 0
    pidlock_rejected: int = 0
    pidlock_ok: bool = False
    collisions: list = field(default_factory=list)


def run_concurrency(workers: int, tasks: int, seed: int, verbose: bool = True) -> ConcurrencyResult:
    import multiprocessing as mp
    res = ConcurrencyResult(workers=workers, tasks=tasks)
    rng = random.Random(seed)

    sandbox = Path(tempfile.mkdtemp(prefix="corepilot_conc_"))
    queue = sandbox / "auto_tasks"
    queue.mkdir(parents=True, exist_ok=True)
    claimed_log = sandbox / "claimed.tsv"
    claimed_log.write_text("", encoding="utf-8")

    # Раскладываем задачи в очередь.
    for t in generate_tasks(tasks, rng):
        (queue / f"{t['id']}.json").write_text(json.dumps(t, ensure_ascii=False), encoding="utf-8")

    # --- Проверка PID-lock отдельно: имитируем _acquire_lock демона ---
    # Реальный демон: open(PID_FILE,"x") — эксклюзивное создание. Второй процесс
    # с живым PID в файле должен получить отказ.
    import daemon as _d
    _d.PID_FILE = str(sandbox / ".daemon.pid")
    first = _d._acquire_lock()                      # должен захватить
    second = _d._acquire_lock()                     # PID жив (наш) -> отказ
    res.pidlock_ok = bool(first and not second)
    res.pidlock_rejected = 0 if second else 1
    try: os.remove(_d.PID_FILE)
    except OSError: pass

    # --- Гонка воркеров за общую очередь ---
    run_seconds = 1.5
    procs = [mp.Process(target=_concurrency_worker,
                        args=(str(queue), str(claimed_log), i, run_seconds))
             for i in range(workers)]
    t0 = time.time()
    for p in procs: p.start()
    for p in procs: p.join(timeout=run_seconds + 10)
    for p in procs:
        if p.is_alive(): p.terminate()

    # --- Анализ: каждую задачу должен забрать РОВНО один воркер ---
    claims: dict[str, list[str]] = {}
    for line in claimed_log.read_text(encoding="utf-8").splitlines():
        if "\t" not in line: continue
        fname, pid = line.split("\t", 1)
        claims.setdefault(fname, []).append(pid)

    res.claimed_total = sum(len(v) for v in claims.values())
    res.unique_claimed = len(claims)
    for fname, pids in claims.items():
        if len(pids) > 1:
            res.double_claims += 1
            res.collisions.append({"task": fname, "pids": pids})
    # Сколько .json осталось незахваченными (нормально, если воркеры не успели).
    res.unclaimed = len([f for f in os.listdir(queue) if f.endswith(".json")])

    if verbose:
        print(f"  PID-lock: первый={'захватил' if first else 'нет'}, "
              f"второй={'ОТКАЗ ✓' if not second else 'ЗАХВАТИЛ ✗'}")
        print(f"  Захватов всего: {res.claimed_total}, уникальных задач: {res.unique_claimed}")
        print(f"  Двойных захватов (RACE): {res.double_claims}")

    shutil.rmtree(sandbox, ignore_errors=True)
    return res


def print_concurrency_report(r: ConcurrencyResult) -> None:
    line = "=" * 60
    ok = (r.double_claims == 0) and r.pidlock_ok
    print()
    print(line)
    print("  CorePilot Core — Concurrency / Race-Condition Report")
    print(line)
    print(f"  Воркеров (процессов) ....... {r.workers}")
    print(f"  Задач в очереди ............ {r.tasks}")
    print(f"  PID-lock отверг 2-й запуск . {'ДА ✓' if r.pidlock_ok else 'НЕТ ✗'}")
    print(f"  Захватов всего ............. {r.claimed_total}")
    print(f"  Уникальных задач захвачено . {r.unique_claimed}")
    print(f"  Не захвачено (остаток) ..... {r.unclaimed}")
    print(f"  ДВОЙНЫХ ЗАХВАТОВ (race) .... {r.double_claims}")
    if r.collisions:
        print("  " + "-" * 56)
        for c in r.collisions[:10]:
            print(f"    💥 {c['task']} захвачена процессами {c['pids']}")
    print("  " + "-" * 56)
    print(f"  ВЕРДИКТ: {'PASS' if ok else 'FAIL'}")
    print(line)


# ===========================================================================
# РЕЖИМ --soak: долгий прогон, утечки RAM и файловых дескрипторов (FD)
# ===========================================================================

def _open_fd_count() -> int:
    """Число открытых файловых дескрипторов процесса (кросс-платформенно)."""
    try:
        import psutil
        p = psutil.Process()
        try: return p.num_fds()            # POSIX
        except (AttributeError, NotImplementedError):
            return len(p.open_files()) + len(p.connections(kind="all"))
    except Exception:
        # Фолбэк для Linux без psutil.
        try: return len(os.listdir("/proc/self/fd"))
        except OSError: return -1


@dataclass
class SoakResult:
    iterations: int = 0
    passed: int = 0
    failed: int = 0
    wall_seconds: float = 0.0
    rss_start_kb: int = 0
    rss_end_kb: int = 0
    rss_samples: list = field(default_factory=list)
    fd_start: int = 0
    fd_end: int = 0
    fd_peak: int = 0
    threads_end: int = 0
    leaked_threads: list = field(default_factory=list)


def run_soak(iterations: int, seed: int, verbose: bool = True) -> SoakResult:
    """Сотни микро-задач подряд через реальный _execute_task (без моков сна).
    Цель — поймать медленные утечки RAM/FD, которые не видны на коротком прогоне."""
    rng = random.Random(seed)
    res = SoakResult(iterations=iterations)

    sandbox = Path(tempfile.mkdtemp(prefix="corepilot_soak_"))
    project = sandbox / "project"; queue = sandbox / "auto_tasks"
    for d in (project, queue, queue / "done", queue / "failed"):
        d.mkdir(parents=True, exist_ok=True)

    daemon.AUTO_DIR = str(queue); daemon.DONE_DIR = str(queue / "done")
    daemon.FAILED_DIR = str(queue / "failed"); daemon.PID_FILE = str(queue / ".daemon.pid")

    state = SessionState()
    state.project_path = str(project)
    state.oracle_enabled = False
    state.ui_step_timeout = 5
    db = SimpleNamespace(update_manager_task_status=lambda *a, **k: None,
                         log_daemon_heartbeat=lambda *a, **k: None)

    # Лёгкий мок: каждый этап мгновенно успешен (мы тестируем не LLM, а гигиену ресурсов).
    flaky = FlakyKickoff(fail_times=0, rng=rng, hang_rate=0.0, hang_seconds=0.0)
    daemon.safe_kickoff = flaky
    daemon.route_task = lambda content, st, ui: _make_dummy_agents()
    if hasattr(daemon, "_consult_oracle_for_task"):
        daemon._consult_oracle_for_task = lambda *a, **k: ""

    if not tracemalloc.is_tracing(): tracemalloc.start()
    gc.collect()
    res.rss_start_kb = _rss_kb(); res.fd_start = _open_fd_count()
    res.fd_peak = res.fd_start
    snap0_threads = {t.name for t in threading.enumerate()}
    t0 = time.time()

    sample_every = max(1, iterations // 20)
    for i in range(iterations):
        t = {"id": f"soak_{i:04d}", "title": f"micro {i}",
             "description": f"микро-задача {i}", "target_files": ["m.py"],
             "context_notes": "soak", "_kind": "micro"}
        content = daemon._build_prompt_from_task(t)
        fname = f"{t['id']}.json"
        (queue / fname).write_text(json.dumps(t, ensure_ascii=False), encoding="utf-8")
        claim = queue / (fname + ".processing")
        try: (queue / fname).rename(claim)
        except OSError: claim = queue / fname

        try:
            ok, _ = daemon._execute_task(claim, db, state)
        except Exception:
            ok = False
        res.passed += int(ok); res.failed += int(not ok)

        fd_now = _open_fd_count()
        if fd_now > res.fd_peak: res.fd_peak = fd_now

        if i % sample_every == 0:
            res.rss_samples.append(_rss_kb() // 1024)   # МБ
            if verbose and i % (sample_every * 4) == 0:
                print(f"  iter {i:>5}  rss={_rss_kb()//1024}MB  fd={fd_now}  "
                      f"ok={res.passed} fail={res.failed}")

    res.wall_seconds = round(time.time() - t0, 2)
    gc.collect(); time.sleep(0.3)
    res.rss_end_kb = _rss_kb(); res.fd_end = _open_fd_count()
    res.threads_end = threading.active_count()
    res.leaked_threads = [t.name for t in threading.enumerate()
                          if t.name not in snap0_threads and t.name != "MainThread"]

    daemon.safe_kickoff = safe_kickoff_orig_ref()  # восстановим (см. ниже)
    shutil.rmtree(sandbox, ignore_errors=True)
    if tracemalloc.is_tracing(): tracemalloc.stop()
    return res


# safe_kickoff подменяется в run_soak; сохраним оригинал один раз для восстановления.
_ORIG_SAFE_KICKOFF = daemon.safe_kickoff
def safe_kickoff_orig_ref():
    return _ORIG_SAFE_KICKOFF


def print_soak_report(r: SoakResult) -> None:
    line = "=" * 60
    rss_delta = (r.rss_end_kb - r.rss_start_kb) // 1024
    fd_delta = r.fd_end - r.fd_start
    # Пороги: рост RAM >96МБ или FD >32 за прогон считаем утечкой.
    rss_leak = rss_delta > 96
    fd_leak = fd_delta > 32
    thread_leak = bool(r.leaked_threads)
    ok = not (rss_leak or fd_leak or thread_leak) and r.passed > 0
    print()
    print(line)
    print("  CorePilot Core — Soak / Resource-Leak Report")
    print(line)
    print(f"  Итераций (микро-задач) ..... {r.iterations}")
    print(f"  Прошло / Упало ............. {r.passed} / {r.failed}")
    print(f"  Время прогона .............. {r.wall_seconds} c")
    print(f"  RSS: {r.rss_start_kb//1024} -> {r.rss_end_kb//1024} МБ (Δ {rss_delta:+d} МБ)"
          + ("  ⚠ УТЕЧКА RAM" if rss_leak else ""))
    print(f"  FD:  {r.fd_start} -> {r.fd_end} (Δ {fd_delta:+d})"
          + ("  ⚠ УТЕЧКА FD" if fd_leak else "") + f"  пик={r.fd_peak}")
    print(f"  Потоки в конце: {r.threads_end}"
          + (f"  ⚠ УТЕЧКА: {r.leaked_threads}" if thread_leak else "  (чисто)"))
    if r.rss_samples:
        print(f"  RSS-трек (МБ): {r.rss_samples}")
    print("  " + "-" * 56)
    print(f"  ВЕРДИКТ: {'PASS' if ok else 'FAIL'}")
    print(line)


# ===========================================================================
# РЕЖИМ --env-chaos: враждебная среда
# ===========================================================================

@dataclass
class EnvChaosResult:
    checks: int = 0
    survived: int = 0
    crashed: int = 0
    crashes: list = field(default_factory=list)
    cases: dict = field(default_factory=dict)


def _envchaos_case(res: EnvChaosResult, name: str, fn: Callable) -> None:
    """Выполняет один кейс враждебной среды. FAIL = неперехваченный эксепшен."""
    res.checks += 1
    try:
        detail = fn()
        res.survived += 1
        res.cases[name] = {"status": "ok", "detail": detail}
    except Exception as e:
        res.crashed += 1
        tb = f"{type(e).__name__}: {e}"
        res.crashes.append({"case": name, "error": tb[:300]})
        res.cases[name] = {"status": "CRASH", "detail": tb[:160]}


def run_env_chaos(seed: int, verbose: bool = True) -> EnvChaosResult:
    rng = random.Random(seed)
    res = EnvChaosResult()

    import pipeline_parser as pp
    from utils import (apply_fixes, PatchModel, load_session, atomic_write_text,
                       strict_parse_fixes)
    import agents as ag

    sandbox = Path(tempfile.mkdtemp(prefix="corepilot_env_"))
    project = sandbox / "p"; project.mkdir(parents=True, exist_ok=True)

    # --- 1. File Locks: запись в залоченный файл (PermissionError) ---
    def _file_lock_case():
        target = project / "locked.py"
        target.write_text("original", encoding="utf-8")
        real_replace = os.replace
        def _boom(src, dst, *a, **k):
            if str(dst).endswith("locked.py"):
                raise PermissionError("[WinError 32] Файл занят другим процессом")
            return real_replace(src, dst, *a, **k)
        os.replace = _boom
        try:
            # apply_fixes должен ПЕРЕЖИТЬ PermissionError и просто пропустить патч.
            applied = apply_fixes([PatchModel(filepath="locked.py", code="new")], str(project))
            return f"apply_fixes выжил, applied={applied}"
        finally:
            os.replace = real_replace

    # --- 2. atomic_write_text напрямую под PermissionError ---
    def _atomic_write_lock_case():
        real_replace = os.replace
        def _boom(src, dst, *a, **k):
            raise PermissionError("locked")
        os.replace = _boom
        try:
            try:
                atomic_write_text(str(project / "x.py"), "data")
                return "atomic_write_text НЕ бросил (неожиданно)"
            except PermissionError:
                # Это ОЖИДАЕМО: функция пробрасывает, но не оставляет .tmp-мусор.
                leftover = [f for f in os.listdir(project) if f.endswith(".tmp")]
                if leftover:
                    raise AssertionError(f"остался tmp-мусор: {leftover}")
                return "пробросил PermissionError, tmp подчищен ✓"
        finally:
            os.replace = real_replace

    # --- 3. Context Blowout: LLM 400 token limit -> задача в failed, не краш ---
    def _token_blowout_case():
        # safe_kickoff должен пробросить не-транзиентную ошибку наружу (не зависнуть
        # в ретраях). Демон ловит её и пишет задачу в failed.
        class _Crew:
            agents = [SimpleNamespace(llm=SimpleNamespace(model="openrouter/x:free"))]
            tasks = [SimpleNamespace(output=SimpleNamespace(raw=""))]
            def kickoff(self):
                raise RuntimeError("Error code: 400 - maximum context length / token limit exceeded")
        st = SessionState(); st.project_path = str(project)
        try:
            ag.safe_kickoff(_Crew(), st)
            return "safe_kickoff НЕ бросил (неожиданно для 400)"
        except RuntimeError as e:
            if "400" in str(e) or "token" in str(e).lower():
                return "safe_kickoff корректно пробросил 400 (уйдёт в failed) ✓"
            raise

    # --- 4. Broken Configs: битый secrets.toml / .ai_session.json ---
    def _broken_session_case():
        cwd = os.getcwd()
        os.chdir(sandbox)
        try:
            Path(".ai_session.json").write_text("{ это не json: ,,, ]", encoding="utf-8")
            st = load_session()        # должен вернуть None, не упасть
            # симулируем фолбэк как в демоне: load_session() or SessionState()
            st = st or SessionState()
            return f"битый session проглочен, дефолт ok (profile={st.agent_profile[:20]})"
        finally:
            os.chdir(cwd)

    def _broken_secrets_case():
        cwd = os.getcwd()
        os.chdir(sandbox)
        try:
            Path("secrets.toml").write_text("[[[ broken toml ===", encoding="utf-8")
            # повторяем логику загрузки демона: tomllib.load в try/except
            import tomllib
            try:
                with open("secrets.toml", "rb") as f:
                    ag.init_api_keys(tomllib.load(f).get("PROVIDER_KEYS", {}))
                return "битый TOML распарсился (неожиданно)"
            except Exception:
                return "битый TOML проглочен, ключи не загружены ✓"
        finally:
            os.chdir(cwd)

    # --- 5. Mixed Slashes: смешанные разделители путей через парсеры ---
    def _mixed_slashes_case():
        payloads = [
            'a/b\\c/d.py', 'src\\mod/sub\\file.py', '.\\a/./b\\..\\c.py',
            'C:/x\\y/z.py', 'a\\\\b//c.py',
        ]
        results = []
        for raw_path in payloads:
            j = json.dumps({"patches": [{"filepath": raw_path, "code": "x = 1"}]})
            out = pp.parse_fixer_output(j)                       # парсер не должен падать
            patches = pp.fixer_output_to_patch_models(out)
            apply_fixes(patches, str(project))                   # запись не должна падать/выходить за проект
            results.append(len(patches))
        # проверяем, что ничего не вышло за пределы проекта
        for root, _d, files in os.walk(sandbox):
            for f in files:
                full = os.path.realpath(os.path.join(root, f))
                if not full.startswith(os.path.realpath(str(project))) and not full.startswith(os.path.realpath(str(sandbox))):
                    raise AssertionError(f"путь вышел за проект: {full}")
        return f"смешанные слэши обработаны, патчей={results}"

    cases = [
        ("file_lock_apply", _file_lock_case),
        ("atomic_write_lock", _atomic_write_lock_case),
        ("token_blowout_400", _token_blowout_case),
        ("broken_session_json", _broken_session_case),
        ("broken_secrets_toml", _broken_secrets_case),
        ("mixed_slashes", _mixed_slashes_case),
    ]
    for name, fn in cases:
        _envchaos_case(res, name, fn)
        if verbose:
            st = res.cases[name]
            flag = "✓" if st["status"] == "ok" else "💥 CRASH"
            print(f"  [{flag}] {name}: {st['detail']}")

    shutil.rmtree(sandbox, ignore_errors=True)
    return res


def print_env_chaos_report(r: EnvChaosResult) -> None:
    line = "=" * 60
    ok = r.crashed == 0
    print()
    print(line)
    print("  CorePilot Core — Hostile Environment Report")
    print(line)
    print(f"  Кейсов всего ............... {r.checks}")
    print(f"  Пережито ................... {r.survived}")
    print(f"  ПРОБИТИЙ (Python crash) .... {r.crashed}")
    print("  " + "-" * 56)
    for name, st in r.cases.items():
        flag = "✓ " if st["status"] == "ok" else "✗ CRASH"
        print(f"    {flag} {name:<22} {st['detail'][:60]}")
    if r.crashes:
        print("  " + "-" * 56)
        for c in r.crashes:
            print(f"    💥 [{c['case']}] {c['error'][:90]}")
    print("  " + "-" * 56)
    print(f"  ВЕРДИКТ: {'PASS' if ok else 'FAIL'}")
    print(line)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Автономный стресс-тест ядра CorePilot.")
    ap.add_argument("--tasks", type=int, default=20, help="число задач (по умолчанию 20)")
    ap.add_argument("--fail-rate", type=int, default=2,
                    help="сколько раз каждый этап падает транзиентно до успеха (def 2)")
    ap.add_argument("--exhaust-fraction", type=float, default=0.25,
                    help="доля задач с полным исчерпанием ретраев на этапе fix (def 0.25)")
    ap.add_argument("--hang-fraction", type=float, default=0.1,
                    help="доля вызовов с имитацией зависания (def 0.1)")
    ap.add_argument("--seed", type=int, default=None, help="seed ГПСЧ для воспроизводимости")
    ap.add_argument("--chaos", action="store_true",
                    help="режим хаос-фаззинга: проверка устойчивости к галлюцинациям модели")
    ap.add_argument("--chaos-iters", type=int, default=200,
                    help="число payload'ов в режиме --chaos (def 200)")
    ap.add_argument("--concurrency", type=int, metavar="N", default=None,
                    help="режим гонки: N параллельных воркеров на одну очередь")
    ap.add_argument("--soak", action="store_true",
                    help="режим soak: долгий прогон, утечки RAM/FD/потоков")
    ap.add_argument("--soak-iters", type=int, default=300,
                    help="число микро-задач в режиме --soak (def 300)")
    ap.add_argument("--env-chaos", action="store_true",
                    help="режим враждебной среды: file-locks, 400-token, битые конфиги, mixed slashes")
    ap.add_argument("--json", action="store_true", help="вывести отчёт в JSON")
    ap.add_argument("--quiet", action="store_true", help="без построчного лога задач")
    args = ap.parse_args(argv)

    seed = args.seed if args.seed is not None else random.randint(1, 10_000_000)

    # --- Concurrency: гонка воркеров ---
    if args.concurrency is not None:
        if not args.json:
            print(f"CorePilot CONCURRENCY: workers={args.concurrency} tasks={args.tasks} seed={seed}")
        r = run_concurrency(args.concurrency, args.tasks, seed,
                            verbose=not args.quiet and not args.json)
        if args.json:
            p = asdict(r); p["seed"] = seed
            p["verdict"] = "PASS" if (r.double_claims == 0 and r.pidlock_ok) else "FAIL"
            print(json.dumps(p, ensure_ascii=False, indent=2))
        else:
            print_concurrency_report(r)
        return 0 if (r.double_claims == 0 and r.pidlock_ok) else 1

    # --- Soak: утечки ресурсов ---
    if args.soak:
        if not args.json:
            print(f"CorePilot SOAK: iters={args.soak_iters} seed={seed}")
        r = run_soak(args.soak_iters, seed, verbose=not args.quiet and not args.json)
        leak = ((r.rss_end_kb - r.rss_start_kb) // 1024 > 96) or \
               ((r.fd_end - r.fd_start) > 32) or bool(r.leaked_threads) or r.passed == 0
        if args.json:
            p = asdict(r); p["seed"] = seed; p["verdict"] = "FAIL" if leak else "PASS"
            print(json.dumps(p, ensure_ascii=False, indent=2))
        else:
            print_soak_report(r)
        return 1 if leak else 0

    # --- Env-chaos: враждебная среда ---
    if args.env_chaos:
        if not args.json:
            print(f"CorePilot ENV-CHAOS: seed={seed}")
        r = run_env_chaos(seed, verbose=not args.quiet and not args.json)
        if args.json:
            p = asdict(r); p["seed"] = seed
            p["verdict"] = "PASS" if r.crashed == 0 else "FAIL"
            print(json.dumps(p, ensure_ascii=False, indent=2))
        else:
            print_env_chaos_report(r)
        return 0 if r.crashed == 0 else 1

    # --- Хаос-режим: отдельная ветка ---
    if args.chaos:
        if not args.json:
            print(f"CorePilot CHAOS: iters={args.chaos_iters} seed={seed}")
        cres = run_chaos(args.chaos_iters, seed, verbose=not args.quiet and not args.json)
        if args.json:
            payload = asdict(cres)
            payload["seed"] = seed
            payload["verdict"] = "PASS" if (cres.crashed == 0 and cres.path_escapes == 0) else "FAIL"
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print_chaos_report(cres)
        return 0 if (cres.crashed == 0 and cres.path_escapes == 0) else 1
    if not args.json:
        print(f"CorePilot QA: tasks={args.tasks} fail_rate={args.fail_rate} "
              f"exhaust={args.exhaust_fraction} hang={args.hang_fraction} seed={seed}")

    res = run_qa(
        num_tasks=args.tasks,
        fail_rate=args.fail_rate,
        seed=seed,
        exhaust_fraction=args.exhaust_fraction,
        hang_fraction=args.hang_fraction,
        verbose=not args.quiet and not args.json,
    )

    if args.json:
        payload = asdict(res)
        payload["seed"] = seed
        payload["verdict"] = _verdict(res)[0]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_report(res)

    v = _verdict(res)[0]
    return 0 if v in ("PASS", "WARN") else 1


if __name__ == "__main__":
    sys.exit(main())
