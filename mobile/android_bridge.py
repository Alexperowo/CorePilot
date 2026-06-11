#!/usr/bin/env python3
"""
android_bridge.py — единственный модуль, знающий про Android (pyjnius, пути, subprocess).

Зачем мост: pyjnius и системные пути Android работают ТОЛЬКО на устройстве. Чтобы
приложение можно было запускать и отлаживать на ПК (flet run), все платформенные
вызовы изолированы здесь и имеют безопасные фолбэки. Остальной код (UI, логика
сервера) платформы не знает и работает одинаково на ПК и на телефоне.

Содержит:
  - детектор платформы (ANDROID);
  - пути публичной папки CorePilot (общей с ПК-обновлятором);
  - TTS (синтез речи);
"""
from __future__ import annotations

import os
import sys

# --- Детект платформы --------------------------------------------------------
# ANDROID: True если мы на Android-устройстве (по файловой системе, без jnius).
# JNIUS_OK: True только если jnius импортировался успешно.
# Flet использует serious_python, поэтому jnius-путь должен быть максимально
# ленивым и опираться на ActivityThread, а не на UI-фреймворк.
#   1. ANDROID определяем по файловой системе (безопасно).
#   2. JNIUS_OK выставляем только при успешном импорте jnius.
#   3. Все вызовы autoclass откладываются до _ensure_jnius(),
#      вызываемой ЛЕНИВО (не при старте), с обёрткой в try/except.
ANDROID = os.path.exists("/data/data") and os.path.exists("/storage/emulated/0")
JNIUS_OK = False
autoclass = None  # type: ignore
cast = None  # type: ignore
try:
    from jnius import autoclass, cast  # type: ignore
    JNIUS_OK = True
except Exception:
    autoclass = None  # type: ignore
    cast = None  # type: ignore


def _ensure_jnius() -> bool:
    """Проверяет, что jnius реально работает и доступен системный контекст.
    Вызывать ТОЛЬКО из пользовательского взаимодействия, не при старте.
    Возвращает False если недоступно, чтобы вызывающий код мог упасть мягко."""
    if not JNIUS_OK:
        return False
    try:
        ActivityThread = autoclass("android.app.ActivityThread")
        app = ActivityThread.currentApplication()
        return app is not None
    except Exception:
        return False


# --- Публичные пути (общие с ПК через adb push) ------------------------------
# КРИТИЧНО: движок и модели лежат в ПУБЛИЧНОЙ папке, а не внутри .apk. Это
# позволяет обновлять llama-server и .gguf с ПК (update_mobile.bat) без
# переустановки приложения.
PUBLIC_ROOT = "/storage/emulated/0/Download/CorePilot"
ENGINE_DIR = os.path.join(PUBLIC_ROOT, "engine")     # сюда кладётся llama-server
MODELS_DIR = os.path.join(PUBLIC_ROOT, "models")     # сюда кладутся .gguf
LOG_DIR = os.path.join(PUBLIC_ROOT, "logs")

# На ПК (разработка) — кладём рядом с приложением, чтобы всё работало без Android.
if not ANDROID:
    # Размещаем папку ПК-разработки снаружи mobile/, чтобы Flet не упаковывал
    # тяжелые модели и логи внутрь APK (иначе размер APK будет несколько гигабайт!).
    _local = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "_corepilot_data"))
    PUBLIC_ROOT = _local
    ENGINE_DIR = os.path.join(_local, "engine")
    MODELS_DIR = os.path.join(_local, "models")
    LOG_DIR = os.path.join(_local, "logs")


def ensure_dirs() -> None:
    for d in (PUBLIC_ROOT, ENGINE_DIR, MODELS_DIR, LOG_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass


def get_native_lib_dir() -> str | None:
    """Возвращает nativeLibraryDir приложения — ЕДИНСТВЕННОЕ место на нерутованном
    Android, откуда разрешено исполнять нативный код (read-only, но executable).
    Внешнее хранилище (Download) смонтировано noexec, а домашняя папка приложения
    запрещает execve() по правилу W^X (targetAPI>=29) — поэтому бинарник движка
    упаковывается в APK как lib*.so и лежит здесь."""
    if not ANDROID or not _ensure_jnius():
        return None
    try:
        ActivityThread = autoclass("android.app.ActivityThread")
        app = ActivityThread.currentApplication()
        if app is None:
            return None
        ctx = app.getApplicationContext()
        return ctx.getApplicationInfo().nativeLibraryDir
    except Exception as e:
        print(f"[engine] не удалось получить nativeLibraryDir: {e}")
        return None


def find_server_binary() -> str | None:
    """Ищет исполняемый llama-server.

    На Android: ТОЛЬКО в nativeLibraryDir (упакован в APK как libllama-server.so) —
    из Download/engine исполнять нельзя (noexec). На ПК: в локальной папке engine/."""
    # Имена, под которыми движок упаковывается как нативная библиотека APK.
    native_names = ["libllama-server.so", "libllama_server.so", "libllamaserver.so"]
    if ANDROID:
        nd = get_native_lib_dir()
        if nd:
            for name in native_names:
                p = os.path.join(nd, name)
                if os.path.isfile(p):
                    return p
        return None  # на Android из других мест запускать нельзя

    # ПК (разработка): обычный бинарник в локальной папке engine/.
    candidates = ["llama-server", "llama-server-aarch64", "server", "main",
                  "llama-server.exe", "server.exe"]
    try:
        for name in candidates + native_names:
            p = os.path.join(ENGINE_DIR, name)
            if os.path.isfile(p):
                return p
        # Фолбэк на десктопную папку llama/
        parent_llama = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "llama"))
        for name in candidates:
            p = os.path.join(parent_llama, name)
            if os.path.isfile(p):
                return p
        for f in os.listdir(ENGINE_DIR):
            p = os.path.join(ENGINE_DIR, f)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    except OSError:
        pass
    return None


def get_engine_env() -> dict:
    """Переменные окружения для запуска движка. На Android задаёт LD_LIBRARY_PATH
    на nativeLibraryDir, иначе llama-server не найдёт свои .so (libllama.so,
    libggml.so и т.д.), упакованные рядом в APK."""
    env = dict(os.environ)
    if ANDROID:
        nd = get_native_lib_dir()
        if nd:
            prev = env.get("LD_LIBRARY_PATH", "")
            env["LD_LIBRARY_PATH"] = nd + (os.pathsep + prev if prev else "")
    return env


# ===========================================================================
# JNI-запуск нативного процесса (обход W^X Android 12+)
# ===========================================================================
# На Android 12+ (targetSdkVersion ≥ 31) ядро запрещает execve() из Python-потока
# через политику W^X. Java-сторона (JVM) такого ограничения лишена — процесс,
# запущенный через ProcessBuilder, система считает законным дочерним процессом
# Java-приложения. Поэтому на Android мы заменяем subprocess.Popen на jni_popen().

class _JavaStdout:
    """File-like обёртка вокруг Java BufferedReader для построчного чтения stdout
    нативного процесса. Реализует __iter__/__next__ — совместима с pump_output."""

    def __init__(self, reader):
        self._reader = reader

    def __iter__(self):
        return self

    def __next__(self) -> str:
        line = self._reader.readLine()
        if line is None:
            raise StopIteration
        return line + "\n"

    def close(self) -> None:
        try:
            self._reader.close()
        except Exception:
            pass


class JniProcess:
    """Обёртка вокруг java.lang.Process с интерфейсом subprocess.Popen.

    Методы poll/wait/terminate/kill и атрибут stdout реализованы идентично
    subprocess.Popen — llama_server.py не знает разницы между бэкендами."""

    def __init__(self, java_proc):
        self._proc = java_proc
        # Оборачиваем Java InputStream в BufferedReader для построчного чтения.
        _ISR = autoclass("java.io.InputStreamReader")
        _BR  = autoclass("java.io.BufferedReader")
        self.stdout = _JavaStdout(_BR(_ISR(java_proc.getInputStream())))
        # PID: доступен через Process.pid() начиная с Java 9 / Android API 26.
        try:
            self.pid = java_proc.pid()
        except Exception:
            self.pid = -1

    def poll(self):
        """None если процесс жив, иначе код завершения (как subprocess.Popen)."""
        try:
            return self._proc.exitValue()   # бросает IllegalThreadStateException если жив
        except Exception:
            return None

    def wait(self, timeout: float | None = None) -> int:
        if timeout is not None:
            _TU = autoclass("java.util.concurrent.TimeUnit")
            self._proc.waitFor(int(timeout), _TU.SECONDS)
        else:
            self._proc.waitFor()
        return self._proc.exitValue()

    def terminate(self) -> None:
        try:
            self._proc.destroy()
        except Exception:
            pass

    def kill(self) -> None:
        try:
            self._proc.destroyForcibly()
        except Exception:
            pass


def jni_popen(cmd: list[str], env: dict | None = None,
              cwd: str | None = None) -> "JniProcess":
    """Запустить нативный процесс через java.lang.ProcessBuilder.

    Возвращает JniProcess — дроп-ин замена subprocess.Popen.
    Бросает RuntimeError если jnius недоступен (вызывающий код должен поймать
    и упасть с понятным сообщением)."""
    if not _ensure_jnius():
        raise RuntimeError("jnius/JNI недоступен — невозможно запустить через Java")

    _PB       = autoclass("java.lang.ProcessBuilder")
    _ArrayList = autoclass("java.util.ArrayList")
    _File      = autoclass("java.io.File")

    # Команда как java.util.ArrayList<String>
    java_cmd = _ArrayList()
    for arg in cmd:
        java_cmd.add(arg)

    pb = _PB(java_cmd)
    pb.redirectErrorStream(True)   # stderr → stdout (как STDOUT в subprocess)

    if cwd:
        pb.directory(_File(cwd))

    if env is not None:
        java_env = pb.environment()
        java_env.clear()
        for k, v in env.items():
            java_env.put(str(k), str(v))

    return JniProcess(pb.start())


# ===========================================================================

def list_models() -> list[dict]:
    """Список .gguf моделей в публичной папке (имя + размер ГБ).
    При ошибке доступа возвращает список с одним элементом-ошибкой:
    {"error": True, "reason": "..."}  — UI покажет понятное сообщение."""
    out = []
    try:
        entries = sorted(os.listdir(MODELS_DIR))
    except OSError as e:
        reason = str(e)
        if "Permission" in reason or "13" in reason:
            print(f"[models] нет прав на папку моделей: {reason}")
            return [{"error": True, "reason": "Нет доступа к папке моделей — выдайте разрешение (кнопка ниже)."}]
        elif "No such file" in reason or "2]" in reason:
            print(f"[models] папка моделей не существует: {MODELS_DIR}")
            return [{"error": True, "reason": f"Папка не найдена: {MODELS_DIR}\nСкопируйте .gguf с ПК через update_mobile.bat."}]
        else:
            print(f"[models] ошибка доступа к папке моделей: {reason}")
            return [{"error": True, "reason": f"Ошибка доступа к папке моделей: {reason}"}]
    for f in entries:
        if f.lower().endswith(".gguf"):
            p = os.path.join(MODELS_DIR, f)
            try:
                gb = round(os.path.getsize(p) / (1024 ** 3), 2)
            except OSError:
                gb = 0.0
            out.append({"name": f, "path": p, "size_gb": gb})
    return out


def check_manage_external_storage() -> bool:
    """Проверяет, выдано ли разрешение MANAGE_EXTERNAL_STORAGE (Android 11+).
    На ПК всегда возвращает True."""
    if not ANDROID or not _ensure_jnius():
        return True
    try:
        Environment = autoclass("android.os.Environment")
        return bool(Environment.isExternalStorageManager())
    except Exception:
        return False


def request_manage_external_storage() -> None:
    """Открывает системный экран выдачи разрешения MANAGE_EXTERNAL_STORAGE.
    Вызывать только после старта UI, не при инициализации.
    На ПК — нет-оп."""
    if not ANDROID or not _ensure_jnius():
        return
    try:
        ActivityThread = autoclass("android.app.ActivityThread")
        app = ActivityThread.currentApplication()
        if app is None:
            return
        Settings = autoclass("android.provider.Settings")
        Intent = autoclass("android.content.Intent")
        Uri = autoclass("android.net.Uri")
        intent = Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION)
        intent.setData(Uri.parse(f"package:{app.getPackageName()}"))
        # Flet использует Activity через ActivityThread; startActivity через контекст.
        ctx = app.getApplicationContext()
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        ctx.startActivity(intent)
    except Exception as e:
        print(f"[storage] не удалось открыть экран разрешений: {e}")


def make_executable(path: str) -> bool:
    """Ставит бит исполняемости (бинарник, прилетевший с ПК через adb, его теряет)."""
    try:
        import stat
        st = os.stat(path)
        os.chmod(path, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return True
    except OSError:
        return False


# ===========================================================================
# Синтез речи (TTS) — android.speech.tts.TextToSpeech
# ===========================================================================

class TTSEngine:
    """Обёртка над системным TTS Android. На ПК — печатает в консоль (фолбэк).

    Инициализация jnius откладывается до первого вызова speak() — это критично:
    вызов autoclass() при старте из asyncio-потока Flet вызывает SIGABRT."""

    def __init__(self):
        self._tts = None
        self._ready = False
        self._init_tried = False

    def _init_android(self):
        if self._init_tried:
            return
        self._init_tried = True
        if not _ensure_jnius():
            return
        try:
            ActivityThread = autoclass("android.app.ActivityThread")
            app = ActivityThread.currentApplication()
            if app is None:
                return
            ctx = app.getApplicationContext()
            TextToSpeech = autoclass("android.speech.tts.TextToSpeech")
            # listener=None: нет коллбэка готовности — движок инициализируется асинхронно.
            # Задержка 1 с гарантирует, что первый вызов speak() не упадёт в ENGINE_NOT_READY.
            self._tts = TextToSpeech(ctx, None)
            self._TextToSpeech = TextToSpeech
            import time as _time
            _time.sleep(1)   # ждём асинхронную инициализацию движка
            self._ready = True
        except Exception as e:
            print(f"[TTS] инициализация не удалась: {e}")
            self._tts = None
            self._ready = False

    def speak(self, text: str) -> None:
        if not text:
            return
        if not ANDROID:
            print(f"[TTS-фолбэк] {text}")
            return
        if not self._ready:
            self._init_android()
        if not self._tts:
            print(f"[TTS-фолбэк] {text}")
            return
        try:
            # QUEUE_FLUSH = 0: новая фраза прерывает предыдущую.
            self._tts.speak(text, 0, None, "corepilot")
        except Exception as e:
            print(f"[TTS] ошибка озвучки: {e}")

    def stop(self) -> None:
        if ANDROID and self._tts:
            try:
                self._tts.stop()
            except Exception:
                pass

    def shutdown(self) -> None:
        if ANDROID and self._tts:
            try:
                self._tts.shutdown()
            except Exception:
                pass
