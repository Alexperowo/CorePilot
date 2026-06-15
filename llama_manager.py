#!/usr/bin/env python3
"""
llama_manager.py - интерактивный запуск локальных GGUF-моделей через llama-server.

Возможности:
  - сканирует models/ на .gguf (подсказывает, где взять, если пусто);
  - выбор квантования KV-кэша (-ctk/-ctv) с пояснениями и авто-включением -fa;
  - оценка VRAM и предложение оптимального -ngl (с правом переопределить);
  - выбор размера контекста -c (дефолт 4096 — безопасно для 8ГБ VRAM);
  - формирует и запускает команду llama-server.exe на порту 8080.

Анти-хардкод: пути и параметры определяются на лету или спрашиваются у пользователя.
Окружение можно переопределить переменными: LLAMA_MODELS_DIR, LLAMA_BIN_DIR,
LLAMA_PORT, LLAMA_HOST.
"""
from __future__ import annotations

import os
import sys
import shutil
import subprocess
from pathlib import Path

# --- Константы и пути (определяются относительно расположения скрипта) --------
HERE = Path(__file__).resolve().parent
# В портативной сборке структура: <root>/app/llama_manager.py, <root>/models, <root>/llama
PORTABLE_ROOT = HERE
DEFAULT_MODELS = os.environ.get("LLAMA_MODELS_DIR") or str(PORTABLE_ROOT / "models")
DEFAULT_BIN = os.environ.get("LLAMA_BIN_DIR") or str(PORTABLE_ROOT / "llama")
PORT = os.environ.get("LLAMA_PORT", "8080")
HOST = os.environ.get("LLAMA_HOST", "127.0.0.1")

# Файл токена доступа к серверу (bearer). Генерируется один раз, переживает
# перезапуски. Передаётся в llama-server через --api-key и нужен клиентам
# (мобильному приложению) в заголовке Authorization: Bearer <token>.
_TOKEN_FILE = PORTABLE_ROOT / "server_token.txt"


def get_or_create_token() -> str:
    """Возвращает постоянный токен доступа; создаёт при первом обращении."""
    try:
        if _TOKEN_FILE.is_file():
            t = _TOKEN_FILE.read_text(encoding="utf-8").strip()
            if t:
                return t
    except OSError:
        pass
    import secrets
    token = secrets.token_urlsafe(24)
    try:
        _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_FILE.write_text(token, encoding="utf-8")
    except OSError:
        pass  # не смогли сохранить — токен валиден на эту сессию
    return token

# Квантование KV-кэша: метка -> (значение флага, краткое пояснение).
# Значения сверены с llama-server: f16(дефолт), q8_0, q5_1, q5_0, q4_1, q4_0, iq4_nl.
KV_CACHE_OPTIONS: list[tuple[str, str, str]] = [
    ("None (f16)", "f16",    "Без сжатия. Максимальное качество, больше всего VRAM."),
    ("q8_0",       "q8_0",   "Почти без потерь, ~2x экономия. Безопасный выбор."),
    ("q5_1",       "q5_1",   "Хороший баланс качества и памяти."),
    ("q5_0",       "q5_0",   "Граница безопасности - ниже логика начинает страдать."),
    ("q4_1",       "q4_1",   "Агрессивно. Возможна деградация рассуждений."),
    ("q4_0",       "q4_0",   "Очень агрессивно. Заметная деградация качества."),
    ("iq4_nl",     "iq4_nl", "4-бит non-linear. Компактно, качество может страдать."),
]
# Значения, для которых kv-кэш считается сжатым (нужен flash-attention).
_QUANTIZED_KV = {"q8_0", "q5_1", "q5_0", "q4_1", "q4_0", "iq4_nl"}

GGUF_DOWNLOAD_HINT = (
    "В папке models нет ни одного .gguf файла.\n"
    "Скачайте модель в формате GGUF (например с https://huggingface.co/models?library=gguf)\n"
    "и положите .gguf-файл в:\n  %s\n"
    "Для 8 ГБ VRAM подойдут модели 7-8B в квантовании Q4_K_M (~4.5-5 ГБ)."
)


# --- Вспомогательные функции ввода -------------------------------------------
def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    try:
        ans = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nОтменено.")
        sys.exit(1)
    return ans or (default or "")


def _ask_int(prompt: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    while True:
        raw = _ask(prompt, str(default))
        try:
            val = int(raw)
        except ValueError:
            print("  Введите целое число.")
            continue
        if lo is not None and val < lo:
            print(f"  Минимум {lo}.")
            continue
        if hi is not None and val > hi:
            print(f"  Максимум {hi}.")
            continue
        return val


def _ask_choice(prompt: str, count: int, default: int = 1) -> int:
    while True:
        raw = _ask(prompt, str(default))
        try:
            idx = int(raw)
        except ValueError:
            print("  Введите номер из списка.")
            continue
        if 1 <= idx <= count:
            return idx
        print(f"  Введите номер от 1 до {count}.")


# --- Поиск бинарника и моделей ------------------------------------------------
def find_server_binary() -> str | None:
    """Ищет llama-server.exe: сначала в LLAMA_BIN_DIR, затем рекурсивно в нём,
    затем в PATH."""
    exe = "llama-server.exe" if os.name == "nt" else "llama-server"
    direct = Path(DEFAULT_BIN) / exe
    if direct.is_file():
        return str(direct)
    bin_dir = Path(DEFAULT_BIN)
    if bin_dir.is_dir():
        for found in bin_dir.rglob(exe):
            if found.is_file():
                return str(found)
    on_path = shutil.which(exe)
    return on_path


def scan_models() -> list[Path]:
    models_dir = Path(DEFAULT_MODELS)
    if not models_dir.is_dir():
        models_dir.mkdir(parents=True, exist_ok=True)
        return []
    return sorted(models_dir.rglob("*.gguf"))


def select_model(models: list[Path]) -> Path:
    print("\nНайденные модели (.gguf):")
    for i, m in enumerate(models, 1):
        size_gb = m.stat().st_size / (1024 ** 3)
        print(f"  [{i}] {m.name}  ({size_gb:.2f} ГБ)")
    idx = _ask_choice("Выберите модель", len(models))
    return models[idx - 1]


# --- KV-кэш -------------------------------------------------------------------
def select_kv_cache() -> tuple[str, bool]:
    """Возвращает (значение_флага, нужен_ли_flash_attention)."""
    print("\nКвантование KV-кэша (экономит VRAM, влияет на качество):")
    print("  Подсказка: ниже q5_0 логика модели может страдать.")
    for i, (label, _, desc) in enumerate(KV_CACHE_OPTIONS, 1):
        print(f"  [{i}] {label:<11} - {desc}")
    idx = _ask_choice("Выберите тип KV-кэша", len(KV_CACHE_OPTIONS), default=2)  # q8_0 по умолчанию
    _, value, _ = KV_CACHE_OPTIONS[idx - 1]
    needs_fa = value in _QUANTIZED_KV
    if needs_fa:
        print("  -> Для квантованного KV-кэша будет включён Flash Attention (-fa 1).")
    return value, needs_fa


# --- Оценка VRAM и -ngl -------------------------------------------------------
_VRAM_CACHE: "int | None" = None  # кеш — dxdiag медленный, вызываем один раз


def detect_vram_mb() -> "int | None":
    """Определяет объём видеопамяти (МБ). Результат кешируется на сессию.

    Порядок попыток:
    1. Реестр Windows — 64-битное HardwareInformation.MemorySize (точно для NVIDIA).
    2. WMI AdapterRAM — быстро, но у AMD RX 5xxx/6xxx/7xxx возвращает ровно 4 ГБ
       из-за 32-битного поля. Если значение ≤ 4100 МБ — подозрительно, идём дальше.
    3. dxdiag /t — единственный надёжный способ для AMD >4 ГБ на Windows.
       Медленный (~8 с), но вызывается один раз и кешируется.
    """
    global _VRAM_CACHE
    if _VRAM_CACHE is not None:
        return _VRAM_CACHE

    result = _detect_vram_uncached()
    _VRAM_CACHE = result
    return result


def _detect_vram_uncached() -> "int | None":
    if os.name != "nt":
        return None

    # 1) Реестр: Display adapter class {4d36e968-…}
    reg_result: "int | None" = None
    try:
        import winreg
        base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        best = 0
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as hbase:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(hbase, i)
                    i += 1
                    if not sub.isdigit():
                        continue
                    try:
                        with winreg.OpenKey(hbase, sub) as hk:
                            val, _ = winreg.QueryValueEx(hk, "HardwareInformation.MemorySize")
                            if isinstance(val, int) and val > 0:
                                best = max(best, val // (1024 * 1024))
                    except OSError:
                        pass
                except OSError:
                    break
        if best:
            reg_result = best
    except Exception:
        pass

    def _parse_bytes(text: str) -> int:
        best = 0
        for line in text.splitlines():
            line = line.strip()
            if line.isdigit():
                best = max(best, int(line) // (1024 * 1024))
        return best

    # 2) wmic — 32-битное, занижает AMD-карты >4 ГБ
    wmi_result: "int | None" = None
    try:
        out = subprocess.run(
            ["wmic", "path", "win32_VideoController", "get", "AdapterRAM"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        if (mb := _parse_bytes(out)):
            wmi_result = mb
    except Exception:
        pass

    if not wmi_result:
        try:
            ps = ("Get-CimInstance Win32_VideoController | "
                  "Select-Object -ExpandProperty AdapterRAM")
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, text=True, timeout=15,
            ).stdout
            if (mb := _parse_bytes(out)):
                wmi_result = mb
        except Exception:
            pass

    # Если оба метода дали >4100 МБ — доверяем (NVIDIA/Intel), возвращаем.
    fast_result = reg_result or wmi_result
    if fast_result and fast_result > 4100:
        return fast_result

    # 3) dxdiag: единственно надёжный путь для AMD >4 ГБ.
    # Парсим строку "Dedicated Memory: XXXX MB" из текстового отчёта.
    try:
        import re, tempfile
        tmp = os.path.join(tempfile.gettempdir(), "corepilot_dxdiag.txt")
        subprocess.run(["dxdiag", "/t", tmp], capture_output=True, timeout=20)
        if os.path.exists(tmp):
            text = open(tmp, encoding="utf-8", errors="ignore").read()
            try:
                os.unlink(tmp)
            except OSError:
                pass
            m = re.search(r"Dedicated Memory:\s*(\d+)\s*MB", text)
            if m:
                val = int(m.group(1))
                if val > 0:
                    return val
    except Exception:
        pass

    return fast_result  # последний шанс — возможно заниженное WMI-значение


def query_vram_usage_mb() -> "int | None":
    """Текущее использование VRAM (МБ) через счётчики Windows Performance.
    Работает для AMD и NVIDIA. Возвращает None если счётчики недоступны."""
    if os.name != "nt":
        return None
    try:
        ps = (
            "try { $s=(Get-Counter '\\GPU Adapter Memory(*)\\Dedicated Usage'"
            " -ErrorAction Stop -MaxSamples 1).CounterSamples |"
            " Where-Object {$_.CookedValue -gt 0} |"
            " Measure-Object -Property CookedValue -Maximum;"
            " [math]::Round($s.Maximum/1MB) } catch { 0 }"
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=8,
        ).stdout.strip()
        val = int(out) if out.isdigit() else 0
        return val if val > 0 else None
    except Exception:
        return None


def find_gguf_path(name: str) -> "Path | None":
    """Ищет .gguf-файл по имени (basename) в стандартных директориях.
    Нужно для жонглирования моделями: state хранит имя файла, не полный путь."""
    search_dirs: list[Path] = [Path(DEFAULT_MODELS)]
    for drive in ("D:/", "C:/", "E:/"):
        for sub in ("models", "models/Local", "LLM", "GGUF"):
            p = Path(drive) / sub
            if p.is_dir():
                search_dirs.append(p)
    seen: set[Path] = set()
    for base in search_dirs:
        if base in seen or not base.is_dir():
            continue
        seen.add(base)
        for found in base.rglob(name):
            if found.is_file():
                return found
    return None


def _kv_factor(quant: str) -> float:
    """Коэффициент сжатия KV-кеша относительно f16 (1.0 = без сжатия)."""
    if quant == "f16":   return 1.0
    if quant == "q8_0":  return 0.50
    if quant == "q5_1":  return 0.34
    if quant == "q5_0":  return 0.31
    if quant in ("q4_1", "iq4_nl"): return 0.27
    if quant == "q4_0":  return 0.25
    return 0.30  # неизвестный тип — консервативная оценка


def suggest_ngl(model_path: Path, ctx: int, kv_value: str, vram_mb: int,
                ctv: str = "") -> int:
    """Грубая эвристика числа слоёв на GPU (-ngl) под доступную VRAM.

    Модель полностью на GPU = -ngl 99. Если не влезает - оцениваем долю.
    Это ПРЕДЛОЖЕНИЕ; пользователь подтверждает или вводит своё.
    ctv — квантование V-кеша (если отличается от kv_value/ctk).
    Асимметрия K=q5_1 + V=iq4_nl экономит ~9% VRAM по сравнению с q5_1+q5_1.
    """
    model_mb = model_path.stat().st_size / (1024 * 1024)

    # Запас под KV-кэш и накладные расходы.
    # При асимметричном KV усредняем факторы K и V.
    ctv_val = ctv if ctv else kv_value
    avg_factor = (_kv_factor(kv_value) + _kv_factor(ctv_val)) / 2
    # Грубая оценка: ~0.5 МБ на токен контекста при f16.
    kv_mb = ctx * 0.5 * avg_factor
    overhead_mb = 600  # буферы, компиляция шейдеров и т.п.

    needed_full = model_mb + kv_mb + overhead_mb
    if vram_mb >= needed_full:
        return 99  # всё на GPU

    # Доля модели, которую можно разместить (оставляя место под кэш+оверхед).
    usable = max(0, vram_mb - kv_mb - overhead_mb)
    frac = max(0.0, min(1.0, usable / model_mb)) if model_mb > 0 else 0.0
    # Оценка числа слоёв по размеру модели (при ~4.5 бит/вес в среднем).
    params_b = (model_mb * 1024 * 1024 * 8) / (4.5 * 1e9)
    if params_b < 4:
        approx_layers = 18
    elif params_b < 10:
        approx_layers = 32
    elif params_b < 20:
        approx_layers = 42
    elif params_b < 40:
        approx_layers = 62
    elif params_b < 80:
        approx_layers = 80
    else:
        approx_layers = 96
    ngl = int(approx_layers * frac)
    return max(0, ngl)


def select_ngl(model_path: Path, ctx: int, kv_value: str) -> int:
    print("\nРаспределение слоёв на GPU (-ngl):")
    vram = detect_vram_mb()
    if vram:
        print(f"  Обнаружено видеопамяти: ~{vram} МБ ({vram/1024:.1f} ГБ).")
    else:
        print("  Не удалось автоопределить VRAM.")
        ans = _ask("  Ввести лимит VRAM в МБ вручную? (Enter - пропустить оценку)", "")
        vram = int(ans) if ans.isdigit() else 0

    if vram:
        suggested = suggest_ngl(model_path, ctx, kv_value, vram)
        if suggested >= 99:
            print("  Рекомендация: -ngl 99 (вся модель помещается на GPU).")
        else:
            print(f"  Рекомендация: -ngl {suggested} (частичная выгрузка, остальное на CPU).")
        default_ngl = suggested
    else:
        print("  Без оценки VRAM рекомендация по умолчанию: -ngl 99 (уменьшите при нехватке).")
        default_ngl = 99

    return _ask_int("Сколько слоёв выгрузить на GPU (-ngl, 99 = все, 0 = только CPU)",
                    default_ngl, lo=0, hi=999)


# --- Сборка и запуск ----------------------------------------------------------
def build_command(server: str, model: Path, ctx: int, ngl: int,
                  kv_value: str, needs_fa: bool, host: str = HOST,
                  api_key: str = "", no_mmap: bool = True,
                  ctv: str = "") -> list[str]:
    """ctv — отдельное квантование для V-кеша (если отличается от kv_value/ctk).
    Асимметрия K+V: ctk=q5_1, ctv=iq4_nl даёт ~9% экономии VRAM при минимальных
    потерях качества (K важнее для точности attention, V менее чувствителен)."""
    cmd = [server, "-m", str(model), "--host", host, "--port", str(PORT),
           "-c", str(ctx), "-ngl", str(ngl)]
    ctk = kv_value
    ctv_val = ctv if ctv else kv_value
    if ctk != "f16":
        cmd += ["-ctk", ctk]
    if ctv_val != "f16":
        cmd += ["-ctv", ctv_val]
    if needs_fa:
        cmd += ["-fa", "on"]   # в актуальном llama-server: on|off|auto (не 1/0)
    # --no-mmap: НЕ отображать файл модели в RAM. По умолчанию llama.cpp держит
    # копию весов в системной памяти (mmap), даже когда слои выгружены на GPU —
    # на системе с 16 ГБ RAM это даёт дублирование (5 ГБ VRAM + 5 ГБ RAM) и своп.
    # Отключаем, чтобы веса жили только в VRAM.
    if no_mmap:
        cmd += ["--no-mmap"]
    # Токен доступа: защищает эндпоинт от посторонних в локальной сети.
    if api_key:
        cmd += ["--api-key", api_key]
    return cmd


# ===========================================================================
# Программный API (для GUI): запуск/останов сервера без интерактивных вопросов
# ===========================================================================

# Дескриптор запущенного процесса сервера (одиночка на процесс приложения).
_SERVER_PROC: "Optional[subprocess.Popen]" = None
_SERVER_INFO: dict = {}


def list_models() -> list[dict]:
    """Список GGUF-моделей с размером (для выпадающего списка в UI)."""
    out = []
    for m in scan_models():
        try:
            out.append({"path": str(m), "name": m.name,
                        "size_gb": round(m.stat().st_size / (1024 ** 3), 2)})
        except OSError:
            continue
    return out


def kv_cache_options() -> list[dict]:
    """Варианты квантования KV-кэша для UI (метка, значение, пояснение)."""
    return [{"label": lbl, "value": val, "desc": desc}
            for lbl, val, desc in KV_CACHE_OPTIONS]


def server_status() -> dict:
    """Состояние управляемого сервера: запущен ли, PID, на каком порту, модель."""
    running = _SERVER_PROC is not None and _SERVER_PROC.poll() is None
    return {
        "running": running,
        "pid": _SERVER_PROC.pid if running else None,
        "port": PORT,
        "host": _SERVER_INFO.get("host", HOST),
        "url": f"http://{_SERVER_INFO.get('host', HOST)}:{PORT}/v1",
        "model": _SERVER_INFO.get("model", ""),
        "lan": _SERVER_INFO.get("lan", False),
        "token": get_or_create_token(),
        "binary_found": bool(find_server_binary()),
    }


def start_server(model_path: str, ctx: int = 4096, ngl: int = 99,
                 kv_value: str = "q8_0", vram_mb: Optional[int] = None,
                 lan_access: bool = False, ctv: str = "") -> dict:
    """Запускает llama-server как фоновый процесс (без интерактива).
    Возвращает {ok, message, status}. Если vram_mb задан и ngl<0 — подберёт сам.
    lan_access=True биндит 0.0.0.0 (доступ с телефона) — только с токеном.
    ctv — отдельное квантование V-кеша (если пусто = совпадает с kv_value/ctk)."""
    global _SERVER_PROC, _SERVER_INFO
    if _SERVER_PROC is not None and _SERVER_PROC.poll() is None:
        return {"ok": False, "message": "Сервер уже запущен. Остановите перед перезапуском.",
                "status": server_status()}
    # Проверяем, не занят ли порт другим процессом (например, предыдущей сессией).
    import socket as _sock
    with _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM) as _s:
        if _s.connect_ex(("127.0.0.1", int(PORT))) == 0:
            return {"ok": False,
                    "message": f"Порт {PORT} уже занят другим процессом. "
                               f"Закройте предыдущий llama-server или измените LLAMA_PORT.",
                    "status": server_status()}
    server = find_server_binary()
    if not server:
        return {"ok": False, "message": f"llama-server не найден в {DEFAULT_BIN}. "
                f"Соберите портативную версию или задайте LLAMA_BIN_DIR.",
                "status": server_status()}
    mp = Path(model_path)
    if not mp.is_file():
        return {"ok": False, "message": f"Модель не найдена: {model_path}",
                "status": server_status()}

    # Автоподбор -ngl, если запрошено (ngl < 0) и известен объём VRAM.
    if ngl < 0:
        vram = vram_mb or detect_vram_mb() or 0
        ngl = suggest_ngl(mp, ctx, kv_value, vram, ctv=ctv) if vram else 99

    ctv_val = ctv if ctv else kv_value
    needs_fa = kv_value in _QUANTIZED_KV or ctv_val in _QUANTIZED_KV
    # Безопасность: токен обязателен всегда. Доступ с телефона (LAN) —
    # биндинг 0.0.0.0 ТОЛЬКО вместе с токеном, иначе остаёмся на localhost.
    token = get_or_create_token()
    host = "0.0.0.0" if lan_access else HOST
    cmd = build_command(server, mp, ctx, ngl, kv_value, needs_fa, host=host, api_key=token, ctv=ctv)
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        _SERVER_PROC = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except Exception as e:
        _SERVER_PROC = None
        return {"ok": False, "message": f"Не удалось запустить: {e}", "status": server_status()}

    _SERVER_INFO = {"model": mp.name, "model_path": str(mp), "ctx": ctx,
                    "ngl": ngl, "kv": kv_value, "ctv": ctv, "host": host, "lan": lan_access}
    kv_desc = f"{kv_value}+{ctv}" if ctv and ctv != kv_value else kv_value
    note = " (доступ с телефона: токен в настройках)" if lan_access else ""
    return {"ok": True,
            "message": f"Сервер запущен: {mp.name} (ctx={ctx}, ngl={ngl}, kv={kv_desc}){note}.",
            "status": server_status(), "command": " ".join(cmd)}


def stop_server() -> dict:
    """Останавливает управляемый сервер."""
    global _SERVER_PROC
    if _SERVER_PROC is None or _SERVER_PROC.poll() is not None:
        _SERVER_PROC = None
        return {"ok": False, "message": "Сервер не запущен.", "status": server_status()}
    try:
        _SERVER_PROC.terminate()
        try:
            _SERVER_PROC.wait(timeout=8)
        except subprocess.TimeoutExpired:
            _SERVER_PROC.kill()
    except Exception as e:
        return {"ok": False, "message": f"Ошибка остановки: {e}", "status": server_status()}
    _SERVER_PROC = None
    return {"ok": True, "message": "Сервер остановлен.", "status": server_status()}


def restart_for_model(new_model_name: str, vram_mb: Optional[int] = None) -> dict:
    """Жонглирование для llamacpp: останавливает текущий сервер и запускает новый
    с моделью new_model_name. Контекст и kv берёт из последнего _SERVER_INFO.
    Нужен при смене модели между агентами (разные модели на разные роли).
    Если модели одинаковы — no-op.

    new_model_name — имя файла .gguf (как хранится в state.model_{role}).
    Полный путь ищется через find_gguf_path(). Если не найден — возвращает ошибку."""
    current = _SERVER_INFO.get("model", "")
    if current and (current == new_model_name or current.startswith(new_model_name)):
        return {"ok": True, "message": f"Модель '{new_model_name}' уже загружена, перезапуск не нужен.",
                "status": server_status()}

    # Найти полный путь к новой модели
    new_path = find_gguf_path(new_model_name)
    if new_path is None:
        return {"ok": False, "message": f"Файл модели '{new_model_name}' не найден ни в одной из директорий.",
                "status": server_status()}

    # Параметры сервера берём из предыдущего запуска
    ctx = _SERVER_INFO.get("ctx", 4096)
    kv = _SERVER_INFO.get("kv", "q8_0")
    ctv = _SERVER_INFO.get("ctv", "")
    lan = _SERVER_INFO.get("lan", False)

    stop_server()

    # Небольшая пауза — Windows может не сразу освободить порт
    import time as _t
    _t.sleep(1.5)

    ngl = -1  # автоподбор с учётом нового размера модели
    return start_server(str(new_path), ctx=ctx, ngl=ngl, kv_value=kv,
                        vram_mb=vram_mb, lan_access=lan, ctv=ctv)


def main() -> int:
    print("=" * 64)
    print("  CorePilot - llama.cpp launcher")
    print("=" * 64)

    server = find_server_binary()
    if not server:
        print(f"\n[ОШИБКА] Не найден llama-server.")
        print(f"Ожидался в: {DEFAULT_BIN}")
        print("Соберите портативную версию (Portable.bat) или задайте LLAMA_BIN_DIR.")
        return 1

    models = scan_models()
    if not models:
        print("\n" + GGUF_DOWNLOAD_HINT % DEFAULT_MODELS)
        return 1

    model = select_model(models)
    ctx = _ask_int("\nРазмер контекста (-c)", 4096, lo=256, hi=1_048_576)
    kv_value, needs_fa = select_kv_cache()
    ngl = select_ngl(model, ctx, kv_value)

    cmd = build_command(server, model, ctx, ngl, kv_value, needs_fa)

    print("\n" + "-" * 64)
    print("Команда запуска:")
    print("  " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    print("-" * 64)
    print(f"Сервер будет доступен на: http://{HOST}:{PORT}/v1")
    print("В настройках CorePilot выберите локальный бэкенд 'llamacpp'.\n")

    if _ask("Запустить? (y/n)", "y").lower() not in ("y", "yes", "д", "да"):
        print("Отменено пользователем.")
        return 0

    try:
        return subprocess.call(cmd)
    except FileNotFoundError:
        print("[ОШИБКА] Не удалось запустить бинарник сервера.")
        return 1
    except KeyboardInterrupt:
        print("\nСервер остановлен.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
