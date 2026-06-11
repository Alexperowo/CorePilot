#!/usr/bin/env python3
"""
cloud_api.py — облачные провайдеры для CorePilot Mobile.

Автономный чат-бот: отправка текстовых запросов и генерация изображений через
сторонние Cloud API с РОТАЦИЕЙ КЛЮЧЕЙ (round-robin, до 4 ключей на провайдера).

Полностью кроссплатформенный модуль: только сеть (urllib), без платформенных
вызовов (те остаются в android_bridge.py). Сетевые вызовы синхронные — вызывающий
код (main.py) обязан запускать их в фоне через page.run_thread().

Поведение ротации:
  • round-robin: каждый следующий запрос берёт следующий ключ;
  • при 429 (лимит) / 401 / 403 (исчерпан баланс) — прозрачно переключаемся на
    следующий ключ и повторяем (максимум len(keys) попыток);
  • если все ключи исчерпаны — понятная ошибка «Все ключи провайдера X исчерпали лимит».
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.request
import urllib.error


# --- Каталог облачных провайдеров (OpenAI-совместимые, где возможно) ----------
# Для каждого: базовый URL, тип. Имена ключей в настройках — "<id>_keys".
# Только провайдеры с бесплатными тарифами вынесены вперёд.
TEXT_PROVIDERS = {
    "huggingface": {
        "label": "Cloud: HuggingFace",
        "base_url": "https://router.huggingface.co/v1",
        "default_model": "meta-llama/Llama-3.1-8B-Instruct",
    },
    "openrouter": {
        "label": "Cloud: OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "auto",
    },
    "cerebras": {
        "label": "Cloud: Cerebras",
        "base_url": "https://api.cerebras.ai/v1",
        "default_model": "llama-3.3-70b",
    },
    "groq": {
        "label": "Cloud: Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "default_model": "llama-3.3-70b-versatile",
    },
    "openai": {
        "label": "Cloud: OpenAI",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
    },
}

# Провайдеры генерации изображений. api_format: 'huggingface' (POST inputs -> бинарь)
# или 'openai' (v1/images/generations -> JSON с b64_json/url).
IMAGE_PROVIDERS = {
    "huggingface": {
        "label": "HuggingFace (FLUX/SDXL)",
        "url_tmpl": "https://api-inference.huggingface.co/models/{model}",
        "default_model": "black-forest-labs/FLUX.1-schnell",
        "api_format": "huggingface",
    },
    "openai_image": {
        "label": "Cloud: OpenAI-совместимые (с лимитами)",
        "url_tmpl": "https://api.together.xyz/v1/images/generations",
        "default_model": "black-forest-labs/FLUX.1-schnell",
        "api_format": "openai",
    },
}

# Коды, означающие «ключ исчерпан/не годен — пробуй следующий».
_ROTATE_CODES = (429, 401, 403, 402)
MAX_KEYS = 4


def parse_keys(raw) -> list:
    """Из строки 'k1, k2 , k3' или списка делает чистый список (max MAX_KEYS)."""
    if isinstance(raw, list):
        items = raw
    else:
        items = str(raw or "").split(",")
    out = []
    for k in items:
        k = (k or "").strip()
        if k and k not in out:
            out.append(k)
    return out[:MAX_KEYS]


class CloudAPIManager:
    """Инкапсулирует ротацию ключей и вызовы облачных API.

    Ключи берутся из settings по схеме "<provider>_keys" (строка через запятую
    или список). Счётчики round-robin — в памяти процесса (на сессию)."""

    def __init__(self, settings: dict):
        self.settings = settings or {}
        self._counters: dict[str, int] = {}
        self._lock = threading.Lock()  # защита round-robin от гонок (быстрые тапы)

    # -- ключи -------------------------------------------------------------
    def keys_for(self, provider: str) -> list:
        return parse_keys(self.settings.get(f"{provider}_keys"))

    def has_keys(self, provider: str) -> bool:
        return bool(self.keys_for(provider))

    def _ordered_keys(self, provider: str) -> list:
        """Ключи, начиная с текущего round-robin (чтобы чередовать между запросами).
        Под Lock — иначе быстрые тапы сбивают индекс и ротацию."""
        keys = self.keys_for(provider)
        if not keys:
            return []
        with self._lock:
            start = self._counters.get(provider, 0) % len(keys)
            self._counters[provider] = (start + 1) % len(keys)
        return keys[start:] + keys[:start]

    # -- текстовый чат -----------------------------------------------------
    def chat(self, provider: str, messages_or_prompt, model: str = "",
             max_tokens: int = 0, temperature: float = -1.0) -> str:
        """OpenAI-совместимый /chat/completions с ротацией ключей.

        messages_or_prompt — либо строка (одиночный запрос, обратная совместимость),
        либо list[dict] с полной историей {"role": ..., "content": ...}.
        Системный промпт из настроек вставляется первым, ТОЛЬКО если история не содержит
        уже сообщения с role="system".
        Возвращает текст ответа или строку-ошибку (маркер [ERR])."""
        meta = TEXT_PROVIDERS.get(provider)
        if not meta:
            return f"[ERR] Неизвестный провайдер: {provider}"
        keys = self._ordered_keys(provider)
        if not keys:
            return f"[ERR] Нет ключей для {provider}. Добавьте их в Настройках."
        model = (model or self.settings.get(f"{provider}_model") or meta["default_model"])
        if provider == "openrouter" and model in ("", "auto", "free"):
            model = "openrouter/auto"
        # Параметры из настроек (с разумными дефолтами). 2048 токенов вместо 512 —
        # чтобы длинные ответы (код, объяснения) не обрывались на полуслове.
        if max_tokens <= 0:
            max_tokens = int(self.settings.get("chat_max_tokens", 2048) or 2048)
        if temperature < 0:
            temperature = float(self.settings.get("chat_temperature", 0.7) or 0.7)

        # Нормализуем вход: строка → список с одним user-сообщением.
        if isinstance(messages_or_prompt, str):
            history: list[dict] = [{"role": "user", "content": messages_or_prompt}]
        else:
            history = list(messages_or_prompt)  # копия, не мутируем оригинал

        # Системный промпт ставим первым, если его ещё нет в истории.
        sys_prompt = (self.settings.get("system_prompt", "") or "").strip()
        has_system = any(m.get("role") == "system" for m in history)
        if sys_prompt and not has_system:
            history = [{"role": "system", "content": sys_prompt}] + history

        messages = history
        url = f"{meta['base_url']}/chat/completions"
        payload = json.dumps({
            "model": model, "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature,
        }).encode("utf-8")

        last_err = ""
        for attempt, key in enumerate(keys, 1):
            headers = {"Content-Type": "application/json",
                       "Authorization": f"Bearer {key}"}
            try:
                req = urllib.request.Request(url, data=payload, headers=headers)
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                return data["choices"][0]["message"]["content"].strip()
            except urllib.error.HTTPError as he:
                code = he.code
                last_err = f"HTTP {code}"
                if code in _ROTATE_CODES:
                    # ключ исчерпан/не годен — пробуем следующий
                    continue
                # прочие ошибки (400/500) — нет смысла менять ключ
                try:
                    body = he.read().decode("utf-8")[:200]
                    last_err = f"HTTP {code}: {body}"
                except Exception:
                    pass
                break
            except Exception as ex:
                last_err = str(ex)[:200]
                break

        if last_err.startswith("HTTP") and any(str(c) in last_err for c in _ROTATE_CODES):
            return (f"[ERR] Все ключи провайдера {TEXT_PROVIDERS[provider]['label']} "
                    f"исчерпали лимит (или недействительны).")
        return f"[ERR] {TEXT_PROVIDERS[provider]['label']}: {last_err}"

    # -- генерация изображений --------------------------------------------
    def generate_image(self, provider: str, prompt: str, model: str = "") -> dict:
        """Генерация картинки с ротацией ключей. Возвращает dict:
        {"ok": True, "image_b64": "..."} либо {"ok": False, "error": "..."}.
        HF Inference API отдаёт бинарный PNG/JPEG — кодируем в base64 для ft.Image."""
        meta = IMAGE_PROVIDERS.get(provider)
        if not meta:
            return {"ok": False, "error": f"Неизвестный image-провайдер: {provider}"}
        keys = self._ordered_keys(provider)
        if not keys:
            return {"ok": False, "error": f"Нет ключей для {provider}. Добавьте в Настройках."}
        model = (model or self.settings.get(f"{provider}_image_model") or meta["default_model"])
        url = meta["url_tmpl"].format(model=model)
        api_format = meta.get("api_format", "huggingface")
        # Тело запроса зависит от формата провайдера.
        if api_format == "openai":
            body = {"prompt": prompt, "model": model, "response_format": "b64_json"}
            size = (self.settings.get("image_size", "") or "").strip()
            if size:
                body["size"] = size  # напр. 1024x1024
            payload = json.dumps(body).encode("utf-8")
        else:
            payload = json.dumps({"inputs": prompt}).encode("utf-8")

        last_err = ""
        for key in keys:
            headers = {"Content-Type": "application/json",
                       "Authorization": f"Bearer {key}", "Accept": "image/png"}
            try:
                req = urllib.request.Request(url, data=payload, headers=headers)
                with urllib.request.urlopen(req, timeout=180) as resp:
                    ctype = resp.headers.get("Content-Type", "")
                    raw = resp.read()
                if ctype.startswith("image/"):
                    return {"ok": True, "image_b64": base64.b64encode(raw).decode("ascii")}
                # JSON-ответ: умный парсинг под оба формата
                try:
                    j = json.loads(raw.decode("utf-8"))
                    # OpenAI-совместимый: {"data":[{"b64_json":...}|{"url":...}]}
                    if api_format == "openai" and isinstance(j, dict) and j.get("data"):
                        img_data = j["data"][0]
                        if "b64_json" in img_data:
                            return {"ok": True, "image_b64": img_data["b64_json"]}
                        if "url" in img_data:
                            ir = urllib.request.Request(img_data["url"])
                            with urllib.request.urlopen(ir, timeout=60) as iresp:
                                return {"ok": True,
                                        "image_b64": base64.b64encode(iresp.read()).decode("ascii")}
                    # HuggingFace JSON-вариант
                    if isinstance(j, list) and j and "generated_image" in str(j[0]):
                        return {"ok": True, "image_b64": j[0].get("generated_image", "")}
                    last_err = str(j)[:200]
                except Exception:
                    last_err = "Неожиданный ответ сервера или сбой парсинга"
                continue
            except urllib.error.HTTPError as he:
                last_err = f"HTTP {he.code}"
                if he.code in _ROTATE_CODES:
                    continue
                if he.code == 503:
                    # модель на HF «прогревается» — даём весам подняться и повторяем тем же ключом
                    time.sleep(5)
                    try:
                        req = urllib.request.Request(url, data=payload, headers=headers)
                        with urllib.request.urlopen(req, timeout=180) as resp:
                            ctype = resp.headers.get("Content-Type", "")
                            raw = resp.read()
                        if ctype.startswith("image/"):
                            return {"ok": True, "image_b64": base64.b64encode(raw).decode("ascii")}
                    except Exception:
                        pass
                    last_err = "HTTP 503 (модель загружается, попробуйте ещё раз через минуту)"
                    continue
                break
            except Exception as ex:
                last_err = str(ex)[:200]
                break

        if last_err.startswith("HTTP") and any(str(c) in last_err for c in _ROTATE_CODES):
            return {"ok": False, "error": f"Все ключи {provider} исчерпали лимит."}
        return {"ok": False, "error": f"{provider}: {last_err}"}
