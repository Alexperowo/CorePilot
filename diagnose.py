#!/usr/bin/env python3
"""
diagnose.py — сборщик диагностики CorePilot в один файл.

Назначение: собрать всё, что нужно для разбора проблемы, в один текстовый файл
(corepilot_diagnostics.txt), который пользователь отправляет разработчику. Сам
пользователь в логах разбираться не должен — скрипт делает всё за него.

Что собирает:
  1) версии Python и ключевых библиотек (что установлено на самом деле);
  2) самопроверку сервисного слоя — работает ли ядро;
  3) проверку импорта интерфейса (ловит ошибки до запуска окна);
  4) юнит-тесты ядра;
  5) полевые данные: настройки ролей, наличие секретов, состояние очереди задач;
  6) хвост лога Демона, если он есть;
  Любая ошибка шага ловится и попадает в отчёт с трейсбеком (скрипт не падает сам).

Запуск:  python diagnose.py   (или двойной клик по diagnose.bat)
Результат: corepilot_diagnostics.txt рядом со скриптом.
"""
from __future__ import annotations

import datetime
import io
import os
import platform
import subprocess
import sys
import traceback

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corepilot_diagnostics.txt")
_buf = io.StringIO()

# Окружение для дочерних процессов: форсим UTF-8, иначе на Windows вывод с
# кириллицей придёт в cp1251 и может дать UnicodeDecodeError или мусор.
_CHILD_ENV = dict(os.environ)
_CHILD_ENV["PYTHONIOENCODING"] = "utf-8"
_CHILD_ENV["PYTHONUTF8"] = "1"


def _run(args, timeout):
    """subprocess.run с UTF-8 и безопасным декодированием (errors='replace')."""
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace", env=_CHILD_ENV)


def w(line: str = "") -> None:
    """Пишет строку и в файл-буфер, и в консоль (чтобы пользователь видел прогресс)."""
    print(line)
    _buf.write(line + "\n")


def section(title: str) -> None:
    w("\n" + "=" * 64)
    w("  " + title)
    w("=" * 64)


def safe(fn, label: str) -> None:
    """Выполняет шаг диагностики, ловя ЛЮБУЮ ошибку (скрипт не должен падать сам)."""
    try:
        fn()
    except Exception:
        w(f"[{label}] ОШИБКА:")
        w(traceback.format_exc())


# --- 1. Окружение -----------------------------------------------------------
def diag_environment():
    section("1. ОКРУЖЕНИЕ")
    w(f"Дата:           {datetime.datetime.now().isoformat(timespec='seconds')}")
    w(f"Python:         {sys.version.splitlines()[0]}")
    w(f"Исполняемый:    {sys.executable}")
    w(f"ОС:             {platform.platform()}")
    w(f"Машина:         {platform.machine()}")
    w(f"Рабочая папка:  {os.getcwd()}")
    w(f"В venv:         {'да' if sys.prefix != sys.base_prefix else 'НЕТ (системный Python!)'}")


# --- 2. Версии библиотек ----------------------------------------------------
def diag_packages():
    section("2. УСТАНОВЛЕННЫЕ БИБЛИОТЕКИ")
    pkgs = ["crewai", "litellm", "pydantic", "dearpygui", "psutil",
            "requests", "chainlit", "flet", "openai"]
    try:
        from importlib.metadata import version, PackageNotFoundError
    except Exception:
        w("Не удалось получить доступ к importlib.metadata.")
        return
    for p in pkgs:
        try:
            w(f"  {p:14} {version(p)}")
        except PackageNotFoundError:
            w(f"  {p:14} НЕ установлен")
        except Exception as e:
            w(f"  {p:14} ошибка чтения версии: {e}")


# --- 3. Самопроверка сервисного слоя ----------------------------------------
def diag_service_selftest():
    section("3. САМОПРОВЕРКА СЕРВИСНОГО СЛОЯ")
    # Запускаем в подпроцессе, чтобы возможный жёсткий сбой не уронил диагностику.
    try:
        r = _run([sys.executable, "service_layer.py"], 120)
        w("--- stdout ---"); w(r.stdout.strip() or "(пусто)")
        if r.stderr.strip():
            w("--- stderr ---"); w(r.stderr.strip())
        w(f"--- код возврата: {r.returncode} ({'OK' if r.returncode == 0 else 'СБОЙ'})")
    except subprocess.TimeoutExpired:
        w("Самопроверка зависла (таймаут 120с) — возможна проблема с окружением.")
    except FileNotFoundError:
        w("service_layer.py не найден — запускайте diagnose из папки проекта.")


# --- 4. Импорт интерфейса (ловит ошибки до запуска окна) --------------------
def diag_ui_import():
    section("4. ПРОВЕРКА ИНТЕРФЕЙСА (импорт без запуска окна)")
    # Импорт в подпроцессе: если dearpygui не встанет или будет ошибка API —
    # увидим точную причину, а окно при этом не откроется.
    code = (
        "import importlib;"
        "[importlib.import_module(m) for m in "
        "('ui_common','ui_tabs','ui_dpg')];"
        "print('Импорт ui_common, ui_tabs, ui_dpg: OK')"
    )
    try:
        r = _run([sys.executable, "-c", code], 120)
        w(r.stdout.strip() or "(нет вывода)")
        if r.stderr.strip():
            w("--- ошибки импорта ---"); w(r.stderr.strip())
        w(f"--- код возврата: {r.returncode} ({'OK' if r.returncode == 0 else 'СБОЙ ИМПОРТА'})")
    except subprocess.TimeoutExpired:
        w("Импорт интерфейса завис (таймаут).")


# --- 5. Юнит-тесты (если есть) ----------------------------------------------
def diag_unit_tests():
    section("5. ЮНИТ-ТЕСТЫ ЯДРА")
    runner = os.path.join("tests", "run_all.py")
    if not os.path.isfile(runner):
        w("tests/run_all.py не найден — пропуск.")
        return
    try:
        r = _run([sys.executable, runner], 180)
        out = (r.stdout + r.stderr).strip()
        # берём только итоговую строку + возможные провалы, чтобы не раздувать файл
        lines = out.splitlines()
        tail = [l for l in lines if "ИТОГО" in l or "FAIL" in l or "Провал" in l]
        w("\n".join(tail) if tail else out[-1500:])
    except subprocess.TimeoutExpired:
        w("Тесты зависли (таймаут).")


# --- 6. Полевые данные (настройки, секреты, очередь задач) ------------------
def diag_field_state():
    section("6. ПОЛЕВЫЕ ДАННЫЕ")
    import json
    # Настройки (без чувствительных значений — только ключи и режимы ролей).
    # Файл сессии — .ai_session.json (а не settings.json).
    cfg_path = ".ai_session.json" if os.path.isfile(".ai_session.json") else "settings.json"
    try:
        if os.path.isfile(cfg_path):
            with open(cfg_path, "r", encoding="utf-8", errors="replace") as f:
                cfg = json.load(f)
            roles = ("gatherer", "architect", "coder", "auditor", "oracle")
            w("Настройки ролей (источник/провайдер/модель):")
            for r in roles:
                src = cfg.get(f"backend_{r}", "?")
                prov = cfg.get(f"provider_{r}", "?")
                mdl = cfg.get(f"model_{r}", "?")
                legacy = cfg.get(f"mode_{r}")
                tail = f"  [старый mode={legacy}]" if legacy else ""
                w(f"  {r:10} {src} / {prov} / {mdl}{tail}")
            w(f"Локальный URL: {cfg.get('local_base_url', '?')}")
        else:
            w(".ai_session.json не найден (ещё не сохраняли настройки в приложении).")
    except Exception as e:
        w(f"Не удалось прочитать settings.json: {e}")

    # Наличие секретов — НЕ содержимое (ключи не выводим).
    found = [n for n in ("secrets.toml", "secrets.txt", ".streamlit/secrets.toml")
             if os.path.isfile(n)]
    w(f"Файл секретов: {'найден (' + ', '.join(found) + ')' if found else 'НЕ найден'}")

    # Состояние файловой очереди задач.
    qdir = "auto_tasks"
    if os.path.isdir(qdir):
        try:
            files = os.listdir(qdir)
            jobs = [f for f in files if f.endswith(".json")]
            w(f"Очередь задач ({qdir}): файлов-задач {len(jobs)}")
            from collections import Counter
            st = Counter()
            for jf in jobs[:200]:
                try:
                    with open(os.path.join(qdir, jf), "r", encoding="utf-8",
                              errors="replace") as f:
                        st[json.load(f).get("status", "?")] += 1
                except Exception:
                    st["нечитаемо"] += 1
            if st:
                w("  по статусам: " + ", ".join(f"{k}={v}" for k, v in st.items()))
        except OSError as e:
            w(f"  ошибка чтения очереди: {e}")
    else:
        w(f"Папка очереди {qdir} отсутствует (Демон/задачи ещё не запускались).")


# --- 7. Лог Демона ----------------------------------------------------------
def diag_daemon_log():
    section("7. ЛОГ ДЕМОНА (последние строки)")
    candidates = [os.path.join("auto_tasks", "daemon.log"),
                  os.path.join(".", "auto_tasks", "daemon.log")]
    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()[-100:]
                w(f"(файл: {path}, последние {len(lines)} строк)")
                w("".join(lines).strip() or "(пусто)")
                return
            except OSError as e:
                w(f"Не удалось прочитать {path}: {e}")
                return
    w("Лог демона не найден (демон ещё не запускался — это нормально).")


def main() -> int:
    w("CorePilot — диагностический отчёт")
    w("Отправьте файл corepilot_diagnostics.txt разработчику.")
    safe(diag_environment, "окружение")
    safe(diag_packages, "библиотеки")
    safe(diag_service_selftest, "самопроверка")
    safe(diag_ui_import, "импорт UI")
    safe(diag_unit_tests, "тесты")
    safe(diag_field_state, "полевые данные")
    safe(diag_daemon_log, "лог демона")

    section("ГОТОВО")
    w(f"Отчёт сохранён: {OUT}")
    try:
        with open(OUT, "w", encoding="utf-8") as f:
            f.write(_buf.getvalue())
    except OSError as e:
        print(f"Не удалось записать файл отчёта: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
