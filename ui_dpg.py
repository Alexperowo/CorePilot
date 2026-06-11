#!/usr/bin/env python3
"""
ui_dpg.py — графический клиент CorePilot на DearPyGui (точка входа).

Архитектура (после дробления монолита):
  - ui_common.py — палитра статусов, звук, Accessibility, BackgroundRunner;
  - ui_tabs.py   — классы всех вкладок;
  - ui_dpg.py    — AppWindow (окно + render loop) и main().

Принципы:
  - UI импортирует ТОЛЬКО service_layer (изоляция от ядра/демона/utils);
  - тяжёлые операции — в фоновых потоках через BackgroundRunner, UI не замерзает;
  - живое обновление: раз в ~1.5 с дёргаем service_layer.get_board()/get_health()
    и точечно обновляем виджеты (дифф по состоянию — без мерцания).

Доступность (для пользователей со слабым зрением): крупный резкий шрифт (TTF,
регулируется A/A+/A++), высококонтрастная тёмная тема, статусы дублируются
текстом и символами (не только цветом), опциональные звуковые сигналы.

Запуск:  python ui_dpg.py
Зависимости: dearpygui  (pip install dearpygui)
"""
from __future__ import annotations

import time

import dearpygui.dearpygui as dpg

import service_layer as svc
from ui_common import Accessibility, BackgroundRunner, _play_sound, REFRESH_SECONDS
from ui_tabs import (HealthTab, DagTab, KanbanTab, PipelineTab, CleanerTab,
                     SettingsTab, LlamaTab, QaTab, LogTab, ControlTab)


# ===========================================================================
# Главное окно
# ===========================================================================

class AppWindow:
    def __init__(self):
        self.runner = BackgroundRunner()
        self.access = Accessibility()
        self.dag = DagTab(on_requeue=self._requeue, on_remove=self._remove)
        self.kanban = KanbanTab(on_requeue=self._requeue, on_remove=self._remove)
        self.control = ControlTab(self.runner, notify=self._notify)
        self.pipeline = PipelineTab(self.runner, notify=self._notify)
        self.cleaner = CleanerTab(self.runner, notify=self._notify)
        self.settings = SettingsTab(notify=self._notify)
        self.llama = LlamaTab(self.runner, notify=self._notify)
        self.qa = QaTab(self.runner, notify=self._notify)
        self.health = HealthTab(self.runner)
        self.log = LogTab()
        self._sound_on = True
        self._prev_counts = {}      # для звука при смене done/failed
        self._last_refresh = 0.0
        self._main_tag = "primary_window"

    def _set_font(self, size_name: str):
        self.access.set_font_size(size_name)
        self._notify(f"Размер шрифта: {size_name}")

    # ---- действия канбана (быстрые операции, можно синхронно) ----
    def _requeue(self, task_id: str):
        ok, msg = svc.requeue_task(task_id)
        self._notify(msg)

    def _remove(self, task_id: str):
        ok, msg = svc.remove_task(task_id)
        self._notify(msg)

    def _notify(self, text: str):
        dpg.set_value("status_bar", text)

    def _panic(self):
        report = svc.panic_stop()
        # Фоновый поток (Конвейер и т.п.) нельзя убить извне, но UI разблокируем,
        # чтобы спиннер не «висел» и кнопки снова работали. Сам поток завершится
        # сам по таймауту запроса (timeout у LLM).
        for spin, btn in (("pipe_spinner", "pipe_run_btn"),
                          ("cleaner_spinner", None), ("qa_spinner", None)):
            try:
                dpg.configure_item(spin, show=False)
            except Exception:
                pass
            if btn:
                try: dpg.configure_item(btn, enabled=True)
                except Exception: pass
        try:
            dpg.set_value("pipe_verdict", "Остановлено пользователем (СТОП ВСЁ). "
                          "Фоновый запрос завершится по таймауту.")
        except Exception:
            pass
        self._notify("ПАНИКА: " + "; ".join(f"{k}: {v}" for k, v in report.items()))
        _play_sound("fail") if self._sound_on else None

    def _toggle_sound(self):
        self._sound_on = not self._sound_on
        self._notify(f"Звук: {'вкл' if self._sound_on else 'выкл'}")

    def build(self):
        with dpg.window(tag=self._main_tag):
            with dpg.group(horizontal=True):
                dpg.add_text("CorePilot — Фабрика ИИ-агентов", color=(120, 200, 255))
                dpg.add_spacer(width=20)
                dpg.add_text("Шрифт:", color=(180, 180, 180))
                b1 = dpg.add_button(label="A", callback=lambda: self._set_font("normal"))
                b2 = dpg.add_button(label="A+", callback=lambda: self._set_font("large"))
                b3 = dpg.add_button(label="A++", callback=lambda: self._set_font("huge"))
                dpg.add_spacer(width=20)
                bsnd = dpg.add_button(label="[))] Звук", callback=self._toggle_sound)
                dpg.add_spacer(width=20)
                bpanic = dpg.add_button(label="[X] СТОП ВСЁ", callback=self._panic)
            # Тултипы (idea 8) — крупные подсказки при наведении.
            for tgt, txt in ((b1, "Обычный шрифт (22px)"), (b2, "Крупный шрифт (30px)"),
                             (b3, "Очень крупный (40px)"),
                             (bsnd, "Вкл/выкл звуковые сигналы при завершении задач"),
                             (bpanic, "Аварийно остановить демон и llama-сервер")):
                with dpg.tooltip(tgt):
                    dpg.add_text(txt)
            dpg.add_separator()
            with dpg.tab_bar():
                self.health.build()
                self.dag.build()
                self.kanban.build()
                self.pipeline.build()
                self.cleaner.build()
                self.llama.build()
                self.qa.build()
                self.log.build()
                self.control.build()
                self.settings.build()
            dpg.add_separator()
            dpg.add_text("готов", tag="status_bar", color=(150, 200, 150))

        # Горячие клавиши (idea 8): F5 — обновить квоты, Esc-Esc не вешаем во избежание
        # случайного выхода; Ctrl+стоп через кнопку.
        with dpg.handler_registry():
            dpg.add_key_press_handler(dpg.mvKey_F5, callback=lambda: self.health._load_quotas())

    # ---- цикл обновления ----
    def _process_background(self):
        # События прогресса (из фоновых потоков) применяем ЗДЕСЬ — в главном потоке,
        # т.к. DearPyGui не потокобезопасен для мутаций графа.
        for tag, payload in self.runner.drain_progress():
            if tag == "pipeline":
                try:
                    self.pipeline.apply_progress(payload)
                except Exception:
                    pass
        for tag, result in self.runner.drain():
            if tag == "gen_backlog":
                self.control.on_backlog_ready(result)
            elif tag == "quotas":
                self.control.on_quotas_ready(result)
            elif tag == "pipeline":
                self.pipeline.on_result(result)
            elif tag == "cleaner_scan":
                self.cleaner.on_scan_result(result)
            elif tag in ("llama_start", "llama_stop"):
                self.llama.on_result(result)
            elif tag == "qa":
                self.qa.on_result(result)
            elif tag == "health_quotas":
                self.health.on_quotas(result)
            elif tag in ("daemon_start", "daemon_stop"):
                if isinstance(result, tuple) and len(result) == 2 and result[0] != "__error__":
                    self._notify(result[1])
                elif isinstance(result, tuple) and result and result[0] == "__error__":
                    self._notify(f"Ошибка: {result[1]}")

    def _tick(self):
        # фоновые результаты — каждый кадр (дёшево)
        self._process_background()
        # выбор узла в графе — каждый кадр (дёшево, читает get_selected_nodes)
        try:
            self.dag.poll_selection()
        except Exception:
            pass
        # снимок доски — раз в REFRESH_SECONDS
        now = time.time()
        if now - self._last_refresh >= REFRESH_SECONDS:
            self._last_refresh = now
            try:
                board = svc.get_board()
                health = svc.get_health()
            except Exception as e:
                self._notify(f"Ошибка чтения очереди: {e}")
                return
            self.dag.update(board)
            self.kanban.update(board)
            self.control.update(board)
            self.health.update(health)
            self.log.update()
            # Звук при изменении числа завершённых/проваленных задач (idea 7).
            if self._sound_on and self._prev_counts:
                if board.counts.get("done", 0) > self._prev_counts.get("done", 0):
                    _play_sound("done")
                if board.counts.get("failed", 0) > self._prev_counts.get("failed", 0):
                    _play_sound("fail")
            self._prev_counts = dict(board.counts)

    def run(self):
        dpg.create_context()
        # Доступность: крупный резкий шрифт + высококонтрастная тёмная тема.
        self.access.setup_fonts()
        self.access.setup_theme()
        self.build()
        dpg.create_viewport(title="CorePilot", width=1400, height=900)
        dpg.setup_dearpygui()
        dpg.set_primary_window(self._main_tag, True)
        dpg.show_viewport()
        # стартовая подгрузка квот в фоне
        self.runner.run("quotas", svc.get_quotas)
        # ручной render loop ради живого обновления без блокировки
        while dpg.is_dearpygui_running():
            self._tick()
            dpg.render_dearpygui_frame()
        dpg.destroy_context()


def main():
    AppWindow().run()


if __name__ == "__main__":
    main()
