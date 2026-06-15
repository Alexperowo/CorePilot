#!/usr/bin/env python3
"""
service_layer.py — тонкий сервисный слой между ядром CorePilot и интерфейсом.

Цель: UI (DearPyGui сейчас, веб потом) общается ТОЛЬКО с этим фасадом и не знает
внутренностей демона/очереди/agents. Слой не содержит бизнес-логики ИИ — он лишь
читает состояние очереди, управляет процессом Демона и проксирует вызовы ядра.

Контракт стабилен: меняется ядро — меняется слой, но UI остаётся прежним.

Зависимости: только стандартная библиотека + ядро проекта. Без сети и тяжёлых
импортов на уровне модуля (agents/crewai тянутся лениво, чтобы UI стартовал быстро).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

_log = logging.getLogger("SERVICE")

# --- Пути очереди (единый источник правды с демоном; без хардкода в UI) -------
# AUTO_DIR привязан к РАСПОЛОЖЕНИЮ проекта (__file__), а не к текущей рабочей папке.
# Иначе при запуске из разных директорий (ярлык / .bat / IDE) приложение и демон
# видели РАЗНЫЕ auto_tasks — профили и задачи «пропадали». Переменная окружения
# COREPILOT_AUTO_DIR (если задана) имеет приоритет.
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
AUTO_DIR = os.environ.get("COREPILOT_AUTO_DIR", os.path.join(_PROJECT_DIR, "auto_tasks"))
DONE_DIR = os.path.join(AUTO_DIR, "done")
FAILED_DIR = os.path.join(AUTO_DIR, "failed")
PID_FILE = os.path.join(AUTO_DIR, ".daemon.pid")
# Системные файлы в AUTO_DIR, которые НЕ являются задачами (не парсим как задачи).
_NON_TASK_FILES = {"qa_history.json", "config_profiles.json", "profiles.json",
                   "settings.json", "quotas.json"}

# Статусы задач (единый словарь для UI).
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_FROZEN = "frozen"   # производный статус: ждёт невыполненных/проваленных родителей


# ===========================================================================
# Модель задачи для UI
# ===========================================================================

@dataclass
class TaskView:
    """Богатое представление задачи для графа/канбана. Всё, что нужно UI, и
    ничего лишнего из внутренней кухни."""
    task_id: str
    title: str
    status: str
    depends_on: list[str] = field(default_factory=list)
    target_files: list[str] = field(default_factory=list)
    description: str = ""
    failed_reason: str = ""
    needs_review: bool = False


@dataclass
class BoardSnapshot:
    """Снимок всей доски задач — один атомарный объект для перерисовки UI."""
    tasks: list[TaskView] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    daemon_running: bool = False
    daemon_pid: Optional[int] = None
    ts: float = 0.0


# ===========================================================================
# Чтение состояния очереди
# ===========================================================================

def _read_task_file(path: Path) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = json.loads(f.read())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _norm_task(data: dict, status: str) -> Optional[TaskView]:
    tid = data.get("task_id")
    if tid is None:
        return None
    deps = data.get("depends_on") or []
    if not isinstance(deps, list):
        deps = [str(deps)]
    tfiles = data.get("target_files") or []
    if not isinstance(tfiles, list):
        tfiles = [str(tfiles)]
    return TaskView(
        task_id=str(tid),
        title=str(data.get("title", f"Задача {tid}"))[:200],
        status=status,
        depends_on=[str(d) for d in deps],
        target_files=[str(f) for f in tfiles],
        description=str(data.get("description", ""))[:1000],
        failed_reason=str(data.get("failed_reason", ""))[:500],
        needs_review=bool(data.get("_needs_review", False)),
    )


def _scan_dir(dirpath: str, status: str) -> dict[str, TaskView]:
    """task_id -> TaskView из одной папки очереди."""
    out: dict[str, TaskView] = {}
    try:
        entries = list(os.scandir(dirpath))
    except OSError:
        return out
    for e in entries:
        n = e.name
        if ".json" not in n:
            continue
        # Системные файлы в AUTO_DIR — НЕ задачи: их парсинг давал ложное
        # «нечитаемо». Пропускаем по имени (qa_history, профили, настройки, квоты).
        if n in _NON_TASK_FILES:
            continue
        data = _read_task_file(Path(e.path))
        if not data:
            continue
        # .processing в AUTO_DIR -> задача в работе.
        st = STATUS_PROCESSING if (status == STATUS_PENDING and n.endswith(".processing")) else status
        tv = _norm_task(data, st)
        if not tv:
            continue
        # failed > done > processing > pending: не понижаем статус при дублях.
        prev = out.get(tv.task_id)
        if prev and _status_rank(prev.status) >= _status_rank(tv.status):
            continue
        out[tv.task_id] = tv
    return out


def _status_rank(s: str) -> int:
    return {STATUS_FAILED: 4, STATUS_DONE: 3, STATUS_PROCESSING: 2,
            STATUS_PENDING: 1, STATUS_FROZEN: 1}.get(s, 0)


def _apply_frozen(tasks: dict[str, TaskView]) -> None:
    """Помечает pending-задачи как frozen, если их родители не done (или провалены).
    Повторяет логику блокировки демона — но только для ОТОБРАЖЕНИЯ, не трогая очередь."""
    status_map = {t.task_id: t.status for t in tasks.values()}
    for t in tasks.values():
        if t.status != STATUS_PENDING or not t.depends_on:
            continue
        for d in t.depends_on:
            ps = status_map.get(d)
            if ps != STATUS_DONE:  # родитель не выполнен (failed/pending/отсутствует)
                t.status = STATUS_FROZEN
                if ps == STATUS_FAILED and not t.failed_reason:
                    t.failed_reason = f"заморожено: родитель {d} провалён"
                elif ps is None and not t.failed_reason:
                    t.failed_reason = f"заморожено: родитель {d} отсутствует"
                break


_board_cache: dict = {"sig": None, "tasks": None}


def _queue_signature() -> tuple:
    """Подпись очереди: (mtime, число записей) по трём папкам. Меняется при любом
    add/remove/move файла — тогда и только тогда пересобираем доску."""
    sig = []
    for d in (AUTO_DIR, DONE_DIR, FAILED_DIR):
        try:
            st = os.stat(d)
            n = sum(1 for _ in os.scandir(d))
            sig.append((round(st.st_mtime, 3), n))
        except OSError:
            sig.append((0.0, 0))
    return tuple(sig)


def get_board() -> BoardSnapshot:
    """Главный метод для UI: полный снимок доски задач + состояние демона.
    Список задач кэшируется по mtime папок (UI вызывает раз в ~1.5с — не
    перечитываем сотни файлов, если очередь не менялась). Статус демона всегда
    свежий (дешёвая проверка PID)."""
    sig = _queue_signature()
    if sig == _board_cache["sig"] and _board_cache["tasks"] is not None:
        tasks = _board_cache["tasks"]
    else:
        tasks = _build_board_tasks()
        _board_cache["sig"] = sig
        _board_cache["tasks"] = tasks

    counts = {s: 0 for s in (STATUS_PENDING, STATUS_PROCESSING, STATUS_DONE,
                             STATUS_FAILED, STATUS_FROZEN)}
    for t in tasks:
        counts[t.status] = counts.get(t.status, 0) + 1

    running, pid = daemon_status()
    return BoardSnapshot(
        tasks=tasks, counts=counts,
        daemon_running=running, daemon_pid=pid, ts=time.time(),
    )


def _build_board_tasks() -> list:
    """Собирает отсортированный список TaskView из трёх папок очереди (дорогой путь
    — читает и парсит файлы; вызывается только при изменении очереди)."""
    tasks: dict[str, TaskView] = {}
    # Порядок важен: done/failed перекрывают pending одного task_id (см. _status_rank).
    for d, st in ((AUTO_DIR, STATUS_PENDING), (DONE_DIR, STATUS_DONE), (FAILED_DIR, STATUS_FAILED)):
        for tid, tv in _scan_dir(d, st).items():
            prev = tasks.get(tid)
            if prev and _status_rank(prev.status) >= _status_rank(tv.status):
                continue
            tasks[tid] = tv

    _apply_frozen(tasks)
    return sorted(tasks.values(), key=lambda x: x.task_id)


# ===========================================================================
# Управление Демоном
# ===========================================================================

def daemon_status() -> tuple[bool, Optional[int]]:
    """(работает ли демон, его PID). Проверяет живость процесса по PID-файлу."""
    if not os.path.exists(PID_FILE):
        return False, None
    try:
        with open(PID_FILE, "r") as f:
            pid = int(f.read().strip())
    except Exception:
        return False, None
    try:
        import psutil
        return (psutil.pid_exists(pid), pid)
    except Exception:
        # Фолбэк без psutil: на POSIX сигнал 0, на Windows считаем живым по файлу.
        if os.name != "nt":
            try:
                os.kill(pid, 0); return True, pid
            except OSError:
                return False, pid
        return True, pid


def start_daemon(project_dir: Optional[str] = None) -> tuple[bool, str]:
    """Запускает Демон отдельным процессом. Возвращает (успех, сообщение)."""
    running, pid = daemon_status()
    if running:
        return False, f"Демон уже запущен (PID {pid})."
    # Снимаем устаревший PID-файл мёртвого процесса.
    if os.path.exists(PID_FILE):
        try: os.remove(PID_FILE)
        except OSError as e: _log.debug("Не удалось снять устаревший PID-файл: %s", e)
    cwd = project_dir or os.getcwd()
    daemon_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "daemon.py")
    try:
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen([sys.executable, daemon_script], cwd=cwd, creationflags=flags)
        else:
            subprocess.Popen([sys.executable, daemon_script], cwd=cwd)
    except Exception as e:
        return False, f"Не удалось запустить демон: {e}"
    # Дать процессу записать PID.
    for _ in range(20):
        time.sleep(0.1)
        if daemon_status()[0]:
            return True, "Демон запущен."
    return True, "Демон стартует (PID ещё не подтверждён)."


def stop_daemon() -> tuple[bool, str]:
    """Останавливает Демон по PID. Возвращает (успех, сообщение)."""
    running, pid = daemon_status()
    if not running or pid is None:
        # Подчистим осиротевший PID-файл.
        if os.path.exists(PID_FILE):
            try: os.remove(PID_FILE)
            except OSError as e: _log.debug("Не удалось снять осиротевший PID-файл: %s", e)
        return False, "Демон не запущен."
    try:
        if os.name == "nt":
            subprocess.call(["taskkill", "/F", "/PID", str(pid)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
    except Exception as e:
        return False, f"Ошибка остановки: {e}"
    return True, f"Демон остановлен (PID {pid})."


# ===========================================================================
# Управление задачами в очереди
# ===========================================================================

def _ensure_dirs() -> None:
    for d in (AUTO_DIR, DONE_DIR, FAILED_DIR):
        os.makedirs(d, exist_ok=True)


def enqueue_task(task: dict) -> tuple[bool, str]:
    """Кладёт одну задачу в очередь. task должен содержать task_id; depends_on
    сохраняется как есть. Используется UI для ручной постановки."""
    _ensure_dirs()
    tid = str(task.get("task_id", "")).strip()
    if not tid:
        return False, "task_id обязателен."
    import re
    safe = re.sub(r"[^A-Za-z0-9_-]", "", tid)[:24] or "task"
    fname = f"task_{int(time.time())}_{safe}.json"
    try:
        tmp = os.path.join(AUTO_DIR, fname + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(task, f, ensure_ascii=False, indent=2)
        os.replace(tmp, os.path.join(AUTO_DIR, fname))
    except Exception as e:
        return False, f"Ошибка записи: {e}"
    return True, fname


def enqueue_backlog(items: list[dict]) -> tuple[int, int]:
    """Ставит пачку задач (DAG-бэклог). Возвращает (поставлено, пропущено)."""
    ok = skip = 0
    for it in items:
        success, _ = enqueue_task(it)
        ok += int(success); skip += int(not success)
    return ok, skip


def _find_task_files(task_id: str) -> list[Path]:
    """Все файлы (во всех папках), относящиеся к данному task_id."""
    found = []
    for d in (AUTO_DIR, DONE_DIR, FAILED_DIR):
        try: names = os.listdir(d)
        except OSError: continue
        for n in names:
            if ".json" not in n:
                continue
            data = _read_task_file(Path(d) / n)
            if data and str(data.get("task_id", "")) == str(task_id):
                found.append(Path(d) / n)
    return found


def requeue_task(task_id: str) -> tuple[bool, str]:
    """Возвращает проваленную/выполненную задачу обратно в очередь (повторить).
    Снимает метки провала. Полезно после исправления причины или обновления лимитов."""
    files = _find_task_files(task_id)
    if not files:
        return False, f"Задача {task_id} не найдена."
    # Берём самую свежую копию как источник.
    src = max(files, key=lambda p: p.stat().st_mtime)
    data = _read_task_file(src)
    if not data:
        return False, "Не удалось прочитать задачу."
    data.pop("failed_reason", None)
    data.pop("failed_at", None)
    data.pop("_needs_review", None)
    # Удаляем старые копии (в т.ч. из failed/done), ставим заново в AUTO_DIR.
    for p in files:
        try: p.unlink()
        except OSError: pass
    return enqueue_task(data)


def remove_task(task_id: str) -> tuple[bool, str]:
    """Удаляет задачу из очереди (все её копии)."""
    files = _find_task_files(task_id)
    if not files:
        return False, f"Задача {task_id} не найдена."
    removed = 0
    for p in files:
        try: p.unlink(); removed += 1
        except OSError: pass
    return removed > 0, f"Удалено копий: {removed}"


def clear_finished() -> int:
    """Чистит архивы done/failed. Возвращает число удалённых файлов."""
    n = 0
    for d in (DONE_DIR, FAILED_DIR):
        try: names = os.listdir(d)
        except OSError: continue
        for name in names:
            try: (Path(d) / name).unlink(); n += 1
            except OSError: pass
    return n


# ===========================================================================
# Квоты провайдеров (проксируем ядро, ленивый импорт)
# ===========================================================================

def get_quotas() -> list[dict]:
    """Живые квоты всех провайдеров с ключами. Без хардкода — данные из API/заголовков."""
    try:
        from agents import fetch_all_quotas, _lazy_reload_keys
        _lazy_reload_keys()  # гарантируем загрузку ключей до итерации API_KEYS
        return fetch_all_quotas()
    except Exception as e:
        return [{"provider": "?", "status": "error", "detail": str(e)[:200]}]


# ===========================================================================
# Бэклог из цели (синхронная генерация через Менеджер-Crew)
# ===========================================================================

def generate_backlog(goal: str, state=None) -> tuple[Optional[list[dict]], str]:
    """Прогоняет цель через Product Owner -> Scrum Master и возвращает DAG-бэклог.
    Тяжёлая операция (вызывает LLM) — UI должен звать её в отдельном потоке.
    Возвращает (бэклог|None, сообщение)."""
    try:
        from crewai import Crew, Task
        from manager_agents import make_manager_crew, parse_backlog
        from agents import build_role_llm, safe_kickoff
        from utils import load_session, SessionState
    except Exception as e:
        return None, f"Ядро недоступно: {e}"

    st = state or (load_session() or SessionState())
    try:
        llm = build_role_llm(st, "architect")
        po, sm = make_manager_crew(st, llm)
        crew = Crew(agents=[po, sm], tasks=[
            Task(description=f"Сформируй концепцию для цели:\n{goal}", agent=po,
                 expected_output="Концепция (текст)"),
            Task(description="Разбей концепцию на DAG-бэклог задач (JSON).", agent=sm,
                 expected_output="JSON-массив задач с depends_on"),
        ])
        raw = safe_kickoff(crew, st)
    except Exception as e:
        return None, f"Ошибка генерации: {type(e).__name__}: {e}"

    backlog = parse_backlog(str(raw))
    if not backlog:
        return None, "Scrum Master не вернул валидный бэклог."
    return backlog, f"Сформировано задач: {len(backlog)}."


# ===========================================================================
# Настройки (SessionState <-> плоский dict для UI)
# ===========================================================================

# Поля настроек, которые UI вправе читать/писать. Группировка — для удобной
# раскладки формы в интерфейсе.
SETTINGS_SCHEMA = {
    "Проект": [
        ("project_path", "str", "Путь к проекту"),
        ("agent_profile", "str", "Стек агентов"),
        ("task_mode", "str", "Режим задачи"),
        ("speed", "choice:fast,medium,slow", "Скорость / качество"),
    ],
    "Поведение": [
        ("auto_apply", "bool", "Авто-применение патчей"),
        ("strict_sandbox", "bool", "Строгая песочница"),
        ("oracle_enabled", "bool", "Мастер-Оракул включён"),
        ("force_local_reasoning", "bool", "Принудительное рассуждение для локальных моделей (CoT)"),
        ("force_json_output", "bool", "Принудительный валидный JSON для локальных моделей"),
        ("web_search_enabled", "bool", "Доступ в интернет"),
        ("vram_unload_between_agents", "bool", "Выгружать модель из VRAM между агентами (включите при разных локальных моделях и ≤16ГБ RAM — иначе риск нехватки памяти)"),
        ("vram_override_mb", "int", "Ручной лимит VRAM (МБ). 0 = автоопределение. Нужно для AMD-карт >4 ГБ: WMI занижает до 4 ГБ из-за 32-битного ограничения."),
        ("kv_ctv", "str", "Квантование V-кеша KV (если пусто = совпадает с kv_ctk). Пример: iq4_nl. Асимметрия K=q5_1 + V=iq4_nl даёт ~9% экономии VRAM."),
        ("dup_full_hash", "bool", "Полный хэш дубликатов"),
        ("quarantine_same_drive", "bool", "Карантин на диске источника"),
        ("debug_mode", "bool", "Режим отладки"),
    ],
    "Оракул-Титан (эскалация)": [
        ("titan_model", "str", "Локальный Титан: модель-тяжеловес 14-26b (фолбэк, если облако недоступно)"),
        ("titan_backend", "choice:lmstudio,ollama,llamacpp,lemonade", "Бэкенд Титана"),
    ],
    "Бэкенды": [
        ("local_backend", "choice:lmstudio,ollama,llamacpp,lemonade,mobile", "Локальный бэкенд (по умолч.)"),
        ("local_base_url", "str", "URL локального бэкенда"),
        ("mobile_base_url", "str", "URL компаньона (IP Android)"),
    ],
    "Генерация изображений": [
        ("image_source", "choice:forge,comfy,cloud", "Источник (forge / comfy / облако)"),
        ("forge_url", "str", "SD Forge API URL (для forge)"),
        ("forge_model", "str", "Модель SD Forge (опц.)"),
        ("forge_upscale", "bool", "Нейросетевой апскейл через Forge ESRGAN после генерации"),
        ("forge_upscale_scale", "int:2:8", "Масштаб апскейла (2=1024px, 4=2048px/4K, 8=4096px)"),
        ("comfy_url", "str", "ComfyUI API URL (для comfy)"),
        ("comfy_model", "str", "Чекпойнт ComfyUI .safetensors (опц.)"),
        ("image_provider", "choice:huggingface,openai,together,stability,fal,replicate",
         "Облачный провайдер (для cloud)"),
        ("image_cloud_model", "str",
         "Модель облака (пусто = дефолт провайдера: FLUX.1-schnell / dall-e-3 / и т.д.)"),
    ],
    "Лимиты": [
        ("ui_max_iter", "int:1:30", "Макс. итераций агентов"),
        ("ui_max_rpm", "int:1:60", "Макс. запросов/мин (RPM)"),
        ("ui_file_limit_kb", "int:100:5000", "Макс. размер файла (КБ)"),
        ("max_tool_output_chars", "int:1000:50000", "Лимит вывода инструментов"),
        ("ui_step_timeout", "int:60:1800", "Таймаут шага агента (сек)"),
        ("backup_retention_days", "int:1:90", "Хранить бэкапы (дней)"),
    ],
}

# Роли LLM (5 каскадов): mode/backend/provider/model на каждую.
ROLES = ["gatherer", "architect", "coder", "auditor", "oracle"]
ROLE_LABELS = {"gatherer": "Сборщик", "architect": "Архитектор", "coder": "Кодер",
               "auditor": "Аудитор", "oracle": "Оракул"}
PROVIDERS = ["groq", "openrouter", "cerebras", "sambanova", "huggingface", "cohere",
             "anthropic", "openai", "gemini", "mistral", "deepseek"]
BACKENDS = ["lmstudio", "ollama", "llamacpp", "lemonade", "mobile"]
# Источники для роли в UI: локальные бэкенды + облако одним списком.
# 'cloud' означает облачные вычисления (тогда работает provider+model).
SOURCES = BACKENDS + ["cloud"]


def _migrate_role_sources(cfg: dict) -> dict:
    """Миграция старого формата: если у роли есть mode_<role>, но backend_<role>
    не задан/пуст — выводим источник из mode (cloud->cloud, local->lmstudio).
    Это «доводит» старые профили до нового потока backend_<role>."""
    for role in ROLES:
        bk = cfg.get(f"backend_{role}")
        if bk in SOURCES:
            continue  # уже в новом формате
        mode = cfg.get(f"mode_{role}")
        if mode == "cloud":
            cfg[f"backend_{role}"] = "cloud"
        elif mode == "local":
            cfg[f"backend_{role}"] = "lmstudio"
    return cfg


def load_settings() -> dict:
    """Возвращает текущие настройки как плоский dict (UI не трогает utils напрямую)."""
    try:
        from utils import load_session, SessionState
        st = load_session() or SessionState()
        # pydantic v2 -> dict; берём только сериализуемые поля.
        try:
            return _migrate_role_sources(st.model_dump())
        except Exception:
            return _migrate_role_sources({k: getattr(st, k) for k in dir(st)
                    if not k.startswith("_") and isinstance(getattr(st, k, None), (str, int, float, bool, list))})
    except Exception as e:
        return {"__error__": str(e)}


def save_settings(values: dict) -> tuple[bool, str]:
    """Применяет изменённые поля к SessionState и сохраняет сессию."""
    try:
        from utils import load_session, save_session, SessionState
        st = load_session() or SessionState()
        applied = 0
        for k, v in values.items():
            if hasattr(st, k):
                try:
                    setattr(st, k, v)
                    applied += 1
                except AttributeError:
                    pass  # Игнорируем вычисляемые/read-only свойства (например, power_mode)
        save_session(st)
        return True, f"Сохранено полей: {applied}."
    except Exception as e:
        return False, f"Ошибка сохранения: {e}"


def list_local_models(backend: str, base_url: str = "") -> list[str]:
    """Список локальных моделей с запущенного бэкенда (для выпадающих списков ролей).
    Пусто = бэкенд офлайн. Без хардкода имён — спрашиваем сам сервер."""
    import requests
    try:
        if backend == "ollama":
            # base_url может быть от ДРУГОГО бэкенда (напр. LM Studio :1234) —
            # для ollama он не подходит. Берём его только если это реально ollama
            # (порт 11434), иначе — дефолт ollama.
            if base_url and "11434" in base_url:
                url = base_url
            else:
                url = "http://localhost:11434"
            r = requests.get(f"{url.rstrip('/').replace('/v1','')}/api/tags", timeout=4)
            return [m["name"] for m in r.json().get("models", [])]
        # lmstudio / llamacpp / lemonade / mobile — OpenAI-совместимый /models
        url = base_url or {"lmstudio": "http://localhost:1234/v1",
                           "llamacpp": "http://localhost:8080/v1",
                           "lemonade": "http://localhost:8000/v1",
                           "mobile": "http://192.168.1.1:8080/v1"}.get(backend, "")
        if not url:
            return []
        r = requests.get(f"{url.rstrip('/')}/models", timeout=4)
        return [m["id"] for m in r.json().get("data", [])]
    except Exception:
        return []


def list_cloud_models(provider: str, api_key: str = "") -> list[str]:
    """Список облачных моделей провайдера — ЖИВЫМ запросом к его /models (не хардкод!).
    Поэтому список всегда актуален на текущую дату. Бесплатные модели (OpenRouter
    :free) поднимаются в начало списка. Пусто = нет ключа/сети/ошибка.

    Ключ берём из переданного api_key либо из загруженных секретов (init_api_keys)."""
    import requests
    if not api_key:
        try:
            import agents
            # peek, а не next: получение списка моделей не должно сдвигать счётчик
            # ротации ключей (иначе открытие Настроек зря «прокручивает» ключи).
            api_key = agents.peek_api_key(provider) or ""
        except Exception:
            api_key = ""

    try:
        if provider == "openrouter":
            # Листинг открыт без ключа; :free = бесплатные.
            r = requests.get("https://openrouter.ai/api/v1/models", timeout=6)
            data = r.json().get("data", [])
            free = [m["id"] for m in data if str(m.get("id", "")).endswith(":free")]
            paid = [m["id"] for m in data if not str(m.get("id", "")).endswith(":free")]
            # 'auto' (сам выбирает доступную модель) — всегда первым: удобно и не
            # требует знать конкретное имя. Раньше пропадал при обновлении списка.
            return ["openrouter/auto"] + free + paid

        if not api_key:
            return []

        if provider == "groq":
            r = requests.get("https://api.groq.com/openai/v1/models", timeout=6,
                             headers={"Authorization": f"Bearer {api_key}"})
            return [m["id"] for m in r.json().get("data", [])]

        # Cerebras / SambaNova — OpenAI-совместимые /models (быстрые бесплатные тарифы).
        if provider == "cerebras":
            r = requests.get("https://api.cerebras.ai/v1/models", timeout=6,
                             headers={"Authorization": f"Bearer {api_key}"})
            return [m["id"] for m in r.json().get("data", [])]

        if provider == "sambanova":
            r = requests.get("https://api.sambanova.ai/v1/models", timeout=6,
                             headers={"Authorization": f"Bearer {api_key}"})
            return [m["id"] for m in r.json().get("data", [])]

        if provider == "huggingface":
            # HF Router (OpenAI-совместимый) отдаёт список доступных моделей инференса.
            r = requests.get("https://router.huggingface.co/v1/models", timeout=6,
                             headers={"Authorization": f"Bearer {api_key}"})
            return [m["id"] for m in r.json().get("data", [])]

        if provider == "cohere":
            r = requests.get("https://api.cohere.ai/v1/models", timeout=6,
                             headers={"Authorization": f"Bearer {api_key}"})
            data = r.json()
            return [m.get("name", "") for m in data.get("models", data.get("data", [])) if m.get("name")]

        if provider == "openai":
            r = requests.get("https://api.openai.com/v1/models", timeout=6,
                             headers={"Authorization": f"Bearer {api_key}"})
            return sorted(m["id"] for m in r.json().get("data", []))

        if provider == "anthropic":
            r = requests.get("https://api.anthropic.com/v1/models", timeout=6,
                             headers={"x-api-key": api_key,
                                      "anthropic-version": "2023-06-01"})
            return [m["id"] for m in r.json().get("data", [])]

        if provider == "gemini":
            r = requests.get(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}",
                timeout=6)
            # name вида "models/gemini-..." — оставляем короткое имя
            return [m["name"].split("/", 1)[-1] for m in r.json().get("models", [])]

        if provider == "mistral":
            r = requests.get("https://api.mistral.ai/v1/models", timeout=6,
                             headers={"Authorization": f"Bearer {api_key}"})
            return [m["id"] for m in r.json().get("data", [])]

        if provider == "deepseek":
            r = requests.get("https://api.deepseek.com/models", timeout=6,
                             headers={"Authorization": f"Bearer {api_key}"})
            return [m["id"] for m in r.json().get("data", [])]
    except Exception:
        return []
    return []


# ===========================================================================
# Конвейер (синхронный, для UI): gather -> architect -> fix -> audit
# ===========================================================================

@dataclass
class PipelineResult:
    ok: bool = False
    verdict: str = ""
    summary: str = ""
    diffs: list = field(default_factory=list)   # [(filepath, unified_diff)]
    patches: list = field(default_factory=list)  # [(filepath, code)]
    error: str = ""
    overlay_dir: str = ""


def run_pipeline(request: str, progress=None, state=None) -> PipelineResult:
    """Прогоняет конвейер по запросу синхронно (UI зовёт в фоновом потоке).
    progress(stage:str, text:str) — колбэк для живого статуса в UI.
    НЕ применяет патчи автоматически — возвращает diff'ы для подтверждения в UI."""
    def _p(stage, text=""):
        if progress:
            try: progress(stage, text)
            except Exception: pass

    res = PipelineResult()
    try:
        from crewai import Crew, Task
        from agents import safe_kickoff, maybe_unload_between
        from pipeline_agents import make_pipeline_agents
        from pipeline_parser import (parse_gatherer_output, parse_architect_output,
                                      parse_fixer_output, parse_auditor_verdict,
                                      fixer_output_to_patch_models)
        from utils import (load_session, SessionState, ProjectOverlay,
                           PipelineCheckpoint, generate_unified_diff,
                           set_runtime_context, reset_runtime_context, RuntimeContext,
                           DummyInteractionHandler)
    except Exception as e:
        res.error = f"Ядро недоступно: {e}"
        return res

    st = state or (load_session() or SessionState())
    overlay = ProjectOverlay(st.project_path)
    ckpt = PipelineCheckpoint(st.project_path, request)
    token = None
    try:
        ctx = RuntimeContext(state=st, overlay=overlay,
                             ui=DummyInteractionHandler())
        token = set_runtime_context(ctx)
        gatherer, architect, fixer, auditor = make_pipeline_agents(st, None)

        _FIXER_SCHEMA = ('{"patches":[{"filepath":"file.py","code":"full new file content",'
                         '"change_summary":"what changed","lines_changed":"1-10"}],'
                         '"no_changes_needed":false,"fixer_notes":""}')

        def _stage(name, agent, desc, validator=None):
            """Этап конвейера. Если validator(raw) вернул False (модель выдала
            невалидный JSON — сервер мог 'fail open'), один повтор с усиленной
            инструкцией формата. Та же страховка, что в Демоне."""
            cached = ckpt.get(name)
            if cached:
                _p(name, "из чекпойнта")
                return cached

            def _once(d, expected="результат"):
                task = Task(description=d, agent=agent, expected_output=expected)
                raw = str(safe_kickoff(Crew(agents=[agent], tasks=[task]), st))
                return getattr(task.output, "raw", None) or raw

            # Fixer получает явную JSON-схему в expected_output (cloud-модели не знают FixerOutput).
            exp = (f"JSON строго по схеме (no_changes_needed ВСЕГДА false когда есть изменения): {_FIXER_SCHEMA}"
                   if name == "fix" else "результат")
            raw = _once(desc, expected=exp)
            if validator and not validator(raw):
                _p(name, "повтор (невалидный JSON)…")
                raw2 = _once(desc + "\n\nВНИМАНИЕ: предыдущий ответ был невалиден. "
                             "Верни СТРОГО валидный JSON и ничего больше — без markdown, "
                             "пояснений и текста до/после.", expected=exp)
                if validator(raw2):
                    raw = raw2
            ckpt.save(name, raw)
            return raw

        # Валидаторы JSON-этапов (парсер вернул структурированный результат?).
        def _v_g(r):
            try: return parse_gatherer_output(r).is_structured
            except Exception: return False
        def _v_a(r):
            try: return parse_architect_output(r).is_structured
            except Exception: return False
        def _v_f(r):
            try:
                o = parse_fixer_output(r)
                return o.is_structured or o.no_changes_needed or bool(o.patches)
            except Exception: return False

        _p("gather", "Сбор контекста…")
        g_raw = _stage("gather", gatherer, f"Собери контекст по задаче:\n{request}",
                       validator=_v_g)
        manifest = parse_gatherer_output(g_raw)
        maybe_unload_between(st, "gatherer", "architect")

        _p("architect", "Построение плана…")
        a_raw = _stage("architect", architect,
                       f"Задача:\n{request}\nМанифест:\n{manifest.model_dump_json()}",
                       validator=_v_a)
        plan = parse_architect_output(a_raw)
        maybe_unload_between(st, "architect", "coder")

        _p("fix", "Написание кода…")
        f_raw = _stage("fix", fixer, f"План:\n{plan.model_dump_json()}", validator=_v_f)
        fixer_out = parse_fixer_output(f_raw)
        patches = fixer_output_to_patch_models(fixer_out)
        if not patches or fixer_out.no_changes_needed:
            ckpt.clear()
            res.ok = True; res.verdict = "Изменений не требуется"; res.summary = "Fixer не предложил патчей."
            return res
        overlay.apply_dry_fixes(patches)
        maybe_unload_between(st, "coder", "auditor")

        _p("audit", "Проверка качества…")
        au_raw = _stage("audit", auditor,
                        f"План:\n{plan.model_dump_json()}\nПроверь реализацию, верни 'Вердикт: ОК' или 'ОТКЛОНЕНО'.")
        audit = parse_auditor_verdict(au_raw)
        ckpt.clear()

        # Готовим diff'ы для показа в UI (не применяя в реальный проект).
        for p in patches:
            try:
                old = ""
                read_path = overlay.resolve_read(p.filepath)
                # читаем оригинал из root (не overlay) для честного diff
                import os as _os
                orig = _os.path.join(overlay.root, p.filepath)
                if _os.path.exists(orig):
                    with open(orig, "r", encoding="utf-8", errors="replace") as fh:
                        old = fh.read()
                res.diffs.append((p.filepath, generate_unified_diff(old, p.code, p.filepath)))
                res.patches.append((p.filepath, p.code))
            except Exception:
                res.patches.append((p.filepath, p.code))

        res.verdict = getattr(audit, "verdict", "") or au_raw[:80]
        res.ok = "ок" in res.verdict.lower() or "ok" in res.verdict.lower()
        res.summary = f"Файлов изменено: {len(patches)}. Вердикт аудита: {res.verdict}"
        res.overlay_dir = overlay.overlay
        return res
    except Exception as e:
        res.error = f"{type(e).__name__}: {e}"
        return res
    finally:
        if token is not None:
            try: reset_runtime_context(token)
            except Exception: pass
        # overlay НЕ чистим: его файлы нужны для apply_pipeline_patches.


def apply_pipeline_patches(patches: list, state=None) -> tuple[bool, str]:
    """Применяет подтверждённые патчи (filepath, code) в реальный проект с бэкапом."""
    try:
        from utils import load_session, SessionState, apply_fixes, PatchModel
    except Exception as e:
        return False, f"Ядро недоступно: {e}"
    st = state or (load_session() or SessionState())
    models = [PatchModel(filepath=fp, code=code) for fp, code in patches]
    try:
        applied = apply_fixes(models, st.project_path)
        return True, f"Применено файлов: {len(applied)}."
    except Exception as e:
        return False, f"Ошибка применения: {e}"


# ===========================================================================
# AI Cleaner (детерминированные сканеры + карантин)
# ===========================================================================

@dataclass
class CleanerItem:
    path: str
    size_mb: float
    category: str
    risk: str          # safe / warn / danger
    explanation: str = ""


def cleaner_scan(kind: str, root: str = "", min_size_mb: float = 5.0,
                 full_hash: bool = True) -> tuple[list[CleanerItem], str]:
    """Запускает сканер нужного типа (deterministic, без LLM). Возвращает (items, сообщение).
    kind: disk | downloads | dups | startup | report."""
    import json as _j
    try:
        import cleaner_tools as ct
    except Exception as e:
        return [], f"Cleaner недоступен: {e}"
    try:
        if kind == "disk":
            raw = ct.scan_disk_intelligent(root, min_size_mb)
        elif kind == "downloads":
            raw = ct.scan_downloads_folder(min_size_mb)
        elif kind == "dups":
            raw = ct.find_duplicate_files(root, min_size_mb, 60, full_hash)
        elif kind == "startup":
            raw = ct.scan_startup_entries()
        elif kind == "report":
            raw = ct.get_disk_usage_report(root)
        else:
            return [], f"Неизвестный сканер: {kind}"
        data = _j.loads(raw)
    except Exception as e:
        return [], f"Ошибка сканирования: {e}"

    items = []
    for it in data.get("items", []):
        items.append(CleanerItem(
            path=it.get("path", ""),
            size_mb=float(it.get("size_mb", 0) or 0),
            category=it.get("category", kind),
            risk=it.get("risk_hint", "warn"),
            explanation=it.get("explanation", ""),
        ))
    items.sort(key=lambda x: x.size_mb, reverse=True)
    msg = f"Найдено объектов: {len(items)} ({round(sum(i.size_mb for i in items),1)} МБ)."
    if data.get("error"):
        msg = data["error"]
    return items, msg


def cleaner_quarantine(paths: list[str], same_drive: bool = True) -> tuple[bool, str]:
    """Перемещает выбранные пути в карантин (обратимо)."""
    import json as _j
    try:
        import cleaner_tools as ct
    except Exception as e:
        return False, f"Cleaner недоступен: {e}"
    payload = _j.dumps({"items": [{"path": p, "reason": "ui"} for p in paths]})
    try:
        res = _j.loads(ct.move_to_quarantine(payload, same_drive))
        return True, (f"В карантин: {res.get('moved_count',0)} "
                      f"(освобождено {res.get('freed_mb',0)} МБ). Сессия: {res.get('session_id','')}")
    except Exception as e:
        return False, f"Ошибка: {e}"


def cleaner_sessions() -> list[dict]:
    import json as _j
    try:
        import cleaner_tools as ct
        return _j.loads(ct.list_quarantine_sessions()).get("sessions", [])
    except Exception:
        return []


def cleaner_undo(session_id: str) -> tuple[bool, str]:
    import json as _j
    try:
        import cleaner_tools as ct
        r = _j.loads(ct.undo_quarantine(session_id))
        return True, f"Восстановлено: {r.get('restored_count',0)}."
    except Exception as e:
        return False, f"Ошибка: {e}"


def cleaner_delete_forever(session_id: str) -> tuple[bool, str]:
    import json as _j
    try:
        import cleaner_tools as ct
        r = _j.loads(ct.execute_permanent_deletion(session_id))
        return True, f"Удалено навсегда: {r.get('permanently_deleted_count',0)} ({r.get('freed_mb',0)} МБ)."
    except Exception as e:
        return False, f"Ошибка: {e}"


# ===========================================================================
# Локальный сервер llama.cpp (управление)
# ===========================================================================

def load_current_session():
    """Возвращает SessionState из текущего .ai_session.json (для UI, без прямого импорта utils)."""
    try:
        from utils import load_session
        return load_session()
    except Exception:
        return None


def llama_list_models() -> list[dict]:
    try:
        import llama_manager as lm
        return lm.list_models()
    except Exception:
        return []


def llama_kv_options() -> list[dict]:
    try:
        import llama_manager as lm
        return lm.kv_cache_options()
    except Exception:
        return [{"label": "q8_0", "value": "q8_0", "desc": ""}]


def llama_status() -> dict:
    try:
        import llama_manager as lm
        return lm.server_status()
    except Exception as e:
        return {"running": False, "binary_found": False, "url": "", "model": "", "error": str(e)}


def llama_query_vram_usage() -> Optional[int]:
    """Текущее использование VRAM (МБ) через счётчики Windows Performance."""
    try:
        import llama_manager as lm
        return lm.query_vram_usage_mb()
    except Exception:
        return None


def llama_detect_vram(state: Optional["SessionState"] = None) -> Optional[int]:
    """Возвращает объём VRAM (МБ). Приоритет: vram_override_mb > автодетекция."""
    try:
        if state is not None:
            override = int(getattr(state, "vram_override_mb", 0) or 0)
            if override > 0:
                return override
        import llama_manager as lm
        return lm.detect_vram_mb()
    except Exception:
        return None


def llama_start(model_path: str, ctx: int = 8192, ngl: int = -1,
                kv_value: str = "q8_0", vram_mb: Optional[int] = None,
                lan_access: bool = False,
                state: Optional["SessionState"] = None,
                ctv: str = "") -> dict:
    """Запускает llama-server (ngl=-1 => автоподбор по VRAM).
    lan_access=True открывает доступ с телефона (0.0.0.0) с токеном.
    state передаётся для чтения vram_override_mb и kv_ctv.
    ctv — отдельное квантование V-кеша (если пусто, берётся из state.kv_ctv)."""
    try:
        import llama_manager as lm
        effective_vram = vram_mb
        if effective_vram is None and state is not None:
            override = int(getattr(state, "vram_override_mb", 0) or 0)
            if override > 0:
                effective_vram = override
        if not ctv and state is not None:
            ctv = getattr(state, "kv_ctv", "") or ""
        return lm.start_server(model_path, ctx, ngl, kv_value, effective_vram, lan_access, ctv=ctv)
    except Exception as e:
        return {"ok": False, "message": f"Ошибка: {e}", "status": {}}


def llama_stop() -> dict:
    try:
        import llama_manager as lm
        return lm.stop_server()
    except Exception as e:
        return {"ok": False, "message": f"Ошибка: {e}", "status": {}}


# ===========================================================================
# Auto QA (стресс-тесты ядра)
# ===========================================================================

QA_MODES = {
    "basic":       "Базовый прогон (задачи + чекпойнты)",
    "chaos":       "Хаос: устойчивость к галлюцинациям",
    "concurrency": "Конкуренция: гонка воркеров за очередь",
    "soak":        "Soak: утечки RAM / FD / потоков",
    "env-chaos":   "Враждебная среда (file-locks, 400-token, битьё конфигов)",
}


def run_qa_mode(mode: str, params: Optional[dict] = None) -> dict:
    """Запускает выбранный режим auto_qa и возвращает результат как dict + verdict.
    Тяжёлая операция — UI зовёт в фоновом потоке. Полностью изолирован (песочница)."""
    from dataclasses import asdict
    params = params or {}
    seed = int(params.get("seed", 0)) or None
    try:
        import auto_qa as qa
    except Exception as e:
        return {"verdict": "FAIL", "error": f"auto_qa недоступен: {e}"}

    import random as _r
    if seed is None:
        seed = _r.randint(1, 10_000_000)

    try:
        if mode == "basic":
            res = qa.run_qa(num_tasks=int(params.get("tasks", 20)),
                            fail_rate=int(params.get("fail_rate", 2)),
                            seed=seed,
                            exhaust_fraction=float(params.get("exhaust_fraction", 0.25)),
                            hang_fraction=float(params.get("hang_fraction", 0.0)),
                            verbose=False)
            d = asdict(res)
            d["verdict"] = qa._verdict(res)[0]
        elif mode == "chaos":
            res = qa.run_chaos(int(params.get("chaos_iters", 200)), seed, verbose=False)
            d = asdict(res)
            d["verdict"] = "PASS" if (res.crashed == 0 and res.path_escapes == 0) else "FAIL"
        elif mode == "concurrency":
            res = qa.run_concurrency(int(params.get("workers", 8)),
                                     int(params.get("tasks", 200)), seed, verbose=False)
            d = asdict(res)
            d["verdict"] = "PASS" if (res.double_claims == 0 and res.pidlock_ok) else "FAIL"
        elif mode == "soak":
            res = qa.run_soak(int(params.get("soak_iters", 300)), seed, verbose=False)
            d = asdict(res)
            leak = ((res.rss_end_kb - res.rss_start_kb) // 1024 > 96) or \
                   ((res.fd_end - res.fd_start) > 32) or bool(res.leaked_threads) or res.passed == 0
            d["verdict"] = "FAIL" if leak else "PASS"
        elif mode == "env-chaos":
            res = qa.run_env_chaos(seed, verbose=False)
            d = asdict(res)
            d["verdict"] = "PASS" if res.crashed == 0 else "FAIL"
        else:
            return {"verdict": "FAIL", "error": f"Неизвестный режим: {mode}"}
        d["seed"] = seed
        d["mode"] = mode
        record_qa_run(d)
        return d
    except Exception as e:
        err = {"verdict": "FAIL", "error": f"{type(e).__name__}: {e}", "mode": mode, "seed": seed}
        record_qa_run(err)
        return err


# ===========================================================================
# Дашборд здоровья фабрики (агрегатор)  [идея 1]
# ===========================================================================

@dataclass
class HealthSnapshot:
    daemon_running: bool = False
    daemon_pid: Optional[int] = None
    llama_running: bool = False
    llama_model: str = ""
    counts: dict = field(default_factory=dict)
    quotas: list = field(default_factory=list)
    last_qa_verdict: str = ""
    last_qa_mode: str = ""
    ts: float = 0.0


def get_health() -> HealthSnapshot:
    """Сводка состояния всей фабрики одним объектом — для обзорного дашборда.
    Дёшево: переиспользует уже существующие читалки, без тяжёлых вызовов."""
    board = get_board()
    h = HealthSnapshot(
        daemon_running=board.daemon_running,
        daemon_pid=board.daemon_pid,
        counts=board.counts,
        ts=time.time(),
    )
    try:
        st = llama_status()
        h.llama_running = bool(st.get("running"))
        h.llama_model = st.get("model", "")
    except Exception:
        pass
    hist = qa_history(limit=1)
    if hist:
        h.last_qa_verdict = hist[0].get("verdict", "")
        h.last_qa_mode = hist[0].get("mode", "")
    return h  # квоты НЕ тянем здесь (сеть) — UI грузит их отдельно в фоне


# ===========================================================================
# Живой лог Демона  [идея 3]
# ===========================================================================

DAEMON_LOG = os.path.join(AUTO_DIR, "daemon.log")


def read_daemon_log(max_lines: int = 200, level: str = "") -> list[str]:
    """Возвращает хвост лога Демона (если он пишет в файл). level: фильтр
    INFO/WARNING/ERROR (пусто = все). Демон должен быть запущен с файловым логом."""
    path = DAEMON_LOG
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-max_lines * 3:]  # запас под фильтр
    except OSError:
        return []
    if level:
        lines = [ln for ln in lines if level.upper() in ln]
    return [ln.rstrip("\n") for ln in lines[-max_lines:]]


# ===========================================================================
# История прогонов QA  [идея 5]
# ===========================================================================

_QA_HISTORY_FILE = os.path.join(AUTO_DIR, "qa_history.json")


def record_qa_run(result: dict) -> None:
    """Сохраняет краткую сводку QA-прогона (для тренда устойчивости)."""
    try:
        _ensure_dirs()
        hist = []
        if os.path.exists(_QA_HISTORY_FILE):
            with open(_QA_HISTORY_FILE, "r", encoding="utf-8") as f:
                hist = json.load(f) or []
        entry = {
            "ts": time.time(), "mode": result.get("mode", "?"),
            "verdict": result.get("verdict", "?"), "seed": result.get("seed"),
            # пара ключевых метрик для тренда (что есть в данном режиме)
            "passed": result.get("passed"), "failed": result.get("failed"),
            "crashed": result.get("crashed"), "survived": result.get("survived"),
            "double_claims": result.get("double_claims"),
            "rss_delta_kb": result.get("rss_delta_kb"),
        }
        hist.append(entry)
        hist = hist[-200:]  # храним последние 200
        with open(_QA_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False)
    except Exception:
        pass


def qa_history(limit: int = 50) -> list[dict]:
    """Последние прогоны QA, новейшие сверху."""
    try:
        if not os.path.exists(_QA_HISTORY_FILE):
            return []
        with open(_QA_HISTORY_FILE, "r", encoding="utf-8") as f:
            hist = json.load(f) or []
        return list(reversed(hist))[:limit]
    except Exception:
        return []


# ===========================================================================
# Профили конфигурации  [идея 6]
# ===========================================================================

# Профили храним в ОТДЕЛЬНОЙ папке user_data/ (рядом с проектом, но не среди рабочих
# файлов задач). Это устойчивее к ручному слиянию архивов и к антивирусу/облачной
# синхронизации, которые могут перехватывать файлы во время записи.
_USER_DATA_DIR = os.environ.get("COREPILOT_USER_DATA",
                                os.path.join(_PROJECT_DIR, "user_data"))
_PROFILES_FILE = os.path.join(_USER_DATA_DIR, "config_profiles.json")
# Старые расположения — для одноразовой миграции (профили не теряются при переходе).
_LEGACY_PROFILE_PATHS = [
    os.path.join(AUTO_DIR, "config_profiles.json"),
    os.path.join(_PROJECT_DIR, "config_profiles.json"),
]


def _read_profiles_file(path: str) -> dict:
    """Читает JSON-профили из path; пусто/битый -> {}."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _recover_stuck_profile() -> dict:
    """Подхватывает профили из застрявших временных файлов (.processing/.tmp),
    если основной .json отсутствует. Такие огрызки оставляет прерванная запись
    или антивирус/облачная синхронизация на Windows. Возвращает найденные профили."""
    candidates = []
    for base in [_PROFILES_FILE] + _LEGACY_PROFILE_PATHS:
        for suff in (".processing", ".tmp"):
            p = base + suff
            if os.path.exists(p):
                candidates.append(p)
        # рядом могут лежать config_profiles.json.processing и т.п.
        d = os.path.dirname(base)
        if os.path.isdir(d):
            for fn in os.listdir(d):
                if fn.startswith("config_profiles") and (fn.endswith(".processing") or fn.endswith(".tmp")):
                    candidates.append(os.path.join(d, fn))
    for p in candidates:
        prof = _read_profiles_file(p)
        if prof:
            return prof
    return {}


def _load_profiles_raw() -> dict:
    # 1) основной файл
    if os.path.exists(_PROFILES_FILE):
        prof = _read_profiles_file(_PROFILES_FILE)
        if prof:
            return prof
    # 2) миграция со старых расположений (один раз перенесём в user_data/)
    for legacy in _LEGACY_PROFILE_PATHS:
        if os.path.exists(legacy):
            prof = _read_profiles_file(legacy)
            if prof:
                _write_profiles_atomic(prof)  # перенос в новое место
                return prof
    # 3) восстановление из застрявших .processing/.tmp (вернёт «потерянные» профили)
    prof = _recover_stuck_profile()
    if prof:
        _write_profiles_atomic(prof)  # закрепим нормальным файлом
        return prof
    return {}


def _write_profiles_atomic(profiles: dict) -> None:
    """Атомарно записывает профили: пишем во временный файл и os.replace.
    На Windows os.replace атомарен — антивирус/синхронизация не оставят огрызок
    вместо валидного файла, и чтение никогда не увидит «полузаписанное»."""
    _ensure_user_data_dir()
    tmp = _PROFILES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _PROFILES_FILE)


def _ensure_user_data_dir() -> None:
    try:
        os.makedirs(_USER_DATA_DIR, exist_ok=True)
    except Exception:
        pass


def list_profiles() -> list[str]:
    return sorted(_load_profiles_raw().keys())


def save_profile(name: str) -> tuple[bool, str]:
    """Сохраняет ТЕКУЩИЕ настройки под именем профиля."""
    name = (name or "").strip()
    if not name:
        return False, "Имя профиля пустое."
    try:
        profiles = _load_profiles_raw()
        profiles[name] = load_settings()
        _write_profiles_atomic(profiles)
        return True, f"Профиль «{name}» сохранён."
    except Exception as e:
        return False, f"Ошибка: {e}"


def apply_profile(name: str) -> tuple[bool, str]:
    """Применяет сохранённый профиль (записывает его настройки в сессию)."""
    profiles = _load_profiles_raw()
    if name not in profiles:
        return False, f"Профиль «{name}» не найден."
    ok, msg = save_settings(profiles[name])
    return ok, (f"Профиль «{name}» применён. " + msg) if ok else msg


def delete_profile(name: str) -> tuple[bool, str]:
    profiles = _load_profiles_raw()
    if name not in profiles:
        return False, "Профиль не найден."
    del profiles[name]
    try:
        _write_profiles_atomic(profiles)
        return True, f"Профиль «{name}» удалён."
    except Exception as e:
        return False, f"Ошибка: {e}"


def export_profiles(dest_path: str) -> tuple[bool, str]:
    """Сохраняет ВСЕ профили в указанный JSON-файл (явная резервная копия).
    Пользователь может положить его куда угодно и не бояться слияния/антивируса."""
    try:
        profiles = _load_profiles_raw()
        if not profiles:
            return False, "Нет профилей для экспорта."
        dest_path = (dest_path or "").strip()
        if not dest_path:
            return False, "Не указан путь для экспорта."
        if not dest_path.lower().endswith(".json"):
            dest_path += ".json"
        with open(dest_path, "w", encoding="utf-8") as f:
            json.dump(profiles, f, ensure_ascii=False, indent=2)
        return True, f"Экспортировано профилей: {len(profiles)} → {dest_path}"
    except Exception as e:
        return False, f"Ошибка экспорта: {e}"


def import_profiles(src_path: str, overwrite: bool = False) -> tuple[bool, str]:
    """Загружает профили из JSON-файла и добавляет к существующим.
    overwrite=False — не затирать профили с совпадающими именами."""
    try:
        src_path = (src_path or "").strip()
        if not src_path or not os.path.exists(src_path):
            return False, f"Файл не найден: {src_path}"
        incoming = _read_profiles_file(src_path)
        if not incoming:
            return False, "В файле нет валидных профилей."
        current = _load_profiles_raw()
        added, skipped = 0, 0
        for name, data in incoming.items():
            if name in current and not overwrite:
                skipped += 1
                continue
            current[name] = data
            added += 1
        _write_profiles_atomic(current)
        msg = f"Импортировано: {added}."
        if skipped:
            msg += f" Пропущено (уже есть): {skipped}."
        return True, msg
    except Exception as e:
        return False, f"Ошибка импорта: {e}"


# ===========================================================================
# Кнопка паники: остановить всё  [идея 4]
# ===========================================================================

def panic_stop() -> dict:
    """Аварийно останавливает демон и llama-сервер. Возвращает отчёт по каждому."""
    report = {}
    try:
        ok, msg = stop_daemon(); report["daemon"] = msg
    except Exception as e:
        report["daemon"] = f"ошибка: {e}"
    try:
        r = llama_stop(); report["llama"] = r.get("message", "")
    except Exception as e:
        report["llama"] = f"ошибка: {e}"
    return report


def _selftest() -> int:
    """Быстрая проверка слоя на временной очереди (без демона и сети)."""
    import tempfile, shutil
    global AUTO_DIR, DONE_DIR, FAILED_DIR, PID_FILE
    sandbox = tempfile.mkdtemp(prefix="corepilot_svc_")
    AUTO_DIR = os.path.join(sandbox, "auto_tasks")
    DONE_DIR = os.path.join(AUTO_DIR, "done")
    FAILED_DIR = os.path.join(AUTO_DIR, "failed")
    PID_FILE = os.path.join(AUTO_DIR, ".daemon.pid")
    _ensure_dirs()

    # DAG: T1 -> T2 -> T3; T1 done, T2 failed => T3 должна стать frozen.
    enqueue_task({"task_id": "T1", "title": "база", "depends_on": []})
    enqueue_task({"task_id": "T2", "title": "модуль", "depends_on": ["T1"]})
    enqueue_task({"task_id": "T3", "title": "тесты", "depends_on": ["T2"]})
    # Симулируем результаты: T1 -> done, T2 -> failed.
    for tid, dst in (("T1", DONE_DIR), ("T2", FAILED_DIR)):
        for p in _find_task_files(tid):
            data = _read_task_file(p)
            if data and str(data["task_id"]) == tid:
                p.unlink()
                tgt = os.path.join(dst, f"{tid}.json")
                with open(tgt, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)

    board = get_board()
    by = {t.task_id: t for t in board.tasks}
    assert by["T1"].status == STATUS_DONE, by["T1"].status
    assert by["T2"].status == STATUS_FAILED, by["T2"].status
    assert by["T3"].status == STATUS_FROZEN, f"T3 ожидался frozen, получили {by['T3'].status}"
    print("✓ DAG-статусы: T1=done, T2=failed, T3=frozen (каскад заморожен)")

    # requeue T2 -> снова pending, метки провала сняты.
    ok, _ = requeue_task("T2")
    assert ok
    b2 = get_board(); t2 = next(t for t in b2.tasks if t.task_id == "T2")
    assert t2.status == STATUS_PENDING and not t2.failed_reason
    print("✓ requeue: T2 вернулась в pending без меток провала")

    assert board.counts[STATUS_DONE] == 1 and board.counts[STATUS_FAILED] == 1
    print("✓ counts корректны")

    running, _ = daemon_status()
    assert running is False
    print("✓ daemon_status: не запущен (как и ожидалось)")

    shutil.rmtree(sandbox, ignore_errors=True)
    print("\nСЕРВИСНЫЙ СЛОЙ: САМОПРОВЕРКА ПРОЙДЕНА ✅")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
