#!/usr/bin/env python3
"""
llama_server.py — управление локальным сервером llama.cpp на телефоне (Edge AI Node).

Запускает бинарник llama-server из ПУБЛИЧНОЙ папки движка (обновляемой с ПК),
ведёт лог в файл и в память (для вывода в UI), даёт старт/стоп. Платформу не знает —
все пути берёт из android_bridge.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from collections import deque

import android_bridge as ab


class LlamaServer:
    """Управляемый процесс llama-server. Один экземпляр на приложение."""

    def __init__(self, max_log_lines: int = 500):
        self._proc: subprocess.Popen | None = None
        self._log: deque[str] = deque(maxlen=max_log_lines)
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._cfg: dict = {}

    # ---- состояние ----
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def status(self) -> dict:
        running = self.is_running()
        return {
            "running": running,
            "pid": self._proc.pid if running else None,
            "port": self._cfg.get("port", 8080),
            "model": os.path.basename(self._cfg.get("model", "")) if self._cfg.get("model") else "",
            "url": f"http://127.0.0.1:{self._cfg.get('port', 8080)}/v1",
            # "binary" намеренно исключён: find_server_binary() делает JNI-вызов
            # из фонового потока _status_loop каждые 2 с → SIGABRT на Android.
            # Путь к движку читается один раз при построении UI в _show_edge_ai().
        }

    def get_log(self) -> str:
        with self._lock:
            return "\n".join(self._log)

    def _append(self, line: str) -> None:
        with self._lock:
            self._log.append(line.rstrip("\n"))

    # ---- запуск ----
    def start(self, model_path: str, ctx: int = 4096, port: int = 8080,
              ngl: int = 99, extra_args: list[str] | None = None) -> tuple[bool, str]:
        if self.is_running():
            return False, "Сервер уже запущен. Остановите перед перезапуском."

        binary = ab.find_server_binary()
        if not binary:
            if ab.ANDROID:
                return False, ("Движок llama-server не найден в составе приложения. "
                               "Переустановите APK со встроенным движком.")
            return False, (f"Бинарник llama-server не найден в {ab.ENGINE_DIR}. "
                           f"Положите движок в эту папку.")
        if not model_path or not os.path.isfile(model_path):
            return False, f"Модель не найдена: {model_path}"

        # На Android бинарник лежит в nativeLibraryDir (read-only, уже executable) —
        # chmod не нужен и невозможен. На ПК ставим бит исполняемости.
        if not ab.ANDROID:
            ab.make_executable(binary)

        cmd = [binary, "-m", model_path, "--host", "127.0.0.1",
               "--port", str(port), "-c", str(ctx), "-ngl", str(ngl)]
        if extra_args:
            cmd += extra_args

        self._log.clear()
        self._append(f"$ {' '.join(cmd)}")

        env  = ab.get_engine_env()   # LD_LIBRARY_PATH на nativeLibraryDir (Android)
        cwd  = ab.MODELS_DIR

        if ab.ANDROID:
            # Android 12+ (targetAPI≥31): execve() из Python-потока блокируется W^X.
            # Запускаем через Java ProcessBuilder — система считает такой процесс
            # законным потомком JVM, W^X-ограничение не применяется.
            try:
                self._proc = ab.jni_popen(cmd, env=env, cwd=cwd)
            except RuntimeError as e:
                self._proc = None
                return False, (
                    f"JNI-запуск недоступен: {e}. "
                    "Убедитесь, что pyjnius включён в сборку APK."
                )
            except Exception as e:
                self._proc = None
                return False, f"Ошибка запуска через JNI: {e}"
        else:
            # ПК (разработка): обычный subprocess.Popen.
            try:
                self._proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, cwd=cwd, env=env,
                )
            except PermissionError as e:
                self._proc = None
                return False, f"Нет прав на запуск бинарника: {e}"
            except Exception as e:
                self._proc = None
                return False, f"Не удалось запустить: {e}"

        self._cfg = {"model": model_path, "ctx": ctx, "port": port, "ngl": ngl}
        self._reader = threading.Thread(target=self._pump_output, daemon=True)
        self._reader.start()
        return True, f"Сервер запущен: {os.path.basename(model_path)} (порт {port}, ctx {ctx})."

    def _pump_output(self) -> None:
        """Читает stdout процесса в лог (в фоне)."""
        proc = self._proc
        if not proc or not proc.stdout:
            return
        try:
            for line in proc.stdout:
                self._append(line)
        except Exception as e:
            self._append(f"[чтение лога прервано: {e}]")
        rc = proc.poll()
        self._append(f"[сервер завершился, код {rc}]")

    # ---- остановка ----
    def stop(self) -> tuple[bool, str]:
        if not self.is_running():
            self._proc = None
            return False, "Сервер не запущен."
        try:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        except Exception as e:
            return False, f"Ошибка остановки: {e}"
        self._proc = None
        self._append("[сервер остановлен пользователем]")
        return True, "Сервер остановлен."
