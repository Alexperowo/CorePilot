#!/usr/bin/env python3
"""
ui_common.py — общая основа GUI CorePilot (DearPyGui).

Содержит то, что используют все вкладки: палитру статусов (символ+слово+цвет для
доступности), звуковые сигналы, класс Accessibility (крупный шрифт + тема) и
BackgroundRunner (фоновые потоки, чтобы UI не замерзал).

Изоляция сохраняется: импортирует только dearpygui + стандартную библиотеку.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Optional

import dearpygui.dearpygui as dpg

import service_layer as svc

# --- Высококонтрастная палитра статусов (для слабовидящих) -------------------
# Цвета подобраны яркими и насыщенными на тёмном фоне. КЛЮЧЕВОЕ: статус всегда
# дублируется СИМВОЛОМ и СЛОВОМ, а не только цветом — различимо при любом зрении
# и при дальтонизме.
STATUS_COLORS = {
    svc.STATUS_DONE:       (80, 220, 100),   # яркий зелёный
    svc.STATUS_FAILED:     (255, 80, 80),    # яркий красный
    svc.STATUS_FROZEN:     (170, 170, 180),  # светло-серый (видим на тёмном)
    svc.STATUS_PROCESSING: (255, 200, 50),   # янтарный
    svc.STATUS_PENDING:    (90, 165, 255),   # яркий голубой
}
STATUS_LABEL = {
    svc.STATUS_DONE: "ГОТОВО", svc.STATUS_FAILED: "ОШИБКА", svc.STATUS_FROZEN: "ЗАМОРОЖЕНО",
    svc.STATUS_PROCESSING: "В РАБОТЕ", svc.STATUS_PENDING: "ОЖИДАЕТ",
}
# Символ статуса — второй (не-цветовой) канал различения.
STATUS_SYMBOL = {
    svc.STATUS_DONE: "[OK]", svc.STATUS_FAILED: "[X]", svc.STATUS_FROZEN: "[||]",
    svc.STATUS_PROCESSING: "[>>]", svc.STATUS_PENDING: "[..]",
}
REFRESH_SECONDS = 1.5


def _play_sound(kind: str = "done"):
    """Короткий звуковой сигнал (idea 7). Канал обратной связи, удобный при
    слабом зрении. Кроссплатформенно, тихо деградирует если звук недоступен."""
    def _worker():
        try:
            if __import__("os").name == "nt":
                import winsound
                freq = 880 if kind == "done" else (440 if kind == "fail" else 660)
                winsound.Beep(freq, 200)
            else:
                # POSIX: системный звонок терминала (best-effort).
                import sys as _s
                _s.stdout.write("\a"); _s.stdout.flush()
        except Exception:
            pass
    threading.Thread(target=_worker, daemon=True).start()

# Размеры шрифта (px). Базовый намеренно крупный — это приложение для пользователя
# с сильным нарушением зрения. Регулируется в UI на лету.
FONT_SIZES = {"normal": 22, "large": 30, "huge": 40}
DEFAULT_FONT_SIZE = "large"


# ===========================================================================
# Доступность: крупный резкий шрифт + высококонтрастная тёмная тема
# ===========================================================================

class Accessibility:
    """Загружает крупный системный TTF в нескольких размерах (резкий шрифт, без
    размытия от global_font_scale) и строит высококонтрастную тёмную тему с
    увеличенными отступами/кнопками. Если TTF не найден — мягкий фолбэк."""

    # Системные шрифты по платформам (берём первый существующий).
    _FONT_CANDIDATES = [
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\tahoma.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]

    def __init__(self):
        self.fonts: dict[str, int] = {}
        self.font_path: Optional[str] = None
        self.global_theme: Optional[int] = None

    def _find_font(self) -> Optional[str]:
        import os
        for p in self._FONT_CANDIDATES:
            if os.path.exists(p):
                return p
        return None

    def setup_fonts(self):
        """Регистрирует крупные шрифты. Кириллица: подключаем диапазон глифов."""
        self.font_path = self._find_font()
        if not self.font_path:
            # Фолбэк: масштаб (чуть размыто, но крупно — лучше, чем мелко).
            dpg.set_global_font_scale(1.7)
            return
        with dpg.font_registry():
            for name, size in FONT_SIZES.items():
                # В DearPyGui 2.x диапазоны глифов подключаются автоматически —
                # ручные add_font_range/add_font_range_hint больше не нужны
                # (кириллица и латиница берутся из шрифта сами).
                self.fonts[name] = dpg.add_font(self.font_path, size)
        dpg.bind_font(self.fonts.get(DEFAULT_FONT_SIZE, list(self.fonts.values())[0]))

    def set_font_size(self, name: str):
        if name in self.fonts:
            dpg.bind_font(self.fonts[name])
        elif not self.fonts:
            # фолбэк-режим: масштабом
            dpg.set_global_font_scale({"normal": 1.4, "large": 1.7, "huge": 2.2}.get(name, 1.7))

    def setup_theme(self):
        """Высококонтрастная тёмная тема: глубокий фон, светлый текст, крупные
        отступы и заметные границы — легче фокусировать взгляд."""
        with dpg.theme() as theme:
            with dpg.theme_component(dpg.mvAll):
                # Фон — почти чёрный, текст — почти белый (макс. контраст).
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg, (18, 18, 22), category=dpg.mvThemeCat_Core)
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (28, 28, 34), category=dpg.mvThemeCat_Core)
                dpg.add_theme_color(dpg.mvThemeCol_Text, (240, 240, 245), category=dpg.mvThemeCat_Core)
                dpg.add_theme_color(dpg.mvThemeCol_Border, (90, 90, 110), category=dpg.mvThemeCat_Core)
                # Кнопки — заметные, с контрастной заливкой и явным hover.
                dpg.add_theme_color(dpg.mvThemeCol_Button, (45, 90, 150), category=dpg.mvThemeCat_Core)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (70, 130, 210), category=dpg.mvThemeCat_Core)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (90, 160, 240), category=dpg.mvThemeCat_Core)
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (40, 40, 50), category=dpg.mvThemeCat_Core)
                dpg.add_theme_color(dpg.mvThemeCol_Header, (45, 90, 150), category=dpg.mvThemeCat_Core)
                dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, (70, 130, 210), category=dpg.mvThemeCat_Core)
                dpg.add_theme_color(dpg.mvThemeCol_TabActive, (70, 130, 210), category=dpg.mvThemeCat_Core)
                dpg.add_theme_color(dpg.mvThemeCol_Tab, (40, 40, 50), category=dpg.mvThemeCat_Core)
                # Крупные отступы и скругление — больше «воздуха», легче целиться.
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 14, 10, category=dpg.mvThemeCat_Core)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 12, 12, category=dpg.mvThemeCat_Core)
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 18, 18, category=dpg.mvThemeCat_Core)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 8, category=dpg.mvThemeCat_Core)
                dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 2, category=dpg.mvThemeCat_Core)
                dpg.add_theme_style(dpg.mvStyleVar_ScrollbarSize, 24, category=dpg.mvThemeCat_Core)
        self.global_theme = theme
        dpg.bind_theme(theme)


# ===========================================================================
# Фоновые задачи: результат возвращается в UI через потокобезопасную очередь
# ===========================================================================

class BackgroundRunner:
    """Запускает функцию в отдельном потоке; результат кладёт в очередь, которую
    UI опрашивает в render loop. Так тяжёлые вызовы не морозят интерфейс."""

    def __init__(self):
        self._results: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._progress: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._busy: set[str] = set()
        self._lock = threading.Lock()

    def push_progress(self, tag: str, payload) -> None:
        """Потокобезопасно кладёт событие прогресса из ФОНОВОГО потока. Применять
        его (мутации DPG) должен ГЛАВНЫЙ поток через drain_progress в render loop —
        DearPyGui не потокобезопасен для add_*/configure_item/delete_item."""
        self._progress.put((tag, payload))

    def drain_progress(self):
        """Возвращает все накопленные события прогресса (вызывать в render loop)."""
        out = []
        while True:
            try:
                out.append(self._progress.get_nowait())
            except queue.Empty:
                break
        return out

    def run(self, tag: str, fn, *args, **kwargs) -> bool:
        """Запускает fn под именем tag. Если задача с этим tag уже идёт — отказ."""
        with self._lock:
            if tag in self._busy:
                return False
            self._busy.add(tag)

        def _worker():
            try:
                res = fn(*args, **kwargs)
                self._results.put((tag, res))
            except Exception as e:  # фоновый поток не должен ронять процесс
                self._results.put((tag, ("__error__", f"{type(e).__name__}: {e}")))
            finally:
                with self._lock:
                    self._busy.discard(tag)

        threading.Thread(target=_worker, daemon=True).start()
        return True

    def is_busy(self, tag: str) -> bool:
        with self._lock:
            return tag in self._busy

    def drain(self):
        """Возвращает все готовые результаты (вызывать в render loop)."""
        out = []
        while True:
            try:
                out.append(self._results.get_nowait())
            except queue.Empty:
                break
        return out

