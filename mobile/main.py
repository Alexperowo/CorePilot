#!/usr/bin/env python3
"""
main.py — CorePilot Mobile (Flet). Приложение-компаньон для Android.

Две роли в одном:
  • Edge AI Node — запуск тяжёлых GGUF-моделей локально на телефоне (llama.cpp);
  • Пульт управления ПК — связь с десктопным CorePilot по сети + голосовой чат.

Интерфейс: тёмная высококонтрастная тема, NavigationBar на 4 раздела
(Edge AI · Чат · Логи · Настройки). Платформенные вызовы изолированы в
android_bridge — на ПК приложение запускается с фолбэками (flet run).

Запуск (разработка):  flet run
Сборка APK:           flet build apk
"""
from __future__ import annotations

import json
import os
import threading
import urllib.request

import flet as ft

import android_bridge as ab
from llama_server import LlamaServer
from cloud_api import CloudAPIManager, TEXT_PROVIDERS, IMAGE_PROVIDERS, MAX_KEYS, parse_keys

# --- Высококонтрастная тёмная палитра ---------------------------------------
BG = "#0F1115"
SURFACE = "#1A1D24"
SURFACE_HI = "#242832"
TEXT = "#F0F2F5"
MUTED = "#9AA0AA"
ACCENT = "#4DA3FF"
OK = "#50DC64"
WARN = "#FFC832"
ERR = "#FF5050"

SETTINGS_FILE = os.path.join(ab.PUBLIC_ROOT, "mobile_settings.json")
_SETTINGS_KEY = "corepilot.mobile_settings"  # ключ в client_storage Flet


def load_settings(page=None) -> dict:
    """Читает настройки. Приоритет — внутреннее хранилище Flet (page.client_storage):
    оно НЕ требует прав на Android и доступно ДО запроса разрешений (раньше чтение
    из публичной папки Download падало с PermissionError -> серый экран на старте).
    Фолбэк — старый JSON в публичной папке (одноразовая миграция)."""
    if page is not None:
        try:
            raw = page.client_storage.get(_SETTINGS_KEY)
            if raw:
                return json.loads(raw) if isinstance(raw, str) else dict(raw)
        except Exception:
            pass
    # миграция/фолбэк: старый файл в публичной папке (может не быть прав — тихо)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(d: dict, page=None) -> None:
    """Сохраняет настройки во внутреннее хранилище Flet (без прав). В публичную
    папку НЕ пишем — там только тяжёлые .gguf модели."""
    if page is not None:
        try:
            page.client_storage.set(_SETTINGS_KEY, json.dumps(d, ensure_ascii=False))
            return
        except Exception:
            pass
    # крайний фолбэк (десктопная отладка без client_storage)
    try:
        ab.ensure_dirs()
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


class CorePilotMobile:
    def __init__(self, page: ft.Page):
        self.page = page
        self.server = LlamaServer()
        self.tts = ab.TTSEngine()
        self.settings = load_settings(page)  # client_storage — без прав, не падает
        self.cloud = CloudAPIManager(self.settings)  # облачные провайдеры + ротация ключей
        # История чата для LLM: список {"role": "user"/"assistant", "content": "..."}
        # Передаётся целиком при каждом запросе — модель «помнит» контекст сессии.
        self.chat_history: list[dict] = []
        # chat_list создаётся ОДИН раз здесь, а не в _show_chat — иначе при каждом
        # переключении вкладок история чата и картинки уничтожались («амнезия чата»).
        self.chat_list = ft.ListView(expand=True, spacing=10, auto_scroll=True, padding=12)
        # Флаг жизненного цикла: False при закрытии → _status_loop завершается корректно.
        self._running = True
        self._typing_bubble = None  # пузырь «⏳ Думаю…» во время ожидания ответа
        # Создание публичных папок (Download/CorePilot) может требовать прав на Android.
        # Не валим старт, если прав ещё нет — папки нужны только для .gguf моделей.
        try:
            ab.ensure_dirs()
        except Exception:
            pass

        page.title = "CorePilot Mobile"
        page.theme_mode = ft.ThemeMode.DARK
        page.bgcolor = BG
        page.padding = 0
        page.fonts = {}
        page.theme = ft.Theme(color_scheme_seed=ACCENT, font_family="Roboto")

        self._build()
        # периодическое обновление статуса/лога
        self.page.run_thread(self._status_loop)

    # ===================================================================
    # Построение UI
    # ===================================================================
    def _build(self):
        self.body = ft.Container(expand=True, bgcolor=BG, padding=16)
        self.nav = ft.NavigationBar(
            bgcolor=SURFACE,
            indicator_color=ACCENT,
            selected_index=0,
            on_change=self._on_nav,
            destinations=[
                ft.NavigationBarDestination(icon=ft.Icons.MEMORY, label="Edge AI"),
                ft.NavigationBarDestination(icon=ft.Icons.CHAT_BUBBLE_OUTLINE, label="Чат"),
                ft.NavigationBarDestination(icon=ft.Icons.TERMINAL, label="Логи"),
                ft.NavigationBarDestination(icon=ft.Icons.SETTINGS, label="Настройки"),
            ],
        )
        self.page.add(
            ft.SafeArea(
                ft.Column([self.body, self.nav], expand=True, spacing=0),
                expand=True,
            )
        )
        self._show_edge_ai()

    def _on_nav(self, e):
        idx = e.control.selected_index
        [self._show_edge_ai, self._show_chat, self._show_logs, self._show_settings][idx]()

    def _header(self, title: str, subtitle: str = "") -> ft.Control:
        items = [ft.Text(title, size=26, weight=ft.FontWeight.BOLD, color=TEXT)]
        if subtitle:
            items.append(ft.Text(subtitle, size=14, color=MUTED))
        return ft.Column(items, spacing=2)

    def _card(self, *controls) -> ft.Container:
        return ft.Container(
            content=ft.Column(list(controls), spacing=12),
            bgcolor=SURFACE, border_radius=14, padding=16,
        )

    # ===================================================================
    # Вкладка 1 — Edge AI Node (llama-сервер)
    # ===================================================================
    def _show_edge_ai(self):
        raw_models = ab.list_models()
        # Разделяем нормальные модели и ошибки доступа
        error_entry = next((m for m in raw_models if m.get("error")), None)
        models = [m for m in raw_models if not m.get("error")]

        model_opts = [ft.dropdown.Option(m["name"], f"{m['name']}  ({m['size_gb']} ГБ)")
                      for m in models]
        self._models = {m["name"]: m["path"] for m in models}

        self.dd_model = ft.Dropdown(
            label="Модель (.gguf)", options=model_opts,
            value=self.settings.get("model_name") or (models[0]["name"] if models else None),
            border_color=SURFACE_HI, color=TEXT, label_style=ft.TextStyle(color=MUTED),
            filled=True, bgcolor=SURFACE_HI,
        )
        self.tf_ctx = ft.TextField(
            label="Контекст (токены)", value=str(self.settings.get("ctx", 4096)),
            keyboard_type=ft.KeyboardType.NUMBER, color=TEXT, bgcolor=SURFACE_HI,
            border_color=SURFACE_HI, label_style=ft.TextStyle(color=MUTED), filled=True,
            expand=1,
        )
        self.tf_port = ft.TextField(
            label="Порт", value=str(self.settings.get("port", 8080)),
            keyboard_type=ft.KeyboardType.NUMBER, color=TEXT, bgcolor=SURFACE_HI,
            border_color=SURFACE_HI, label_style=ft.TextStyle(color=MUTED), filled=True,
            expand=1,
        )
        self.tf_ngl = ft.TextField(
            label="Слоёв на GPU (-ngl)", value=str(self.settings.get("ngl", 99)),
            keyboard_type=ft.KeyboardType.NUMBER, color=TEXT, bgcolor=SURFACE_HI,
            border_color=SURFACE_HI, label_style=ft.TextStyle(color=MUTED), filled=True,
        )
        self.lbl_status = ft.Text("", size=15, color=MUTED)

        btn_start = ft.Button(
            "▶ Запустить сервер", bgcolor=OK, color="#06210C",
            on_click=self._start_server, expand=True, height=52,
        )
        btn_stop = ft.Button(
            "■ Остановить", bgcolor=ERR, color="#2A0606",
            on_click=self._stop_server, expand=True, height=52,
        )

        # Блок ошибки доступа к хранилищу — показывается вместо «нет моделей»
        # когда список вернул error=True (нет прав на Android 11+).
        def _grant_storage(e):
            ab.request_manage_external_storage()
            self._toast("Выдайте разрешение в открывшемся окне, затем перезайдите на вкладку.", WARN)

        storage_error_block = ft.Container(
            content=ft.Column([
                ft.Text(error_entry["reason"] if error_entry else "", color=WARN, size=13),
                ft.Button(
                    "Выдать доступ к папке моделей",
                    bgcolor=WARN, color="#1A1000",
                    on_click=_grant_storage, height=44,
                ),
            ], spacing=8),
            visible=bool(error_entry), padding=ft.Padding(top=4),
        )

        no_models = ft.Container(
            content=ft.Text(
                f"Моделей не найдено в {ab.MODELS_DIR}\n"
                f"Скопируйте .gguf с ПК командой update_mobile.bat.",
                color=WARN, size=13),
            visible=(not models and not error_entry), padding=ft.Padding(top=4),
        )

        self.body.content = ft.Column([
            self._header("Edge AI Node", "Локальный запуск моделей на телефоне"),
            ft.Container(height=8),
            self._card(
                self.dd_model, storage_error_block, no_models,
                ft.Row([self.tf_ctx, self.tf_port], spacing=12),
                self.tf_ngl,
                ft.Row([btn_start, btn_stop], spacing=12),
                self.lbl_status,
            ),
            ft.Container(height=8),
            ft.Text(f"Движок: {ab.find_server_binary() or 'встроен в APK (не найден — переустановите)'}",
                    size=11, color=MUTED),
            ft.Text(f"Модели: {ab.MODELS_DIR}", size=11, color=MUTED),
        ], scroll=ft.ScrollMode.AUTO, expand=True)
        self._refresh_status_label()
        self.page.update()

    def _start_server(self, e):
        name = self.dd_model.value
        if not name:
            self._toast("Выберите модель из списка или скопируйте .gguf в папку моделей.", WARN)
            return
        path = self._models.get(name, "")
        try:
            ctx = int(self.tf_ctx.value or 4096)
            port = int(self.tf_port.value or 8080)
            ngl = int(self.tf_ngl.value or 99)
        except ValueError:
            self._toast("Контекст/порт/ngl должны быть числами.", ERR); return
        # сохраняем выбор
        self.settings.update({"model_name": name, "ctx": ctx, "port": port, "ngl": ngl})
        save_settings(self.settings, self.page)

        ok, msg = self.server.start(path, ctx=ctx, port=port, ngl=ngl)
        self._toast(msg, OK if ok else ERR)
        self._refresh_status_label()
        self.page.update()

    def _stop_server(self, e):
        ok, msg = self.server.stop()
        self._toast(msg, OK if ok else WARN)
        self._refresh_status_label()
        self.page.update()

    def _refresh_status_label(self):
        if not hasattr(self, "lbl_status"):
            return
        st = self.server.status()
        if st["running"]:
            self.lbl_status.value = f"🟢 РАБОТАЕТ · {st['model']} · {st['url']}"
            self.lbl_status.color = OK
        else:
            self.lbl_status.value = "⚪ Остановлен"
            self.lbl_status.color = MUTED

    # ===================================================================
    # Вкладка 2 — Чат с агентом (голос + текст)
    # ===================================================================
    def _show_chat(self):
        # Селектор цели: локальный сервер, ПК, или облачные провайдеры.
        # Провайдеры без ключей показываются как disabled — пользователь видит их
        # и понимает, что нужно зайти в Настройки, а не думает «функция сломана».
        target_opts = [
            ft.dropdown.Option("local", "Local Llama (телефон)"),
            ft.dropdown.Option("pc", "PC CorePilot (по IP)"),
        ]
        for pid, meta in TEXT_PROVIDERS.items():
            has = self.cloud.has_keys(pid)
            target_opts.append(
                ft.dropdown.Option(
                    f"cloud:{pid}",
                    meta["label"] if has else f"{meta['label']}  (нужен ключ → Настройки)",
                    disabled=not has,
                )
            )
        self.dd_target = ft.Dropdown(
            options=target_opts, value=self.settings.get("chat_target", "local"),
            on_select=self._on_target_change, dense=True,
            color=TEXT, bgcolor=SURFACE_HI, border_color=SURFACE_HI, filled=True,
            text_size=14, expand=True,
        )
        # Переключатель «генерировать картинку» (если есть ключи image-провайдера).
        self.sw_image = ft.Switch(
            label="Картинка", value=False, active_color=ACCENT,
            visible=any(self.cloud.has_keys(p) for p in IMAGE_PROVIDERS),
        )
        self.tf_msg = ft.TextField(
            hint_text="Сообщение агенту…", expand=True, color=TEXT, bgcolor=SURFACE_HI,
            border_color=SURFACE_HI, filled=True, on_submit=self._send_msg,
            multiline=True, min_lines=1, max_lines=4,
            content_padding=ft.padding.symmetric(horizontal=16, vertical=12),
        )
        btn_send = ft.IconButton(
            icon=ft.Icons.SEND, icon_color=ACCENT, icon_size=32,
            tooltip="Отправить", on_click=self._send_msg,
        )
        btn_clear = ft.IconButton(
            icon=ft.Icons.DELETE_OUTLINE, icon_color=MUTED, icon_size=22,
            tooltip="Очистить чат", on_click=lambda e: self._clear_chat(),
        )
        chat_col = ft.Column([
            ft.Row([
                self._header("Чат с агентом", self._chat_target_hint()),
                btn_clear,
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
               vertical_alignment=ft.CrossAxisAlignment.START),
            ft.Row([ft.Icon(ft.Icons.HUB_OUTLINED, color=MUTED, size=18), self.dd_target,
                    self.sw_image], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Container(content=self.chat_list, expand=True, bgcolor=SURFACE,
                         border_radius=14, padding=10),
            ft.Row([self.tf_msg, btn_send],
                   vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=6),
        ], expand=True, spacing=12)
        # max_width не поддерживается в Flet 0.80.5, используем обычный контейнер.
        self.body.content = ft.Container(content=chat_col,
                                         alignment=ft.Alignment(0, 0), expand=True)
        self.page.update()

    def _on_target_change(self, e):
        """Запоминаем выбранную цель чата (на лету)."""
        self.settings["chat_target"] = self.dd_target.value
        save_settings(self.settings, self.page)

    def _clear_chat(self):
        """Очистить визуальную историю пузырьков и контекст LLM."""
        self.chat_list.controls.clear()
        self.chat_history.clear()
        self._typing_bubble = None
        self.page.update()

    def _chat_target_hint(self) -> str:
        if self.server.is_running():
            return f"Локальная модель · {self.server.status()['model']}"
        ip = self.settings.get("pc_ip", "")
        return f"ПК CorePilot · {ip}" if ip else "Цель не задана (см. Настройки)"

    def _add_bubble(self, text: str, mine: bool, image_b64: str = "",
                    return_ref: bool = False) -> ft.Control | None:
        """Пузырь сообщения.
        - Сообщения пользователя (mine=True) — ft.Text (сырой текст).
        - Ответы агента (mine=False) — ft.Markdown для рендера кода/таблиц/bold.
        - Если задан image_b64 — добавляется ft.Image выше текста.
        - return_ref=True — возвращает ft.Row-обёртку (для удаления пузыря-заглушки)."""
        inner = []
        if image_b64:
            inner.append(ft.Image(src_base64=image_b64, fit=ft.ImageFit.FIT_WIDTH,
                                  border_radius=8))
        if text:
            if mine:
                inner.append(ft.Text(text, color=TEXT, size=16, selectable=True))
            else:
                # ft.Markdown: рендерит **bold**, `code`, блоки кода, списки.
                inner.append(ft.Markdown(
                    text,
                    selectable=True,
                    extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
                    code_theme="atom-one-dark",
                ))
        bubble = ft.Container(
            content=ft.Column(inner, spacing=8, tight=True) if len(inner) > 1
                    else (inner[0] if inner else ft.Text("", color=TEXT)),
            bgcolor=ACCENT if mine else SURFACE_HI,
            padding=ft.padding.symmetric(12, 16), border_radius=14,
            margin=ft.margin.only(left=40 if mine else 0, right=0 if mine else 40),
            border=ft.border.all(1, color=ACCENT if mine else "#303540"),
        )
        row = ft.Row([bubble], alignment=ft.MainAxisAlignment.END if mine
                     else ft.MainAxisAlignment.START)
        self.chat_list.controls.append(row)
        self.page.update()
        return row if return_ref else None

    def _send_msg(self, e):
        text = (self.tf_msg.value or "").strip()
        if not text:
            return
        self._add_bubble(text, mine=True)
        self.tf_msg.value = ""
        # Пузырь-заглушка «⏳ Думаю…» даёт мгновенную обратную связь — убирается
        # при получении реального ответа в _query_agent().
        self._typing_bubble = self._add_bubble("⏳ Думаю…", mine=False, return_ref=True)
        self.page.update()
        # запрос к модели/ПК — в фоне, UI не блокируется
        self.page.run_thread(self._query_agent, text)

    def _query_agent(self, prompt: str):
        target = self.settings.get("chat_target", "local")
        # Режим генерации картинки (если включён тумблер и есть image-провайдер с ключами)
        if getattr(self, "sw_image", None) and self.sw_image.value:
            img_provider = next((p for p in IMAGE_PROVIDERS if self.cloud.has_keys(p)), "")
            self._remove_typing_bubble()
            if not img_provider:
                self._add_bubble("Нет ключей для генерации картинок. Добавьте в Настройках.", mine=False)
                return
            res = self.cloud.generate_image(img_provider, prompt)
            if res.get("ok"):
                self._add_bubble("", mine=False, image_b64=res["image_b64"])
            else:
                self._add_bubble(f"⚠️ {res.get('error', 'ошибка генерации')}", mine=False)
            return

        # Добавляем пользовательское сообщение в историю перед запросом.
        self.chat_history.append({"role": "user", "content": prompt})

        # Текстовый чат — передаём полную историю провайдеру.
        if target.startswith("cloud:"):
            provider = target.split(":", 1)[1]
            reply = self.cloud.chat(provider, self.chat_history)
        else:
            reply = self._call_backend(self.chat_history, target)

        # Сохраняем ответ в историю (только при успехе, не при ошибке [ERR]).
        if not reply.startswith("[ERR] ") and not reply.startswith("⚠️"):
            self.chat_history.append({"role": "assistant", "content": reply})

        # Убираем пузырь «⏳ Думаю…» и показываем реальный ответ.
        self._remove_typing_bubble()
        display = reply[len("[ERR] "):] if reply.startswith("[ERR] ") else reply
        prefix = "⚠️ " if reply.startswith("[ERR] ") else ""
        self._add_bubble(prefix + display, mine=False)
        if self.settings.get("tts_enabled", True) and not reply.startswith("[ERR] "):
            self.tts.speak(reply)

    def _remove_typing_bubble(self):
        """Убрать пузырь «⏳ Думаю…» если он ещё висит."""
        if self._typing_bubble is not None:
            try:
                self.chat_list.controls.remove(self._typing_bubble)
                self.page.update()
            except ValueError:
                pass
            finally:
                self._typing_bubble = None

    def _call_backend(self, history: list[dict], target: str = "local") -> str:
        """Локальный сервер или ПК CorePilot (OpenAI-совместимый /v1/chat/completions).
        Принимает полную историю сообщений (list[dict]) — системный промпт добавляется
        первым, если его нет в истории. Облачные провайдеры обслуживает CloudAPIManager."""
        if target == "local" and self.server.is_running():
            base = self.server.status()["url"]
        elif target == "local":
            return "Локальный сервер не запущен. Запустите его на вкладке Edge AI или выберите другую цель."
        else:  # pc
            ip = self.settings.get("pc_ip", "").strip()
            if not ip:
                return "IP ПК не задан. Укажите его в Настройках или выберите другую цель."
            port = self.settings.get("pc_port", 8080)
            base = f"http://{ip}:{port}/v1"
        try:
            messages = list(history)  # копия, не мутируем оригинал
            sys_prompt = (self.settings.get("system_prompt", "") or "").strip()
            has_system = any(m.get("role") == "system" for m in messages)
            if sys_prompt and not has_system:
                messages = [{"role": "system", "content": sys_prompt}] + messages
            payload = json.dumps({
                "model": "local", "messages": messages,
                "max_tokens": int(self.settings.get("chat_max_tokens", 2048) or 2048),
                "temperature": float(self.settings.get("chat_temperature", 0.7) or 0.7),
            }).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            token = self.settings.get("pc_token", "").strip()
            if token:
                headers["Authorization"] = f"Bearer {token}"
            req = urllib.request.Request(f"{base}/chat/completions", data=payload,
                                         headers=headers)
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()
        except Exception as ex:
            return f"⚠️ Не удалось получить ответ ({base}): {ex}"

    # ===================================================================
    # Вкладка 3 — Логи сервера
    # ===================================================================
    def _show_logs(self):
        self.log_view = ft.Text(
            self.server.get_log() or "Лог пуст. Запустите сервер на вкладке Edge AI.",
            size=12, color=TEXT, selectable=True, font_family="monospace",
        )
        self.body.content = ft.Column([
            ft.Row([
                self._header("Логи сервера"),
                ft.IconButton(icon=ft.Icons.REFRESH, icon_color=ACCENT,
                              on_click=lambda e: self._refresh_log(),
                              tooltip="Обновить"),
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            ft.Container(
                content=ft.Column([self.log_view], scroll=ft.ScrollMode.ALWAYS,
                                  expand=True),
                bgcolor="#06080C", border_radius=12, padding=12, expand=True,
            ),
        ], expand=True, spacing=12)
        self.page.update()

    def _refresh_log(self):
        if hasattr(self, "log_view"):
            self.log_view.value = self.server.get_log() or "Лог пуст."
            self.page.update()

    # ===================================================================
    # Вкладка 4 — Настройки
    # ===================================================================
    def _show_settings(self):
        self.tf_ip = ft.TextField(
            label="IP-адрес домашнего ПК", value=self.settings.get("pc_ip", ""),
            hint_text="например 192.168.1.50", color=TEXT, bgcolor=SURFACE_HI,
            border_color=SURFACE_HI, label_style=ft.TextStyle(color=MUTED), filled=True,
        )
        self.tf_pc_port = ft.TextField(
            label="Порт ПК", value=str(self.settings.get("pc_port", 8080)),
            keyboard_type=ft.KeyboardType.NUMBER, color=TEXT, bgcolor=SURFACE_HI,
            border_color=SURFACE_HI, label_style=ft.TextStyle(color=MUTED), filled=True,
        )
        self.tf_token = ft.TextField(
            label="Токен доступа (с вкладки Llama-сервер на ПК)",
            value=self.settings.get("pc_token", ""), password=True, can_reveal_password=True,
            hint_text="обязателен для связи с ПК", color=TEXT, bgcolor=SURFACE_HI,
            border_color=SURFACE_HI, label_style=ft.TextStyle(color=MUTED), filled=True,
        )
        self.sw_tts = ft.Switch(
            label="Озвучивать ответы агента (TTS)", value=self.settings.get("tts_enabled", True),
            active_color=ACCENT,
        )
        # --- Параметры чата: системный промпт, температура, лимит токенов ---
        self.tf_sysprompt = ft.TextField(
            label="Системный промпт (как вести себя модели)",
            value=self.settings.get("system_prompt", ""),
            hint_text="напр. Ты эксперт-программист. Отвечай кратко.",
            multiline=True, min_lines=2, max_lines=5,
            color=TEXT, bgcolor=SURFACE_HI, border_color=SURFACE_HI,
            label_style=ft.TextStyle(color=MUTED), filled=True,
        )
        self.tf_temp = ft.TextField(
            label="Температура (0.0-2.0)", value=str(self.settings.get("chat_temperature", 0.7)),
            keyboard_type=ft.KeyboardType.NUMBER, color=TEXT, bgcolor=SURFACE_HI,
            border_color=SURFACE_HI, label_style=ft.TextStyle(color=MUTED), filled=True, expand=1,
        )
        self.tf_maxtok = ft.TextField(
            label="Макс. токенов (256-8192)", value=str(self.settings.get("chat_max_tokens", 2048)),
            keyboard_type=ft.KeyboardType.NUMBER, color=TEXT, bgcolor=SURFACE_HI,
            border_color=SURFACE_HI, label_style=ft.TextStyle(color=MUTED), filled=True, expand=1,
        )
        self.dd_imgsize = ft.Dropdown(
            label="Размер картинки", value=self.settings.get("image_size", "1024x1024"),
            options=[ft.dropdown.Option(s) for s in ("1024x1024", "1024x768", "768x1024", "512x512")],
            color=TEXT, bgcolor=SURFACE_HI, border_color=SURFACE_HI,
            label_style=ft.TextStyle(color=MUTED), filled=True,
        )
        # Поля ключей облачных провайдеров (до 4 через запятую). Имена — "<id>_keys".
        self._cloud_key_fields = {}
        self._cloud_model_fields = {}
        cloud_rows = [ft.Text(f"Облачные API-ключи (до {MAX_KEYS} на провайдера, через запятую) + модель",
                              color=MUTED, size=13)]
        for pid, meta in TEXT_PROVIDERS.items():
            tf = ft.TextField(
                label=meta["label"], value=self.settings.get(f"{pid}_keys", ""),
                password=True, can_reveal_password=True,
                hint_text="ключ1, ключ2, …", color=TEXT, bgcolor=SURFACE_HI,
                border_color=SURFACE_HI, label_style=ft.TextStyle(color=MUTED), filled=True,
            )
            mf = ft.TextField(
                label=f"  модель {pid}", value=self.settings.get(f"{pid}_model", ""),
                hint_text=f"пусто = {meta['default_model']}", color=TEXT, bgcolor=SURFACE_HI,
                border_color=SURFACE_HI, label_style=ft.TextStyle(color=MUTED), filled=True,
            )
            self._cloud_key_fields[pid] = tf
            self._cloud_model_fields[pid] = mf
            cloud_rows.append(tf)
            cloud_rows.append(mf)
        # HF-ключ для картинок (если у HF общий ключ — можно тот же)
        for pid, meta in IMAGE_PROVIDERS.items():
            if pid in self._cloud_key_fields:
                continue  # уже есть поле (общий ключ HF)
            tf = ft.TextField(
                label=f"{meta['label']} (картинки)", value=self.settings.get(f"{pid}_keys", ""),
                password=True, can_reveal_password=True,
                hint_text="ключ1, ключ2, …", color=TEXT, bgcolor=SURFACE_HI,
                border_color=SURFACE_HI, label_style=ft.TextStyle(color=MUTED), filled=True,
            )
            self._cloud_key_fields[pid] = tf
            cloud_rows.append(tf)

        self.body.content = ft.Column([
            self._header("Настройки"),
            ft.Container(height=8),
            self._card(
                ft.Text("Связь с десктопным CorePilot", color=MUTED, size=13),
                self.tf_ip, self.tf_pc_port, self.tf_token,
            ),
            self._card(
                ft.Text("Параметры чата", color=MUTED, size=13),
                self.tf_sysprompt,
                ft.Row([self.tf_temp, self.tf_maxtok], spacing=12),
                self.dd_imgsize,
            ),
            self._card(*cloud_rows),
            self._card(
                ft.Text("Голос", color=MUTED, size=13),
                self.sw_tts,
                ft.Button("🔊 Проверить озвучку", bgcolor=SURFACE_HI, color=TEXT,
                          on_click=lambda e: self.tts.speak("Синтез речи работает.")),
            ),
            self._card(
                ft.Text("Поддержка проекта", color=MUTED, size=13),
                ft.Text("Если вам нравится CorePilot, вы можете поддержать его разработку:", color=TEXT, size=14),
                ft.Row([
                    ft.Image(src=os.path.join(os.path.dirname(__file__), "QR_поддержать.jpg"), width=110, height=110) if os.path.exists(os.path.join(os.path.dirname(__file__), "QR_поддержать.jpg")) else ft.Text("QR-код поддержки не найден"),
                    ft.Column([
                        ft.Text("Сканируйте QR-код для пожертвования разработчику", color=MUTED, size=13, width=180, wrap=True)
                    ], spacing=4)
                ], spacing=16)
            ),
            ft.Button("💾 Сохранить настройки", bgcolor=ACCENT, color="#06210C",
                      on_click=self._save_settings, height=52, width=10000),
        ], scroll=ft.ScrollMode.AUTO, expand=True, spacing=12)
        self.page.update()

    def _save_settings(self, e):
        try:
            self.settings["pc_ip"] = self.tf_ip.value.strip()
            self.settings["pc_port"] = int(self.tf_pc_port.value or 8080)
            self.settings["pc_token"] = self.tf_token.value.strip()
            self.settings["tts_enabled"] = self.sw_tts.value
            # Параметры чата
            self.settings["system_prompt"] = (self.tf_sysprompt.value or "").strip()
            try:
                self.settings["chat_temperature"] = max(0.0, min(2.0, float(self.tf_temp.value or 0.7)))
            except ValueError:
                self.settings["chat_temperature"] = 0.7
            try:
                self.settings["chat_max_tokens"] = max(256, min(8192, int(self.tf_maxtok.value or 2048)))
            except ValueError:
                self.settings["chat_max_tokens"] = 2048
            self.settings["image_size"] = self.dd_imgsize.value or "1024x1024"
            # Облачные ключи провайдеров (поля <id>_keys); нормализуем до MAX_KEYS.
            for pid, tf in getattr(self, "_cloud_key_fields", {}).items():
                self.settings[f"{pid}_keys"] = ",".join(parse_keys(tf.value))
            for pid, mf in getattr(self, "_cloud_model_fields", {}).items():
                self.settings[f"{pid}_model"] = (mf.value or "").strip()
            save_settings(self.settings, self.page)
            # Пересоздаём менеджер с новыми ключами (чтобы чат сразу их увидел).
            self.cloud = CloudAPIManager(self.settings)
            self._toast("Настройки сохранены.", OK)
        except ValueError:
            self._toast("Порт должен быть числом.", ERR)

    # ===================================================================
    # Сервисное
    # ===================================================================
    def _toast(self, text: str, color: str = ACCENT):
        # Фон снэкбара семантически окрашен: зелёный = успех, красный = ошибка,
        # жёлтый = предупреждение. Без ключа — нейтральный SURFACE_HI.
        bg = {OK: "#0D2B14", ERR: "#2B0D0D", WARN: "#2B210D"}.get(color, SURFACE_HI)
        snack = ft.SnackBar(
            content=ft.Text(text, color=TEXT), bgcolor=bg,
            duration=3000,
        )
        if hasattr(self.page, "open"):
            self.page.open(snack)
        else:
            self.page.snack_bar = snack
            self.page.snack_bar.open = True
            self.page.update()

    def _status_loop(self):
        import time
        while self._running:
            time.sleep(2)
            try:
                if self.nav.selected_index == 0:
                    self._refresh_status_label(); self.page.update()
                elif self.nav.selected_index == 2:
                    self._refresh_log()
            except Exception:
                pass


def main(page: ft.Page):
    # Crash logger: если старт падает (особенно на Android до первой отрисовки),
    # показываем traceback прямо на экране и пишем в client_storage — иначе виден
    # только «серый экран смерти» без причины.
    try:
        app = CorePilotMobile(page)
        # Останавливаем фоновый поток при закрытии приложения / уходе в фон.
        page.on_close = lambda _: setattr(app, "_running", False)
    except Exception:
        import traceback
        tb = traceback.format_exc()
        try:
            page.client_storage.set("corepilot.last_crash", tb)
        except Exception:
            pass
        try:
            page.controls.clear()
            page.scroll = ft.ScrollMode.AUTO
            page.add(ft.Column([
                ft.Text("CorePilot: ошибка запуска", size=20, weight=ft.FontWeight.BOLD,
                        color="#FF5050"),
                ft.Text("Скопируйте текст ниже и пришлите разработчику:", color="#FFFFFF"),
                ft.Text(tb, size=12, selectable=True, color="#FFD0D0"),
            ], scroll=ft.ScrollMode.AUTO, spacing=12))
            page.update()
        except Exception:
            pass


if __name__ == "__main__":
    ft.run(main)
