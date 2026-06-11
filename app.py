from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import tomllib
import chainlit as cl
import litellm
import psutil

from crewai import Crew, Task

# Из-за лимитов VRAM очистка GPU-памяти (cleanup_old_llm) при ротации моделей
from agents import build_role_llm, safe_kickoff, init_api_keys, cleanup_old_llm, install_secret_redaction, fetch_all_quotas
from manager_agents import make_manager_crew, parse_backlog
from context_manager import DatabaseManager
from pipeline_flow import run_pipeline_v2, run_image_crew
from utils import (
    atomic_write_text,
    InteractionHandler,
    SessionState,
    load_session,
    save_session,
)
from cleaner_flow import (
    run_cleaner_flow,
    action_clean_cache_deep, action_clean_orphans,
    action_clean_downloads, action_find_dups,
    action_cleaner_delete_safe, action_cleaner_delete_warn,
    action_cleaner_delete_all, action_cleaner_undo_last,
    action_cleaner_permanent_delete, action_cleaner_quarantine_status
)

os.environ['CREWAI_DISABLE_TELEMETRY'] = '1'
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
install_secret_redaction()  # редактируем секреты в логах root (покрывает litellm/crewai-трейсы)
logger = logging.getLogger('APP')

BT = chr(96) * 3
AUTO_DIR = './auto_tasks'
DONE_DIR = os.path.join(AUTO_DIR, 'done')
FAILED_DIR = os.path.join(AUTO_DIR, 'failed')

PROFILE_PIPELINE = "🛠 Конвейер"
PROFILE_MANAGER  = "📋 Менеджер"
PROFILE_CLEANER  = "🧹 AI Cleaner"

_daemon_pid: int | None = None
_daemon_lock: asyncio.Lock | None = None

_VALID_AGENT_PROFILES = [
    'Универсальный Senior Developer',
    'Python / Backend',
    'JavaScript / TypeScript / Frontend',
    'C++ / Системное программирование',
    'Java / Kotlin / Android',
    'Swift / iOS',
    'C# / .NET',
    'PHP / Web',
    'Go / Микросервисы',
]

_VALID_TASK_MODES = [
    'Универсальная задача (Автоопределение)',
    '1. Анализ и исправление',
    '2. Только анализ',
    '3. Создание тестов',
    '4. Поиск уязвимостей',
    '5. Оптимизация и рефакторинг',
    '6. Написание документации',
]

_BACKEND_DEFAULT_URLS: dict[str, str] = {
    "ollama":   "http://localhost:11434/v1",
    "lmstudio": "http://localhost:1234/v1",
    "llamacpp": "http://localhost:8080/v1",
    "lemonade": "http://localhost:8000/v1",
}

# Порядок согласно ТЗ
_LOCAL_BACKENDS: list[str] = ["lmstudio", "ollama", "llamacpp", "lemonade"]
_CLOUD_PROVIDERS: list[str] = ["groq", "openrouter", "cerebras", "sambanova", "huggingface", "cohere", "openai", "anthropic", "gemini", "mistral", "deepseek"]

# Метка для пустого Select, когда локальный бэкенд офлайн (никаких TextInput для моделей).
_OFFLINE_SENTINEL = "⚠️ бэкенд офлайн — запустите LM Studio/Ollama"

# Актуальные идентификаторы моделей (стандарты мая 2026).
CLOUD_MODELS: dict[str, list[str]] = {
    "groq": [
        "llama-3.3-70b-versatile",
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
        "llama-3.1-8b-instant",
    ],
    "openrouter": [
        # === Бесплатные (:free) — приоритет для бесплатного режима ===
        # Подтверждённый tool-calling (нужен агентам CrewAI), данные на май 2026:
        "deepseek/deepseek-chat-v3-0324:free",   # лучший баланс для агентов
        "qwen/qwen3-235b-a22b:free",             # MoE, Tools+Reasoning
        "meta-llama/llama-4-maverick:free",      # длинный контекст, vision
        "meta-llama/llama-4-scout:free",         # быстрый чат
        "deepseek/deepseek-r1-0528:free",        # рассуждения/математика
        "deepseek/deepseek-r1:free",             # код
        "openai/gpt-oss-120b:free",              # сильный для кода
        "meta-llama/llama-3.3-70b-instruct:free",
        "mistralai/mistral-7b-instruct:free",    # лёгкий фолбэк
        # === Авто-роутинг и платные (нужны кредиты) ===
        "auto",
        "anthropic/claude-opus-4-7",
        "anthropic/claude-sonnet-4-6",
        "openai/gpt-5.1",
        "google/gemini-3.5-flash",
        "deepseek/deepseek-chat",
        "qwen/qwen-max",
    ],
    "cerebras": [
        "llama-3.3-70b",
        "llama3.1-8b",
    ],
    "sambanova": [
        "llama-3.3-70b-instruct",
        "llama-3.1-8b-instruct",
    ],
    "huggingface": [
        "meta-llama/Llama-3.1-8B-Instruct",
    ],
    "cohere": [
        "command-r-plus",
        "command-r",
    ],
    "anthropic": [
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
    ],
    "openai": [
        "gpt-5.1",
        "gpt-5",
        "gpt-4o",
        "gpt-4o-mini",
    ],
    "gemini": [
        "gemini-3.5-flash",
        "gemini-3.1-pro-preview",
        "gemini-3.1-flash-lite",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
    ],
    "mistral": [
        "mistral-large-latest",
        "mistral-medium-latest",
        "mistral-small-latest",
        "codestral-latest",
        "open-mistral-nemo",
    ],
    "deepseek": [
        "deepseek-chat",
        "deepseek-reasoner",
    ],
}

def _url_from_backend(backend: str) -> str:
    return _BACKEND_DEFAULT_URLS.get(backend, _BACKEND_DEFAULT_URLS["lmstudio"])

def get_model_from_settings(state: SessionState):
    """LLM для профиля Менеджер и SD-промптов — берёт конфиг роли Архитектор."""
    return build_role_llm(state, "architect")

class ChainlitInteractionHandler(InteractionHandler):
    def ask_question(self, question: str, choices: list | None = None) -> str:
        async def _ask():
            if choices:
                actions = [cl.Action(name=c, payload={'value': c}, label=c) for c in choices]
                res = await cl.AskActionMessage(content=question, actions=actions, timeout=300).send()
                return res['payload'].get('value', choices[0]) if res and res.get('payload') else choices[0]
            res = await cl.AskUserMessage(content=question, timeout=300).send()
            return res['output'] if res else "Нет ответа"
        return cl.run_sync(_ask())

    def confirm_command(self, command: str, terminal: str) -> bool:
        async def _confirm():
            _salt = str(time.time())
            res = await cl.AskActionMessage(
                content=f'Выполнить в **{terminal}**:\n{BT}\n{command}\n{BT}\nРазрешить?',
                actions=[
                    cl.Action(name='y', payload={'value': 'y', 'salt': _salt}, label='✅ Разрешить'),
                    cl.Action(name='n', payload={'value': 'n', 'salt': _salt}, label='❌ Отклонить'),
                ],
                timeout=300,
            ).send()
            return bool(res and res.get('payload', {}).get('value') == 'y')
        return cl.run_sync(_confirm())

    def log_event(self, level: str, message: str) -> None:
        try:
            loop = asyncio.get_running_loop()
            asyncio.run_coroutine_threadsafe(
                cl.Message(content=f'[{level.upper()}] {message}').send(), loop
            )
        except RuntimeError:
            logging.getLogger('UI').log(
                getattr(logging, level.upper(), logging.INFO), message
            )

    async def confirm_patch(self, diff: str) -> bool:
        if not diff or diff.strip().startswith('// Нет изменений'):
            return False
        _salt = str(time.time())
        res = await cl.AskActionMessage(
            content=f'📄 **Предлагаемые изменения:**\n{BT}diff\n{diff}\n{BT}\nПрименить?',
            actions=[
                cl.Action(name='y', payload={'value': 'y', 'salt': _salt}, label='✅ Применить'),
                cl.Action(name='n', payload={'value': 'n', 'salt': _salt}, label='❌ Пропустить'),
            ],
            timeout=600,
        ).send()
        return bool(res and res.get('payload', {}).get('value') == 'y')

def _apply_debug_mode(enabled: bool) -> None:
    level = logging.DEBUG if enabled else logging.INFO
    logging.getLogger().setLevel(level)
    for name in ('APP', 'AGENTS', 'UTILS', 'ROUTER', 'DAEMON'):
        logging.getLogger(name).setLevel(level)
    # litellm.set_verbose устарел; используем актуальный API с фолбэком на старый.
    try:
        if enabled:
            litellm._turn_on_debug()
        else:
            logging.getLogger("LiteLLM").setLevel(logging.WARNING)
    except Exception:
        try: litellm.set_verbose = enabled
        except Exception: pass
    logging.getLogger('APP').debug('debug_mode переключён: %s', enabled)

def _get_safe_state() -> SessionState:
    kwargs = {}
    for k in SessionState.model_fields:
        val = cl.user_session.get(k)
        if val is not None:
            kwargs[k] = val
    return SessionState.model_validate(kwargs)

async def _show_cleaner_menu() -> None:
    import cleaner_tools as _ct
    quarantine_mb = 0.0
    try:
        _raw = await asyncio.to_thread(_ct.list_quarantine_sessions)
        _data = {}
        try:
            _data = json.loads(_raw) if isinstance(_raw, str) else (_raw or {})
        except Exception:
            import re
            _m = re.search(r'"total_mb"\s*:\s*([\d.]+)', _raw or "")
            if _m:
                quarantine_mb = float(_m.group(1))
        if isinstance(_data, dict):
            _sessions = _data.get("sessions", [])
            quarantine_mb = _data.get("total_mb") or sum(s.get("size_mb", 0) for s in _sessions)
    except Exception:
        quarantine_mb = 0.0
    freed_gb = quarantine_mb / 1024.0
    salt = str(time.time())
    actions = [
        cl.Action(name='clean_cache_deep',       label='🧹 Глубокий кэш',  payload={'value': 'cache_deep', 'salt': salt}),
        cl.Action(name='clean_orphans',           label='👻 Остатки программ',payload={'value': 'orphans', 'salt': salt}),
        cl.Action(name='clean_downloads',         label='📥 Анализ загрузок', payload={'value': 'downloads', 'salt': salt}),
        cl.Action(name='find_dups',               label='👯 Найти дубликаты', payload={'value': 'dups', 'salt': salt}),
        cl.Action(name='cleaner_quarantine_status', label='🗃 Статус карантина',payload={'value': 'quarantine', 'salt': salt}),
    ]
    await cl.Message(
        content=f'🧹 **AI Cleaner** — выберите действие:\n\n'
                f'📉 Освобождено за всё время: **{freed_gb:.2f} ГБ**',
        actions=actions,
    ).send()

def _make_cleaner_callback(action_fn):
    async def _handler(action: cl.Action) -> None:
        try:
            await action.remove()
        except Exception:
            pass
        try:
            await action_fn(action)
        finally:
            await _show_cleaner_menu()
    return _handler

_CLEANER_ACTIONS: dict[str, object] = {
    'clean_cache_deep':        action_clean_cache_deep,
    'clean_orphans':           action_clean_orphans,
    'clean_downloads':         action_clean_downloads,
    'find_dups':               action_find_dups,
    'cleaner_delete_safe':     action_cleaner_delete_safe,
    'cleaner_delete_warn':     action_cleaner_delete_warn,
    'cleaner_delete_all':      action_cleaner_delete_all,
    'cleaner_undo_last':       action_cleaner_undo_last,
    'cleaner_permanent_delete': action_cleaner_permanent_delete,
    'cleaner_quarantine_status': action_cleaner_quarantine_status,
}

for _action_name, _action_fn in _CLEANER_ACTIONS.items():
    cl.action_callback(_action_name)(_make_cleaner_callback(_action_fn))

@cl.set_chat_profiles
async def set_profile():
    return [
        cl.ChatProfile(name=PROFILE_PIPELINE, markdown_description="Интерактивная работа с агентами."),
        cl.ChatProfile(name=PROFILE_MANAGER, markdown_description="Автономная генерация задач для Демона."),
        cl.ChatProfile(name=PROFILE_CLEANER, markdown_description="Анализ и безопасная очистка системы."),
    ]

def _fetch_ollama_models(base_url: str = "http://localhost:11434") -> list[str]:
    try:
        import requests as _req
        r = _req.get(f"{base_url}/api/tags", timeout=2)
        if r.status_code == 200:
            models = [m["name"] for m in r.json().get("models", [])]
            return models if models else []
    except Exception:
        pass
    return []

def _fetch_openai_models(base_url: str) -> list[str]:
    try:
        import requests as _req
        url = base_url.rstrip("/") + "/models"
        r = _req.get(url, timeout=2)
        if r.status_code == 200:
            models = [m["id"] for m in r.json().get("data", [])]
            return models if models else []
    except Exception:
        pass
    return []

def _fetch_local_models_for(backend: str, base_url: str | None = None) -> list[str]:
    """Список моделей конкретного бэкенда. base_url — опциональный override
    (иначе берётся дефолт бэкенда)."""
    url = (base_url or "").strip() or _BACKEND_DEFAULT_URLS.get(backend, _BACKEND_DEFAULT_URLS["lmstudio"])
    if backend == "ollama":
        ollama_root = url.replace("/v1", "").rstrip("/") or "http://localhost:11434"
        return _fetch_ollama_models(ollama_root)
    return _fetch_openai_models(url)

def _fetch_local_models(state) -> list[str]:
    """Модели глобально выбранного бэкенда (для виджета SD-промптов)."""
    return _fetch_local_models_for(getattr(state, "local_backend", "lmstudio"),
                                   getattr(state, "local_base_url", None))

def _backend_url_for(state, backend: str) -> str:
    """URL для бэкенда роли: override из local_base_url применяется только если
    бэкенд роли совпадает с глобально выбранным."""
    if backend == getattr(state, "local_backend", "lmstudio"):
        ov = (getattr(state, "local_base_url", None) or "").strip()
        if ov:
            return ov
    return _BACKEND_DEFAULT_URLS.get(backend, _BACKEND_DEFAULT_URLS["lmstudio"])

_FORGE_MODELS_DIR_DEFAULT = r"C:\Forge\webui\models\Stable-diffusion"
_FORGE_MODEL_EXTS = {".gguf", ".safetensors", ".ckpt", ".pt"}

def _fetch_forge_models(models_dir: str | None = None) -> list[str]:
    directory = models_dir or os.environ.get("FORGE_MODELS_DIR", "") or _FORGE_MODELS_DIR_DEFAULT
    try:
        from pathlib import Path
        p = Path(directory)
        if not p.is_dir():
            return []
        return sorted(f.name for f in p.iterdir() if f.is_file() and f.suffix.lower() in _FORGE_MODEL_EXTS)
    except Exception:
        return []

def _model_select(widget_id: str, label: str, values: list[str], current: str) -> "cl.input_widget.Select":
    """Select для модели. Если список пуст (бэкенд офлайн) — Select с единственной
    меткой-заглушкой (никаких TextInput для моделей)."""
    if not values:
        return cl.input_widget.Select(id=widget_id, label=label, values=[_OFFLINE_SENTINEL], initial_value=_OFFLINE_SENTINEL)
    initial = current if current in values else values[0]
    return cl.input_widget.Select(id=widget_id, label=label, values=values, initial_value=initial)

def _build_image_model_widget(state, local_models: list[str]) -> list:
    return [_model_select("model_image_prompt", "🎨 Модель для SD-промптов",
                          local_models, getattr(state, "model_image_prompt", "") or "")]

def _build_forge_model_widget(state) -> list:
    models_dir = os.environ.get("FORGE_MODELS_DIR", "") or None
    models = _fetch_forge_models(models_dir)
    current = getattr(state, "forge_model", "") or ""
    if not models:
        return [cl.input_widget.Select(id="forge_model", label="🖼 Модель SD Forge",
                                       values=[_OFFLINE_SENTINEL], initial_value=_OFFLINE_SENTINEL)]
    initial = current if current in models else models[0]
    return [cl.input_widget.Select(id="forge_model", label="🖼 Модель SD Forge", values=models, initial_value=initial)]

@cl.on_chat_start
async def start():
    try:
        secret_path = '.chainlit/secrets.toml' if os.path.exists('.chainlit/secrets.toml') else 'secrets.toml'
        if os.path.exists(secret_path):
            with open(secret_path, 'rb') as f:
                data = tomllib.load(f)
                keys_data = data.get('PROVIDER_KEYS', data)
                init_api_keys(keys_data)
    except Exception as e:
        logger.error(f"Ошибка загрузки secrets.toml: {e}")

    for d in (AUTO_DIR, DONE_DIR, FAILED_DIR):
        os.makedirs(d, exist_ok=True)

    db = DatabaseManager()
    cl.user_session.set('db', db)

    state = load_session() or SessionState()
    if state.debug_mode:
        _apply_debug_mode(True)
    state.local_base_url = _url_from_backend(getattr(state, 'local_backend', 'lmstudio'))
    for k, v in state.model_dump().items():
        cl.user_session.set(k, v)

    widgets = await asyncio.to_thread(_build_settings_widgets, state)
    await cl.ChatSettings(widgets).send()

    profile = cl.user_session.get('chat_profile')

    if profile == PROFILE_PIPELINE:
        _pq_salt = str(time.time())
        await cl.Message(content='⚙️ **Конвейер готов.** Ожидаю задачу по коду.',
                         actions=[cl.Action(name='show_quotas', label='📊 Квоты', payload={'value': 'quotas', 'salt': _pq_salt})]).send()
    elif profile == PROFILE_CLEANER:
        await _show_cleaner_menu()
    else:
        stats_msg = ''
        if hasattr(db, 'get_manager_stats'):
            stats = db.get_manager_stats()
            if stats:
                stats_msg = '\n\n📊 **Статистика Демона:**\n' + '\n'.join(f'- {k}: {v}' for k, v in stats.items())
        _start_salt = str(time.time())
        actions = [
            cl.Action(name='start_daemon', label='🚀 Запустить Демона', payload={'value': 'start', 'salt': _start_salt}),
            cl.Action(name='stop_daemon',  label='🛑 Остановить Демона', payload={'value': 'stop',  'salt': _start_salt}),
            cl.Action(name='show_quotas',  label='📊 Квоты', payload={'value': 'quotas', 'salt': _start_salt}),
        ]
        await cl.Message(content=f'📋 **Менеджер активен.**\nВведите глобальную цель.{stats_msg}', actions=actions).send()

_ROLE_META: list[tuple[str, str, str]] = [
    ("gatherer",  "🔍", "Сборщик"),
    ("architect", "🏛", "Архитектор"),
    ("coder",     "💻", "Кодер"),
    ("auditor",   "🔬", "Аудитор"),
    ("oracle",    "🔮", "Оракул"),
]

def _build_settings_widgets(state) -> list:
    # Кэш моделей по бэкенду, чтобы не опрашивать один и тот же сервер несколько раз.
    _local_cache: dict[str, list[str]] = {}
    def _local_models(backend: str) -> list[str]:
        if backend not in _local_cache:
            _local_cache[backend] = _fetch_local_models_for(backend, _backend_url_for(state, backend))
        return _local_cache[backend]

    def _build_role_cascade(role: str, emoji: str, label: str) -> list:
        mode = getattr(state, f"mode_{role}", "local")
        model = getattr(state, f"model_{role}", "") or ""
        widgets = [
            cl.input_widget.Select(
                id=f"mode_{role}", label=f"{emoji} {label}: источник",
                values=["local", "cloud"], initial_value=mode if mode in ("local", "cloud") else "local",
            )
        ]
        if mode == "cloud":
            provider = getattr(state, f"provider_{role}", "gemini")
            if provider not in _CLOUD_PROVIDERS: provider = _CLOUD_PROVIDERS[0]
            widgets.append(cl.input_widget.Select(
                id=f"provider_{role}", label=f"{emoji} {label}: провайдер",
                values=_CLOUD_PROVIDERS, initial_value=provider,
            ))
            widgets.append(_model_select(f"model_{role}", f"{emoji} {label}: модель",
                                         CLOUD_MODELS.get(provider, []), model))
        else:
            backend = getattr(state, f"backend_{role}", "lmstudio")
            if backend not in _LOCAL_BACKENDS: backend = _LOCAL_BACKENDS[0]
            widgets.append(cl.input_widget.Select(
                id=f"backend_{role}", label=f"{emoji} {label}: бэкенд",
                values=_LOCAL_BACKENDS, initial_value=backend,
            ))
            widgets.append(_model_select(f"model_{role}", f"{emoji} {label}: модель",
                                         _local_models(backend), model))
        return widgets

    role_widgets: list = []
    for role, emoji, label in _ROLE_META:
        role_widgets.extend(_build_role_cascade(role, emoji, label))

    image_models = _local_models(getattr(state, "local_backend", "lmstudio"))

    return [
        # === Каскад 5 ролей — В САМОМ ВЕРХУ ===
        *role_widgets,
        # === Общие настройки ===
        cl.input_widget.Switch(id='oracle_enabled', label='🔮 Мастер-Оракул включён', initial=getattr(state, 'oracle_enabled', True)),
        cl.input_widget.Switch(id='vram_unload_between_agents', label='🧹 Выгружать модель из VRAM между агентами (для 8 ГБ)', initial=getattr(state, 'vram_unload_between_agents', False)),
        cl.input_widget.Switch(id='dup_full_hash', label='🔐 Полный хэш дубликатов (надёжнее)', initial=getattr(state, 'dup_full_hash', True)),
        cl.input_widget.Switch(id='quarantine_same_drive', label='💽 Карантин на диске источника', initial=getattr(state, 'quarantine_same_drive', True)),
        cl.input_widget.Select(id='local_backend', label='🖥 Локальный бэкенд (по умолч. / SD)', values=_LOCAL_BACKENDS, initial_value=getattr(state, 'local_backend', 'lmstudio') if getattr(state, 'local_backend', 'lmstudio') in _LOCAL_BACKENDS else 'lmstudio'),
        cl.input_widget.TextInput(id='local_base_url', label='🔌 URL локального бэкенда', initial=state.local_base_url),
        cl.input_widget.TextInput(id="forge_url", label="🖼 Forge API URL", initial=state.forge_url),
        cl.input_widget.Select(id='task_mode', label='🎯 Режим задачи', values=_VALID_TASK_MODES, initial_value=state.task_mode if state.task_mode in _VALID_TASK_MODES else 'Универсальная задача (Автоопределение)'),
        cl.input_widget.Select(id='agent_profile', label='🧑‍💻 Стек агентов', values=_VALID_AGENT_PROFILES, initial_value=state.agent_profile if state.agent_profile in _VALID_AGENT_PROFILES else 'Универсальный Senior Developer'),
        cl.input_widget.TextInput(id='project_path', label='📁 Путь к проекту', initial=state.project_path),
        cl.input_widget.Switch(id='auto_apply', label='💾 Авто-применение патчей', initial=state.auto_apply),
        cl.input_widget.Switch(id='strict_sandbox', label='🔒 Строгая песочница', initial=state.strict_sandbox),
        cl.input_widget.Select(id='speed', label='🚀 Скорость / качество', values=['fast', 'medium', 'slow'], initial_value=state.speed),
        *_build_image_model_widget(state, image_models),
        cl.input_widget.Switch(id='web_search_enabled', label='🌐 Доступ в интернет', initial=getattr(state, 'web_search_enabled', False)),
        *_build_forge_model_widget(state),
        cl.input_widget.Slider(id='ui_max_iter', label='🔁 Макс. итераций агентов', initial=state.ui_max_iter, min=1, max=30, step=1),
        cl.input_widget.Slider(id='ui_max_rpm', label='⏱ Макс. запросов/мин (RPM)', initial=state.ui_max_rpm, min=1, max=60, step=1),
        cl.input_widget.Slider(id='ui_file_limit_kb', label='📄 Макс. размер файла (КБ)', initial=state.ui_file_limit_kb, min=100, max=5000, step=100),
        cl.input_widget.Slider(id='max_tool_output_chars', label='📤 Лимит вывода инструментов', initial=state.max_tool_output_chars, min=1000, max=50000, step=1000),
        cl.input_widget.Slider(id='ui_step_timeout', label='⏳ Таймаут шага агента (сек)', initial=getattr(state, 'ui_step_timeout', 300), min=60, max=1800, step=30),
        cl.input_widget.Slider(id='ui_tree_limit', label='🌳 Лимит файлов дерева проекта', initial=state.ui_tree_limit, min=100, max=2000, step=100),
        cl.input_widget.Switch(id='debug_mode', label='🐛 Режим отладки', initial=state.debug_mode),
    ]

_ROLES = ("gatherer", "architect", "coder", "auditor", "oracle")
_VRAM_MODEL_KEYS = tuple(f"model_{r}" for r in _ROLES) + ("model_image_prompt",)

def _role_available_models(state, role: str) -> list[str]:
    """Список моделей для роли в её ТЕКУЩЕМ (уже применённом в state) режиме."""
    if getattr(state, f"mode_{role}", "local") == "cloud":
        return CLOUD_MODELS.get(getattr(state, f"provider_{role}", "gemini"), [])
    backend = getattr(state, f"backend_{role}", "lmstudio")
    return _fetch_local_models_for(backend, _backend_url_for(state, backend))

@cl.on_settings_update
async def on_settings_update(settings: dict):
    state = _get_safe_state()

    # VRAM: перед сменой локальной модели на Ollama-бэкенде выгружаем СТАРЫЕ модели
    # (state ещё хранит прежние значения), чтобы на 8 ГБ не держать две сразу.
    old_ollama = [
        getattr(state, f"model_{r}", "")
        for r in _ROLES
        if getattr(state, f"mode_{r}", "local") == "local"
        and getattr(state, f"backend_{r}", "lmstudio") == "ollama"
        and getattr(state, f"model_{r}", "")
    ]
    model_changed = any(k in settings and settings.get(k) != getattr(state, k, None) for k in _VRAM_MODEL_KEYS)
    if old_ollama and model_changed:
        try:
            await asyncio.to_thread(cleanup_old_llm, state, old_ollama)
            logger.info('🧹 VRAM: выгружены старые Ollama-модели перед сменой.')
        except Exception as e:
            logger.debug('cleanup_old_llm не сработал: %s', e)

    # Применяем настройки. Значение-заглушка офлайн-бэкенда не сохраняем как модель.
    for k, v in settings.items():
        if v is None: v = ''
        if k.startswith('model_') and v == _OFFLINE_SENTINEL:
            v = ''
        if k == 'project_path' and isinstance(v, str) and v.strip():
            from pathlib import Path as _P
            try: v = str(_P(v.strip()))
            except Exception: pass
        cl.user_session.set(k, v)
        if hasattr(state, k): setattr(state, k, v)

    # Валидация перечислений и каскадная коррекция модели роли при смене
    # источника / бэкенда / провайдера.
    for role in _ROLES:
        if getattr(state, f"backend_{role}", "lmstudio") not in _LOCAL_BACKENDS:
            setattr(state, f"backend_{role}", _LOCAL_BACKENDS[0]); cl.user_session.set(f"backend_{role}", _LOCAL_BACKENDS[0])
        if getattr(state, f"provider_{role}", "gemini") not in _CLOUD_PROVIDERS:
            setattr(state, f"provider_{role}", _CLOUD_PROVIDERS[0]); cl.user_session.set(f"provider_{role}", _CLOUD_PROVIDERS[0])

        cascade_changed = any(k in settings for k in (f"mode_{role}", f"backend_{role}", f"provider_{role}"))
        if cascade_changed:
            available = await asyncio.to_thread(_role_available_models, state, role)
            current = getattr(state, f"model_{role}", "")
            if available and current not in available:
                setattr(state, f"model_{role}", available[0])
                cl.user_session.set(f"model_{role}", available[0])

    if 'local_backend' in settings:
        custom_url = (settings.get('local_base_url') or '').strip()
        state.local_base_url = custom_url or _url_from_backend(settings['local_backend'])
        cl.user_session.set('local_base_url', state.local_base_url)

    if 'debug_mode' in settings: _apply_debug_mode(bool(settings['debug_mode']))
    save_session(state)
    widgets = await asyncio.to_thread(_build_settings_widgets, state)
    await cl.ChatSettings(widgets).send()
    await cl.Message(content='✅ Настройки сохранены.').send()

@cl.action_callback('start_daemon')
async def on_daemon_start(action: cl.Action):
    global _daemon_pid, _daemon_lock
    if _daemon_lock is None: _daemon_lock = asyncio.Lock()
    async with _daemon_lock:
        pid_file = os.path.join(AUTO_DIR, '.daemon.pid')
        if os.path.exists(pid_file):
            try:
                with open(pid_file, 'r') as f:
                    old_pid = int(f.read().strip())
                if psutil.pid_exists(old_pid):
                    return await cl.Message(content=f'⚠️ Демон уже работает (PID: `{old_pid}`).').send()
                os.remove(pid_file)
            except: pass
        if os.name == 'nt':
            proc = subprocess.Popen([sys.executable, 'daemon.py'], cwd=os.getcwd(), creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            proc = subprocess.Popen([sys.executable, 'daemon.py'], cwd=os.getcwd())
        _daemon_pid = proc.pid
        import threading
        threading.Thread(target=proc.wait, daemon=True).start()
        await cl.Message(content=f'✅ Фоновый демон запущен (PID: `{proc.pid}`).').send()

@cl.action_callback('stop_daemon')
async def on_daemon_stop(action: cl.Action):
    pid_file = os.path.join(AUTO_DIR, '.daemon.pid')
    if not os.path.exists(pid_file):
        return await cl.Message(content='ℹ️ Демон не запущен.').send()
    try:
        with open(pid_file, 'r') as f: pid = int(f.read().strip())
        if psutil.pid_exists(pid):
            if os.name == 'nt':
                subprocess.call(['taskkill', '/F', '/PID', str(pid)])
            else:
                import signal
                os.kill(pid, signal.SIGTERM)
            await cl.Message(content=f'🛑 Сигнал остановки отправлен демону (PID: `{pid}`).').send()
        else:
            os.remove(pid_file)
            await cl.Message(content='ℹ️ Демон уже не работает.').send()
    except Exception as e:
        await cl.Message(content=f'❌ Ошибка: {e}').send()

def _fmt_quota(q: dict) -> str:
    """Форматирует живую квоту провайдера. Никаких зашитых лимитов — только то,
    что вернул API/заголовки."""
    p = q.get("provider", "?")
    status = q.get("status")
    keys = q.get("keys", 0)
    head = f"**{p}** ({keys} ключ.)"
    if status == "no_key":
        return f"{head}: ключ не задан"
    if status == "unreachable":
        return f"{head}: ⚠️ API недоступен"
    if status == "unknown":
        return f"{head}: лимиты появятся после первого запроса"
    # OpenRouter (source=api)
    if q.get("source") == "api":
        parts = []
        if q.get("is_free_tier") is not None:
            parts.append("free-tier" if q["is_free_tier"] else "платный")
        if q.get("remaining") is not None:
            lim = q.get("limit")
            parts.append(f"остаток кредитов: {q['remaining']}" + (f" из {lim}" if lim is not None else " (безлимит по балансу)"))
        rl = q.get("rate_limit")
        if isinstance(rl, dict) and rl.get("requests"):
            parts.append(f"rate: {rl.get('requests')}/{rl.get('interval','?')}")
        return f"{head}: " + ", ".join(parts) if parts else f"{head}: данные получены"
    # Заголовки x-ratelimit-* (Groq/OpenAI и др.)
    parts = []
    if "remaining_requests" in q: parts.append(f"запросов осталось: {q['remaining_requests']}" + (f"/{q['limit_requests']}" if 'limit_requests' in q else ""))
    if "remaining_tokens" in q:   parts.append(f"токенов осталось: {q['remaining_tokens']}" + (f"/{q['limit_tokens']}" if 'limit_tokens' in q else ""))
    if "reset" in q:              parts.append(f"сброс: {q['reset']}")
    return f"{head}: " + (", ".join(parts) if parts else "данные получены")

@cl.action_callback('show_quotas')
async def on_show_quotas(action: cl.Action):
    async with cl.Step(name='📊 Опрос квот провайдеров') as step:
        quotas = await asyncio.to_thread(fetch_all_quotas)
    if not quotas:
        return await cl.Message(content='ℹ️ Нет настроенных облачных ключей. Добавьте их в `secrets.toml`.').send()
    lines = [_fmt_quota(q) for q in quotas]
    body = '📊 **Остаток квот (в реальном времени):**\n\n' + '\n'.join(f'- {l}' for l in lines)
    body += '\n\n_Значения берутся из API провайдеров / заголовков ответов — без зашитых лимитов._'
    _salt = str(time.time())
    await cl.Message(content=body, actions=[cl.Action(name='show_quotas', label='🔄 Обновить', payload={'value': 'refresh', 'salt': _salt})]).send()

_IMAGE_KEYWORDS = {"нарисуй", "нарисовать", "сгенерируй", "сделай арт"}

@cl.on_message
async def on_message(msg: cl.Message):
    profile = cl.user_session.get('chat_profile')
    if cl.user_session.get('is_running'):
        return await cl.Message(content='⏳ Система занята.').send()
    cl.user_session.set('is_running', True)
    from utils import extract_agent_reasoning
    msg.content, _ = extract_agent_reasoning(msg.content)
    try:
        if profile == PROFILE_PIPELINE:
            await run_pipeline_v2(msg)
        elif profile == PROFILE_CLEANER:
            await run_cleaner_flow(msg.content)
            await _show_cleaner_menu()
        else:
            state = _get_safe_state()
            if any(kw in msg.content.lower() for kw in _IMAGE_KEYWORDS):
                llm = get_model_from_settings(state)
                await run_image_crew(msg.content, state, llm)
                return
            await _run_manager(msg)
    finally:
        cl.user_session.set('is_running', False)

async def _run_manager(msg: cl.Message) -> None:
    state = _get_safe_state()
    llm = get_model_from_settings(state)
    db = cl.user_session.get("db")
    
    async with cl.Step(name="🔍 Product Owner") as step_po:
        product_owner, scrum_master = make_manager_crew(state, llm)
        po_task = Task(description=f"Запрос:\n{msg.content}\nИзучи проект.", agent=product_owner, expected_output="Описание концепции")
        await asyncio.to_thread(safe_kickoff, Crew(agents=[product_owner], tasks=[po_task]), state)
        concept = getattr(po_task.output, "raw", None) or f"Запрос: {msg.content}"
        await step_po.stream_token(concept)

    async with cl.Step(name="📋 Scrum Master") as step_sm:
        sm_task = Task(description=f"Концепция:\n{concept}\nСформируй JSON-бэклог.", agent=scrum_master, expected_output="JSON-массив задач")
        await asyncio.to_thread(safe_kickoff, Crew(agents=[scrum_master], tasks=[sm_task]), state)
        raw_json = getattr(sm_task.output, "raw", None) or ""
        await step_sm.stream_token(raw_json)

    backlog = parse_backlog(raw_json)
    if not backlog:
        return await cl.Message(content="⚠️ Ошибка генерации бэклога.").send()

    os.makedirs(AUTO_DIR, exist_ok=True)
    existing_files = set(os.listdir(AUTO_DIR))
    created = 0
    for item in backlog:
        task_hash = hashlib.md5((item.get("title", "") + item.get("description", "")).encode("utf-8")).hexdigest()[:8]
        _safe_tid = re.sub(r"[^A-Za-z0-9_-]", "", str(item.get("task_id", "0")))[:24] or "0"
        safe_name = f"task_{int(time.time())}_{_safe_tid}_{task_hash}.json"
        if any(task_hash in f for f in existing_files): continue
        item["_source_request"] = msg.content[:200]
        item["_created_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        try:
            atomic_write_text(os.path.join(AUTO_DIR, safe_name), json.dumps(item, ensure_ascii=False, indent=2))
            created += 1
            if hasattr(db, "add_manager_task"): db.add_manager_task(safe_name, "queued")
        except Exception: pass
    await cl.Message(content=f"✅ Бэклог сформирован: **{created}** задач.").send()
