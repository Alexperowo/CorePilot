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
PORTABLE_ROOT = HERE.parent
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
def detect_vram_mb() -> int | None:
    """Пытается определить объём видеопамяти (МБ). Сначала wmic (быстро),
    при неудаче - PowerShell Get-CimInstance (wmic удалён в свежих Win11).
    Возвращает None, если не удалось.

    ВНИМАНИЕ: AdapterRAM в WMI - 32-битное значение и для карт >4 ГБ занижается
    до ~4 ГБ. Для RX 6600 (8 ГБ) вернёт ~4 ГБ; пользователь может ввести точный
    лимит вручную в select_ngl."""
    if os.name != "nt":
        return None

    def _parse_bytes(text: str) -> int:
        best = 0
        for line in text.splitlines():
            line = line.strip()
            if line.isdigit():
                best = max(best, int(line) // (1024 * 1024))
        return best

    # 1) wmic (есть в Win10 и части Win11)
    try:
        out = subprocess.run(
            ["wmic", "path", "win32_VideoController", "get", "AdapterRAM"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        if (mb := _parse_bytes(out)):
            return mb
    except Exception:
        pass

    # 2) PowerShell — на случай отсутствия wmic
    try:
        ps = ("Get-CimInstance Win32_VideoController | "
              "Select-Object -ExpandProperty AdapterRAM")
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=15,
        ).stdout
        if (mb := _parse_bytes(out)):
            return mb
    except Exception:
        pass

    return None


def suggest_ngl(model_path: Path, ctx: int, kv_value: str, vram_mb: int) -> int:
    """Грубая эвристика числа слоёв на GPU (-ngl) под доступную VRAM.

    Модель полностью на GPU = -ngl 99. Если не влезает - оцениваем долю.
    Это ПРЕДЛОЖЕНИЕ; пользователь подтверждает или вводит своё.
    """
    model_mb = model_path.stat().st_size / (1024 * 1024)

    # Запас под KV-кэш и накладные расходы. f16 - самый тяжёлый кэш.
    kv_factor = 1.0 if kv_value == "f16" else (0.5 if kv_value == "q8_0" else 0.3)
    # Очень грубая оценка кэша: ~0.5 МБ на токен контекста при f16 для 7-8B.
    kv_mb = ctx * 0.5 * kv_factor
    overhead_mb = 600  # буферы, компиляция шейдеров и т.п.

    needed_full = model_mb + kv_mb + overhead_mb
    if vram_mb >= needed_full:
        return 99  # всё на GPU

    # Доля модели, которую можно разместить (оставляя место под кэш+оверхед).
    usable = max(0, vram_mb - kv_mb - overhead_mb)
    frac = max(0.0, min(1.0, usable / model_mb)) if model_mb > 0 else 0.0
    # Предполагаем ~32 слоя у типовой 7-8B; пользователь скорректирует при иной.
    approx_layers = 32
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
                  api_key: str = "", no_mmap: bool = True) -> list[str]:
    cmd = [server, "-m", str(model), "--host", host, "--port", str(PORT),
           "-c", str(ctx), "-ngl", str(ngl)]
    if kv_value != "f16":
        cmd += ["-ctk", kv_value, "-ctv", kv_value]
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
                 lan_access: bool = False) -> dict:
    """Запускает llama-server как фоновый процесс (без интерактива).
    Возвращает {ok, message, status}. Если vram_mb задан и ngl<0 — подберёт сам.
    lan_access=True биндит 0.0.0.0 (доступ с телефона) — только с токеном."""
    global _SERVER_PROC, _SERVER_INFO
    if _SERVER_PROC is not None and _SERVER_PROC.poll() is None:
        return {"ok": False, "message": "Сервер уже запущен. Остановите перед перезапуском.",
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
        ngl = suggest_ngl(mp, ctx, kv_value, vram) if vram else 99

    needs_fa = kv_value in _QUANTIZED_KV
    # Безопасность: токен обязателен всегда. Доступ с телефона (LAN) —
    # биндинг 0.0.0.0 ТОЛЬКО вместе с токеном, иначе остаёмся на localhost.
    token = get_or_create_token()
    host = "0.0.0.0" if lan_access else HOST
    cmd = build_command(server, mp, ctx, ngl, kv_value, needs_fa, host=host, api_key=token)
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        _SERVER_PROC = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except Exception as e:
        _SERVER_PROC = None
        return {"ok": False, "message": f"Не удалось запустить: {e}", "status": server_status()}

    _SERVER_INFO = {"model": mp.name, "ctx": ctx, "ngl": ngl, "kv": kv_value,
                    "host": host, "lan": lan_access}
    note = " (доступ с телефона: токен в настройках)" if lan_access else ""
    return {"ok": True,
            "message": f"Сервер запущен: {mp.name} (ctx={ctx}, ngl={ngl}, kv={kv_value}){note}.",
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
