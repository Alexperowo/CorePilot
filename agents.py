import os
import time
import re
import random
import logging
import threading
from crewai import Crew, LLM
from typing import Optional
from utils import SessionState

_SPEED_TEMPERATURE: dict[str, float] = {
    'fast':   0.35,
    'medium': 0.1,
    'slow':   0.0,
}

BACKEND_CONFIGS: dict[str, dict] = {
    "ollama":   {"base_url": "http://localhost:11434/v1", "api_key": "ollama",    "prefix": "ollama/"},
    "lmstudio": {"base_url": "http://localhost:1234/v1",  "api_key": "lm-studio", "prefix": "openai/"},
    "llamacpp": {"base_url": "http://localhost:8080/v1",  "api_key": "none",      "prefix": "openai/"},
    "lemonade": {"base_url": "http://localhost:8000/v1",  "api_key": "lemonade",  "prefix": "openai/"},
}
_LOCAL_LLM_PREFIXES: frozenset[str] = frozenset(c['prefix'] for c in BACKEND_CONFIGS.values())

logger = logging.getLogger("AGENTS")

class _RedactSecretsFilter(logging.Filter):
    _PATTERN = re.compile(
        r'\b(sk-[A-Za-z0-9]{20,}|gsk_[A-Za-z0-9]{20,}|AIzaSy[A-Za-z0-9_-]{33}|'
        r'[A-Za-z0-9]{32,64}(?=[\'"\s,\]])|eyJ[A-Za-z0-9_-]{20,})\b'
    )
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._PATTERN.sub(lambda m: m.group()[:4] + "****" + m.group()[-4:], str(record.msg))
        return True

logging.getLogger("AGENTS").addFilter(_RedactSecretsFilter())
logging.getLogger("litellm").addFilter(_RedactSecretsFilter())

def install_secret_redaction() -> None:
    """Навешивает редактор секретов на root-логгер и его хендлеры. Вызывать ПОСЛЕ
    logging.basicConfig в app.py и daemon.py, чтобы ключи не утекали в трейсы."""
    root = logging.getLogger()
    if not any(isinstance(f, _RedactSecretsFilter) for f in root.filters):
        root.addFilter(_RedactSecretsFilter())
    # Фильтр на логгере не всегда применяется к записям дочерних логгеров,
    # поэтому дублируем на хендлерах root (через них проходят все записи).
    for h in root.handlers:
        if not any(isinstance(f, _RedactSecretsFilter) for f in h.filters):
            h.addFilter(_RedactSecretsFilter())

def install_cache_breakpoint_fix() -> None:
    """Обходит баг CrewAI 1.14 (#5886): CrewAI вставляет 'cache_breakpoint' в
    сообщения для НЕ-Anthropic провайдеров (Groq, OpenAI-совместимые, и т.п.), а они
    его отвергают: `property 'cache_breakpoint' is unsupported`. Чистим этот ключ из
    messages на уровне litellm.completion для всех вызовов, кроме anthropic/claude
    (там он легитимен). Идемпотентно."""
    try:
        import litellm
    except Exception:
        return
    if getattr(litellm, "_corepilot_cb_fix", False):
        return
    _orig = litellm.completion

    def _clean(messages, model: str):
        low = (model or "").lower()
        if "anthropic" in low or "claude" in low:
            return messages  # Anthropic реально поддерживает cache_control/breakpoint
        cleaned = []
        for m in (messages or []):
            if isinstance(m, dict) and "cache_breakpoint" in m:
                m = {k: v for k, v in m.items() if k != "cache_breakpoint"}
            cleaned.append(m)
        return cleaned

    def _patched(*args, **kwargs):
        try:
            if "messages" in kwargs:
                kwargs["messages"] = _clean(kwargs["messages"], kwargs.get("model", ""))
        except Exception:
            pass
        return _orig(*args, **kwargs)

    try:
        litellm.completion = _patched
        litellm._corepilot_cb_fix = True
    except Exception:
        pass

def safe_get_output(task_output) -> str:
    if hasattr(task_output, "raw") and task_output.raw: return task_output.raw
    if hasattr(task_output, "output") and task_output.output: return task_output.output
    if hasattr(task_output, "result") and task_output.result: return task_output.result
    return str(task_output)

API_KEYS: dict[str, list[str]] = {}
KEY_COUNTERS: dict[str, int] = {}
_key_lock = threading.RLock()  # реентерабельный: next_api_key может внутри звать
# _lazy_reload_keys -> init_api_keys, которые тоже берут этот лок. С обычным
# Lock это давало дедлок (облачные списки моделей висли на 20с таймаут).

def init_api_keys(secrets: dict) -> None:
    global API_KEYS, KEY_COUNTERS
    # Синонимы имён ключей в secrets.toml: пользователи пишут по-разному
    # (OPENROUTER, OPEN_ROUTER_API_KEY, GOOGLE_API_KEY для gemini и т.п.).
    # Принимаем любой из вариантов, чтобы ключ не «терялся» из-за имени.
    _ALIASES = {
        "gemini": ["GEMINI_KEYS", "GEMINI_API_KEYS", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                   "GOOGLE_API_KEYS", "GOOGLE_KEYS", "GEMINI", "GOOGLE_GEMINI_API_KEY"],
        "openrouter": ["OPENROUTER_KEYS", "OPENROUTER_API_KEYS", "OPENROUTER_API_KEY",
                       "OPEN_ROUTER_API_KEY", "OPEN_ROUTER_API_KEYS", "OPEN_ROUTER_KEYS",
                       "OPENROUTER", "OPENROUTER_KEY"],
        "groq": ["GROQ_KEYS", "GROQ_API_KEYS", "GROQ_API_KEY", "GROQ"],
        "openai": ["OPENAI_KEYS", "OPENAI_API_KEYS", "OPENAI_API_KEY", "OPENAI"],
        "anthropic": ["ANTHROPIC_KEYS", "ANTHROPIC_API_KEYS", "ANTHROPIC_API_KEY",
                      "CLAUDE_API_KEY", "ANTHROPIC"],
        "mistral": ["MISTRAL_KEYS", "MISTRAL_API_KEYS", "MISTRAL_API_KEY", "MISTRAL"],
        "deepseek": ["DEEPSEEK_KEYS", "DEEPSEEK_API_KEYS", "DEEPSEEK_API_KEY", "DEEPSEEK"],
        "cerebras": ["CEREBRAS_KEYS", "CEREBRAS_API_KEYS", "CEREBRAS_API_KEY", "CEREBRAS"],
        "sambanova": ["SAMBANOVA_KEYS", "SAMBANOVA_API_KEYS", "SAMBANOVA_API_KEY",
                      "SAMBANOVA", "SAMBA_NOVA_API_KEY"],
        "huggingface": ["HUGGINGFACE_KEYS", "HUGGINGFACE_API_KEYS", "HUGGINGFACE_API_KEY",
                        "HF_TOKEN", "HF_API_KEY", "HF_API_TOKEN", "HUGGINGFACE",
                        "HUGGING_FACE_API_KEY", "HUGGINGFACEHUB_API_TOKEN"],
        "cohere": ["COHERE_KEYS", "COHERE_API_KEYS", "COHERE_API_KEY", "COHERE", "CO_API_KEY"],
    }
    # секреты ищем без учёта регистра и пробелов в именах
    norm = {str(k).strip().upper(): v for k, v in secrets.items()}
    with _key_lock:
        for provider in ["gemini", "mistral", "deepseek", "groq", "openrouter",
                         "openai", "anthropic", "cerebras", "sambanova", "huggingface", "cohere"]:
            raw = None
            for alias in _ALIASES[provider]:
                if norm.get(alias):
                    raw = norm[alias]
                    break
            # Умный фолбэк: имя записано нестандартно (напр. OPENROUTER_TOKENS) —
            # берём любую непустую строку секретов, начинающуюся с имени провайдера.
            if not raw:
                pfx = provider.upper()
                for k, v in norm.items():
                    if v and k.startswith(pfx):
                        raw = v
                        break
            if not raw: continue
            
            if isinstance(raw, list):
                keys = []
                for item in raw:
                    keys.extend(k.strip() for k in str(item).split(',') if k.strip())
            else:
                keys = [k.strip() for k in str(raw).split(',') if k.strip()]
            
            if keys:
                API_KEYS[provider] = keys
                os.environ[f"{provider.upper()}_API_KEY"] = keys[0]
                logger.info("🔑 %s: загружено %d ключ(ей).", provider, len(keys))
        KEY_COUNTERS = {k: 0 for k in API_KEYS}
    _install_litellm_quota_hook()
    install_cache_breakpoint_fix()  # обход бага CrewAI #5886 (Groq/OpenAI-совмест.)

_SECRETS_CANDIDATES = ('.chainlit/secrets.toml', 'secrets.toml')

def _lazy_reload_keys() -> None:
    import tomllib
    for path in _SECRETS_CANDIDATES:
        if os.path.exists(path):
            try:
                with open(path, 'rb') as fh: data = tomllib.load(fh)
                init_api_keys(data.get('PROVIDER_KEYS', data))
                return
            except Exception as exc:
                logger.warning("Lazy reload failed: %s", exc)

def next_api_key(provider: str) -> Optional[str]:
    with _key_lock:
        if not API_KEYS.get(provider): _lazy_reload_keys()
        keys = API_KEYS.get(provider, [])
        if not keys: return None
        idx = KEY_COUNTERS.get(provider, 0)
        KEY_COUNTERS[provider] = (idx + 1) % len(keys)
        return keys[idx]

def peek_api_key(provider: str) -> Optional[str]:
    """Возвращает ТЕКУЩИЙ ключ провайдера БЕЗ сдвига счётчика ротации. Для операций
    чтения (списки моделей), которые не должны влиять на балансировку ключей —
    иначе открытие Настроек/«Обновить списки» зря крутит счётчик (совет ревью)."""
    with _key_lock:
        if not API_KEYS.get(provider): _lazy_reload_keys()
        keys = API_KEYS.get(provider, [])
        if not keys: return None
        return keys[KEY_COUNTERS.get(provider, 0) % len(keys)]

def get_cloud_llm(provider: str, model_name: str) -> Optional[LLM]:
    api_key = next_api_key(provider)
    if not api_key: return None
    # OpenRouter: 'free'/'auto'/пусто — это не имя модели. Правильное авто-имя,
    # которое само перебирает доступные модели — 'openrouter/auto'. Иначе запрос
    # к несуществующей модели висит/падает (частая причина зависания Аудитора).
    if provider == "openrouter" and (not model_name or
                                     model_name.strip().lower() in ("free", "auto", "openrouter/auto")):
        model_name = "auto"
    # timeout ОБЯЗАТЕЛЕН: без него зависший сетевой вызов держит поток Конвейера
    # бесконечно (спиннер крутится, ошибки нет). 120с — потолок на один запрос.
    # litellm-префиксы провайдеров: большинство нативные (cerebras/, huggingface/,
    # cohere/, mistral/, groq/, gemini/...). SambaNova — через OpenAI-совместимый
    # endpoint с base_url. Имя модели уже без префикса (берётся из списка провайдера).
    _model = model_name
    if "/" not in _model or provider in ("openrouter", "huggingface"):
        _model = f"{provider}/{model_name}" if not model_name.startswith(f"{provider}/") else model_name
    kwargs = dict(model=_model, api_key=api_key, temperature=0.0, timeout=120)
    if provider == "openrouter":
        kwargs["base_url"] = "https://openrouter.ai/api/v1"
    elif provider == "sambanova":
        # SambaNova OpenAI-совместима; litellm берёт её через openai/ + base_url.
        kwargs["model"] = f"openai/{model_name}"
        kwargs["base_url"] = "https://api.sambanova.ai/v1"
    return LLM(**kwargs)

# ===================== Динамические квоты провайдеров =======================
# Без единого хардкода лимитов: значения берутся ЛИБО из API провайдера
# (OpenRouter), ЛИБО из заголовков x-ratelimit-* реальных ответов (Groq/OpenAI и
# др.). Если данных нет — честно сообщаем "неизвестно", а не выдумываем цифры.

# Кэш последних увиденных заголовков ratelimit по провайдеру (заполняется из
# ответов litellm). Структура: provider -> {"remaining":..,"limit":..,"reset":..}
_RATELIMIT_CACHE: dict[str, dict] = {}
_quota_lock = threading.Lock()

def record_ratelimit_headers(provider: str, headers: dict) -> None:
    """Запоминает заголовки x-ratelimit-* из реального ответа провайдера.
    Вызывается оппортунистически; ничего не выдумывает."""
    if not headers:
        return
    low = {str(k).lower(): v for k, v in headers.items()}
    snap = {}
    # Поддерживаем оба распространённых семейства заголовков (requests и tokens).
    for canon, variants in {
        "remaining_requests": ("x-ratelimit-remaining-requests", "x-ratelimit-remaining"),
        "limit_requests":     ("x-ratelimit-limit-requests", "x-ratelimit-limit"),
        "remaining_tokens":   ("x-ratelimit-remaining-tokens",),
        "limit_tokens":       ("x-ratelimit-limit-tokens",),
        "reset":              ("x-ratelimit-reset-requests", "x-ratelimit-reset"),
    }.items():
        for h in variants:
            if h in low and low[h] not in (None, ""):
                snap[canon] = low[h]
                break
    if snap:
        snap["_ts"] = time.time()
        with _quota_lock:
            _RATELIMIT_CACHE[provider] = snap

def _fetch_openrouter_quota(api_key: str) -> Optional[dict]:
    """OpenRouter отдаёт остаток через /api/v1/auth/key — авторитетный источник."""
    import requests
    try:
        r = requests.get(
            "https://openrouter.ai/api/v1/auth/key",
            headers={"Authorization": f"Bearer {api_key}"}, timeout=8,
        )
        if r.status_code != 200:
            return None
        d = r.json().get("data", {})
        limit = d.get("limit")            # None = безлимит (есть кредиты)
        usage = d.get("usage")
        remaining = d.get("limit_remaining")
        if remaining is None and limit is not None and usage is not None:
            remaining = round(limit - usage, 4)
        return {
            "source": "api",
            "is_free_tier": d.get("is_free_tier"),
            "limit": limit,
            "usage": usage,
            "remaining": remaining,
            "rate_limit": d.get("rate_limit"),  # {requests, interval}
        }
    except Exception:
        return None

def fetch_quota(provider: str) -> dict:
    """Возвращает живую информацию о квоте провайдера БЕЗ хардкода чисел.
    Поля зависят от источника; ключ 'status' всегда присутствует."""
    keys = API_KEYS.get(provider, [])
    if not keys:
        return {"provider": provider, "status": "no_key", "detail": "ключ не задан"}

    key = keys[0]
    if provider == "openrouter":
        q = _fetch_openrouter_quota(key)
        if q:
            q.update({"provider": provider, "status": "ok", "keys": len(keys)})
            return q
        return {"provider": provider, "status": "unreachable", "keys": len(keys),
                "detail": "не удалось опросить /auth/key"}

    # Прочие провайдеры: специального endpoint нет — отдаём то, что увидели в
    # заголовках реальных ответов (если уже были вызовы).
    with _quota_lock:
        snap = dict(_RATELIMIT_CACHE.get(provider, {}))
    if snap:
        snap.update({"provider": provider, "status": "ok", "source": "headers", "keys": len(keys)})
        return snap
    return {"provider": provider, "status": "unknown", "keys": len(keys),
            "detail": "лимиты появятся после первого запроса"}

def fetch_all_quotas() -> list[dict]:
    """Квоты по всем провайдерам, у которых есть ключи."""
    return [fetch_quota(p) for p in API_KEYS.keys()]

def _install_litellm_quota_hook() -> None:
    """Регистрирует success-callback litellm, который вытягивает заголовки
    x-ratelimit-* из реальных ответов и кэширует их по провайдеру. Идемпотентно."""
    try:
        import litellm
    except Exception:
        return
    if getattr(litellm, "_corepilot_quota_hook", False):
        return

    def _hook(kwargs, completion_response, start_time, end_time):
        try:
            model = (kwargs.get("model") or "")
            provider = model.split("/", 1)[0] if "/" in model else ""
            if not provider:
                return
            hidden = getattr(completion_response, "_hidden_params", {}) or {}
            headers = hidden.get("additional_headers") or hidden.get("response_headers") or {}
            if headers:
                record_ratelimit_headers(provider, headers)
        except Exception:
            pass

    try:
        cbs = list(getattr(litellm, "success_callback", []) or [])
        cbs.append(_hook)
        litellm.success_callback = cbs
        litellm._corepilot_quota_hook = True
    except Exception:
        pass

def get_oracle_llm(state) -> Optional[LLM]:
    """LLM Мастера-Оракула по каскаду роли oracle. Возвращает None, если облачный
    провайдер без ключа."""
    try:
        return build_role_llm(state, "oracle")
    except Exception:
        return None


# === Принудительное рассуждение (CoT) для локальных моделей =================
# Малые локальные модели (7-8b, низкий квант) склонны ошибаться, если отвечают
# сразу. Явный chain-of-thought заметно снижает шанс первой ошибки. Включается
# тумблером force_local_reasoning (по умолчанию ВКЛ) и только для local-ролей.

_COT_INSTRUCTION = (
    "\n\nВАЖНО (режим рассуждения): прежде чем дать ответ, рассуждай пошагово "
    "внутри блока <think>...</think> — разбери задачу, продумай краевые случаи и "
    "проверь себя. После </think> выдай ТОЛЬКО финальный результат в требуемом "
    "формате (JSON/код), без блока рассуждения."
)


def cot_suffix(state: SessionState, role: str) -> str:
    """Возвращает CoT-приставку к backstory, если роль локальная и тумблер включён.
    Для cloud-ролей — пусто (они и так умеют рассуждать).

    ВАЖНО: для JSON-ролей при включённом force_json_output CoT НЕ добавляется —
    response_format=json_object требует, чтобы весь вывод был чистым JSON, а блок
    <think> его сломал бы. Принудительный JSON — более сильная гарантия, она и
    выигрывает. Рассуждение модель тогда делает «в уме», без текстового блока."""
    if _role_mode(state, role) != "local":
        return ""
    if not getattr(state, "force_local_reasoning", True):
        return ""
    if role in _JSON_ROLES and getattr(state, "force_json_output", True):
        return ""  # конфликт с json_object — JSON-режим приоритетнее
    return _COT_INSTRUCTION


# === Лестница эскалации: Оракул-Титан переписывает залипший код =============

_TITAN_SYSTEM = (
    "Ты — Мастер-Оракул (Титан), старший инженер по отладке (Windows / Python 3.12). "
    "Младшая модель ЗАСТРЯЛА: дважды выдала один и тот же нерабочий код с одной ошибкой. "
    "Твоя задача — не советовать, а ПЕРЕПИСАТЬ решение начисто и правильно. "
    "Сначала рассуждай в <think>...</think>: найди корень повторяющейся ошибки и как его обойти. "
    "После </think> верни СТРОГО валидный JSON вида "
    '{"patches":[{"filepath":"...","code":"...полный новый код файла..."}]} без пояснений.'
)


def _titan_llm_chain(state: SessionState):
    """Возвращает список (метка, kwargs) для litellm в порядке приоритета:
    1) облачный Оракул (если есть ключ); 2) локальный Титан (фолбэк).
    Пусто — если не настроен ни один путь."""
    import os as _os
    chain = []
    model = (getattr(state, "model_oracle", "") or "").strip()
    # Оракул облако-приоритетный: если источник не задан явно — считаем cloud.
    _backend_oracle = (getattr(state, "backend_oracle", "") or "").strip().lower()
    if _backend_oracle == "cloud":
        mode = "cloud"
    elif _backend_oracle in ("lmstudio", "ollama", "llamacpp", "lemonade"):
        mode = "local"
    else:
        mode = getattr(state, "mode_oracle", "cloud")  # старый профиль / дефолт

    # 1) Облачный приоритетный путь (если оракул настроен на cloud ИЛИ задан titan_cloud).
    if mode == "cloud" and model:
        provider = getattr(state, "provider_oracle", "groq")
        key = next_api_key(provider) or _os.environ.get(f"{provider.upper()}_API_KEY", "")
        if key:
            chain.append(("oracle-cloud", dict(model=f"{provider}/{model}", api_key=key)))

    # 2) Локальный Титан-фолбэк: отдельная тяжёлая модель (14-26b) или, если не задана,
    #    локальная модель оракула. Берём backend_oracle/model_oracle в local-режиме,
    #    либо явные titan_* поля, если заданы.
    titan_model = (getattr(state, "titan_model", "") or "").strip()
    titan_backend = getattr(state, "titan_backend", "") or getattr(state, "backend_oracle", "lmstudio")
    if not titan_model and mode == "local" and model:
        titan_model = model  # оракул сам локальный — он и есть Титан
    if titan_model:
        cfg = get_backend_config(state, titan_backend)
        chain.append(("titan-local", dict(model=f"{cfg['prefix']}{titan_model}",
                                           base_url=cfg["base_url"], api_key=cfg["api_key"])))
    return chain


def _free_local_models_for_titan(state: SessionState) -> None:
    """Перед подъёмом ЛОКАЛЬНОГО Титана (14-26B) освобождает память от малых
    локальных моделей рабочих ролей. Защита от OOM: при 16GB RAM + 8GB VRAM
    7-8B и Титан одновременно не помещаются. Выгружаем всё локальное, что можем.

    Ollama выгружается через keep_alive=0. Для llama.cpp/LM Studio программной
    выгрузки по API нет — но если сервером управляем мы (llama_manager), его
    останавливаем, чтобы освободить память под Титана."""
    seen = set()
    for role in ("coder", "gatherer", "architect", "auditor"):
        if _role_mode(state, role) != "local":
            continue
        backend = getattr(state, f"backend_{role}", "lmstudio")
        model = (getattr(state, f"model_{role}", "") or "").strip()
        key = (backend, model)
        if key in seen:
            continue
        seen.add(key)
        if backend == "ollama" and model:
            cleanup_old_llm(state, [model])  # keep_alive=0
    # Если локальный сервер llama.cpp под нашим управлением — останавливаем,
    # освобождая VRAM/RAM. Титан-local поднимется на своём бэкенде.
    try:
        import llama_manager as lm
        st = lm.server_status()
        if st.get("running"):
            logger.info("Останавливаю управляемый llama-server перед подъёмом Титана (память).")
            lm.stop_server()
    except Exception as e:
        logger.debug("Не удалось остановить llama-server перед Титаном: %s", e)


def consult_oracle_titan(task_data: dict, broken_code: str, repeated_error: str,
                         state: SessionState) -> tuple[str, str]:
    """Лестница эскалации. Оракул-Титан ПЕРЕПИСЫВАЕТ залипшее решение.
    Пробует облако, при недоступности — локального Титана.
    Возвращает (raw_json_с_патчами, метка_источника). Пусто — если все пути отпали."""
    if not getattr(state, "oracle_enabled", True):
        return "", ""
    import litellm
    chain = _titan_llm_chain(state)
    if not chain:
        return "", ""

    user_msg = (
        f"ЗАДАЧА: {task_data.get('title','?')}\n"
        f"ОПИСАНИЕ: {task_data.get('description','')[:600]}\n\n"
        f"ПОВТОРЯЮЩАЯСЯ ОШИБКА (младшая модель застряла на ней):\n{repeated_error[:1000]}\n\n"
        f"НЕРАБОЧИЙ КОД, который она выдала дважды:\n{broken_code[:4000]}\n\n"
        f"Перепиши решение правильно."
    )
    _freed = False
    for label, kwargs in chain:
        # Перед ЛОКАЛЬНЫМ Титаном обязательно освобождаем память от малых моделей.
        # Перед облачным — не нужно (инференс не на нашем железе).
        if label == "titan-local" and not _freed:
            _free_local_models_for_titan(state)
            _freed = True
        try:
            resp = litellm.completion(
                messages=[{"role": "system", "content": _TITAN_SYSTEM},
                          {"role": "user", "content": user_msg}],
                max_tokens=4096, temperature=0.1, timeout=180, **kwargs,
            )
            out = (resp.choices[0].message.content or "").strip()
            if out:
                logger.info("Оракул-Титан ответил через '%s'.", label)
                return out, label
        except Exception as e:
            logger.warning("Оракул-Титан путь '%s' недоступен: %s", label, str(e)[:120])
            continue
    return "", ""

def get_backend_config(state: SessionState, backend: Optional[str] = None) -> dict:
    """Возвращает конфиг бэкенда. Если backend не задан — берётся глобальный
    local_backend. URL-override из local_base_url применяется только когда
    запрошенный бэкенд совпадает с глобально выбранным."""
    backend = backend or getattr(state, "local_backend", "lmstudio")
    cfg = BACKEND_CONFIGS.get(backend, BACKEND_CONFIGS["lmstudio"])
    base_url = cfg["base_url"]
    if backend == getattr(state, "local_backend", "lmstudio"):
        override = (getattr(state, "local_base_url", None) or "").strip()
        if override:
            base_url = override
    return {**cfg, "base_url": base_url}

_LOCAL_ROLES = ("gatherer", "architect", "coder", "auditor", "oracle")

def cleanup_old_llm(state: SessionState, models: Optional[list[str]] = None) -> None:
    """Оптимизация VRAM: выгружает модели из Ollama через /api/generate keep_alive=0.
    Если models не передан — собирает локальные модели всех ролей с бэкендом Ollama
    (актуально для 8 ГБ VRAM на RX 6600)."""
    import requests
    cfg = get_backend_config(state, "ollama")
    base_url = cfg["base_url"].replace("/v1", "").rstrip("/")
    if models is None:
        models = [
            getattr(state, f"model_{r}", "")
            for r in _LOCAL_ROLES
            if _role_mode(state, r) == "local"
            and getattr(state, f"backend_{r}", "lmstudio") == "ollama"
        ]
    for model in {m for m in models if m}:
        try:
            requests.post(f"{base_url}/api/generate", json={"model": model, "keep_alive": 0}, timeout=2)
        except Exception as e:
            logger.debug(f"Failed to cleanup Ollama model {model}: {e}")

def _wait_local_model_ready(state, model: str, attempts: int = 6, delay: float = 1.5) -> bool:
    """Readiness-probe: ждёт, пока локальный сервер сообщит, что модель загружена
    (видна среди загруженных). Решает гонку «Model unloaded»: после выгрузки/эвикта
    модель поднимается по требованию не мгновенно. Возвращает True, если модель
    готова; False — если не дождались (тогда обычный повтор всё равно случится).
    Для не-LM Studio / при ошибках — не блокируем, считаем готовой."""
    backend = getattr(state, "local_backend", "lmstudio") or "lmstudio"
    base = (getattr(state, "local_base_url", "") or "http://localhost:1234/v1")
    host = base.rstrip("/").replace("/v1", "")
    import time as _t
    for _ in range(max(1, attempts)):
        try:
            import requests
            # LM Studio v0 отдаёт state загруженных моделей; ищем нашу в loaded/ready.
            r = requests.get(f"{host}/api/v0/models", timeout=3)
            data = r.json() if r.ok else {}
            items = data.get("data", data) if isinstance(data, dict) else data
            for it in (items or []):
                mid = (it.get("id") or it.get("key") or "") if isinstance(it, dict) else str(it)
                st_ = (it.get("state") or it.get("status") or "").lower() if isinstance(it, dict) else ""
                if model in mid and (not st_ or "load" in st_ or "ready" in st_):
                    return True
        except Exception:
            return True  # сервер не отвечает на probe — не блокируем, пусть решает повтор
        _t.sleep(delay)
    return False


def unload_role_llm(state: SessionState, role: str) -> None:
    """Выгружает локальную модель указанной роли из VRAM, освобождая место для
    следующей. Поддержаны Ollama (keep_alive=0) и LM Studio (REST-эндпоинт
    /api/v0/models/unload). Для cloud-ролей и прочих бэкендов — безопасный no-op."""
    if _role_mode(state, role) != "local":
        return
    backend = getattr(state, f"backend_{role}", "lmstudio")
    model = (getattr(state, f"model_{role}", "") or "").strip()
    if not model:
        return
    if backend == "ollama":
        cleanup_old_llm(state, [model])
        return
    if backend == "lmstudio":
        # LM Studio: программная выгрузка из памяти через REST (порт сервера).
        try:
            import requests
            base = (getattr(state, "local_base_url", "") or "http://localhost:1234/v1")
            host = base.rstrip("/").replace("/v1", "")
            # v0 принимает model, v1 — instance_id; пробуем оба, ошибки глушим.
            for url, payload in (
                (f"{host}/api/v0/models/unload", {"model": model}),
                (f"{host}/api/v1/models/unload", {"instance_id": model}),
            ):
                try:
                    requests.post(url, json=payload, timeout=4)
                except Exception:
                    continue
        except Exception:
            pass

def _same_local_target(state: SessionState, r1: str, r2: str) -> bool:
    """True, если две роли используют ОДНУ локальную модель/бэкенд — тогда выгрузка
    между ними бессмысленна (модель сразу понадобится снова)."""
    if _role_mode(state, r1) != "local" or _role_mode(state, r2) != "local":
        return False
    return (getattr(state, f"backend_{r1}", "lmstudio") == getattr(state, f"backend_{r2}", "lmstudio")
            and (getattr(state, f"model_{r1}", "") or "") == (getattr(state, f"model_{r2}", "") or ""))

def maybe_unload_between(state: SessionState, prev_role: str, next_role: str) -> None:
    """Выгружает модель prev_role перед ходом next_role, если включён тумблер
    vram_unload_between_agents и роли используют РАЗНЫЕ локальные модели. После
    выгрузки проактивно ждёт готовности модели next_role (readiness-probe), чтобы
    следующий запрос не пришёл раньше JIT-загрузки и не получил 'Model unloaded'."""
    if not getattr(state, "vram_unload_between_agents", False):
        return
    if _same_local_target(state, prev_role, next_role):
        return
    unload_role_llm(state, prev_role)
    # Если следующая роль локальная — дождаться, пока её модель поднимется.
    if _role_mode(state, next_role) == "local":
        next_model = (getattr(state, f"model_{next_role}", "") or "").strip()
        if next_model:
            _wait_local_model_ready(state, next_model)

def build_local_llm(state: SessionState, backend: str, model: str, temperature: float,
                    force_json: bool = False) -> LLM:
    cfg = get_backend_config(state, backend)
    kwargs = dict(model=f"{cfg['prefix']}{model}", base_url=cfg["base_url"],
                  api_key=cfg["api_key"], temperature=temperature, timeout=300)
    # response_format у локальных моделей по умолчанию НЕ шлём: LM Studio и многие
    # GGUF-сборки его отвергают (`does not support response_format`), что роняет ПЕРВЫЙ
    # же запрос (до срабатывания кэша). Парсер _extract_json и так надёжно достаёт JSON
    # (облачный путь работает именно так). response_format включаем ТОЛЬКО для связок,
    # явно помеченных как совместимые (_RF_OK) — их можно накопить при желании.
    if force_json and getattr(state, "force_json_output", True):
        if (backend, model) in _RF_OK and (backend, model) not in _NO_RESPONSE_FORMAT:
            kwargs["response_format"] = {"type": "json_object"}
    return LLM(**kwargs)

# Роли, чей вывод обязан быть строгим JSON (для них включаем response_format).
_JSON_ROLES = {"gatherer", "architect", "coder"}
# Capability-кэш: связки (backend, model), которые отвергли response_format.
# ПЕРСИСТЕНТНЫЙ (на диске) — демон и UI запускаются отдельно и стартуют «с нуля»,
# поэтому знание о несовместимости модели надо сохранять между запусками.
_NO_RF_FILE = os.path.join(os.environ.get("COREPILOT_AUTO_DIR", os.path.join(".", "auto_tasks")),
                           "no_response_format.json")

def _load_no_rf() -> set:
    try:
        import json
        with open(_NO_RF_FILE, "r", encoding="utf-8") as f:
            return {tuple(x) for x in json.load(f)}
    except Exception:
        return set()

def _save_no_rf() -> None:
    try:
        import json
        os.makedirs(os.path.dirname(_NO_RF_FILE), exist_ok=True)
        with open(_NO_RF_FILE, "w", encoding="utf-8") as f:
            json.dump([list(x) for x in _NO_RESPONSE_FORMAT], f)
    except Exception:
        pass

_NO_RESPONSE_FORMAT: set = _load_no_rf()
# Белый список локальных (backend, model), проверенно поддерживающих response_format.
# Пуст по умолчанию: безопаснее НЕ слать (парсер JSON надёжен). Можно пополнять, если
# у вас есть локальная модель с подтверждённой поддержкой structured output.
_RF_OK: set = set()

def _role_mode(state, role: str) -> str:
    """Единая точка определения режима роли: 'local' или 'cloud'.

    Новая логика (по UX-упрощению): источник роли хранится в backend_<role>, и
    значение 'cloud' в этом списке означает облако — отдельный переключатель
    mode_<role> больше не нужен. Обратная совместимость: старые профили, где ещё
    записан mode_<role>='cloud', продолжают работать."""
    backend = (getattr(state, f"backend_{role}", "") or "").strip().lower()
    if backend == "cloud":
        return "cloud"
    # Явно выбранный локальный сервер ПОБЕЖДАЕТ устаревшее mode_<role>. Иначе
    # старый профиль с mode_=cloud перебивал бы новый выбор источника (роль на
    # lmstudio уходила в облако — баг с 'qwenclaude is not a valid model ID').
    if backend in ("lmstudio", "ollama", "llamacpp", "lemonade"):
        return "local"
    legacy_mode = getattr(state, f"mode_{role}", None)
    if legacy_mode in ("local", "cloud"):
        return legacy_mode  # источник не задан — уважаем старый профиль
    return "local"


def build_role_llm(state: SessionState, role: str) -> LLM:
    """Единая каскадная фабрика LLM для роли (gatherer|architect|coder|auditor|oracle).
    Источник роли — backend_<role>: локальный сервер (lmstudio/ollama/...) либо
    'cloud' (тогда работают provider_<role>+model_<role>)."""
    mode = _role_mode(state, role)
    model = (getattr(state, f"model_{role}", "") or "").strip()
    if mode == "cloud":
        provider = getattr(state, f"provider_{role}", "gemini")
        # Защита от рассинхрона: если облачной роли достался локальный model-name
        # (имя из локального сервера, напр. 'qwenclaude'), облако вернёт загадочный
        # 'not a valid model ID'. Даём понятную ошибку ДО запроса.
        if model and provider != "openrouter":
            try:
                import service_layer as _svc
                local_names = set()
                for _b in ("lmstudio", "ollama", "llamacpp", "lemonade"):
                    try:
                        local_names |= set(_svc.list_local_models(_b, getattr(state, "local_base_url", "") or ""))
                    except Exception:
                        pass
                if model in local_names:
                    raise ValueError(
                        f"Роль '{role}': выбрана ЛОКАЛЬНАЯ модель '{model}', но источник — облако "
                        f"('{provider}'). Откройте Настройки, нажмите «Обновить списки моделей» и "
                        f"выберите облачную модель (или смените источник на локальный сервер).")
            except ValueError:
                raise
            except Exception:
                pass  # проверка best-effort, не блокируем при сбое запроса списка
        llm = get_cloud_llm(provider, model or "gemini-3.5-flash")
        if not llm:
            raise ValueError(f"Cloud LLM init failed для роли '{role}' (провайдер '{provider}': нет API-ключа?)")
        return llm
    backend = getattr(state, f"backend_{role}", "lmstudio")
    temp = 0.0 if role == "coder" else _SPEED_TEMPERATURE.get(getattr(state, "speed", "medium"), 0.1)
    if not model:
        raise ValueError(f"Не выбрана локальная модель для роли '{role}' (бэкенд '{backend}' офлайн?).")
    return build_local_llm(state, backend, model, temp, force_json=(role in _JSON_ROLES))

def _fallback_local_llm(state: SessionState):
    """Аварийный локальный LLM для safe_kickoff. Берёт РЕАЛЬНО доступную модель:
    1) роль на local с заданной моделью; 2) иначе — спрашивает у локального сервера
    список загруженных моделей и берёт первую. Если локальных моделей нет вовсе —
    возвращает None (фолбэк невозможен, пусть всплывёт исходная облачная ошибка)."""
    for role in ("coder", "gatherer", "architect", "auditor"):
        if _role_mode(state, role) == "local":
            model = (getattr(state, f"model_{role}", "") or "").strip()
            if model:
                return build_local_llm(state, getattr(state, f"backend_{role}", "lmstudio"), model, 0.0)
    # Все роли облачные (или у локальных нет модели). Спрашиваем у сервера, что
    # реально загружено — нельзя слать облачное имя модели на локальный сервер.
    backend = getattr(state, "local_backend", "lmstudio") or "lmstudio"
    base_url = getattr(state, "local_base_url", "") or ""
    try:
        import service_layer as _svc
        loaded = _svc.list_local_models(backend, base_url)
    except Exception:
        loaded = []
    if loaded:
        return build_local_llm(state, backend, loaded[0], 0.0)
    return None  # фолбэк невозможен

# Потолок ретраев при rate-limit/перегрузке. Был 10 — но при облачном timeout=120с
# это давало многоминутное «зависание» Конвейера на free-tier лимитах. 4 попытки
# с ротацией ключей/фолбэком достаточно; дальше — честная ошибка пользователю.
MAX_RETRIES = 4

def _is_cloud_agent(agent) -> bool:
    llm = getattr(agent, 'llm', None)
    if not llm: return False
    model: str = getattr(llm, 'model', '') or ''
    return not any(model.startswith(p) for p in _LOCAL_LLM_PREFIXES)

def _provider_of(agent) -> str:
    """Извлекает имя облачного провайдера из строки модели агента ('groq/llama...' -> 'groq')."""
    model = getattr(getattr(agent, 'llm', None), 'model', '') or ''
    return model.split('/', 1)[0] if '/' in model else ''

def _model_tail_of(agent) -> str:
    """Хвост модели после провайдера ('openrouter/openai/gpt-4o' -> 'openai/gpt-4o')."""
    model = getattr(getattr(agent, 'llm', None), 'model', '') or ''
    return model.split('/', 1)[1] if '/' in model else model

def _propagate_llm(agent, llm) -> None:
    agent.llm = llm
    if hasattr(agent, 'agent_executor') and agent.agent_executor:
        agent.agent_executor.llm = llm

def _next_backup_provider(failed_provider: str, tried: set) -> Optional[str]:
    """Выбирает следующего РЕЗЕРВНОГО облачного провайдера, у которого есть ключ и
    который ещё не пробовали. Порядок предпочтения — быстрые/щедрые бесплатные сперва.
    Это вторая/третья линия обороны при падении основного провайдера."""
    preference = ["cerebras", "groq", "gemini", "openrouter", "sambanova",
                  "mistral", "huggingface", "cohere", "deepseek", "openai", "anthropic"]
    for p in preference:
        if p == failed_provider or p in tried:
            continue
        if API_KEYS.get(p):  # ключ есть
            return p
    return None

def _build_backup_llm(provider: str) -> Optional["LLM"]:
    """Строит LLM резервного провайдера. Модель: 'auto' у OpenRouter, иначе первая
    из живого списка моделей провайдера (бесплатные у OpenRouter уже сверху)."""
    try:
        if provider == "openrouter":
            return get_cloud_llm("openrouter", "auto")
        import service_layer as _svc
        models = _svc.list_cloud_models(provider)
        if not models:
            return None
        # пропускаем псевдо-пункт openrouter/auto, берём реальную первую модель
        model = next((m for m in models if "/auto" not in m), models[0])
        # у некоторых провайдеров имя уже с префиксом — get_cloud_llm разрулит
        tail = model.split("/", 1)[1] if (provider in model and "/" in model) else model
        return get_cloud_llm(provider, tail)
    except Exception:
        return None

def safe_kickoff(crew: Crew, state: SessionState):
    """Запуск crew с устойчивостью к rate-limit/перегрузкам. Три линии защиты:
    1. Ротация ключей упавшего провайдера (429/quota/503/timeout).
    2. Межпровайдерный fallback (_next_backup_provider): cerebras→groq→gemini→openrouter→…
    3. Фолбэк на локальную модель (если облачные варианты исчерпаны).
    Локальные сбои (response_format, model unloaded) обрабатываются отдельно."""
    retries = 0
    _tried_providers: set = set()  # резервные провайдеры, уже испробованные за прогон
    while retries < MAX_RETRIES:
        try:
            return crew.kickoff()
        except Exception as e:
            err_lower = str(e).lower()
            # ЛОКАЛЬНЫЙ сервер (LM Studio/llama.cpp) часто отвергает response_format=
            # json_object для моделей без structured-output: ошибка 400 / 'response_format'
            # / 'json'. Пересоздаём LLM локальных агентов БЕЗ response_format и повторяем.
            if any(x in err_lower for x in ("response_format", "response format", "json_object",
                                            "400", "unsupported", "not supported")):
                stripped = False
                for agent in crew.agents:
                    llm = getattr(agent, "llm", None)
                    rf = getattr(llm, "response_format", None) if llm else None
                    if llm and rf and not _is_cloud_agent(agent):
                        try:
                            llm.response_format = None
                            _propagate_llm(agent, llm)
                            stripped = True
                            # Запоминаем связку, чтобы впредь не слать response_format.
                            m = getattr(llm, "model", "") or ""
                            # model вида 'openai/omnicoder' -> ('lmstudio'?, 'omnicoder')
                            name = m.split("/", 1)[1] if "/" in m else m
                            for bk in ("lmstudio", "ollama", "llamacpp", "lemonade"):
                                _NO_RESPONSE_FORMAT.add((bk, name))
                            _save_no_rf()  # персистентность между запусками
                        except Exception:
                            pass
                if stripped:
                    retries += 1
                    continue  # повтор уже без response_format

            # 'Model unloaded' / 'model_not_loaded' — гонка: модель выгружена (нашим
            # тумблером или Auto-Evict LM Studio), а запрос пришёл раньше JIT-загрузки.
            # Это ВРЕМЕННАЯ ошибка: ждём и повторяем, чтобы сервер успел поднять модель.
            is_unloaded = any(x in err_lower for x in ("model unloaded", "model_not_loaded",
                                                       "no models loaded", "model not loaded"))
            if is_unloaded:
                # перед повтором дождаться готовности локальных моделей агентов
                for agent in crew.agents:
                    if not _is_cloud_agent(agent):
                        llm = getattr(agent, "llm", None)
                        m = (getattr(llm, "model", "") or "") if llm else ""
                        name = m.split("/", 1)[1] if "/" in m else m
                        if name:
                            _wait_local_model_ready(state, name)
                retries += 1
                continue

            if not any(x in err_lower for x in ('429', 'quota', '503', 'overloaded', '500', 'timeout', 'connection', 'none or empty', 'invalid response', '402', 'credits', 'insufficient', 'requires more credits')):
                raise

            time.sleep(random.uniform(2, 5))
            cloud_agents = [a for a in crew.agents if _is_cloud_agent(a)]
            if not cloud_agents:
                retries += 1
                continue

            for agent in cloud_agents:
                provider = _provider_of(agent)

                # OpenRouter: при недоступности модели — авто-роутинг
                if provider == "openrouter" and any(x in err_lower for x in ("404", "model not found", "no endpoints", "context")):
                    fk = next_api_key("openrouter")
                    if fk:
                        _propagate_llm(agent, LLM(model="openrouter/auto", api_key=fk, base_url="https://openrouter.ai/api/v1", temperature=0.0))
                        continue

                # Ротация ключа того же провайдера, если их несколько
                keys = API_KEYS.get(provider, [])
                if provider and keys and len(keys) > 1:
                    new_llm = get_cloud_llm(provider, _model_tail_of(agent))
                    if new_llm:
                        with _key_lock:
                            idx = (KEY_COUNTERS.get(provider, 1) - 1) % len(keys)
                            os.environ[f"{provider.upper()}_API_KEY"] = keys[idx]
                        _propagate_llm(agent, new_llm)
                        continue

                # ВТОРАЯ/ТРЕТЬЯ ЛИНИЯ: провайдер упал/исчерпан — пробуем РЕЗЕРВНОГО
                # провайдера (другой сервис), у которого есть ключ. Это спасает ночной
                # прогон Демона, если, например, OpenRouter лёг. Каждого резервного
                # пробуем один раз за проход; модель — 'auto' у OpenRouter, иначе первая
                # из живого списка провайдера.
                backup = _next_backup_provider(provider, _tried_providers)
                if backup:
                    _tried_providers.add(backup)
                    bl = _build_backup_llm(backup)
                    if bl is not None:
                        logger.warning("Провайдер '%s' недоступен — переключаюсь на резерв '%s'.",
                                       provider, backup)
                        _propagate_llm(agent, bl)
                        continue

                # Последний рубеж — локальная модель (если реально доступна)
                _fb = _fallback_local_llm(state)
                if _fb is not None:
                    _propagate_llm(agent, _fb)

            retries += 1
    raise RuntimeError(f"safe_kickoff: превышено {MAX_RETRIES} попыток.")
