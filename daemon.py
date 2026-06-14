from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import time
import tomllib
import concurrent.futures
from pathlib import Path

from crewai import Crew, Task

from agents import safe_kickoff, init_api_keys, install_secret_redaction
from router import route_task
from context_manager import DatabaseManager
from utils import (
    DummyInteractionHandler,
    ProjectOverlay,
    RuntimeContext,
    SessionState,
    load_session,
    reset_runtime_context,
    set_runtime_context,
    strict_parse_fixes,
    atomic_write_text,
)
from pipeline_parser import (parse_fixer_output as _parse_fixer_output_json,
                             parse_gatherer_output, parse_architect_output)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [DAEMON] %(levelname)s %(message)s")
install_secret_redaction()  # редактируем секреты в логах/трейсах Демона
logger = logging.getLogger("DAEMON")

# AUTO_DIR — то же правило, что в service_layer: привязка к РАСПОЛОЖЕНИЮ проекта
# (__file__), а не к рабочей папке. Иначе демон и UI видят разные auto_tasks.
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
AUTO_DIR = os.environ.get("COREPILOT_AUTO_DIR", os.path.join(_PROJECT_DIR, "auto_tasks"))

# Системные файлы, которые МОГУТ оказаться в AUTO_DIR (от старых версий), но НЕ
# являются задачами. Демон обязан их игнорировать — иначе он принимал, например,
# config_profiles.json за задачу, переименовывал в .processing, и профили «пропадали».
_SYSTEM_FILES = {
    "config_profiles.json", "profiles.json", "qa_history.json",
    ".ai_session.json", "ai_session.json", "no_response_format.json",
    "quota_cache.json",
}


def _is_system_file(name: str) -> bool:
    """True, если имя файла (в т.ч. с суффиксом .processing/.tmp) — системный
    файл, а не задача. Защищает профили/настройки/историю от обработки демоном."""
    base = name
    for suff in (".processing", ".tmp"):
        if base.endswith(suff):
            base = base[: -len(suff)]
    return base in _SYSTEM_FILES or base.startswith("config_profiles")
DONE_DIR = os.path.join(AUTO_DIR, "done")
FAILED_DIR = os.path.join(AUTO_DIR, "failed")
PID_FILE = os.path.join(AUTO_DIR, ".daemon.pid")
HEARTBEAT_FILE = os.path.join(AUTO_DIR, ".daemon.heartbeat")

# Дублируем лог в файл, чтобы GUI мог показывать живой хвост (идея «лог в UI»).
# Ротация лога через RotatingFileHandler: лог не растёт бесконечно даже если демон
# крутится неделями (раньше усечение срабатывало только при старте — за совет спасибо
# ревью). 2 МБ на файл, 1 бэкап.
try:
    os.makedirs(AUTO_DIR, exist_ok=True)
    _log_path = os.path.join(AUTO_DIR, "daemon.log")
    from logging.handlers import RotatingFileHandler
    _fh = RotatingFileHandler(_log_path, maxBytes=2_000_000, backupCount=1, encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s [DAEMON] %(levelname)s %(message)s"))
    from agents import _RedactSecretsFilter  # тот же редактор секретов и в файле
    _fh.addFilter(_RedactSecretsFilter())
    logging.getLogger().addHandler(_fh)
except Exception:
    pass  # файловый лог — удобство, его отсутствие не критично


POLL_INTERVAL = 2
STALE_PROCESSING_AFTER_SEC = 30 * 60
HEARTBEAT_EVERY_N_TICKS = 15
TICK_RESET_AT = 100_000

_NEEDS_REVIEW_SUFFIX = ".NEEDS_REVIEW"
_ORACLE_RETRY_THRESHOLD = 2
# Сколько ОДИНАКОВЫХ (нормализованных) ошибок подряд = «модель застряла».
# Две одинаковые подряд -> эскалация на Оракула-Титана (ждать третью нет смысла).
_STUCK_REPEAT_THRESHOLD = 2
# Жёсткий потолок ОБЩЕГО числа попыток на задачу. Защита от чередующихся ошибок
# (A,B,A,B...), на которых детектор повторов не сработает и задача крутилась бы
# вечно. По достижении — финальный провал без дальнейших ретраев.
_MAX_TOTAL_ATTEMPTS = 5
# На какой попытке пробуем Титана, даже если ошибки РАЗНЫЕ (не повторяются).
# Раньше повтора, но после нескольких безуспешных итераций.
_TITAN_ATTEMPT_THRESHOLD = 3
_MAX_REASON_LEN = 1500

def _acquire_lock() -> bool:
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r") as f: old_pid = int(f.read().strip())
            import psutil
            if psutil.pid_exists(old_pid):
                logger.warning("Демон уже запущен (PID=%d). Выход.", old_pid)
                return False
            os.remove(PID_FILE)
        except Exception: pass

    try:
        with open(PID_FILE, "x") as f: f.write(str(os.getpid()))
        return True
    except FileExistsError:
        return False
    except Exception as e:
        logger.error("Не удалось записать PID: %s", e)
        return False

def _release_lock() -> None:
    try:
        if os.path.exists(PID_FILE): os.remove(PID_FILE)
    except Exception: pass

def _heartbeat(db: DatabaseManager, status: str, details: str = "") -> None:
    try:
        db.log_daemon_heartbeat(os.getpid(), status, details)
        atomic_write_text(HEARTBEAT_FILE, str(time.time()))
    except Exception as e:
        logger.debug("Ошибка heartbeat: %s", e)

def _reclaim_stale_processing_files() -> None:
    now = time.time()
    try:
        for fname in os.listdir(AUTO_DIR):
            if not fname.endswith(".processing"): continue
            p = Path(AUTO_DIR) / fname
            if not p.is_file(): continue
            age = now - p.stat().st_mtime
            if age > STALE_PROCESSING_AFTER_SEC:
                try:
                    p.rename(Path(AUTO_DIR) / fname[: -len(".processing")])
                except Exception: pass
    except Exception: pass

def _build_prompt_from_task(task_data: dict) -> str:
    title = task_data.get("title", "Без названия")
    description = task_data.get("description", "").strip()
    target_files = task_data.get("target_files", [])
    context = task_data.get("context_notes", "").strip()

    parts = [f"Задача: {title}."]
    if description: parts.append(f"Детали: {description}")
    if target_files: parts.append(f"Целевые файлы: {', '.join(str(f) for f in target_files)}")
    if context: parts.append(f"Контекст проекта: {context}")
    return " ".join(parts)

def _write_failure_metadata(claim: Path, task_data: dict, failed_reason: str) -> Path:
    task_data["failed_reason"] = failed_reason[:_MAX_REASON_LEN]
    task_data["failed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    task_data["_needs_review"] = True

    try:
        atomic_write_text(str(claim), json.dumps(task_data, ensure_ascii=False, indent=2))
    except Exception as e:
        logger.error("Не удалось записать метаданные провала в %s: %s", claim, e)
        return claim

    name = claim.name
    if name.endswith(".json.processing"):
        new_name = f"{name[: -len('.json.processing')]}{_NEEDS_REVIEW_SUFFIX}.json.processing"
    else:
        new_name = name.replace(".processing", f"{_NEEDS_REVIEW_SUFFIX}.processing")

    new_path = claim.parent / new_name
    try:
        claim.rename(new_path)
        return new_path
    except Exception as e:
        logger.warning("Не удалось переименовать %s в needs-review: %s", claim, e)
        return claim

def _normalize_error(reason: str) -> str:
    """Приводит причину провала к «скелету» для сравнения повторов: убирает числа,
    пути, hex-адреса, таймстампы. Тогда 'line 42' и 'line 88' — одна ошибка."""
    import re, hashlib
    if not reason:
        return ""
    s = reason.lower()
    s = re.sub(r"0x[0-9a-f]+", "", s)
    s = re.sub(r"\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}:\d{2}", "", s)
    s = re.sub(r"[a-z]:\\[^\s'\"]+|/[^\s'\"]+", "", s)
    s = re.sub(r"\d+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return hashlib.sha1(s[:500].encode("utf-8", "replace")).hexdigest()[:16]


def _escalate_to_titan(task_data: dict, state: SessionState, db) -> bool:
    """Модель застряла (повтор ошибки). Оракул-Титан ПЕРЕПИСЫВАЕТ решение:
    облако → локальный Титан. Применяет полученные патчи в проект напрямую.
    Возвращает True, если патчи получены и применены."""
    from agents import consult_oracle_titan
    from utils import strict_parse_fixes, apply_fixes
    broken = task_data.get("_last_broken_code", "")
    reason = task_data.get("failed_reason", "")
    raw, source = consult_oracle_titan(task_data, broken, reason, state)
    if not raw:
        logger.warning("Эскалация на Титана не дала результата (все пути недоступны).")
        return False
    patches = []
    try:
        patches = _parse_fixer_output_json(raw).patches or []
    except Exception:
        pass
    if not patches:
        res = strict_parse_fixes(raw)
        if res.is_valid:
            patches = res.patches
    if not patches:
        logger.warning("Титан (%s) ответил, но без валидных патчей.", source)
        return False
    try:
        applied = apply_fixes(patches, state.project_path)
        logger.info("Титан (%s) переписал решение: файлов применено %d.", source, len(applied))
        task_data["_titan_source"] = source
        return bool(applied)
    except Exception as e:
        logger.warning("Не удалось применить патчи Титана: %s", str(e)[:120])
        return False


def _consult_oracle_for_task(task_data: dict, failed_reason: str, state: SessionState) -> str:
    if not getattr(state, "oracle_enabled", True): return ""
    import litellm
    from agents import next_api_key, BACKEND_CONFIGS

    mode = getattr(state, "mode_oracle", "cloud")
    model = (getattr(state, "model_oracle", "") or "").strip()
    if not model: return ""

    if mode == "local":
        backend = getattr(state, "backend_oracle", "lmstudio")
        cfg = BACKEND_CONFIGS.get(backend, BACKEND_CONFIGS["lmstudio"])
        kwargs = dict(model=f"{cfg['prefix']}{model}", base_url=cfg["base_url"], api_key=cfg["api_key"])
    else:
        provider = getattr(state, "provider_oracle", "groq")
        api_key = next_api_key(provider) or os.environ.get(f"{provider.upper()}_API_KEY", "")
        if not api_key: return ""
        kwargs = dict(model=f"{provider}/{model}", api_key=api_key)

    _system = "Ты — Мастер-Оракул, эксперт по отладке на Windows / Python 3.12. Дай КРАТКУЮ (до 300 слов) техническую инструкцию. Только конкретика без воды."
    _user_msg = f"ЗАДАЧА: {task_data.get('title', '?')}\nОПИСАНИЕ: {task_data.get('description', '')[:500]}\nПРИЧИНА ПОСЛЕДНЕГО ПРОВАЛА:\n{failed_reason[:800]}"
    try:
        resp = litellm.completion(messages=[{"role": "system", "content": _system}, {"role": "user", "content": _user_msg}], max_tokens=512, temperature=0.1, timeout=45, **kwargs)
        return resp.choices[0].message.content.strip()
    except Exception: return ""

def _task_id_of(path: Path) -> Optional[str]:
    """Извлекает логический task_id из JSON-файла задачи (в очереди/архиве)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = json.loads(f.read())
        tid = data.get("task_id")
        return str(tid) if tid is not None else None
    except Exception:
        return None


# Кэш карты статусов: пересобираем её только если изменилась mtime хотя бы одной
# из трёх папок очереди. Иначе сотни файлов в done/failed перечитывались бы вхолостую
# на КАЖДОМ проходе (раз в POLL_INTERVAL). Файловая архитектура не меняется —
# только убираем лишний дисковый труд.
_status_cache: dict = {"sig": None, "map": {}}


def _dir_signature() -> tuple:
    """Подпись состояния очереди: (mtime, число записей) по каждой папке.
    Меняется при любом добавлении/удалении/перемещении файла."""
    sig = []
    for d in (DONE_DIR, FAILED_DIR, AUTO_DIR):
        try:
            st = os.stat(d)
            # mtime каталога меняется при add/remove; count ловит замену файла тем же mtime.
            n = sum(1 for _ in os.scandir(d))
            sig.append((round(st.st_mtime, 3), n))
        except OSError:
            sig.append((0.0, 0))
    return tuple(sig)


def _collect_task_statuses() -> dict[str, str]:
    """Карта task_id -> статус по всему дереву очереди.
    done (в DONE_DIR), failed (в FAILED_DIR), pending/processing (в AUTO_DIR).
    failed имеет приоритет: если хоть одна копия задачи провалена — она failed.
    Результат кэшируется по mtime папок: при отсутствии изменений файлы не
    перечитываются повторно."""
    sig = _dir_signature()
    if sig == _status_cache["sig"]:
        return _status_cache["map"]

    status: dict[str, str] = {}

    def _scan(dirpath: str, st: str):
        try:
            entries = list(os.scandir(dirpath))   # DirEntry с кэшированным stat
        except OSError:
            return
        for e in entries:
            n = e.name
            if not (n.endswith(".json") or ".json" in n):
                continue
            tid = _task_id_of(Path(e.path))
            if not tid:
                continue
            # failed > done > pending: не понижаем статус.
            prev = status.get(tid)
            if prev == "failed":
                continue
            if st == "failed" or prev is None or (st == "done" and prev == "pending"):
                status[tid] = st

    _scan(DONE_DIR, "done")
    _scan(FAILED_DIR, "failed")
    _scan(AUTO_DIR, "pending")

    _status_cache["sig"] = sig
    _status_cache["map"] = status
    return status


def _deps_satisfied(task_data: dict, statuses: dict[str, str]) -> tuple[bool, str]:
    """Можно ли брать задачу в работу. Возвращает (можно, причина_если_нет).
    Правило: все родители из depends_on должны иметь статус done.
    Если родитель failed/отсутствует/ещё pending — задача замораживается."""
    deps = task_data.get("depends_on") or []
    if not isinstance(deps, list) or not deps:
        return True, ""
    for d in deps:
        d = str(d)
        st = statuses.get(d)
        if st == "done":
            continue
        if st == "failed":
            return False, f"родитель {d} провалён (failed) — задача заморожена"
        if st is None:
            return False, f"родитель {d} отсутствует в очереди — заморожено"
        return False, f"родитель {d} ещё не выполнен ({st})"
    return True, ""


def process_tasks(db: DatabaseManager, state: SessionState) -> None:
    try:
        entries = [e.name for e in os.scandir(AUTO_DIR)
                   if e.name.endswith(".json") and not _is_system_file(e.name)]
    except OSError:
        return
    if not entries:
        return  # пустая очередь — частый случай между задачами, не тратим работу

    # Копия снимка: process_tasks обновляет статусы по ходу прохода (выполненная
    # задача разблокирует потомков), а возвращаемая карта может быть кэшем — не портим её.
    statuses = dict(_collect_task_statuses())

    for filename in sorted(entries):
        if not filename.endswith(".json"): continue
        original_path = Path(AUTO_DIR) / filename

        # === Строгая DAG-блокировка: берём задачу ТОЛЬКО если зависимости выполнены ===
        td = None
        try:
            with open(original_path, "r", encoding="utf-8", errors="replace") as f:
                td = json.loads(f.read())
        except Exception:
            td = None
        if isinstance(td, dict):
            ok, reason = _deps_satisfied(td, statuses)
            if not ok:
                logger.info("⏸  Задача %s заморожена: %s", filename, reason)
                _heartbeat(db, "frozen", f"{filename}: {reason}")
                continue

        processing_path = Path(AUTO_DIR) / (filename + ".processing")
        try: original_path.rename(processing_path)  # атомарный захват
        except OSError: continue

        if hasattr(db, "update_manager_task_status"):
            try: db.update_manager_task_status(filename, "processing")
            except Exception as e: logger.debug("update_manager_task_status(processing) не удался: %s", e)

        _heartbeat(db, "processing", filename)
        success, final_processing_path = _execute_task(processing_path, db, state)
        archive_name = final_processing_path.name
        if archive_name.endswith(".processing"): archive_name = archive_name[: -len(".processing")]
        dst = Path(DONE_DIR if success else FAILED_DIR) / archive_name

        try: shutil.move(str(final_processing_path), str(dst))
        except Exception as e:
            # Критично: незаархивированная задача останется в очереди и будет
            # обработана повторно. Логируем как ошибку.
            logger.error("Не удалось заархивировать задачу %s в %s: %s",
                         final_processing_path.name, dst, e)

        # Обновляем снимок статусов: выполненная задача может разблокировать потомков
        # уже в этом же проходе.
        tid = _task_id_of(dst)
        if tid:
            statuses[tid] = "done" if success else "failed"

        if hasattr(db, "update_manager_task_status"):
            try: db.update_manager_task_status(filename, "done" if success else "failed")
            except Exception as e: logger.debug("update_manager_task_status(done/failed) не удался: %s", e)

def _apply_fixer_patches(raw_patch: str, overlay) -> None:
    """Парсит вывод фиксера и применяет патчи в overlay (без LLM)."""
    try: patches = _parse_fixer_output_json(raw_patch).patches or []
    except Exception: patches = []
    if not patches:
        res = strict_parse_fixes(raw_patch)
        if res.is_valid: patches = res.patches
    overlay.apply_dry_fixes(patches)

def _execute_task(claim: Path, db: DatabaseManager, state: SessionState) -> tuple[bool, Path]:
    try:
        with open(claim, "r", encoding="utf-8", errors="replace") as f: raw = f.read().strip()
    except Exception: return False, claim
    if not raw: return False, claim
    try: task_data = json.loads(raw)
    except json.JSONDecodeError as e: return False, _write_failure_metadata(claim, {"_raw": raw[:200]}, f"JSONDecodeError: {e}")

    content = _build_prompt_from_task(task_data)
    retry_count = int(task_data.get("_retry_count", 0))

    # --- Жёсткий потолок попыток: защита от бесконечного цикла на ЧЕРЕДУЮЩИХСЯ
    #     ошибках (детектор повторов их не ловит). Достигли потолка -> финальный
    #     провал, никаких ретраев, потомки замёрзнут штатно (DAG). ---
    if retry_count >= _MAX_TOTAL_ATTEMPTS:
        logger.error("Задача исчерпала лимит попыток (%d) — финальный провал.",
                     _MAX_TOTAL_ATTEMPTS)
        prev = task_data.get("failed_reason", "")
        return False, _write_failure_metadata(
            claim, task_data,
            f"Исчерпан лимит попыток ({_MAX_TOTAL_ATTEMPTS}). Последняя причина: {prev}"[:_MAX_REASON_LEN])

    # --- Детектор застревания: считаем ПОВТОРЫ одной и той же (нормализованной)
    #     ошибки. Две одинаковые подряд = модель залипла. ---
    prev_sig = task_data.get("_last_error_sig", "")
    cur_sig = _normalize_error(task_data.get("failed_reason", ""))
    repeat_count = int(task_data.get("_error_repeat", 0))
    if cur_sig and cur_sig == prev_sig:
        repeat_count += 1
    elif cur_sig:
        repeat_count = 1  # новая ошибка — счётчик повторов сброшен на «увидели 1 раз»
    task_data["_last_error_sig"] = cur_sig
    task_data["_error_repeat"] = repeat_count

    # Эскалация на Оракула-Титана по ДВУМ триггерам (Титан ПЕРЕПИСЫВАЕТ код):
    #   1) застряли на одной ошибке (repeat >= порога), ИЛИ
    #   2) накопилось достаточно попыток с РАЗНЫМИ ошибками (attempt >= порога) —
    #      это ловит чередующиеся ошибки до исчерпания лимита.
    _titan_patched = False
    stuck_on_repeat = repeat_count >= _STUCK_REPEAT_THRESHOLD
    stuck_on_attempts = retry_count >= _TITAN_ATTEMPT_THRESHOLD
    if stuck_on_repeat or stuck_on_attempts:
        logger.info("Эскалация на Титана: повтор=%d, попытка=%d.", repeat_count, retry_count)
        _titan_patched = _escalate_to_titan(task_data, state, db)
        if _titan_patched:
            task_data["_error_repeat"] = 0
            task_data["_last_error_sig"] = ""
    task_data["_retry_count"] = retry_count + 1

    ui_handler = DummyInteractionHandler(auto_approve=True)
    overlay = ProjectOverlay(state.project_path)
    ctx = RuntimeContext(state=state, ui=ui_handler, overlay=overlay, context_mgr=db)
    token = set_runtime_context(ctx)

    _audit_verdict = []
    try:
        from agents import maybe_unload_between
        from utils import PipelineCheckpoint

        gatherer, architect, fixer, auditor = route_task(content, state, ui_handler)

        # Чекпойнт по содержимому задачи: при сбое (исчерпаны ключи/таймаут) повторный
        # запуск продолжит с незавершённого этапа, а кэшированные выходы предыдущих
        # этапов подставляются как контекст следующему (поведение sequential-Crew
        # сохраняется, но каждый этап — отдельный возобновляемый Crew).
        ckpt = PipelineCheckpoint(state.project_path, content)
        timeout = getattr(state, "ui_step_timeout", 300) * 5  # на этап щедрее, чем в UI

        def _run_stage(name: str, agent, description: str, validator=None) -> str:
            """Возвращает raw-выход этапа: из чекпойнта либо свежим запуском.
            Если задан validator(raw)->bool и он вернул False (модель выдала
            невалидный JSON — сервер мог 'fail open'), делаем ОДИН повтор с явной
            инструкцией формата. Это страховка поверх response_format на случай,
            когда грамматика не применилась."""
            cached = ckpt.get(name)
            if cached:
                logger.info("↩️  Этап '%s' взят из чекпойнта.", name)
                return cached

            def _once(descr: str, expected: str = "результат этапа") -> str:
                task = Task(description=descr, agent=agent, expected_output=expected)
                crew = Crew(agents=[agent], tasks=[task])
                ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                fut = ex.submit(safe_kickoff, crew, state)
                try:
                    res = fut.result(timeout=timeout)
                finally:
                    ex.shutdown(wait=False, cancel_futures=True)
                return getattr(task.output, "raw", None) or str(res)

            _FIXER_SCHEMA = ('{"patches":[{"filepath":"file.py","code":"full new file content",'
                             '"change_summary":"what changed","lines_changed":"1-10"}],'
                             '"no_changes_needed":false,"fixer_notes":""}')
            exp = (f"JSON строго по схеме (no_changes_needed ВСЕГДА false когда есть изменения): {_FIXER_SCHEMA}"
                   if name == "fix" else "результат этапа")
            raw = _once(description, expected=exp)
            if validator and not validator(raw):
                logger.warning("Этап '%s': вывод не прошёл валидацию JSON — повтор с "
                               "усиленной инструкцией формата.", name)
                fix = (description + "\n\nВНИМАНИЕ: предыдущий ответ был невалидным. "
                       "Верни СТРОГО валидный JSON-объект и НИЧЕГО больше — "
                       "без markdown-обёрток, пояснений и текста до/после.")
                raw2 = _once(fix, expected=exp)
                if validator(raw2):
                    raw = raw2
                else:
                    logger.warning("Этап '%s': повтор тоже невалиден — продолжаем с тем, "
                                   "что есть (парсер-фолбэк).", name)
            ckpt.save(name, raw)  # этап завершён — фиксируем ДО перехода к следующему
            return raw

        # Валидаторы JSON-этапов: парсер вернул структурированный результат?
        def _v_gather(r): 
            try: return parse_gatherer_output(r).is_structured
            except Exception: return False
        def _v_arch(r):
            try: return parse_architect_output(r).is_structured
            except Exception: return False
        def _v_fix(r):
            try:
                o = _parse_fixer_output_json(r)
                return o.is_structured or o.no_changes_needed or bool(o.patches)
            except Exception: return False

        # 1: Сбор контекста
        manifest_raw = _run_stage("gather", gatherer, f"Собери контекст по задаче:\n{content}",
                                  validator=_v_gather)
        maybe_unload_between(state, "gatherer", "architect")

        # 2: План (контекст — вывод сбора)
        plan_raw = _run_stage("architect", architect,
                              f"Задача:\n{content}\n\nМАНИФЕСТ СБОРЩИКА:\n{manifest_raw}\n\nПострой JSON-план.",
                              validator=_v_arch)
        maybe_unload_between(state, "architect", "coder")

        # 3: Реализация (контекст — план); патчи кладём в overlay
        fixer_raw = _run_stage("fix", fixer,
                              f"ПЛАН АРХИТЕКТОРА:\n{plan_raw}\n\nРеализуй план. Верни JSON: {{'patches': []}}",
                              validator=_v_fix)
        _apply_fixer_patches(fixer_raw, overlay)
        # Запоминаем, что выдал кодер — пригодится Титану, если задача застрянет.
        task_data["_last_broken_code"] = str(fixer_raw)[:4000]
        maybe_unload_between(state, "coder", "auditor")

        # 4: Аудит (контекст — план + факт применённых патчей)
        verdict_raw = _run_stage("audit", auditor,
                                f"ПЛАН:\n{plan_raw}\n\nРеализация применена в песочнице. "
                                f"Проверь и верни 'Вердикт: ОК' или 'Вердикт: ОТКЛОНЕНО'.")
        _audit_verdict.append(verdict_raw)
        result = verdict_raw

        verdict_tail = str(result)[-600:]
        if "вердикт: ок" in verdict_tail.lower():
            overlay.commit_if_success(retention_days=state.backup_retention_days)
            ckpt.clear()  # задача успешно завершена — чекпойнт не нужен
            return True, claim

        # Провал по вердикту (НЕ по сбою ключей): чекпойнт чистим, т.к. результат
        # этапов валиден, но аудит отклонил — повтор должен идти заново с учётом причины.
        ckpt.clear()
        reason = _audit_verdict[-1] if _audit_verdict else verdict_tail
        return False, _write_failure_metadata(claim, task_data, reason.strip()[:_MAX_REASON_LEN])
    except Exception as e:
        return False, _write_failure_metadata(claim, task_data, f"{type(e).__name__}: {str(e)[:800]}")
    finally:
        overlay.cleanup()
        reset_runtime_context(token)

def main_loop() -> None:
    for d in (AUTO_DIR, DONE_DIR, FAILED_DIR): os.makedirs(d, exist_ok=True)
    if not _acquire_lock(): return
    try:
        secret_path = ".chainlit/secrets.toml" if os.path.exists(".chainlit/secrets.toml") else "secrets.toml"
        if os.path.exists(secret_path):
            with open(secret_path, "rb") as f: init_api_keys(tomllib.load(f).get("PROVIDER_KEYS", {}))
    except Exception: pass

    db, state = DatabaseManager(), load_session() or SessionState()
    if not state.project_path: state.project_path = os.getcwd()
    elif not os.path.exists(state.project_path): os.makedirs(state.project_path, exist_ok=True)
    
    _heartbeat(db, "alive", "startup")
    _reclaim_stale_processing_files()

    def _shutdown(*_args): raise SystemExit(0)
    signal.signal(signal.SIGINT, _shutdown)
    if os.name != "nt": signal.signal(signal.SIGTERM, _shutdown)

    tick = 0
    try:
        while True:
            try:
                tick += 1
                if tick % HEARTBEAT_EVERY_N_TICKS == 0:
                    _heartbeat(db, "alive", "tick")
                    _reclaim_stale_processing_files()
                if tick >= TICK_RESET_AT: tick = 1
                
                if (fresh := load_session()): state = fresh
                process_tasks(db, state)
                time.sleep(POLL_INTERVAL)
            except SystemExit: break
            except Exception: time.sleep(5)
    finally:
        _release_lock()

if __name__ == "__main__": main_loop()
