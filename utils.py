import os
import re
import time
import shutil
import subprocess
import json
import datetime
import tempfile
import logging
import difflib
import uuid
import contextlib
from pathlib import Path
from contextvars import ContextVar, Token
from typing import Protocol, Optional, List, Any
from pydantic import BaseModel, Field, ConfigDict, computed_field
from context_manager import DatabaseManager

logger = logging.getLogger('UTILS')
BT = chr(96) * 3

class PatchModel(BaseModel):
    filepath: str
    code: str

class FixerValidationResult(BaseModel):
    is_valid: bool
    patches: List[PatchModel] = Field(default_factory=list)
    error_msg: Optional[str] = None

def extract_agent_reasoning(text: str) -> tuple[str, str]:
    if not text: return text, ""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    thoughts_parts: list[str] = []

    def _collect(m: re.Match) -> str:
        if content := m.group(1).strip(): thoughts_parts.append(content)
        return ""

    clean = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE).sub(_collect, normalized)
    clean = re.sub(r"</think>", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean, "\n\n---\n\n".join(thoughts_parts)

def strict_parse_fixes(text: str) -> FixerValidationResult:
    if not text: return FixerValidationResult(is_valid=False, error_msg='Пустой ответ.')
    if 'file: none' in text.lower()[:50]: return FixerValidationResult(is_valid=True, patches=[])
    if re.search(rf'{BT}(?:[a-zA-Z0-9+#\-]+)?\n', text) and not re.search(rf'\n{BT}', text[text.index(BT)+3:]):
        return FixerValidationResult(is_valid=False, error_msg='Незакрытый блок кода.')
    
    matches = list(re.finditer(rf'FILE:\s*([^\n]+)\n(?:(?!{BT})[^\n]*\n)*{BT}(?:[a-zA-Z0-9+#\-]+)?\n(.*?)\n{BT}', text, re.DOTALL))
    if not matches: return FixerValidationResult(is_valid=False, error_msg='Нарушен формат.')
    
    patches = [PatchModel(filepath=m.group(1).strip(), code=m.group(2)) for m in matches if m.group(1).strip().lower() != 'none']
    return FixerValidationResult(is_valid=True, patches=patches)

class InteractionHandler(Protocol):
    def ask_question(self, question: str, choices: Optional[List[str]] = None) -> str: ...
    def confirm_command(self, command: str, terminal: str) -> bool: ...
    def log_event(self, level: str, message: str) -> None: ...
    async def confirm_patch(self, diff: str) -> bool: ...

class DummyInteractionHandler:
    def __init__(self, auto_approve: bool = False): self.auto_approve = auto_approve
    def ask_question(self, q: str, c: Optional[List[str]] = None) -> str: return 'ОТКАЗ: Автономный режим.'
    def confirm_command(self, cmd: str, term: str) -> bool: return self.auto_approve
    def log_event(self, lvl: str, msg: str) -> None: getattr(logging, lvl.lower(), logging.info)(msg)
    async def confirm_patch(self, diff: str) -> bool: return self.auto_approve

class RuntimeContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    state: Any
    ui: Any = None        # обработчик подтверждений; None в десктоп-конвейере (нет HITL)
    overlay: Any = None   # песочница патчей; может отсутствовать
    context_mgr: Any = Field(default_factory=DatabaseManager)

_ctx_var: ContextVar['RuntimeContext'] = ContextVar('runtime_context')

def set_runtime_context(ctx: RuntimeContext) -> Token: return _ctx_var.set(ctx)
def get_runtime_context() -> RuntimeContext:
    try: return _ctx_var.get()
    except LookupError: raise RuntimeError('RuntimeContext не установлен.')
def reset_runtime_context(token: Token): _ctx_var.reset(token)

class SessionState(BaseModel):
    project_path: str = Field(default_factory=lambda: os.getcwd())
    agent_profile: str = 'Универсальный Senior Developer'
    task_mode: str = 'Универсальная задача (Автоопределение)'
    speed: str = 'medium'
    auto_apply: bool = False
    # Локальный бэкенд по умолчанию (для генерации SD-промптов и как источник URL-override).
    # Бэкенд каждой РОЛИ выбирается отдельно полем backend_<role>.
    local_backend: str = 'lmstudio'
    local_base_url: str = 'http://localhost:1234/v1'
    forge_url: str = 'http://127.0.0.1:7860'
    forge_model: str = ''
    # Генерация изображений: источник 'forge' | 'comfy' | 'cloud'
    image_source: str = 'forge'
    comfy_url: str = 'http://127.0.0.1:8188'
    comfy_model: str = ''            # имя .safetensors чекпойнта в ComfyUI (опц.)
    image_provider: str = 'huggingface'   # облачный image-провайдер
    image_cloud_model: str = ''      # модель облака (пусто = дефолт FLUX.1-schnell)
    model_image_prompt: str = ''
    image_prompt_threads: int = 6
    ui_max_iter: int = 10
    ui_max_rpm: int = 10
    ui_tree_limit: int = 500
    ui_file_limit_kb: int = 500
    max_tool_output_chars: int = 15000
    ui_step_timeout: int = 300
    debug_mode: bool = False
    ollama_models_dir: str = './ollama_models'
    auto_register_ollama: bool = False
    backup_retention_days: int = 7
    gradle_custom_path: str = ''
    strict_sandbox: bool = True
    persist_session: bool = True
    oracle_enabled: bool = True
    web_search_enabled: bool = False

    # === Настраиваемые опции (анти-хардкод) ===
    # Выгрузка локальной модели из VRAM перед передачей хода следующему агенту.
    # Защищает 8 ГБ VRAM при разных локальных моделях на роль. По умолчанию выкл.
    vram_unload_between_agents: bool = False
    # Полный MD5-хэш финалистов в поиске дубликатов (надёжнее, чуть медленнее).
    dup_full_hash: bool = True
    # Карантин очистки создаётся на том же диске, что и источник (мгновенный rename).
    quarantine_same_drive: bool = True

    # === Каскадная конфигурация 5 ролей ===
    # Для каждой роли: mode (local|cloud), backend_<role> (если local),
    # provider_<role> (если cloud), model_<role> (имя модели).
    mode_gatherer: str = 'local'
    backend_gatherer: str = 'lmstudio'
    provider_gatherer: str = 'gemini'
    model_gatherer: str = ''

    mode_architect: str = 'local'
    backend_architect: str = 'lmstudio'
    provider_architect: str = 'gemini'
    model_architect: str = ''

    mode_coder: str = 'local'
    backend_coder: str = 'lmstudio'
    provider_coder: str = 'gemini'
    model_coder: str = ''

    mode_auditor: str = 'local'
    backend_auditor: str = 'lmstudio'
    provider_auditor: str = 'gemini'
    model_auditor: str = ''

    mode_oracle: str = 'cloud'
    backend_oracle: str = 'lmstudio'
    provider_oracle: str = 'groq'
    model_oracle: str = 'llama-3.3-70b-versatile'

    # Принудительное рассуждение (CoT) для локальных моделей — снижает ошибки
    # малых 7-8b в низком кванте. По умолчанию ВКЛЮЧЕНО.
    force_local_reasoning: bool = True
    # Принудительный валидный JSON для локальных JSON-ролей (response_format на
    # уровне токенов: llama.cpp/LM Studio grammar). По умолчанию ВКЛ; если ваш
    # сервер/модель не поддерживает — выключите, парсер сработает как фолбэк.
    force_json_output: bool = True
    # Локальный Титан — тяжёлая модель-фолбэк Оракула (14-26b), когда облако
    # недоступно. Пусто = использовать локальную модель оракула как Титана.
    titan_model: str = ''
    titan_backend: str = 'lmstudio'

    @computed_field
    @property
    def power_mode(self) -> str:
        modes = {self.mode_gatherer, self.mode_architect, self.mode_coder, self.mode_auditor}
        if modes == {"cloud"}: return "cloud"
        if modes == {"local"}: return "local"
        return "hybrid"

def load_session() -> Optional[SessionState]:
    if os.path.exists('.ai_session.json'):
        try:
            with open('.ai_session.json', 'r', encoding='utf-8') as f: data = json.load(f)
            return SessionState.model_validate(data)
        except Exception as e:
            # Битый файл сессии не должен молча терять настройки — берём дефолты, но логируем.
            logger.warning("Не удалось прочитать .ai_session.json (%s), беру значения по умолчанию.", e)
    return None

def save_session(state: SessionState):
    if not getattr(state, 'persist_session', True): return
    tmp = f'.ai_session.json.{uuid.uuid4().hex}.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f: f.write(state.model_dump_json(indent=2))
        for _ in range(10):
            try:
                os.replace(tmp, '.ai_session.json')
                break
            except OSError: time.sleep(0.05)
        else: os.remove(tmp)
    except Exception:
        with contextlib.suppress(OSError): os.remove(tmp)

def generate_unified_diff(old_code: str, new_code: str, filename: str) -> str:
    return ''.join(difflib.unified_diff(old_code.splitlines(keepends=True), new_code.splitlines(keepends=True), fromfile=f'a/{filename}', tofile=f'b/{filename}'))

def apply_fixes(patches: List[PatchModel], root_path: str) -> List[str]:
    applied = []
    for patch in patches:
        try:
            if not patch.filepath.strip(): continue
            try: fp = safe_resolve_path(root_path, patch.filepath)
            except ValueError: continue

            os.makedirs(os.path.dirname(fp), exist_ok=True)
            if os.path.exists(fp):
                bak_dir = os.path.join(root_path, '.ai_backups', datetime.datetime.now().strftime('%Y%m%d'))
                os.makedirs(bak_dir, exist_ok=True)
                shutil.copy2(fp, os.path.join(bak_dir, f'{patch.filepath.replace(os.sep, "_").replace("/", "_")}.bak'))

            atomic_write_text(fp, patch.code)
            applied.append(patch.filepath)
        except (OSError, RecursionError, RuntimeError, ValueError) as e:
            # Галлюцинированный патч (слишком длинное имя, битый путь и т.п.) —
            # пропускаем его, но НЕ роняем весь этап применения.
            logging.getLogger("UTILS").warning("Патч пропущен (%s): %s", type(e).__name__, str(patch.filepath)[:80])
            continue
    return applied

class ProjectOverlay:
    HEAVY_DIRS = frozenset({'.git', '.gradle', 'node_modules', 'build', 'dist', 'target', '.idea', '.vscode', '.ai_backups', '__pycache__', '.vs', '.ai_session.json', '.ai_memory.txt'})
    def __init__(self, root: str):
        self.root = os.path.realpath(root)
        self.overlay = tempfile.mkdtemp(prefix='ai_factory_')
        
    def apply_dry_fixes(self, patches: List[PatchModel]) -> list:
        return apply_fixes(patches, self.overlay)

    def resolve_read(self, rel_path: str) -> str:
        """Путь для ЧТЕНИЯ: версия из песочницы, если файл там уже есть (применённый
        патч), иначе — оригинал из реального проекта. Запись всегда идёт в overlay."""
        ov = safe_resolve_path(self.overlay, rel_path)
        if os.path.exists(ov):
            return ov
        return safe_resolve_path(self.root, rel_path)

    def commit_if_success(self, retention_days: int = 7) -> bool:
        bak_root = os.path.join(self.root, '.ai_backups')
        bak_dir = os.path.join(bak_root, datetime.datetime.now().strftime('%Y%m%d_%H%M%S'))
        os.makedirs(bak_dir, exist_ok=True)

        def _backup_original(rel: str) -> None:
            """Копирует СУЩЕСТВУЮЩИЙ оригинал из root в bak_dir перед перезаписью."""
            orig = os.path.join(self.root, rel)
            if not os.path.exists(orig):
                return  # новый файл — бэкапить нечего
            dest = os.path.join(bak_dir, rel)
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                if os.path.isdir(orig):
                    shutil.copytree(orig, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns(*self.HEAVY_DIRS))
                else:
                    shutil.copy2(orig, dest)
            except Exception:
                pass  # бэкап best-effort, не блокирует коммит

        for item in os.listdir(self.overlay):
            if item in self.HEAVY_DIRS: continue
            src = os.path.join(self.overlay, item)
            dst = os.path.join(self.root, item)
            _backup_original(item)
            if os.path.isdir(src): shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns(*self.HEAVY_DIRS))
            else: shutil.copy2(src, dst)

        self._rotate_backups(bak_root, retention_days)
        return True

    @staticmethod
    def _rotate_backups(bak_root: str, retention_days: int) -> None:
        """Удаляет папки бэкапов старше retention_days (ротация, закрывает утечку диска)."""
        if retention_days <= 0 or not os.path.isdir(bak_root):
            return
        cutoff = time.time() - retention_days * 86400
        for name in os.listdir(bak_root):
            p = os.path.join(bak_root, name)
            try:
                if os.path.isdir(p) and os.path.getmtime(p) < cutoff:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError as e:
                logger.debug("Не удалось удалить старый бэкап %s: %s", p, e)

    def cleanup(self) -> None:
        try: shutil.rmtree(self.overlay, ignore_errors=True)
        except Exception: pass

def safe_resolve_path(base: str, target: str) -> str:
    # Галлюцинированный путь может быть патологическим (тысячи сегментов, null-байты):
    # любая ошибка нормализации трактуется как небезопасный путь, а не как краш.
    try:
        if Path(target).is_absolute(): raise ValueError("Абсолютный путь запрещён.")
        if "\x00" in target: raise ValueError("Null-байт в пути запрещён.")
        res = (Path(base).resolve() / target).resolve()
        res.relative_to(Path(base).resolve())
    except ValueError:
        raise
    except (RecursionError, OSError, RuntimeError) as e:
        raise ValueError(f"Некорректный путь: {type(e).__name__}")
    return str(res)

def atomic_write_text(path: str, content: str, encoding: str = 'utf-8') -> None:
    p = Path(path)
    tmp = p.with_name(f"{p.stem}_{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, 'w', encoding=encoding) as f: f.write(content)
        os.replace(tmp, p)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _get_dir_size_mb(path: str) -> float:
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            fp = os.path.join(dirpath, name)
            try:
                if not os.path.islink(fp):
                    total += os.path.getsize(fp)
            except OSError:
                pass
    return round(total / 1048576, 1)


class PipelineCheckpoint:
    """Сохраняет результаты завершённых этапов конвейера на диск, чтобы при сбое
    (исчерпаны все ключи / таймаут) повторный запуск ТОЙ ЖЕ задачи продолжил с
    места обрыва, а не с нуля. Ключ — хэш текста запроса + путь проекта.

    Файл: <project>/.ai_checkpoints/<hash>.json. Хранит сырые JSON-строки этапов.
    Дёшево (несколько КБ), переживает перезапуск приложения и обновление лимитов.
    """
    import hashlib as _hashlib

    DIR_NAME = ".ai_checkpoints"
    TTL_SECONDS = 7 * 86400  # неактуальные чекпойнты старше недели подчищаем

    def __init__(self, project_path: str, request: str):
        self.root = project_path
        self.dir = os.path.join(project_path, self.DIR_NAME)
        key = PipelineCheckpoint._hashlib.sha1(
            f"{os.path.abspath(project_path)}::{request.strip()}".encode("utf-8")
        ).hexdigest()[:16]
        self.path = os.path.join(self.dir, f"{key}.json")
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = json.load(f) or {}
        except Exception as e:
            logger.debug("Чекпойнт %s повреждён/нечитаем, игнорирую: %s", self.path, e)
            self._data = {}

    def get(self, stage: str) -> "str | None":
        """Возвращает сохранённый сырой результат этапа или None."""
        val = self._data.get(stage)
        return val if isinstance(val, str) and val else None

    def save(self, stage: str, raw: str) -> None:
        """Сохраняет сырой результат завершённого этапа (best-effort, не падает)."""
        if not raw:
            return
        self._data[stage] = raw
        self._data["_ts"] = time.time()
        try:
            os.makedirs(self.dir, exist_ok=True)
            atomic_write_text(self.path, json.dumps(self._data, ensure_ascii=False))
        except Exception as e:
            # Чекпойнт — ускорение, его отсутствие не критично, но сбой записи
            # (нет места/прав) полезно видеть в отладке.
            logger.debug("Не удалось сохранить чекпойнт %s: %s", self.path, e)

    def clear(self) -> None:
        """Удаляет чекпойнт (после успешного завершения задачи)."""
        self._data = {}
        try:
            if os.path.exists(self.path):
                os.remove(self.path)
        except OSError:
            pass

    @classmethod
    def purge_stale(cls, project_path: str) -> None:
        """Удаляет чекпойнты старше TTL (фоновая гигиена)."""
        d = os.path.join(project_path, cls.DIR_NAME)
        if not os.path.isdir(d):
            return
        cutoff = time.time() - cls.TTL_SECONDS
        for name in os.listdir(d):
            p = os.path.join(d, name)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except OSError:
                pass
