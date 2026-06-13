#!/usr/bin/env python3
"""
ui_tabs.py — классы вкладок GUI CorePilot (DearPyGui).

Каждая вкладка — самостоятельный класс (HealthTab, DagTab, KanbanTab, PipelineTab,
CleanerTab, SettingsTab, LlamaTab, QaTab, LogTab). AppWindow (в ui_dpg.py) создаёт
их и связывает в общий цикл.

Изоляция: импортирует только service_layer (через svc), dearpygui и общую основу
ui_common. Прямых обращений к ядру/демону/utils нет.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

import dearpygui.dearpygui as dpg

import service_layer as svc
from ui_common import (STATUS_COLORS, STATUS_LABEL, STATUS_SYMBOL,
                       BackgroundRunner, _play_sound)

# ===========================================================================
# Вкладка: Граф DAG (Node Editor)
# ===========================================================================

class DagTab:
    """Рисует задачи узлами Node Editor, связи — по depends_on, цвет — по статусу.
    Перестраивается только при изменении состава/статусов задач (без мерцания)."""

    def __init__(self, on_requeue=None, on_remove=None):
        self.editor_tag = "dag_node_editor"
        self.empty_tag = "dag_empty_text"
        self._on_requeue = on_requeue
        self._on_remove = on_remove
        self._shown_tid = None
        # node-темы по статусу (создаются один раз)
        self._themes: dict[str, int] = {}
        # текущее отрисованное состояние для диффа
        self._signature: Optional[tuple] = None
        # карта task_id -> (node_tag, in_attr_tag, out_attr_tag)
        self._nodes: dict[str, tuple[int, int, int]] = {}
        # последний набор задач (для повторного применения авто-раскладки по кнопке)
        self._last_tasks: list = []

    def _ensure_themes(self):
        if self._themes:
            return
        for status, color in STATUS_COLORS.items():
            with dpg.theme() as th:
                with dpg.theme_component(dpg.mvAll):
                    dpg.add_theme_color(dpg.mvNodeCol_TitleBar, color,
                                        category=dpg.mvThemeCat_Nodes)
                    # подсветка выбранного/наведённого — слегка светлее
                    bright = tuple(min(255, c + 30) for c in color)
                    dpg.add_theme_color(dpg.mvNodeCol_TitleBarHovered, bright,
                                        category=dpg.mvThemeCat_Nodes)
                    dpg.add_theme_color(dpg.mvNodeCol_TitleBarSelected, bright,
                                        category=dpg.mvThemeCat_Nodes)
            self._themes[status] = th

    def build(self):
        with dpg.tab(label="Граф DAG"):
            with dpg.group(horizontal=True):
                dpg.add_button(label="[=] Авто-раскладка", callback=lambda: self.apply_layout())
                dpg.add_text("  Узлы — задачи, связи — зависимости. Цвет = статус. "
                             "Кликните узел — детали справа.", color=(170, 170, 170))
            with dpg.group(horizontal=True):
                for status in STATUS_COLORS:
                    dpg.add_text(f"{STATUS_SYMBOL[status]} {STATUS_LABEL[status]}",
                                 color=STATUS_COLORS[status])
                    dpg.add_spacer(width=8)
            dpg.add_separator()
            dpg.add_text("Очередь пуста — поставьте цель во вкладке «Управление».",
                         tag=self.empty_tag, show=True, color=(150, 150, 150))
            # Граф + боковая панель деталей
            with dpg.group(horizontal=True):
                dpg.add_node_editor(tag=self.editor_tag, minimap=True,
                                    minimap_location=dpg.mvNodeMiniMap_Location_BottomRight,
                                    width=-360)
                with dpg.child_window(width=350, border=True):
                    dpg.add_text("Детали задачи", color=(120, 200, 255))
                    dpg.add_separator()
                    dpg.add_text("Кликните узел в графе.", tag="dag_detail_text",
                                 color=(180, 180, 185), wrap=320)
                    dpg.add_spacer(height=8)
                    with dpg.group(tag="dag_detail_actions", show=False):
                        dpg.add_button(label="[<] Повторить задачу", tag="dag_detail_requeue",
                                       callback=self._requeue_selected, width=-1)
                        dpg.add_button(label="[x] Удалить задачу", tag="dag_detail_remove",
                                       callback=self._remove_selected, width=-1)
        self._ensure_themes()

    def _node_to_task(self) -> Optional[object]:
        """Возвращает TaskView выбранного узла (через get_selected_nodes)."""
        try:
            sel = dpg.get_selected_nodes(self.editor_tag)
        except Exception:
            sel = []
        if not sel:
            return None
        node_id = sel[0]
        for tid, (node, _i, _o) in self._nodes.items():
            if node == node_id:
                for t in self._last_tasks:
                    if t.task_id == tid:
                        return t
        return None

    def poll_selection(self):
        """Обновляет панель деталей по выбранному узлу (зовётся в общем тике)."""
        t = self._node_to_task()
        if not t:
            return
        if getattr(self, "_shown_tid", None) == t.task_id:
            return
        self._shown_tid = t.task_id
        txt = (f"[{t.task_id}] {t.title}\n\n"
               f"Статус: {STATUS_SYMBOL.get(t.status,'')} {STATUS_LABEL.get(t.status,t.status)}\n"
               f"Зависит от: {', '.join(t.depends_on) if t.depends_on else '—'}\n"
               f"Файлы: {', '.join(t.target_files) if t.target_files else '—'}\n")
        if t.description:
            txt += f"\n{t.description[:300]}"
        if t.failed_reason:
            txt += f"\n\n[!] {t.failed_reason[:200]}"
        dpg.set_value("dag_detail_text", txt)
        dpg.configure_item("dag_detail_actions", show=True)

    def _requeue_selected(self):
        if getattr(self, "_shown_tid", None) and self._on_requeue:
            self._on_requeue(self._shown_tid)

    def _remove_selected(self):
        if getattr(self, "_shown_tid", None) and self._on_remove:
            self._on_remove(self._shown_tid)

    def _signature_of(self, tasks) -> tuple:
        # Пересобираем граф только если изменился набор задач/статусов/связей.
        return tuple(sorted(
            (t.task_id, t.status, tuple(t.depends_on)) for t in tasks
        ))

    def _compute_layout(self, tasks) -> dict[str, tuple[int, int]]:
        """Топологическая раскладка: уровень узла = самый длинный путь от корня
        (longest path). Узлы без родителей — уровень 0 (слева). Узел с несколькими
        родителями встаёт правее самого глубокого из них, чтобы стрелки шли только
        слева направо и не было визуальных пересечений «назад».

        По X: level * COL_STEP + X0. По Y: позиция внутри уровня * ROW_STEP + Y0.
        Циклов в данных нет (их режет parse_backlog), но на всякий случай защищаемся
        от зацикливания счётчиком итераций."""
        X0, Y0 = 50, 40
        COL_STEP, ROW_STEP = 400, 130   # шаг между уровнями (X) и строками (Y)

        ids = {t.task_id for t in tasks}
        # depends_on, очищенные от ссылок на отсутствующие задачи.
        deps = {t.task_id: [d for d in t.depends_on if d in ids] for t in tasks}

        # Уровень = max(уровень родителя)+1; корни = 0. Итеративно до стабилизации.
        level: dict[str, int] = {tid: 0 for tid in ids}
        for _ in range(len(ids) + 1):  # не более N проходов — гарантия от зацикливания
            changed = False
            for tid in ids:
                if deps[tid]:
                    want = max(level[d] for d in deps[tid]) + 1
                    if want > level[tid]:
                        level[tid] = want
                        changed = True
            if not changed:
                break

        # Группируем по уровням, внутри уровня сортируем по task_id (стабильный порядок).
        by_level: dict[int, list[str]] = {}
        for tid in sorted(ids):
            by_level.setdefault(level[tid], []).append(tid)

        pos: dict[str, tuple[int, int]] = {}
        for lvl, members in by_level.items():
            for row, tid in enumerate(members):
                pos[tid] = (X0 + lvl * COL_STEP, Y0 + row * ROW_STEP)
        return pos

    def apply_layout(self):
        """Применяет topo-координаты к существующим узлам (кнопка «Авто-раскладка»)."""
        if not self._last_tasks:
            return
        pos = self._compute_layout(self._last_tasks)
        for tid, (node, _in, _out) in self._nodes.items():
            if tid in pos:
                dpg.set_item_pos(node, pos[tid])

    def update(self, board: svc.BoardSnapshot):
        sig = self._signature_of(board.tasks)
        if sig == self._signature:
            return  # ничего не поменялось — не трогаем граф (нет мерцания)
        self._signature = sig
        self._last_tasks = list(board.tasks)

        # Полная перестройка содержимого редактора (структура графа изменилась).
        dpg.delete_item(self.editor_tag, children_only=True)
        self._nodes.clear()
        dpg.configure_item(self.empty_tag, show=not board.tasks)

        # Считаем topo-координаты ДО создания узлов — ставим сразу на места.
        pos = self._compute_layout(board.tasks)

        # 1) узлы + атрибуты (вход/выход для связей)
        for t in board.tasks:
            nx, ny = pos.get(t.task_id, (50, 40))
            node = dpg.add_node(label=f"{t.task_id}: {t.title[:36]}",
                                parent=self.editor_tag, pos=(nx, ny))
            in_attr = dpg.add_node_attribute(parent=node,
                                             attribute_type=dpg.mvNode_Attr_Input)
            dpg.add_text(f"{STATUS_SYMBOL.get(t.status,'')} {STATUS_LABEL.get(t.status, t.status)}",
                         parent=in_attr, color=STATUS_COLORS.get(t.status, (240, 240, 245)))
            out_attr = dpg.add_node_attribute(parent=node,
                                              attribute_type=dpg.mvNode_Attr_Output)
            if t.target_files:
                dpg.add_text("→ " + ", ".join(t.target_files[:2])[:30], parent=out_attr)
            else:
                dpg.add_text("→", parent=out_attr)
            if t.failed_reason:
                fa = dpg.add_node_attribute(parent=node,
                                            attribute_type=dpg.mvNode_Attr_Static)
                dpg.add_text(t.failed_reason[:40], parent=fa, color=(230, 140, 140), wrap=180)
            dpg.bind_item_theme(node, self._themes.get(t.status, 0))
            self._nodes[t.task_id] = (node, in_attr, out_attr)

        # 2) связи: ребро parent.out -> child.in для каждой depends_on
        for t in board.tasks:
            child = self._nodes.get(t.task_id)
            if not child:
                continue
            for dep in t.depends_on:
                parent = self._nodes.get(dep)
                if parent:
                    dpg.add_node_link(parent[2], child[1], parent=self.editor_tag)


# ===========================================================================
# Вкладка: Kanban (колонки по статусам)
# ===========================================================================

class KanbanTab:
    """Простые колонки задач по статусам. Содержимое колонок обновляется точечно."""

    COLUMNS = [svc.STATUS_PENDING, svc.STATUS_PROCESSING, svc.STATUS_FROZEN,
               svc.STATUS_DONE, svc.STATUS_FAILED]

    def __init__(self, on_requeue, on_remove):
        self._on_requeue = on_requeue
        self._on_remove = on_remove
        self._col_tags: dict[str, str] = {}
        self._header_tags: dict[str, str] = {}
        self._signature: Optional[tuple] = None

    def build(self):
        with dpg.tab(label="Доска задач"):
            with dpg.group(horizontal=True):
                for status in self.COLUMNS:
                    with dpg.child_window(width=320, autosize_y=True, border=True):
                        htag = f"kanban_hdr_{status}"
                        self._header_tags[status] = htag
                        dpg.add_text(f"{STATUS_SYMBOL[status]} {STATUS_LABEL[status]} (0)", tag=htag,
                                     color=STATUS_COLORS[status])
                        dpg.add_separator()
                        ctag = f"kanban_col_{status}"
                        self._col_tags[status] = ctag
                        dpg.add_group(tag=ctag)

    def _signature_of(self, tasks) -> tuple:
        return tuple(sorted((t.task_id, t.status, bool(t.failed_reason)) for t in tasks))

    def update(self, board: svc.BoardSnapshot):
        sig = self._signature_of(board.tasks)
        if sig == self._signature:
            return
        self._signature = sig

        grouped: dict[str, list] = {s: [] for s in self.COLUMNS}
        for t in board.tasks:
            grouped.setdefault(t.status, []).append(t)

        for status in self.COLUMNS:
            col = self._col_tags[status]
            dpg.delete_item(col, children_only=True)
            items = grouped.get(status, [])
            dpg.configure_item(self._header_tags[status],
                               default_value=f"{STATUS_SYMBOL[status]} {STATUS_LABEL[status]} ({len(items)})")
            for t in items:
                with dpg.child_window(parent=col, height=-1, autosize_y=True,
                                      border=True, no_scrollbar=True):
                    dpg.add_text(f"[{t.task_id}] {t.title[:28]}", wrap=210)
                    if t.depends_on:
                        dpg.add_text("⤷ deps: " + ", ".join(t.depends_on[:4]),
                                     color=(150, 150, 150), wrap=210)
                    if t.failed_reason:
                        dpg.add_text(t.failed_reason[:60], color=(220, 130, 130), wrap=210)
                    with dpg.group(horizontal=True):
                        if status in (svc.STATUS_FAILED, svc.STATUS_DONE, svc.STATUS_FROZEN):
                            dpg.add_button(label="[<] Повторить",
                                           user_data=t.task_id, callback=lambda s, a, u: self._on_requeue(u))
                        dpg.add_button(label="[x]",
                                       user_data=t.task_id, callback=lambda s, a, u: self._on_remove(u))


# ===========================================================================
# Вкладка: Управление / Дашборд
# ===========================================================================

class ControlTab:
    """Управление демоном, генерация бэклога из цели, живые квоты."""

    def __init__(self, runner: BackgroundRunner, notify):
        self._runner = runner
        self._notify = notify
        self._pending_backlog: Optional[list[dict]] = None

    def build(self):
        with dpg.tab(label="Управление"):
            # --- Демон ---
            with dpg.collapsing_header(label="Демон", default_open=True):
                with dpg.group(horizontal=True):
                    dpg.add_button(label="[>] Запустить", callback=self._start_daemon,
                                   tag="btn_start_daemon")
                    dpg.add_button(label="[X] Остановить", callback=self._stop_daemon,
                                   tag="btn_stop_daemon")
                    dpg.add_text("статус: —", tag="daemon_status_text")

            # --- Генерация бэклога ---
            with dpg.collapsing_header(label="Постановка цели (DAG-бэклог)", default_open=True):
                dpg.add_input_text(tag="goal_input", multiline=True, width=-1, height=80,
                                   hint="Опишите глобальную цель проекта…")
                with dpg.group(horizontal=True):
                    dpg.add_button(label="Вставить из буфера",
                                   callback=lambda: self._paste("goal_input"))
                    dpg.add_button(label="Очистить",
                                   callback=lambda: dpg.set_value("goal_input", ""))
                with dpg.group(horizontal=True):
                    dpg.add_button(label="[*] Сгенерировать бэклог", callback=self._gen_backlog,
                                   tag="btn_gen")
                    dpg.add_loading_indicator(tag="gen_spinner", show=False, radius=2)
                    dpg.add_text("", tag="gen_status", color=(180, 180, 180))
                dpg.add_separator()
                dpg.add_text("Предпросмотр бэклога:", color=(170, 170, 170))
                dpg.add_group(tag="backlog_preview")
                dpg.add_button(label="[v] Поставить в очередь", callback=self._enqueue_backlog,
                               tag="btn_enqueue", show=False)

            # --- Квоты ---
            with dpg.collapsing_header(label="Квоты провайдеров", default_open=True):
                with dpg.group(horizontal=True):
                    dpg.add_button(label="[O] Обновить квоты", callback=self._refresh_quotas,
                                   tag="btn_quotas")
                    dpg.add_loading_indicator(tag="quota_spinner", show=False, radius=2)
                dpg.add_group(tag="quota_list")

            # --- Обслуживание очереди ---
            with dpg.collapsing_header(label="Очередь", default_open=False):
                dpg.add_button(label="Очистить архив (done/failed)",
                               callback=self._clear_finished)

    # ---- Демон ----
    def _start_daemon(self):
        # Запуск процесса быстрый, но всё равно уводим в поток ради единообразия.
        self._runner.run("daemon_start", svc.start_daemon)

    def _stop_daemon(self):
        self._runner.run("daemon_stop", svc.stop_daemon)

    # ---- Генерация бэклога (тяжёлая операция -> поток) ----
    def _paste(self, tag: str):
        """Вставляет текст из буфера обмена в поле (для тех, кто не знает Ctrl+V)."""
        try:
            txt = dpg.get_clipboard_text() or ""
            cur = dpg.get_value(tag) or ""
            dpg.set_value(tag, (cur + txt) if cur else txt)
        except Exception as e:
            self._notify(f"Не удалось вставить: {e}")

    def _gen_backlog(self):
        goal = dpg.get_value("goal_input").strip()
        if not goal:
            self._notify("Введите цель.")
            return
        if self._runner.is_busy("gen_backlog"):
            return
        dpg.configure_item("gen_spinner", show=True)
        dpg.configure_item("btn_gen", enabled=False)
        dpg.set_value("gen_status", "Генерация… (Product Owner → Scrum Master)")
        self._runner.run("gen_backlog", svc.generate_backlog, goal)

    def on_backlog_ready(self, result):
        dpg.configure_item("gen_spinner", show=False)
        dpg.configure_item("btn_gen", enabled=True)
        if isinstance(result, tuple) and result and result[0] == "__error__":
            dpg.set_value("gen_status", f"Ошибка: {result[1]}")
            return
        backlog, msg = result
        dpg.set_value("gen_status", msg)
        dpg.delete_item("backlog_preview", children_only=True)
        if not backlog:
            dpg.configure_item("btn_enqueue", show=False)
            self._pending_backlog = None
            return
        self._pending_backlog = backlog
        for t in backlog:
            deps = t.get("depends_on", [])
            line = f"• [{t.get('task_id')}] {t.get('title', '')[:50]}"
            if deps:
                line += f"   ⤷ deps: {', '.join(str(d) for d in deps)}"
            dpg.add_text(line, parent="backlog_preview", wrap=700)
        dpg.configure_item("btn_enqueue", show=True)

    def _enqueue_backlog(self):
        if not self._pending_backlog:
            return
        ok, skip = svc.enqueue_backlog(self._pending_backlog)
        self._notify(f"Поставлено задач: {ok} (пропущено: {skip}).")
        dpg.delete_item("backlog_preview", children_only=True)
        dpg.configure_item("btn_enqueue", show=False)
        self._pending_backlog = None

    # ---- Квоты (сеть -> поток) ----
    def _refresh_quotas(self):
        if self._runner.is_busy("quotas"):
            return
        dpg.configure_item("quota_spinner", show=True)
        self._runner.run("quotas", svc.get_quotas)

    def on_quotas_ready(self, result):
        dpg.configure_item("quota_spinner", show=False)
        dpg.delete_item("quota_list", children_only=True)
        if isinstance(result, tuple) and result and result[0] == "__error__":
            dpg.add_text(f"Ошибка: {result[1]}", parent="quota_list", color=(220, 130, 130))
            return
        if not result:
            dpg.add_text("Нет настроенных ключей.", parent="quota_list", color=(150, 150, 150))
            return
        for q in result:
            dpg.add_text(self._fmt_quota(q), parent="quota_list", wrap=700)

    @staticmethod
    def _fmt_quota(q: dict) -> str:
        p = q.get("provider", "?"); status = q.get("status")
        keys = q.get("keys", 0)
        if status == "no_key": return f"• {p}: ключ не задан"
        if status == "unreachable": return f"• {p}: API недоступен"
        if status == "unknown": return f"• {p}: лимиты появятся после первого запроса"
        if status == "error": return f"• {p}: ошибка ({q.get('detail', '')[:60]})"
        if q.get("source") == "api":
            parts = []
            if q.get("is_free_tier") is not None:
                parts.append("free" if q["is_free_tier"] else "платный")
            if q.get("remaining") is not None:
                parts.append(f"остаток: {q['remaining']}")
            return f"• {p} ({keys} ключ.): " + ", ".join(parts) if parts else f"• {p}: ok"
        parts = []
        if "remaining_requests" in q: parts.append(f"запросов: {q['remaining_requests']}")
        if "remaining_tokens" in q: parts.append(f"токенов: {q['remaining_tokens']}")
        return f"• {p} ({keys} ключ.): " + (", ".join(parts) if parts else "данные получены")

    def _clear_finished(self):
        n = svc.clear_finished()
        self._notify(f"Удалено архивных файлов: {n}.")

    # ---- Живое обновление статуса демона ----
    def update(self, board: svc.BoardSnapshot):
        if board.daemon_running:
            dpg.set_value("daemon_status_text", f"статус: [ON] работает (PID {board.daemon_pid})")
        else:
            dpg.set_value("daemon_status_text", "статус: [--] остановлен")


# ===========================================================================
# Вкладка: Конвейер (интерактивная правка кода)
# ===========================================================================

class PipelineTab:
    """Запрос → живой прогресс этапов → diff → применить/отклонить.
    Тяжёлый прогон — в фоне; UI показывает, на каком этапе агенты сейчас."""

    STAGES = [("gather", "[1] Сбор контекста"), ("architect", "[2] План"),
              ("fix", "[3] Код"), ("audit", "[4] Аудит")]

    def __init__(self, runner: BackgroundRunner, notify):
        self._runner = runner
        self._notify = notify
        self._result: Optional[svc.PipelineResult] = None

    def build(self):
        with dpg.tab(label="Конвейер"):
            dpg.add_text("Опишите задачу по коду — команда агентов выполнит её "
                         "и покажет изменения до применения.", color=(170, 170, 175), wrap=900)
            dpg.add_input_text(tag="pipe_request", multiline=True, width=-1, height=90,
                               hint="Например: добавь обработку ошибок в загрузку файла в utils.py")
            with dpg.group(horizontal=True):
                dpg.add_button(label="[>] Запустить конвейер", callback=self._run, tag="pipe_run_btn")
                dpg.add_button(label="Вставить из буфера",
                               callback=lambda: self._paste("pipe_request"))
                dpg.add_button(label="Очистить",
                               callback=lambda: dpg.set_value("pipe_request", ""))
                dpg.add_loading_indicator(tag="pipe_spinner", show=False, radius=2)
            dpg.add_separator()
            # Живой статус этапов (чек-лист)
            dpg.add_text("Этапы:", color=(170, 170, 175))
            for sid, label in self.STAGES:
                dpg.add_text(f"   ○ {label}", tag=f"pipe_stage_{sid}", color=(120, 120, 130))
            dpg.add_separator()
            dpg.add_text("", tag="pipe_verdict", color=(240, 240, 245), wrap=900)
            # Просмотр diff
            dpg.add_text("Изменения:", color=(170, 170, 175))
            dpg.add_child_window(tag="pipe_diff_area", height=-50, border=True)
            with dpg.group(horizontal=True):
                dpg.add_button(label="[v] Применить изменения", callback=self._apply,
                               tag="pipe_apply_btn", show=False)
                dpg.add_button(label="[x] Отклонить", callback=self._reject,
                               tag="pipe_reject_btn", show=False)

    def _reset_stages(self):
        for sid, label in self.STAGES:
            dpg.configure_item(f"pipe_stage_{sid}", default_value=f"   ○ {label}",
                               color=(120, 120, 130))

    def _paste(self, tag: str):
        try:
            txt = dpg.get_clipboard_text() or ""
            cur = dpg.get_value(tag) or ""
            dpg.set_value(tag, (cur + txt) if cur else txt)
        except Exception as e:
            self._notify(f"Не удалось вставить: {e}")

    def _run(self):
        req = dpg.get_value("pipe_request").strip()
        if not req:
            self._notify("Введите задачу для конвейера.")
            return
        if self._runner.is_busy("pipeline"):
            return
        self._reset_stages()
        dpg.set_value("pipe_verdict", "")
        dpg.delete_item("pipe_diff_area", children_only=True)
        dpg.configure_item("pipe_apply_btn", show=False)
        dpg.configure_item("pipe_reject_btn", show=False)
        dpg.configure_item("pipe_spinner", show=True)
        dpg.configure_item("pipe_run_btn", enabled=False)

        # progress-колбэк зовётся из ФОНОВОГО потока. Прямые вызовы DPG
        # (configure_item) оттуда НЕ потокобезопасны (риск SegFault). Поэтому только
        # кладём событие в очередь; применит его главный поток в _tick -> apply_progress.
        def _progress(stage, text):
            self._runner.push_progress("pipeline", (stage, text))

        self._runner.run("pipeline", svc.run_pipeline, req, _progress)

    def apply_progress(self, payload):
        """Вызывается ГЛАВНЫМ потоком (render loop) для безопасного обновления UI."""
        try:
            stage, text = payload
        except Exception:
            return
        for sid, label in self.STAGES:
            if sid == stage:
                try:
                    dpg.configure_item(f"pipe_stage_{sid}",
                                       default_value=f"   ⏳ {label} — {text}",
                                       color=(255, 200, 50))
                except Exception:
                    pass

    def on_result(self, result):
        dpg.configure_item("pipe_spinner", show=False)
        dpg.configure_item("pipe_run_btn", enabled=True)
        if isinstance(result, tuple) and result and result[0] == "__error__":
            dpg.set_value("pipe_verdict", f"Ошибка: {result[1]}")
            return
        self._result = result
        # отметить все этапы выполненными
        for sid, label in self.STAGES:
            dpg.configure_item(f"pipe_stage_{sid}", default_value=f"   [v] {label}",
                               color=(80, 220, 100))
        if result.error:
            dpg.set_value("pipe_verdict", f"[x] {result.error}")
            return
        ok_color = (80, 220, 100) if result.ok else (255, 200, 50)
        dpg.set_value("pipe_verdict", f"Вердикт: {result.verdict}\n{result.summary}")
        dpg.configure_item("pipe_verdict", color=ok_color)
        # diff'ы
        dpg.delete_item("pipe_diff_area", children_only=True)
        if not result.diffs and not result.patches:
            dpg.add_text("Изменений нет.", parent="pipe_diff_area", color=(170, 170, 175))
            return
        for fp, diff in (result.diffs or [(fp, "(новый файл)") for fp, _ in result.patches]):
            dpg.add_text(f"[f] {fp}", parent="pipe_diff_area", color=(120, 200, 255))
            for line in diff.splitlines()[:200]:
                col = (240, 240, 245)
                if line.startswith("+") and not line.startswith("+++"): col = (80, 220, 100)
                elif line.startswith("-") and not line.startswith("---"): col = (255, 110, 110)
                elif line.startswith("@@"): col = (255, 200, 50)
                dpg.add_text(line[:300], parent="pipe_diff_area", color=col)
            dpg.add_separator(parent="pipe_diff_area")
        dpg.configure_item("pipe_apply_btn", show=True)
        dpg.configure_item("pipe_reject_btn", show=True)

    def _apply(self):
        if not self._result or not self._result.patches:
            return
        ok, msg = svc.apply_pipeline_patches(self._result.patches)
        self._notify(msg)
        dpg.configure_item("pipe_apply_btn", show=False)
        dpg.configure_item("pipe_reject_btn", show=False)
        dpg.set_value("pipe_verdict", dpg.get_value("pipe_verdict") + f"\n→ {msg}")

    def _reject(self):
        self._result = None
        dpg.delete_item("pipe_diff_area", children_only=True)
        dpg.configure_item("pipe_apply_btn", show=False)
        dpg.configure_item("pipe_reject_btn", show=False)
        self._notify("Изменения отклонены.")


# ===========================================================================
# Вкладка: AI Cleaner (очистка диска с карантином)
# ===========================================================================

class CleanerTab:
    """Сканеры (детерминированные) → таблица результатов с чекбоксами →
    карантин (обратимый) / восстановление / удаление навсегда."""

    SCANS = [("disk", "Глубокий кэш"), ("downloads", "Загрузки"),
             ("dups", "Дубликаты"), ("startup", "Автозапуск")]
    RISK_COLOR = {"safe": (80, 220, 100), "warn": (255, 200, 50), "danger": (255, 80, 80)}

    def __init__(self, runner: BackgroundRunner, notify):
        self._runner = runner
        self._notify = notify
        self._items: list[svc.CleanerItem] = []
        self._checks: dict[int, str] = {}   # row tag -> path

    def build(self):
        with dpg.tab(label="Cleaner"):
            dpg.add_text("Анализ диска. Удаляемое сначала уходит в КАРАНТИН — "
                         "можно восстановить. Системные папки защищены.",
                         color=(170, 170, 175), wrap=900)
            with dpg.group(horizontal=True):
                dpg.add_text("Путь:", color=(120, 200, 255))
                dpg.add_input_text(tag="cleaner_root", width=-260,
                                   hint="Пусто = домашняя папка пользователя")
                dpg.add_text("От (МБ):", color=(120, 200, 255))
                # step=0 убирает кнопки [-][+], которые ломают вёрстку при крупном шрифте.
                dpg.add_input_float(tag="cleaner_minmb", default_value=10.0, width=100,
                                    min_value=0.0, step=0, format="%.0f")
            dpg.add_text("Выберите, что искать:", color=(170, 170, 175))
            with dpg.group(horizontal=True):
                for kind, label in self.SCANS:
                    dpg.add_button(label=label, height=35, user_data=kind,
                                   callback=lambda s, a, u: self._scan(u))
                dpg.add_loading_indicator(tag="cleaner_spinner", show=False, radius=2)
            dpg.add_text("", tag="cleaner_status", color=(180, 180, 185))
            dpg.add_separator()
            with dpg.group(horizontal=True):
                dpg.add_button(label="[v] Выбрать всё", callback=lambda: self._select_all(True))
                dpg.add_button(label="[x] Снять всё", callback=lambda: self._select_all(False))
                dpg.add_button(label="[Q] В карантин выбранное", callback=self._quarantine,
                               tag="cleaner_quar_btn")
            dpg.add_child_window(tag="cleaner_results", height=-220, border=True)
            dpg.add_separator()
            # Карантин
            with dpg.group(horizontal=True):
                dpg.add_text("Карантин:", color=(170, 170, 175))
                dpg.add_button(label="[O] Обновить сессии", callback=self._refresh_sessions)
            dpg.add_child_window(tag="cleaner_sessions", height=-1, border=True)

    def _scan(self, kind: str):
        if not kind:
            dpg.set_value("cleaner_status", "Выберите тип сканирования кнопкой выше.")
            return
        if self._runner.is_busy("cleaner_scan"):
            return
        root = dpg.get_value("cleaner_root").strip()
        minmb = float(dpg.get_value("cleaner_minmb") or 10)
        dpg.configure_item("cleaner_spinner", show=True)
        dpg.set_value("cleaner_status", f"Сканирование ({kind})…")
        self._runner.run("cleaner_scan", svc.cleaner_scan, kind, root, minmb)

    def on_scan_result(self, result):
        dpg.configure_item("cleaner_spinner", show=False)
        if isinstance(result, tuple) and result and result[0] == "__error__":
            dpg.set_value("cleaner_status", f"Ошибка: {result[1]}")
            return
        items, msg = result
        self._items = items
        dpg.set_value("cleaner_status", msg)
        dpg.delete_item("cleaner_results", children_only=True)
        self._checks.clear()
        if not items:
            dpg.add_text("Ничего не найдено.", parent="cleaner_results", color=(170, 170, 175))
            return
        for i, it in enumerate(items[:300]):
            with dpg.group(horizontal=True, parent="cleaner_results"):
                # tag не задаём — берём возвращённый id, чтобы пересканирование
                # не конфликтовало с существующими alias'ами DPG.
                chk = dpg.add_checkbox(default_value=(it.risk == "safe"))
                self._checks[chk] = it.path
                dpg.add_text(f"{it.size_mb:>8.1f} МБ", color=(180, 180, 200))
                dpg.add_text(f"[{it.risk.upper()}]", color=self.RISK_COLOR.get(it.risk, (200, 200, 200)))
                dpg.add_text(it.path[:90], wrap=700)

    def _select_all(self, value: bool):
        for tag in self._checks:
            dpg.set_value(tag, value)

    def _quarantine(self):
        paths = [self._checks[t] for t in self._checks if dpg.get_value(t)]
        if not paths:
            self._notify("Ничего не выбрано.")
            return
        ok, msg = svc.cleaner_quarantine(paths, same_drive=True)
        self._notify(msg)
        self._refresh_sessions()
        # убрать обработанные из таблицы
        self._scan_clear_selected(paths)

    def _scan_clear_selected(self, paths):
        remaining = [it for it in self._items if it.path not in set(paths)]
        self.on_scan_result((remaining, f"Осталось объектов: {len(remaining)}."))

    def _refresh_sessions(self):
        sessions = svc.cleaner_sessions()
        dpg.delete_item("cleaner_sessions", children_only=True)
        if not sessions:
            dpg.add_text("Карантин пуст.", parent="cleaner_sessions", color=(170, 170, 175))
            return
        for s in sessions:
            with dpg.group(horizontal=True, parent="cleaner_sessions"):
                dpg.add_text(f"{s.get('session_id','?')}  "
                             f"{s.get('items_count',0)} об.  {s.get('size_mb',0)} МБ",
                             color=(200, 200, 210))
                sid = s.get("session_id", "")
                dpg.add_button(label="[<] Восстановить",
                               user_data=sid, callback=lambda s, a, u: self._undo(u))
                dpg.add_button(label="[!] Удалить навсегда",
                               user_data=sid, callback=lambda s, a, u: self._delete(u))

    def _undo(self, sid):
        ok, msg = svc.cleaner_undo(sid); self._notify(msg); self._refresh_sessions()

    def _delete(self, sid):
        ok, msg = svc.cleaner_delete_forever(sid); self._notify(msg); self._refresh_sessions()


# ===========================================================================
# Вкладка: Настройки (5 каскадов ролей + общие параметры)
# ===========================================================================

class SettingsTab:
    """Полная форма настроек: каскад 5 ролей (mode/backend/provider/model) +
    сгруппированные общие параметры. Сохранение — через service_layer."""

    def __init__(self, notify):
        self._notify = notify
        self._values: dict = {}
        self._widgets: dict[str, str] = {}   # ключ настройки -> тег виджета
        self._model_options: list = []       # подтянутые имена локальных моделей

    def build(self):
        with dpg.tab(label="[*] Настройки"):
            self._values = svc.load_settings()
            if "__error__" in self._values:
                dpg.add_text(f"Ошибка загрузки настроек: {self._values['__error__']}",
                             color=(255, 110, 110))
                self._values = {}
            with dpg.group(horizontal=True):
                dpg.add_button(label="[S] Сохранить настройки", callback=self._save)
                dpg.add_button(label="[O] Перечитать", callback=self._reload)
                dpg.add_text("", tag="settings_status", color=(150, 200, 150))
            dpg.add_separator()
            # --- Профили конфигурации [идея 6] ---
            with dpg.collapsing_header(label="Профили конфигурации", default_open=True):
                dpg.add_text("Сохраните текущие настройки как профиль и переключайтесь "
                             "одним кликом (напр. «эконом free» / «макс качество»).",
                             color=(170, 170, 175), wrap=900)
                with dpg.group(horizontal=True):
                    dpg.add_input_text(tag="profile_name", width=240, hint="имя профиля")
                    dpg.add_button(label="[S] Сохранить как профиль", callback=self._save_profile)
                with dpg.group(horizontal=True):
                    dpg.add_combo(svc.list_profiles(), tag="profile_select", width=240,
                                  default_value="")
                    dpg.add_button(label="[v] Применить", callback=self._apply_profile)
                    dpg.add_button(label="[x] Удалить", callback=self._delete_profile)
                dpg.add_text("Резервная копия профилей (на случай переустановки): "
                             "экспорт сохранит все профили в JSON-файл, импорт — загрузит.",
                             color=(150, 150, 155), wrap=900)
                with dpg.group(horizontal=True):
                    dpg.add_input_text(tag="profiles_io_path", width=360,
                                       hint="путь к JSON (напр. D:\\backup\\profiles.json)")
                    dpg.add_button(label="[E] Экспорт", callback=self._export_profiles)
                    dpg.add_button(label="[I] Импорт", callback=self._import_profiles)
            dpg.add_separator()

            # --- Каскад ролей ---
            with dpg.collapsing_header(label="Модели по ролям (5 агентов)", default_open=True):
                dpg.add_text("Для каждой роли: источник → бэкенд/провайдер → модель. "
                             "Разные локальные модели на роль снижают галлюцинации.",
                             color=(170, 170, 175), wrap=900)
                dpg.add_text("Имя модели можно ввести вручную, либо нажмите «Обновить "
                             "списки моделей»: для роли на 'local' список придёт с "
                             "запущенного сервера (LM Studio/Ollama), для 'cloud' — "
                             "с провайдера (бесплатные модели — вверху списка).",
                             color=(150, 150, 155), wrap=900)
                with dpg.group(horizontal=True):
                    dpg.add_button(label="[O] Обновить списки моделей",
                                   callback=self._show_local_models)
                    dpg.add_text("", tag="set_models_hint", color=(120, 200, 255), wrap=700)
                dpg.add_text("Источник: локальный сервер (lmstudio/ollama/llamacpp/lemonade) "
                             "или «cloud». При «cloud» работает провайдер и облачная модель; "
                             "при локальном — провайдер игнорируется.",
                             color=(150, 150, 155), wrap=900)
                # Контейнер строк ролей — его можно целиком пересобрать при применении
                # профиля (надёжнее, чем точечно обновлять каждое DPG-combo).
                with dpg.group(tag="role_rows_container"):
                    self._build_role_rows()

            # --- Общие параметры по группам ---
            for group, fields in svc.SETTINGS_SCHEMA.items():
                with dpg.collapsing_header(label=group, default_open=(group == "Проект")):
                    for key, typ, label in fields:
                        self._build_field(key, typ, label)

    def _build_field(self, key, typ, label):
        cur = self._values.get(key)
        tag = f"set_{key}"
        self._widgets[key] = tag
        # Относительные ширины (width=-N): поле занимает доступное место, оставляя
        # N пикселей под подпись справа. Не ломается при крупном шрифте (доступность).
        if typ == "bool":
            dpg.add_checkbox(label=label, tag=tag, default_value=bool(cur))
        elif typ.startswith("choice:"):
            opts = typ.split(":", 1)[1].split(",")
            dpg.add_combo(opts, label=label, tag=tag, width=-250,
                          default_value=str(cur) if cur in opts else opts[0])
        elif typ.startswith("int:"):
            _, lo, hi = typ.split(":")
            dpg.add_slider_int(label=label, tag=tag, default_value=int(cur or lo),
                               min_value=int(lo), max_value=int(hi), width=-250)
        else:  # str
            dpg.add_input_text(label=label, tag=tag, width=-250,
                               default_value=str(cur) if cur is not None else "")

    def _build_role_rows(self):
        """Строит строки ролей (источник/провайдер/модель) внутри role_rows_container.
        Combo получают УНИКАЛЬНЫЕ ID (generate_uuid), а не фиксированные строковые
        теги: при пересоздании (применение профиля) фиксированный алиас в DPG 2.x
        не освобождается мгновенно (помещается в очередь до конца кадра), и новый
        combo с тем же тегом «не оживает» — set_value стреляет в зомби-виджет.
        С уникальными ID этой проблемы нет."""
        for role in svc.ROLES:
            with dpg.group(horizontal=True, parent="role_rows_container"):
                dpg.add_text(f"{svc.ROLE_LABELS[role]:<12}", color=(120, 200, 255))
                bk_tag = dpg.generate_uuid()
                self._widgets[f"backend_{role}"] = bk_tag
                saved_backend = self._values.get(f"backend_{role}")
                if saved_backend in svc.SOURCES:
                    src_default = saved_backend
                elif self._values.get(f"mode_{role}") == "cloud":
                    src_default = "cloud"
                else:
                    src_default = "lmstudio"
                dpg.add_combo(svc.SOURCES, tag=bk_tag, width=140,
                              default_value=src_default, user_data=role,
                              callback=self._on_source_change)
                pr_tag = dpg.generate_uuid()
                self._widgets[f"provider_{role}"] = pr_tag
                dpg.add_combo(svc.PROVIDERS, tag=pr_tag, width=130,
                              default_value=self._values.get(f"provider_{role}", "gemini"))
                md_tag = dpg.generate_uuid()
                self._widgets[f"model_{role}"] = md_tag
                cur_model = self._values.get(f"model_{role}", "")
                init_items = list(self._model_options) if self._model_options else []
                if cur_model and cur_model not in init_items:
                    init_items = [cur_model] + init_items
                dpg.add_combo(init_items, tag=md_tag, width=260, default_value=cur_model)

    def _rebuild_role_rows(self):
        """Пересобирает строки ролей из текущих self._values. С уникальными ID
        (generate_uuid) поштучное удаление алиасов не нужно — просто чистим
        контейнер: DPG уничтожит старые ID, _build_role_rows создаст свежие."""
        if dpg.does_item_exist("role_rows_container"):
            dpg.delete_item("role_rows_container", children_only=True)
        self._build_role_rows()

    def _on_source_change(self, sender, app_data, user_data):
        """При смене источника роли (cloud<->локальный сервер) очищаем поле модели:
        локальное имя модели недействительно для облака и наоборот. Пользователь
        затем выберет корректную модель кнопкой «Обновить списки моделей»."""
        role = user_data
        md_tag = self._widgets.get(f"model_{role}")
        if md_tag:
            try:
                dpg.configure_item(md_tag, items=[], default_value="")
                dpg.set_value(md_tag, "")
            except Exception:
                pass
        self._notify(f"Источник роли «{svc.ROLE_LABELS.get(role, role)}» изменён — "
                     f"выберите модель через «Обновить списки моделей».")

    def _show_local_models(self):
        """Заполняет выпадающие списки моделей для КАЖДОЙ роли: если роль на
        'local' — тянет с локального сервера, если 'cloud' — с провайдера (живой
        запрос /models, бесплатные вперёд). Имя по-прежнему можно вписать вручную."""
        filled, errors = 0, []
        local_cache: dict = {}
        for role in svc.ROLES:
            tag = self._widgets.get(f"model_{role}")
            if not tag:
                continue
            source = dpg.get_value(self._widgets.get(f"backend_{role}", "")) or "lmstudio"
            try:
                if source == "cloud":
                    provider = dpg.get_value(self._widgets.get(f"provider_{role}", "")) or "gemini"
                    models = svc.list_cloud_models(provider)
                else:
                    backend = source
                    url = self._values.get("local_base_url", "") or ""
                    if backend not in local_cache:
                        local_cache[backend] = svc.list_local_models(backend, url)
                    models = local_cache[backend]
            except Exception as e:
                errors.append(str(e)); continue
            if not models:
                continue
            cur = dpg.get_value(tag)
            items = list(models)
            if cur and cur not in items:
                items = [cur] + items  # не теряем вручную введённое имя
            dpg.configure_item(tag, items=items)
            filled += 1
        if filled:
            dpg.set_value("set_models_hint",
                          f"Списки обновлены у {filled} ролей. Откройте список у роли "
                          f"(облачные бесплатные — вверху).")
        else:
            dpg.set_value("set_models_hint",
                          "Не удалось получить модели. Для local — запустите LM Studio/"
                          "Ollama; для cloud — проверьте API-ключ в secrets.toml.")

    def _collect(self) -> dict:
        out = {}
        for key, tag in self._widgets.items():
            try:
                out[key] = dpg.get_value(tag)
            except Exception:
                pass
        # Синхронизируем устаревшее mode_<role> с выбранным источником, чтобы старое
        # поле больше никогда не перетирало выбор (источник 'cloud' -> mode 'cloud',
        # локальный сервер -> mode 'local'). Иначе рассинхрон возвращался.
        for role in svc.ROLES:
            src = out.get(f"backend_{role}")
            if src == "cloud":
                out[f"mode_{role}"] = "cloud"
            elif src in ("lmstudio", "ollama", "llamacpp", "lemonade"):
                out[f"mode_{role}"] = "local"
        return out

    def _save(self):
        ok, msg = svc.save_settings(self._collect())
        dpg.set_value("settings_status", msg)
        self._notify(msg)

    def _reload(self):
        self._values = svc.load_settings()
        for key, tag in self._widgets.items():
            if key not in self._values:
                continue
            val = self._values[key]
            try:
                # Для комбо НЕДОСТАТОЧНО set_value: если значение из профиля не входит
                # в текущий список items, оно визуально не применится. Поэтому для
                # комбо ролей пересобираем items так, чтобы новое значение в них было.
                info = dpg.get_item_configuration(tag)
                if "items" in info:
                    items = list(info.get("items") or [])
                    if val and val not in items:
                        # источник/провайдер — фиксированные наборы; модель — свободный
                        if key.startswith("backend_"):
                            items = list(svc.SOURCES)
                        elif key.startswith("provider_"):
                            items = list(svc.PROVIDERS)
                        else:
                            items = [val] + items
                    dpg.configure_item(tag, items=items, default_value=val)
                dpg.set_value(tag, val)
            except Exception:
                try: dpg.set_value(tag, val)
                except Exception: pass
        dpg.set_value("settings_status", "Настройки перечитаны (списки синхронизированы).")

    def _refresh_profiles(self):
        dpg.configure_item("profile_select", items=svc.list_profiles())

    def _save_profile(self):
        name = dpg.get_value("profile_name").strip()
        self._save()  # сначала зафиксировать текущие настройки в сессию
        ok, msg = svc.save_profile(name)
        dpg.set_value("settings_status", msg)
        self._notify(msg)
        self._refresh_profiles()

    def _apply_profile(self):
        name = dpg.get_value("profile_select")
        if not name:
            self._notify("Выберите профиль.")
            return
        ok, msg = svc.apply_profile(name)
        self._notify(msg)
        if ok:
            # Сначала обновляем значения из сессии, затем ПЕРЕСОБИРАЕМ строки ролей
            # с нуля (надёжнее точечного обновления combo) и общие поля через _reload.
            self._values = svc.load_settings()
            self._rebuild_role_rows()
            self._reload()
            self._notify(f"Профиль «{name}» применён — настройки обновлены.")

    def _delete_profile(self):
        name = dpg.get_value("profile_select")
        if not name:
            return
        ok, msg = svc.delete_profile(name)
        self._notify(msg)
        self._refresh_profiles()

    def _export_profiles(self):
        path = dpg.get_value("profiles_io_path")
        ok, msg = svc.export_profiles(path)
        self._notify(msg)

    def _import_profiles(self):
        path = dpg.get_value("profiles_io_path")
        ok, msg = svc.import_profiles(path)
        self._notify(msg)
        if ok:
            self._refresh_profiles()


# ===========================================================================
# Вкладка: Локальный сервер llama.cpp
# ===========================================================================

class LlamaTab:
    """Запуск/останов llama-server: выбор GGUF-модели, контекст, KV-кэш, -ngl
    (с автоподбором по VRAM). Живой статус сервера."""

    def __init__(self, runner: BackgroundRunner, notify):
        self._runner = runner
        self._notify = notify
        self._models: list[dict] = []
        self._kv: list[dict] = []

    def build(self):
        with dpg.tab(label="Llama-сервер"):
            dpg.add_text("Локальный сервер llama.cpp для GGUF-моделей. "
                         "Запускается в фоне, доступен агентам как бэкенд 'llamacpp'.",
                         color=(170, 170, 175), wrap=900)
            with dpg.group(horizontal=True):
                dpg.add_button(label="[O] Обновить модели/VRAM", callback=self._refresh)
                dpg.add_text("", tag="llama_vram_text", color=(180, 180, 200))
            dpg.add_separator()
            # Параметры запуска
            dpg.add_text("Модель (.gguf):", color=(170, 170, 175))
            dpg.add_combo([], tag="llama_model", width=600, default_value="")
            with dpg.group(horizontal=True):
                dpg.add_text("Контекст:")
                dpg.add_slider_int(tag="llama_ctx", default_value=4096, min_value=512,
                                   max_value=131072, width=300)
                dpg.add_text("KV-кэш:")
                dpg.add_combo([], tag="llama_kv", width=120, default_value="q8_0")
            with dpg.group(horizontal=True):
                dpg.add_text("Слои на GPU (-ngl, -1 = авто по VRAM):")
                dpg.add_slider_int(tag="llama_ngl", default_value=-1, min_value=-1,
                                   max_value=999, width=250)
            dpg.add_separator()
            with dpg.group(horizontal=True):
                dpg.add_checkbox(label="Доступ с телефона (LAN)", tag="llama_lan",
                                 default_value=False)
                dpg.add_text("— открывает сервер в локальной сети; защищён токеном ниже",
                             color=(170, 170, 175))
            with dpg.group(horizontal=True):
                dpg.add_button(label="[>] Запустить сервер", callback=self._start, tag="llama_start_btn")
                dpg.add_button(label="[#] Остановить", callback=self._stop, tag="llama_stop_btn")
                dpg.add_loading_indicator(tag="llama_spinner", show=False, radius=2)
            dpg.add_text("статус: —", tag="llama_status_text", color=(180, 180, 185), wrap=900)
            with dpg.group(horizontal=True):
                dpg.add_text("Токен для телефона:", color=(170, 170, 175))
                dpg.add_input_text(tag="llama_token", readonly=True, width=320,
                                   password=True)
                dpg.add_button(label="Показать", tag="llama_token_btn",
                               callback=self._toggle_token)
            self._refresh()

    def _refresh(self):
        self._models = svc.llama_list_models()
        self._kv = svc.llama_kv_options()
        names = [m["name"] for m in self._models] or ["(моделей не найдено)"]
        dpg.configure_item("llama_model", items=names,
                           default_value=names[0] if names else "")
        kv_vals = [k["value"] for k in self._kv] or ["q8_0"]
        dpg.configure_item("llama_kv", items=kv_vals, default_value="q8_0")
        vram = svc.llama_detect_vram()
        dpg.set_value("llama_vram_text",
                      f"VRAM: ~{vram} МБ" if vram else "VRAM: не определена (введите -ngl вручную)")
        self._update_status(svc.llama_status())

    def _model_path(self, name: str) -> str:
        for m in self._models:
            if m["name"] == name:
                return m["path"]
        return ""

    def _start(self):
        name = dpg.get_value("llama_model")
        path = self._model_path(name)
        if not path:
            self._notify("Выберите модель (.gguf).")
            return
        if self._runner.is_busy("llama_start"):
            return
        dpg.configure_item("llama_spinner", show=True)
        self._runner.run("llama_start", svc.llama_start, path,
                         int(dpg.get_value("llama_ctx")), int(dpg.get_value("llama_ngl")),
                         dpg.get_value("llama_kv"), None, bool(dpg.get_value("llama_lan")))

    def _toggle_token(self):
        cur = dpg.get_item_configuration("llama_token").get("password", True)
        dpg.configure_item("llama_token", password=not cur)
        dpg.set_item_label("llama_token_btn", "Скрыть" if cur else "Показать")

    def _stop(self):
        self._runner.run("llama_stop", svc.llama_stop)

    def on_result(self, result):
        dpg.configure_item("llama_spinner", show=False)
        if isinstance(result, tuple) and result and result[0] == "__error__":
            self._notify(f"Ошибка: {result[1]}")
            return
        self._notify(result.get("message", ""))
        self._update_status(result.get("status", {}))

    def _update_status(self, st: dict):
        if not st:
            return
        tok = st.get("token", "")
        if tok:
            dpg.set_value("llama_token", tok)
        if st.get("running"):
            lan = " · доступен с телефона" if st.get("lan") else ""
            dpg.set_value("llama_status_text",
                          f"статус: [ON] работает (PID {st.get('pid')}) — {st.get('model','')} "
                          f"на {st.get('url','')}{lan}")
        elif not st.get("binary_found", True):
            dpg.set_value("llama_status_text",
                          "статус: [!] llama-server не найден (соберите Portable.bat или задайте LLAMA_BIN_DIR)")
        else:
            dpg.set_value("llama_status_text", "статус: [--] остановлен")


# ===========================================================================
# Вкладка: Auto QA (стресс-тесты ядра)
# ===========================================================================

class QaTab:
    """Запуск стресс-тестов ядра из UI: выбор режима, параметры, вердикт PASS/FAIL
    и ключевые метрики. Прогон — в фоне, UI не замерзает."""

    def __init__(self, runner: BackgroundRunner, notify):
        self._runner = runner
        self._notify = notify
        self._mode = "chaos"

    def build(self):
        with dpg.tab(label="Auto QA"):
            dpg.add_text("Автономные стресс-тесты ядра. Изолированы (песочница), "
                         "сеть/модели не нужны — проверяется устойчивость, а не качество LLM.",
                         color=(170, 170, 175), wrap=900)
            dpg.add_text("Режим теста:", color=(170, 170, 175))
            self._mode_labels = list(svc.QA_MODES.values())
            self._mode_keys = list(svc.QA_MODES.keys())
            dpg.add_radio_button(self._mode_labels, tag="qa_mode",
                                 default_value=self._mode_labels[1])  # chaos по умолчанию
            dpg.add_separator()
            # Параметры (общие; используются те, что нужны режиму)
            with dpg.group(horizontal=True):
                dpg.add_text("Итераций/задач:")
                dpg.add_slider_int(tag="qa_iters", default_value=200, min_value=10,
                                   max_value=4000, width=300)
            with dpg.group(horizontal=True):
                dpg.add_text("Воркеров (concurrency):")
                dpg.add_slider_int(tag="qa_workers", default_value=8, min_value=2,
                                   max_value=32, width=200)
                dpg.add_text("Seed (0=случайно):")
                dpg.add_slider_int(tag="qa_seed", default_value=0, min_value=0,
                                   max_value=99999, width=200)
            with dpg.group(horizontal=True):
                dpg.add_button(label="[>] Запустить тест", callback=self._run, tag="qa_run_btn")
                dpg.add_loading_indicator(tag="qa_spinner", show=False, radius=2)
                dpg.add_text("", tag="qa_verdict", color=(240, 240, 245))
                dpg.add_button(label="[~] История", callback=self._show_history)
            dpg.add_separator()
            dpg.add_child_window(tag="qa_report", height=-1, border=True)

    def _show_history(self):
        hist = svc.qa_history(limit=30)
        dpg.delete_item("qa_report", children_only=True)
        dpg.add_text("История прогонов (новейшие сверху):", parent="qa_report",
                     color=(120, 200, 255))
        if not hist:
            dpg.add_text("Пусто.", parent="qa_report", color=(170, 170, 175))
            return
        import datetime as _dt
        for e in hist:
            v = e.get("verdict", "?")
            col = (80, 220, 100) if v == "PASS" else ((255, 200, 50) if v == "WARN" else (255, 80, 80))
            t = _dt.datetime.fromtimestamp(e.get("ts", 0)).strftime("%m-%d %H:%M")
            extra = []
            for k in ("passed", "failed", "crashed", "double_claims", "survived"):
                if e.get(k) is not None:
                    extra.append(f"{k}={e[k]}")
            dpg.add_text(f"{t}  [{v}]  {e.get('mode','?')}  {' '.join(extra)}",
                         parent="qa_report", color=col, wrap=1050)

    def _selected_mode(self) -> str:
        label = dpg.get_value("qa_mode")
        for k, v in svc.QA_MODES.items():
            if v == label:
                return k
        return "chaos"

    def _run(self):
        if self._runner.is_busy("qa"):
            return
        mode = self._selected_mode()
        iters = int(dpg.get_value("qa_iters"))
        params = {
            "seed": int(dpg.get_value("qa_seed")),
            "chaos_iters": iters, "soak_iters": iters, "tasks": iters,
            "workers": int(dpg.get_value("qa_workers")),
            "fail_rate": 2, "exhaust_fraction": 0.25, "hang_fraction": 0.0,
        }
        dpg.configure_item("qa_spinner", show=True)
        dpg.configure_item("qa_run_btn", enabled=False)
        dpg.set_value("qa_verdict", "выполняется…")
        dpg.delete_item("qa_report", children_only=True)
        self._runner.run("qa", svc.run_qa_mode, mode, params)

    def on_result(self, result):
        dpg.configure_item("qa_spinner", show=False)
        dpg.configure_item("qa_run_btn", enabled=True)
        if isinstance(result, tuple) and result and result[0] == "__error__":
            dpg.set_value("qa_verdict", f"Ошибка: {result[1]}")
            return
        verdict = result.get("verdict", "?")
        color = (80, 220, 100) if verdict == "PASS" else \
                ((255, 200, 50) if verdict == "WARN" else (255, 80, 80))
        dpg.set_value("qa_verdict", f"ВЕРДИКТ: {verdict}")
        dpg.configure_item("qa_verdict", color=color)
        dpg.delete_item("qa_report", children_only=True)
        if result.get("error"):
            dpg.add_text(result["error"], parent="qa_report", color=(255, 110, 110), wrap=850)
            return
        # Печатаем ключевые метрики (всё, что вернул режим, кроме служебного).
        skip = {"verdict", "mode", "seed", "leaked_threads", "crashes", "collisions",
                "per_kind", "per_target", "cases", "rss_samples"}
        dpg.add_text(f"Режим: {result.get('mode')}   seed: {result.get('seed')}",
                     parent="qa_report", color=(120, 200, 255))
        for k, v in result.items():
            if k in skip:
                continue
            dpg.add_text(f"  {k:<26} {v}", parent="qa_report")
        # детали проблем
        if result.get("leaked_threads"):
            dpg.add_text(f"  [!] зависшие потоки: {result['leaked_threads']}",
                         parent="qa_report", color=(255, 200, 50))
        for cr in (result.get("crashes") or [])[:8]:
            dpg.add_text(f"  [!] {cr}", parent="qa_report", color=(255, 110, 110), wrap=850)
        for col in (result.get("collisions") or [])[:8]:
            dpg.add_text(f"  [!] гонка: {col}", parent="qa_report", color=(255, 110, 110), wrap=850)


# ===========================================================================
# Вкладка: Дашборд здоровья фабрики  [идея 1]
# ===========================================================================

class HealthTab:
    """Обзор всей фабрики на одном экране: демон, llama, задачи, квоты, последний QA.
    Для слабовидящего — не нужно щёлкать вкладки, всё крупно в одном месте."""

    def __init__(self, runner: BackgroundRunner):
        self._runner = runner

    def build(self):
        with dpg.tab(label="Обзор"):
            dpg.add_text("Состояние фабрики", color=(120, 200, 255))
            dpg.add_separator()
            with dpg.group(horizontal=True):
                with dpg.child_window(width=320, height=-60, border=True):
                    dpg.add_text("ДЕМОН", color=(170, 170, 175))
                    dpg.add_text("—", tag="health_daemon", color=(240, 240, 245), wrap=250)
                    dpg.add_spacer(height=6)
                    dpg.add_text("LLAMA-СЕРВЕР", color=(170, 170, 175))
                    dpg.add_text("—", tag="health_llama", color=(240, 240, 245), wrap=250)
                with dpg.child_window(width=320, height=-60, border=True):
                    dpg.add_text("ЗАДАЧИ", color=(170, 170, 175))
                    for st in (svc.STATUS_PENDING, svc.STATUS_PROCESSING, svc.STATUS_FROZEN,
                               svc.STATUS_DONE, svc.STATUS_FAILED):
                        dpg.add_text("—", tag=f"health_cnt_{st}", color=STATUS_COLORS[st])
                with dpg.child_window(width=320, height=-60, border=True):
                    dpg.add_text("ПОСЛЕДНИЙ QA-ТЕСТ", color=(170, 170, 175))
                    dpg.add_text("—", tag="health_qa", color=(240, 240, 245), wrap=250)
                    dpg.add_spacer(height=6)
                    dpg.add_text("КВОТЫ", color=(170, 170, 175))
                    dpg.add_text("нажмите «Обновить квоты»", tag="health_quota",
                                 color=(180, 180, 185), wrap=250)
                with dpg.child_window(width=320, height=-60, border=True):
                    dpg.add_text("ПОДДЕРЖКА ПРОЕКТА", color=(170, 170, 175))
                    import os
                    has_qr = False
                    qr_path = "QR_поддержать.jpg"
                    if os.path.exists(qr_path):
                        try:
                            width, height, channels, data = dpg.load_image(qr_path)
                            with dpg.texture_registry(show=False):
                                dpg.add_static_texture(width=width, height=height, default_value=data, tag="qr_donation_texture")
                            has_qr = True
                        except Exception as e:
                            print(f"Ошибка загрузки QR: {e}")
                    
                    if has_qr:
                        with dpg.group(horizontal=True):
                            dpg.add_image("qr_donation_texture", width=110, height=110)
                            with dpg.group():
                                dpg.add_spacer(height=10)
                                dpg.add_text("Поддержать\nразработку\nCorePilot", color=(120, 200, 255), wrap=120)
                    else:
                        dpg.add_text("QR-код поддержки\nне найден\n(QR_поддержать.jpg)", color=(150, 150, 150), wrap=250)
            dpg.add_separator()
            with dpg.group(horizontal=True):
                dpg.add_button(label="[O] Обновить квоты", callback=self._load_quotas)
                dpg.add_loading_indicator(tag="health_q_spin", show=False, radius=2)

    def _load_quotas(self):
        if self._runner.is_busy("health_quotas"):
            return
        dpg.configure_item("health_q_spin", show=True)
        self._runner.run("health_quotas", svc.get_quotas)

    def on_quotas(self, result):
        dpg.configure_item("health_q_spin", show=False)
        if isinstance(result, tuple) and result and result[0] == "__error__":
            dpg.set_value("health_quota", f"ошибка: {result[1]}")
            return
        if not result:
            dpg.set_value("health_quota", "ключи не настроены")
            return
        lines = []
        for q in result:
            p = q.get("provider", "?")
            if q.get("status") == "ok" and q.get("remaining") is not None:
                lines.append(f"{p}: {q['remaining']}")
            else:
                lines.append(f"{p}: {q.get('status','?')}")
        dpg.set_value("health_quota", "\n".join(lines))

    def update(self, health: svc.HealthSnapshot):
        if health.daemon_running:
            dpg.set_value("health_daemon", f"[ON] работает (PID {health.daemon_pid})")
            dpg.configure_item("health_daemon", color=(80, 220, 100))
        else:
            dpg.set_value("health_daemon", "[--] остановлен")
            dpg.configure_item("health_daemon", color=(180, 180, 185))
        if health.llama_running:
            dpg.set_value("health_llama", f"[ON] {health.llama_model}")
            dpg.configure_item("health_llama", color=(80, 220, 100))
        else:
            dpg.set_value("health_llama", "[--] остановлен")
            dpg.configure_item("health_llama", color=(180, 180, 185))
        for st in (svc.STATUS_PENDING, svc.STATUS_PROCESSING, svc.STATUS_FROZEN,
                   svc.STATUS_DONE, svc.STATUS_FAILED):
            c = health.counts.get(st, 0)
            dpg.set_value(f"health_cnt_{st}", f"{STATUS_SYMBOL[st]} {STATUS_LABEL[st]}: {c}")
        if health.last_qa_verdict:
            col = (80, 220, 100) if health.last_qa_verdict == "PASS" else \
                  ((255, 200, 50) if health.last_qa_verdict == "WARN" else (255, 80, 80))
            dpg.set_value("health_qa", f"{health.last_qa_mode}: {health.last_qa_verdict}")
            dpg.configure_item("health_qa", color=col)
        else:
            dpg.set_value("health_qa", "ещё не запускался")


# ===========================================================================
# Вкладка: Живой лог Демона  [идея 3]
# ===========================================================================

class LogTab:
    """Хвост лога Демона с фильтром по уровню. Обновляется в общем тике."""

    def __init__(self):
        self._level = ""
        self._last_hash = None

    def build(self):
        with dpg.tab(label="Лог демона"):
            with dpg.group(horizontal=True):
                dpg.add_text("Фильтр:", color=(170, 170, 175))
                dpg.add_combo(["ВСЕ", "INFO", "WARNING", "ERROR"], tag="log_level",
                              default_value="ВСЕ", width=160, callback=self._set_level)
                dpg.add_button(label="Очистить вид", callback=self._clear)
            dpg.add_separator()
            dpg.add_child_window(tag="log_area", height=-1, border=True)

    def _set_level(self, sender, value):
        self._level = "" if value == "ВСЕ" else value
        self._last_hash = None  # форсировать перерисовку

    def _clear(self):
        dpg.delete_item("log_area", children_only=True)
        self._last_hash = "cleared"

    def update(self):
        lines = svc.read_daemon_log(max_lines=200, level=self._level)
        h = hash(tuple(lines))
        if h == self._last_hash:
            return
        self._last_hash = h
        dpg.delete_item("log_area", children_only=True)
        if not lines:
            dpg.add_text("Лог пуст (демон не запущен или ещё не писал).",
                         parent="log_area", color=(170, 170, 175))
            return
        for ln in lines:
            col = (240, 240, 245)
            if "ERROR" in ln: col = (255, 110, 110)
            elif "WARNING" in ln: col = (255, 200, 50)
            dpg.add_text(ln[:400], parent="log_area", color=col, wrap=1100)


