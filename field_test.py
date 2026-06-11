#!/usr/bin/env python3
"""
field_test.py — сквозной автоматический тест CorePilot для полевых испытаний.

ЧТО ДЕЛАЕТ (без кликов по окну — вызывает те же функции сервисного слоя, что и
кнопки интерфейса; это надёжнее GUI-автоматизации и проверяет реальный код):

  1) окружение и версии библиотек;
  2) загрузка секретов — какие провайдеры есть (без вывода самих ключей);
  3) КЛЮЧИ ЖИВЬЁМ: крошечный реальный запрос к каждому настроенному облачному
     провайдеру — проверяет, что ключ не просто есть, а РАБОТАЕТ;
  4) облачные модели: реальный запрос списка у провайдеров ролей;
  5) локальные модели: реальный запрос списка у локальных бэкендов (LM Studio/…);
  6) КОНВЕЙЕР end-to-end: реальный запуск на простой задаче, с таймаутом;
  7) ДЕМОН: создаёт реальную задачу, запускает демон, ждёт смены статуса, гасит;
  8) единый отчёт corepilot_fieldtest.txt — его отправляют разработчику.

Время: общий бюджет (по умолчанию 15 мин) + таймаут на каждую фазу, чтобы одна
зависшая фаза не съела всё. Любая ошибка фазы ловится и попадает в отчёт.

ВНИМАНИЕ: тест делает РЕАЛЬНЫЕ запросы к облаку (расходует квоту/токены) и требует
запущенных локальных серверов (LM Studio/Ollama), если роли настроены на local.

Запуск:  python field_test.py            (бюджет 15 мин)
         python field_test.py 5          (бюджет 5 мин)
         (или двойной клик по field_test.bat)
"""
from __future__ import annotations

import datetime
import io
import os
import sys
import threading
import time
import traceback

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corepilot_fieldtest.txt")
_buf = io.StringIO()
_t0 = time.time()


def w(line: str = "") -> None:
    stamp = f"[{int(time.time() - _t0):>4}s] "
    print(stamp + line)
    _buf.write(stamp + line + "\n")


def section(title: str) -> None:
    w("")
    w("=" * 64)
    w("  " + title)
    w("=" * 64)


def run_phase(name: str, fn, timeout: float) -> None:
    """Выполняет фазу в отдельном потоке с таймаутом. Зависшая фаза не блокирует
    весь тест: поток помечается, и мы идём дальше (демон/сервер всё равно с
    таймаутами на сетевые вызовы). Любая ошибка ловится и пишется в отчёт."""
    result = {"done": False, "error": None}

    def _runner():
        try:
            fn()
            result["done"] = True
        except Exception:
            result["error"] = traceback.format_exc()

    th = threading.Thread(target=_runner, daemon=True)
    th.start()
    th.join(timeout=timeout)
    if th.is_alive():
        w(f"[{name}] ⏱ ТАЙМАУТ ({int(timeout)}с) — фаза не завершилась, идём дальше.")
    elif result["error"]:
        w(f"[{name}] ✗ ОШИБКА:")
        for ln in result["error"].splitlines():
            w("    " + ln)
    # успех фазы печатает сама фаза


# ============================================================================
# Фазы
# ============================================================================

def phase_env():
    section("1. ОКРУЖЕНИЕ И ВЕРСИИ")
    import platform
    w(f"Дата:          {datetime.datetime.now().isoformat(timespec='seconds')}")
    w(f"Python:        {sys.version.splitlines()[0]}")
    w(f"ОС:            {platform.platform()}")
    w(f"Папка:         {os.getcwd()}")
    from importlib.metadata import version, PackageNotFoundError
    for p in ("crewai", "litellm", "pydantic", "dearpygui", "requests", "openai"):
        try:
            w(f"  {p:12} {version(p)}")
        except PackageNotFoundError:
            w(f"  {p:12} НЕ установлен")


def phase_keys_present():
    section("2. СЕКРЕТЫ (наличие, без вывода ключей)")
    import agents
    # Принудительно (лениво) грузим секреты.
    try:
        agents._lazy_reload_keys()
    except Exception:
        pass
    if not agents.API_KEYS:
        w("Ключей не загружено. Проверьте secrets.toml (формат: GROQ_API_KEY=...).")
        return
    for prov, keys in agents.API_KEYS.items():
        w(f"  {prov:12} ключей: {len(keys)}")


def phase_keys_live():
    section("3. КЛЮЧИ ЖИВЬЁМ (реальный мини-запрос к провайдеру)")
    import service_layer as svc
    import agents
    import litellm
    cfg = svc.load_settings()
    # Собираем уникальные облачные провайдеры из ролей.
    provs = set()
    for role in svc.ROLES:
        if (cfg.get(f"backend_{role}") == "cloud") or (cfg.get(f"mode_{role}") == "cloud"):
            provs.add(cfg.get(f"provider_{role}", "gemini"))
    if not provs:
        w("Ни одна роль не настроена на cloud — пропуск живой проверки ключей.")
        return

    def _check_one(prov):
        """Проверка одного провайдера в отдельном потоке с жёстким таймаутом,
        чтобы зависший провайдер (напр. openrouter/auto) не топил всю фазу."""
        key = agents.next_api_key(prov)
        if not key:
            w(f"  {prov:12} ✗ ключ не найден в secrets")
            return
        # модель для проверки берём ТОЛЬКО у CLOUD-роли этого провайдера (у роли
        # на локальном бэкенде имя модели локальное, его нельзя слать в облако).
        def _is_cloud_role(r):
            return (cfg.get(f"backend_{r}") == "cloud") or (cfg.get(f"mode_{r}") == "cloud")
        model = next((cfg.get(f"model_{r}") for r in svc.ROLES
                      if _is_cloud_role(r) and cfg.get(f"provider_{r}") == prov
                      and cfg.get(f"model_{r}")), None)
        # для openrouter 'auto'/'free'/пусто — берём явную :free для быстрого пинга
        if prov == "openrouter" and (not model or model.strip().lower() in ("auto", "free")):
            try:
                free = [m for m in svc.list_cloud_models("openrouter") if str(m).endswith(":free")]
                model = free[0] if free else "openrouter/auto"
            except Exception:
                model = "openrouter/auto"
        if not model:
            w(f"  {prov:12} ⚠ ключ есть, но модель для проверки не задана")
            return
        out = {"r": None}

        def _ping():
            try:
                model_str = f"{prov}/{model}"
                kw = dict(model=model_str, api_key=key, max_tokens=5,
                          timeout=12, messages=[{"role": "user", "content": "ping"}])
                if prov == "openrouter":
                    kw["base_url"] = "https://openrouter.ai/api/v1"
                    # litellm требует префикс openrouter/ ВСЕГДА, даже если имя
                    # модели само содержит '/' (напр. nvidia/nemotron:free).
                    m = model[len("openrouter/"):] if model.startswith("openrouter/") else model
                    kw["model"] = f"openrouter/{m}"
                litellm.completion(**kw)
                out["r"] = "ok"
            except Exception as e:
                out["r"] = f"err: {str(e)[:140]}"

        th = threading.Thread(target=_ping, daemon=True)
        th.start()
        th.join(timeout=15)
        if th.is_alive():
            w(f"  {prov:12} ⏱ не ответил за 15с (модель {model})")
        elif out["r"] == "ok":
            w(f"  {prov:12} ✓ ключ РАБОТАЕТ (модель {model})")
        else:
            w(f"  {prov:12} ✗ {out['r']}")

    for prov in sorted(provs):
        _check_one(prov)


def phase_cloud_models():
    section("4. ОБЛАЧНЫЕ МОДЕЛИ (реальный список у провайдеров)")
    import service_layer as svc
    cfg = svc.load_settings()
    provs = set()
    for role in svc.ROLES:
        if (cfg.get(f"backend_{role}") == "cloud") or (cfg.get(f"mode_{role}") == "cloud"):
            provs.add(cfg.get(f"provider_{role}", "gemini"))
    if not provs:
        provs = {"openrouter"}  # хотя бы общедоступный список
    for prov in sorted(provs):
        out = {"r": None}

        def _list(p=prov):
            try:
                out["r"] = svc.list_cloud_models(p)
            except Exception as e:
                out["r"] = ("__err__", str(e)[:120])

        th = threading.Thread(target=_list, daemon=True)
        th.start()
        th.join(timeout=20)
        if th.is_alive():
            w(f"  {prov:12} ⏱ список не пришёл за 20с (провайдер медленный)")
        elif isinstance(out["r"], tuple) and out["r"] and out["r"][0] == "__err__":
            w(f"  {prov:12} ошибка: {out['r'][1]}")
        elif out["r"]:
            w(f"  {prov:12} моделей: {len(out['r'])} (напр.: {', '.join(out['r'][:5])})")
        else:
            w(f"  {prov:12} пусто (нет ключа/сети или провайдер не отдаёт список)")


def phase_local_models():
    section("5. ЛОКАЛЬНЫЕ МОДЕЛИ (реальный список у бэкендов)")
    import service_layer as svc
    cfg = svc.load_settings()
    backends = set()
    for role in svc.ROLES:
        b = cfg.get(f"backend_{role}")
        if b and b != "cloud":
            backends.add(b)
    if not backends:
        backends = {"lmstudio", "ollama"}  # типовые
        w("(роли не на local — проверяю типовые lmstudio/ollama)")
    url = cfg.get("local_base_url", "")
    for b in sorted(backends):
        # local_base_url настроен под ОДИН бэкенд (обычно LM Studio). Передаём его
        # только если совпадает; иначе пусть list_local_models возьмёт дефолт бэкенда.
        b_url = url if (b == cfg.get("local_backend") or
                        (b == "ollama" and "11434" in url)) else ""
        try:
            models = svc.list_local_models(b, b_url)
            if models:
                w(f"  {b:12} моделей: {len(models)} (напр.: {', '.join(models[:5])})")
            else:
                w(f"  {b:12} пусто — сервер не запущен или нет загруженной модели")
        except Exception as e:
            w(f"  {b:12} ошибка: {str(e)[:120]}")


def phase_pipeline():
    section("6. КОНВЕЙЕР END-TO-END (реальный запуск)")
    import service_layer as svc
    stages = []

    def _progress(name, msg):
        stages.append(name)
        w(f"    этап: {name} — {msg}")

    task = "Создай простой текстовый файл hello.txt с текстом 'привет' (тест)."
    w(f"Задача: {task}")
    w("Запуск… (это может занять минуты при облачных моделях)")
    res = svc.run_pipeline(task, progress=_progress)
    if getattr(res, "error", None):
        w(f"Результат: ✗ ошибка — {res.error}")
    else:
        w(f"Результат: вердикт='{getattr(res, 'verdict', '?')}', "
          f"ok={getattr(res, 'ok', '?')}")
        n_patches = len(getattr(res, "patches", []) or [])
        n_diffs = len(getattr(res, "diffs", []) or [])
        w(f"Патчей: {n_patches}, диффов: {n_diffs}")
    w(f"Пройдено этапов: {len(stages)} ({', '.join(stages) or 'нет'})")


def phase_pipeline_code():
    section("6B. КОНВЕЙЕР НА РЕАЛЬНОМ КОДЕ (исправление бага в песочнице)")
    import service_layer as svc
    import shutil
    
    # 1) Создаём temp-проект
    temp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp_fieldtest_project")
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir, ignore_errors=True)
    os.makedirs(temp_dir, exist_ok=True)
    
    buggy_file = os.path.join(temp_dir, "math_helper.py")
    buggy_code = (
        "def add_numbers(a, b):\n"
        "    # Баг: возвращает разность вместо суммы\n"
        "    return a - b\n"
    )
    try:
        with open(buggy_file, "w", encoding="utf-8") as f:
            f.write(buggy_code)
    except Exception as e:
        w(f"Не удалось создать buggy файл: {e}")
        return
        
    # 2) Настраиваем временный путь проекта в SessionState
    cfg = svc.load_settings()
    orig_path = cfg.get("project_path", "")
    cfg["project_path"] = temp_dir
    svc.save_settings(cfg)
    
    stages = []
    def _progress(name, msg):
        stages.append(name)
        w(f"    этап: {name} — {msg}")
        
    task = "Почини баг в math_helper.py: функция add_numbers должна возвращать сумму a + b, а не разность a - b."
    w(f"Задача: {task}")
    w("Запуск конвейера кода…")
    
    try:
        res = svc.run_pipeline(task, progress=_progress)
    except Exception as e:
        res = type("Dummy", (object,), {"error": str(e)})()
    
    # Восстанавливаем настройки
    cfg["project_path"] = orig_path
    svc.save_settings(cfg)
    
    if getattr(res, "error", None):
        w(f"Результат: ✗ ошибка — {res.error}")
    else:
        w(f"Результат: вердикт='{getattr(res, 'verdict', '?')}', "
          f"ok={getattr(res, 'ok', '?')}")
        n_patches = len(getattr(res, "patches", []) or [])
        n_diffs = len(getattr(res, "diffs", []) or [])
        w(f"Патчей: {n_patches}, диффов: {n_diffs}")
        if n_diffs > 0:
            w("Первый дифф:")
            for line in res.diffs[0][1].splitlines()[:15]:
                w("  " + line)
                
        # Проверяем песочницу: оригинальный файл не должен измениться
        try:
            with open(buggy_file, "r", encoding="utf-8") as f:
                current_code = f.read()
            if current_code == buggy_code:
                w("Песочница ОК: оригинальный файл не изменён конвейером.")
            else:
                w("ВНИМАНИЕ: оригинальный файл был изменён напрямую (песочница нарушена)!")
        except Exception as e:
            w(f"Ошибка проверки песочницы: {e}")
            
    # Чистим temp-проект
    shutil.rmtree(temp_dir, ignore_errors=True)


def phase_manager_dag():
    section("7B. МЕНЕДЖЕР И ДЕМОН НА DAG-ЗАДАЧАХ (цепочки и заморозка)")
    import service_layer as svc
    
    # 1) Проверка генерации бэклога из цели
    w("Тестирование generate_backlog...")
    goal = "Создай калькулятор: сначала модуль вычислений calc.py, затем интерфейс ui.py."
    backlog, msg = svc.generate_backlog(goal)
    w(f"Генерация бэклога: {msg}")
    if not backlog:
        w("✗ Ошибка: бэклог не сгенерирован, использую искусственные задачи для теста DAG.")
        # Создаем искусственную цепочку для продолжения теста
        tid_parent = f"dag_parent_{int(time.time())}"
        tid_child = f"dag_child_{int(time.time())}"
        backlog = [
            {
                "task_id": tid_parent,
                "title": "Родительская задача",
                "description": "Создай файл parent.txt с текстом 'done'.",
                "status": "pending",
                "depends_on": []
            },
            {
                "task_id": tid_child,
                "title": "Дочерняя задача",
                "description": "Создай файл child.txt с текстом 'done'.",
                "status": "pending",
                "depends_on": [tid_parent]
            }
        ]
    else:
        # Проверяем, есть ли зависимости
        has_deps = any(t.get("depends_on") for t in backlog)
        w(f"DAG структура сгенерирована: {has_deps} (всего задач: {len(backlog)})")
        
    # 2) Тест DAG выполнения на Демоне
    w("Ставим бэклог в очередь...")
    # Очистим старые файлы этих задач из auto_tasks/done/failed для чистоты теста
    for t in backlog:
        for f in svc._find_task_files(t["task_id"]):
            try: os.remove(f)
            except Exception: pass
            
    ok_count, skip_count = svc.enqueue_backlog(backlog)
    w(f"Поставлено в очередь: {ok_count}, пропущено: {skip_count}")
    
    # Стартуем демон
    ok, msg = svc.start_daemon()
    w(f"Демон запущен: {ok} — {msg}")
    if not ok:
        return
        
    # Мониторим выполнение
    parent_id = backlog[0]["task_id"]
    child_id = backlog[1]["task_id"] if len(backlog) > 1 else None
    
    deadline = time.time() + 180
    parent_done = False
    child_started_correctly = True
    
    while time.time() < deadline:
        time.sleep(5)
        try:
            tasks = {t.task_id: t.status for t in svc._build_board_tasks()}
        except Exception:
            tasks = {}
            
        p_status = tasks.get(parent_id, "?")
        c_status = tasks.get(child_id, "?") if child_id else "done"
        
        w(f"    статусы: родитель={p_status}, потомок={c_status}")
        
        # Проверяем, что потомок не начал выполняться раньше, чем завершился родитель
        if p_status in ("pending", "processing") and c_status == "processing":
            child_started_correctly = False
            w("⚠️ ВНИМАНИЕ: Дочерняя задача запущена до завершения родительской!")
            
        if p_status == "done":
            parent_done = True
            
        if p_status in ("done", "failed") and (c_status in ("done", "failed") or not child_id):
            break
            
        running, _ = svc.daemon_status()
        if not running:
            w("    демон неожиданно остановился.")
            break
            
    w(f"Проверка порядка выполнения: {'Успешно' if child_started_correctly else 'Провал'}")
    
    svc.stop_daemon()
    w("Демон остановлен.")


def phase_daemon():
    section("7. ДЕМОН (создать задачу → запустить → дождаться статуса)")
    import service_layer as svc
    # 1) кладём простую задачу
    tid = f"fieldtest_{int(time.time())}"
    ok, msg = svc.enqueue_task({
        "task_id": tid,
        "title": "Полевой тест демона",
        "description": "Создай файл daemon_test.txt с текстом 'ok' (тест демона).",
        "status": "pending",
    })
    w(f"Задача создана: {ok} — {msg}")
    if not ok:
        return
    # 2) стартуем демон
    ok, msg = svc.start_daemon()
    w(f"Демон запущен: {ok} — {msg}")
    if not ok:
        return
    # 3) ждём смену статуса (до ~3 мин), опрашивая доску
    deadline = time.time() + 180
    last = None
    final = "?"
    seen_in_queue = False
    while time.time() < deadline:
        time.sleep(5)
        try:
            tasks = {t.task_id: t.status for t in svc._build_board_tasks()}
        except Exception:
            tasks = {}
        st = tasks.get(tid, "?")
        if st != "?":
            seen_in_queue = True
        if st != last:
            w(f"    статус задачи: {st}")
            last = st
        if st in ("done", "failed"):
            final = st
            break
        # ранний выход: демон не запущен и задача уже не в очереди (обработана/исчезла)
        running, _ = svc.daemon_status()
        if not running and not seen_in_queue and time.time() - _t0 > 20:
            w("    демон не запущен и задача не видна в очереди — прекращаю ожидание.")
            break
    w(f"Итоговый статус задачи: {final}")
    # Если провал — достаём причину из файла задачи (она пишется в failed_reason).
    if final == "failed":
        try:
            import json, glob
            cand = glob.glob(os.path.join(svc.FAILED_DIR, f"*{tid}*.json")) + \
                   glob.glob(os.path.join(svc.AUTO_DIR, f"*{tid}*.json"))
            for p in cand:
                try:
                    with open(p, "r", encoding="utf-8", errors="replace") as f:
                        d = json.load(f)
                    reason = d.get("failed_reason") or d.get("error") or ""
                    if reason:
                        w(f"ПРИЧИНА ПРОВАЛА: {reason[:400]}")
                        # 'ОТКЛОНЕНО' — это легитимный вердикт Аудитора (агент решил,
                        # что решение не годится), а НЕ сбой системы. Поясняем.
                        if "отклон" in reason.lower() or "reject" in reason.lower():
                            w("    (Это вердикт агента-Аудитора, не сбой системы: "
                              "цепочка отработала, но Аудитор не принял результат.)")
                        break
                except Exception:
                    continue
        except Exception:
            pass
    # 4) гасим демон
    ok, msg = svc.stop_daemon()
    w(f"Демон остановлен: {ok} — {msg}")
    # лог демона (хвост)
    try:
        log = svc.read_daemon_log(max_lines=15)
        if log:
            w("Последние строки лога демона:")
            for ln in log[-15:]:
                w("    " + ln)
    except Exception:
        pass


def phase_cleaner():
    section("8. CLEANER (полный цикл, дубликаты, защита, удаление)")
    import service_layer as svc
    import tempfile, os as _os
    import shutil
    
    # Создаём временную папку
    d = tempfile.mkdtemp(prefix="cp_cleaner_test_")
    sub = _os.path.join(d, "cache_junk")
    _os.makedirs(sub, exist_ok=True)
    for i in range(3):
        with open(_os.path.join(sub, f"junk_{i}.tmp"), "w") as f:
            f.write("x" * 2_000_000)  # ~2 МБ каждый -> папка ~6 МБ
            
    # Создаем дубликаты для проверки сканера 'dups'
    dup_dir = _os.path.join(d, "dup_test")
    _os.makedirs(dup_dir, exist_ok=True)
    with open(_os.path.join(dup_dir, "file1.txt"), "w", encoding="utf-8") as f:
        f.write("identical content for duplicate scan test")
    with open(_os.path.join(dup_dir, "file2.txt"), "w", encoding="utf-8") as f:
        f.write("identical content for duplicate scan test")

    w(f"Тестовая папка: {d}")
    try:
        # 1) Сканер 'disk'
        items_disk, msg_disk = svc.cleaner_scan("disk", d, 0.01)
        w(f"1. Сканер disk: найдено {len(items_disk)} объектов — {msg_disk}")
        
        # 2) Сканер 'dups'
        items_dups, msg_dups = svc.cleaner_scan("dups", dup_dir, 0.000001)
        w(f"2. Сканер dups: найдено {len(items_dups)} дубликатов — {msg_dups}")
        for item in items_dups[:2]:
            w(f"    дубликат: {item.path} ({item.size_mb:.6f} МБ)")
            
        # 3) Сканер 'downloads' (просто запуск)
        items_dl, msg_dl = svc.cleaner_scan("downloads", min_size_mb=0.0)
        w(f"3. Сканер downloads: найдено {len(items_dl)} объектов — {msg_dl}")
        
        # 4) Сканер 'startup' (просто запуск)
        items_start, msg_start = svc.cleaner_scan("startup")
        w(f"4. Сканер startup: найдено {len(items_start)} объектов — {msg_start}")
        
        # 5) Проверка защиты системных папок
        from cleaner_tools import _is_protected
        is_win_prot = _is_protected("C:\\Windows")
        is_pf_prot = _is_protected("C:\\Program Files")
        w(f"5. Защита папок: C:\\Windows защищен={is_win_prot}, C:\\Program Files защищен={is_pf_prot}")
        
        # Попытка поместить защищенный путь в карантин
        ok_q, msg_q = svc.cleaner_quarantine(["C:\\Windows"], same_drive=True)
        w(f"    Карантин C:\\Windows: ok={ok_q}, msg={msg_q}")
        
        # 6) Проверка карантина и восстановления
        if items_disk:
            target_path = items_disk[0].path
            w(f"6. Карантин тестовой папки: {target_path}")
            ok_q2, msg_q2 = svc.cleaner_quarantine([target_path], same_drive=True)
            w(f"    Карантин: ok={ok_q2}, msg={msg_q2}")
            
            sessions = svc.cleaner_sessions()
            w(f"    Всего сессий карантина: {len(sessions)}")
            if sessions and ok_q2:
                sid = sessions[0].get("session_id", "")
                
                # Тест восстановления (Undo)
                ok_undo, msg_undo = svc.cleaner_undo(sid)
                w(f"    Восстановление сессии {sid}: ok={ok_undo}, msg={msg_undo}")
                
                # Тест удаления навсегда
                # Снова поместим в карантин
                ok_q3, msg_q3 = svc.cleaner_quarantine([target_path], same_drive=True)
                sessions_new = svc.cleaner_sessions()
                if sessions_new and ok_q3:
                    sid_del = sessions_new[0].get("session_id", "")
                    ok_del, msg_del = svc.cleaner_delete_forever(sid_del)
                    w(f"    Удаление навсегда сессии {sid_del}: ok={ok_del}, msg={msg_del}")
                    
    except Exception as e:
        w(f"Cleaner ошибка в фазе: {str(e)[:160]}")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def phase_quotas():
    section("9. КВОТЫ ОБЛАЧНЫХ ПРОВАЙДЕРОВ")
    import service_layer as svc
    try:
        q = svc.get_quotas()
        if not q:
            w("Данных о квотах нет (нормально, если провайдер их не отдаёт).")
            return
        for row in q[:10]:
            name = row.get("provider", row.get("name", "?"))
            info = row.get("status", row.get("info", row.get("remaining", "")))
            w(f"  {str(name):14} {info}")
    except Exception as e:
        w(f"Квоты ошибка: {str(e)[:140]}")


def phase_profiles():
    section("10. ПРОФИЛИ НАСТРОЕК")
    import service_layer as svc
    try:
        profs = svc.list_profiles()
        w(f"Сохранённых профилей: {len(profs)}"
          + (f" ({', '.join(profs[:10])})" if profs else ""))
    except Exception as e:
        w(f"Профили ошибка: {str(e)[:140]}")


def main() -> int:
    budget_min = 15.0
    if len(sys.argv) > 1:
        try:
            budget_min = float(sys.argv[1])
        except ValueError:
            pass
    w("CorePilot — СКВОЗНОЙ ПОЛЕВОЙ ТЕСТ")
    w(f"Бюджет времени: {budget_min:.0f} мин. Отправьте файл "
      f"corepilot_fieldtest.txt разработчику.")
    w("ВНИМАНИЕ: делаются реальные запросы к облаку (расход квоты) и к локальным "
      "серверам.")

    # Распределение таймаутов по фазам (в сумме ~ бюджет).
    budget = budget_min * 60
    phases = [
        ("окружение", phase_env, 30),
        ("секреты", phase_keys_present, 30),
        ("ключи-живьём", phase_keys_live, min(120, budget * 0.12)),
        ("облачные-модели", phase_cloud_models, min(90, budget * 0.1)),
        ("локальные-модели", phase_local_models, 60),
        ("конвейер", phase_pipeline, min(360, budget * 0.4)),
        ("конвейер-код", phase_pipeline_code, min(360, budget * 0.4)),
        ("менеджер-dag", phase_manager_dag, min(360, budget * 0.3)),
        ("демон", phase_daemon, min(240, budget * 0.3)),
        ("cleaner", phase_cleaner, 60),
        ("квоты", phase_quotas, 30),
        ("профили", phase_profiles, 15),
    ]
    for name, fn, to in phases:
        if time.time() - _t0 > budget:
            w(f"\n[бюджет {budget_min:.0f} мин исчерпан — остановка перед фазой '{name}']")
            break
        run_phase(name, fn, to)

    section("ИТОГ")
    w(f"Тест занял {int(time.time() - _t0)}с.")
    w(f"Отчёт сохранён: {OUT}")
    try:
        with open(OUT, "w", encoding="utf-8") as f:
            f.write(_buf.getvalue())
    except OSError as e:
        print(f"Не удалось записать отчёт: {e}")
        return 1
    # На всякий случай гасим возможный запущенный демон/сервер.
    try:
        import service_layer as svc
        svc.panic_stop()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
