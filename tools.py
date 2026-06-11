import os
import time
import shutil
import hashlib
import subprocess
try:
    import winreg
    HAS_WINREG = True
except ImportError:
    winreg = None
    HAS_WINREG = False
import psutil
from pathlib import Path
from crewai.tools import tool
from utils import get_runtime_context, safe_resolve_path, _get_dir_size_mb

IMAGE_SENTINEL = "IMAGE_GENERATION_COMPLETE"


_PROTECTED: frozenset[str] = frozenset({
    "windows", "system32", "syswow64", "winsxs", "program files",
    "program files (x86)", "programdata", "users",
    "$recycle.bin", "system volume information", "recovery",
    "boot", "efi", "perflogs",
})

def _file_hash_reliable(path: str) -> str:
    h = hashlib.md5()
    chunk = 512 * 1024
    try:
        file_size = os.path.getsize(path)
        with open(path, "rb") as f:
            h.update(f.read(chunk))
            if file_size > chunk * 2:
                f.seek(max(0, file_size - chunk))
                h.update(f.read(chunk))
    except OSError: return ""
    return h.hexdigest()

@tool("Decompile APK")
def decompile_apk(apk_path: str, output_dir: str) -> str:
    """Decompiles an APK using jadx or apktool into the specified output directory."""
    try: state = get_runtime_context().state
    except RuntimeError: state = None
    project_path = getattr(state, "project_path", ".") if state else "."
    
    try:
        resolved_apk = safe_resolve_path(project_path, apk_path)
        resolved_out = safe_resolve_path(project_path, output_dir)
    except ValueError as e: return f"❌ Ошибка пути: {e}"

    if not os.path.isfile(resolved_apk): return f"❌ APK не найден: {apk_path}"

    if shutil.which("jadx"):
        cmd = ["jadx", "-d", resolved_out, resolved_apk]
        tool_name = "jadx"
    elif shutil.which("apktool"):
        cmd = ["apktool", "d", resolved_apk, "-o", resolved_out, "--force"]
        tool_name = "apktool"
    else: return "❌ jadx или apktool не найдены."

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except Exception as e: return f"❌ Ошибка запуска: {e}"

    if res.returncode != 0: return f"❌ Ошибка {tool_name} (code {res.returncode}):\n{res.stderr[-2000:]}"
    return f"✅ Успешно {tool_name}:\n{resolved_out}"

@tool("Run ADB Command")
def run_adb_command(command: str) -> str:
    """Runs a specified ADB command, verifying and blocking hazardous or bad operations."""
    for blk in ("erase", "format", "reboot-bootloader", "flashing unlock"):
        if blk in command.lower(): return f"❌ Заблокировано: {blk}"
    if not shutil.which("adb"): return "❌ adb не найден."
    
    import shlex
    try:
        res = subprocess.run(["adb"] + shlex.split(command, posix=False), capture_output=True, text=True, timeout=120)
    except Exception as e: return f"❌ Ошибка adb: {e}"
    
    out = (res.stdout.strip() or res.stderr.strip())[-3000:]
    if res.returncode != 0: return f"❌ adb error {res.returncode}:\n{out}"
    return f"✅ adb {command}\n{out}" if out else f"✅ adb {command}"

@tool("Check Build Result")
def check_build_result(log_path: str) -> str:
    """Parses a build log file pointing out compile errors, warnings, failures, or task issues."""
    try: state = get_runtime_context().state
    except RuntimeError: state = None
    try: resolved = safe_resolve_path(getattr(state, "project_path", "."), log_path)
    except ValueError as e: return f"❌ Ошибка пути: {e}"
    
    if not os.path.isfile(resolved): return f"❌ Файл не найден: {resolved}"
    try: raw = Path(resolved).read_text(encoding="utf-8", errors="replace")
    except Exception as e: return f"❌ Ошибка чтения: {e}"
    
    import re
    incl = re.compile(r"(^\s*(error|warning|failure|failed|conflict|duplicate|unresolved|task .+ failed|> task))", re.I)
    excl = re.compile(r"(^\s*(download|starting|daemon))", re.I)
    
    cap = [l.strip() for l in raw.splitlines() if l.strip() and not excl.search(l) and incl.search(l)]
    if not cap: return f"ℹ️ Ошибок не найдено. Хвост:\n{chr(10).join(raw.splitlines()[-30:])}"
    
    unique = list(dict.fromkeys(cap))
    return "🔨 **Анализ лога сборки**\n" + "\n".join(unique[:40])

@tool("Find Duplicate Files")
def find_duplicate_files(target_dir: str = "", min_size_mb: float = 10.0, timeout_sec: int = 60) -> str:
    """Scans a directory recursively to identify duplicate files based on sizes and MD5 checksum hashes."""
    target_dir = target_dir or os.environ.get("USERPROFILE", "C:\\")
    if not os.path.isdir(target_dir): return f"❌ Папка не найдена: {target_dir}"
    t_end = time.time() + timeout_sec
    sizes = {}
    
    for r, ds, fs in os.walk(target_dir):
        if time.time() > t_end: break
        ds[:] = [d for d in ds if d.lower() not in _PROTECTED]
        for f in fs:
            fp = os.path.join(r, f)
            try:
                if (sz := os.path.getsize(fp)) >= min_size_mb * 1048576:
                    sizes.setdefault(sz, []).append(fp)
            except OSError: pass

    cands = {sz: ps for sz, ps in sizes.items() if len(ps) > 1}
    dups, waste_total = [], 0
    for sz, ps in cands.items():
        if time.time() > t_end: break
        hs = {}
        for p in ps:
            if h := _file_hash_reliable(p): hs.setdefault(h, []).append(p)
        for identical in hs.values():
            if len(identical) > 1:
                waste = (len(identical)-1) * (sz / 1048576)
                waste_total += waste
                dups.append((identical, waste))
    
    if not dups: return "✅ Дубликатов не найдено."
    return f"👯 **Дубликаты** ({waste_total:.1f} МБ лишних):\n" + "\n".join(f"- {len(grp)} файлов, лишних {w:.1f} МБ:\n  " + "\n  ".join(grp) for grp, w in dups[:20])

@tool("Scan System Leftovers")
def scan_system_leftovers(targets: str = "all") -> str:
    """Scans predefined cache paths like .gradle, npm-cache, and pip caches to report storage consumed."""
    user = os.environ.get("USERPROFILE", "C:\\Users\\Default")
    local = os.environ.get("LOCALAPPDATA", os.path.join(user, "AppData", "Local"))
    ps = {"GRADLE": os.path.join(user, ".gradle", "caches"), "NPM": os.path.join(local, "npm-cache"), "PIP": os.path.join(local, "pip", "cache")}
    res, tot = [], 0
    for n, p in ps.items():
        if os.path.exists(p):
            sz = _get_dir_size_mb(p)
            tot += sz
            res.append(f"[{n}] {p} → {sz} МБ")
    return f"📁 **Стандартные кэши:**\n" + "\n".join(res) + f"\nИтого: {tot} МБ" if res else "✅ Кэши пусты."

@tool("Get Project Tree")
def get_project_tree(path: str = ".") -> str:
    """Retrieves the project folder directory tree recursively with configured ignore rules."""
    try:
        ctx = get_runtime_context()
        # Дерево строится по РЕАЛЬНОМУ проекту (root), а не по пустой песочнице.
        base = ctx.overlay.root if ctx.overlay else ctx.state.project_path
        target = safe_resolve_path(base, path)
    except Exception as e: return f"❌ Ошибка пути: {e}"
    
    lim = getattr(ctx.state, 'ui_tree_limit', 500) if 'ctx' in locals() else 500
    ign = {'.git', '__pycache__', 'node_modules', 'venv'}
    tree, tp = [], Path(target).resolve()
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d not in ign]
        lvl = len(Path(root).resolve().relative_to(tp).parts) if tp else 0
        tree.append("    "*lvl + f"{os.path.basename(root)}/")
        tree.extend("    "*(lvl+1) + f for f in files)
    return "\n".join(tree[:lim])

@tool("Read File Content")
def read_file_content(filepath: str) -> str:
    """Reads and returns the text content of a specified file with safety resolving and size boundaries."""
    try:
        ctx = get_runtime_context()
        # Читаем версию из песочницы (если патч уже применён), иначе — оригинал.
        target = ctx.overlay.resolve_read(filepath) if ctx.overlay else safe_resolve_path(ctx.state.project_path, filepath)
        
        lim = getattr(ctx.state, 'ui_file_limit_kb', 500) * 1024
        if os.path.getsize(target) > lim: return f"⚠️ Файл огромный (> {lim//1024} КБ)."
        with open(target, 'r', encoding='utf-8', errors='replace') as f: return f.read()
    except Exception as e: return f"❌ {e}"

@tool("Run Terminal Command")
def run_terminal_command(command: str) -> str:
    """Executes a terminal command WITHOUT a shell (shell=False) to prevent command
    injection via prompt (&&, |, ;, > and other shell operators). Interactive approval
    is required; chained/piped commands are refused with a clear message rather than
    executed through a shell."""
    import shlex
    try:
        ctx = get_runtime_context()
        if getattr(ctx.ui, 'auto_approve', False): return "⛔ Заблокировано в Демоне."
        if not ctx.ui.confirm_command(command, "CMD"): return "⛔ Отклонено человеком."

        # Защита от инъекции: shell-операторы могли прийти из промпта (анализируемый
        # код, ответ LLM). Не пропускаем их в оболочку — отказываем явно.
        _SHELL_META = ("&&", "||", "|", ";", ">", "<", "`", "$(", "&", "\n")
        hit = next((m for m in _SHELL_META if m in command), None)
        if hit:
            return (f"⛔ Команда содержит shell-оператор '{hit}' и отклонена в целях "
                    f"безопасности (защита от инъекции). Запустите одну команду без "
                    f"конвейеров/цепочек, либо выполните шаги по отдельности.")

        try:
            argv = shlex.split(command, posix=(os.name != 'nt'))
        except ValueError as e:
            return f"⛔ Не удалось разобрать команду (незакрытая кавычка?): {e}"
        if not argv:
            return "⛔ Пустая команда."

        target = ctx.overlay.overlay if ctx.overlay else ctx.state.project_path
        env = None
        if gp := getattr(ctx.state, 'gradle_custom_path', '').strip():
            env = os.environ.copy()
            env['PATH'] = gp + os.pathsep + env.get('PATH', '')

        cpg = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x00000200)
        proc = subprocess.Popen(argv, shell=False, cwd=target, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env,
                                creationflags=cpg if os.name == 'nt' else 0)

        try: stdout, stderr = proc.communicate(timeout=1200)
        except subprocess.TimeoutExpired:
            if os.name == 'nt': subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)], capture_output=True)
            else: proc.kill()
            stdout, stderr = proc.communicate()
            return f"⚠️ Таймаут 1200с.\nSTDOUT:\n{stdout.decode('utf8','ignore')}\nSTDERR:\n{stderr.decode('utf8','ignore')}"

        lim = getattr(ctx.state, 'max_tool_output_chars', 4000)
        return f"STDOUT:\n{stdout.decode('utf8','ignore')}\nSTDERR:\n{stderr.decode('utf8','ignore')}"[:lim]
    except FileNotFoundError as e:
        return f"❌ Команда/программа не найдена: {e}"
    except Exception as e: return f"❌ {e}"

@tool("Generate Image")
def generate_image(user_request: str) -> str:
    """Генерирует изображение по текстовому запросу. Источник выбирается настройкой
    image_source: 'forge' (SD Forge/AUTOMATIC1111), 'comfy' (ComfyUI, порт 8188) или
    'cloud' (облачный API, по умолчанию HuggingFace Inference — FLUX/SDXL). Если выбран
    локальный источник, но он недоступен, а облачный ключ есть — автоматический фолбэк в
    облако. Возвращает путь к сохранённому PNG либо понятное сообщение об ошибке."""
    try:
        ctx = get_runtime_context()
        state = ctx.state
    except RuntimeError:
        state = None

    source = (getattr(state, "image_source", "") or "forge").strip().lower()

    # Облачный путь выбран явно — сразу в облако.
    if source == "cloud":
        return _generate_image_cloud(state, user_request)

    # Локальный ComfyUI выбран явно.
    if source == "comfy":
        comfy_result = _generate_image_comfy(state, user_request)
        if comfy_result.startswith("⚠️") and _has_image_cloud_key(state):
            cloud_result = _generate_image_cloud(state, user_request)
            if cloud_result.startswith("✅"):
                return cloud_result + "\n(ComfyUI был недоступен — сгенерировано облаком.)"
            return comfy_result + "\n— и облачный фолбэк не удался: " + cloud_result
        return comfy_result

    # По умолчанию Forge; при недоступности — фолбэк в облако, если есть ключ.
    forge_result = _generate_image_forge(state, user_request)
    if forge_result.startswith("⚠️") and _has_image_cloud_key(state):
        cloud_result = _generate_image_cloud(state, user_request)
        if cloud_result.startswith("✅"):
            return cloud_result + "\n(SD Forge был недоступен — сгенерировано облаком.)"
        return forge_result + "\n— и облачный фолбэк не удался: " + cloud_result
    return forge_result


def _generate_image_forge(state, user_request: str) -> str:
    """Локальная генерация через SD Forge / AUTOMATIC1111 (txt2img API)."""
    import base64
    import requests
    forge_url = (getattr(state, "forge_url", "") or "http://127.0.0.1:7860").rstrip("/")
    model = (getattr(state, "forge_model", "") or "").strip()

    payload = {
        "prompt": user_request,
        "steps": 25, "width": 512, "height": 512,
        "cfg_scale": 7, "sampler_name": "DPM++ 2M",
    }
    if model:
        payload["override_settings"] = {"sd_model_checkpoint": model}

    try:
        resp = requests.post(f"{forge_url}/sdapi/v1/txt2img", json=payload, timeout=600)
    except requests.exceptions.ConnectionError:
        return (f"⚠️ SD Forge недоступен по адресу {forge_url}. "
                f"Запустите SD Forge / AUTOMATIC1111 с флагом --api и укажите Forge API URL в настройках.")
    except Exception as e:
        return f"❌ Ошибка обращения к SD Forge: {e}"

    if resp.status_code != 200:
        return f"❌ SD Forge вернул код {resp.status_code}: {resp.text[:300]}"

    try:
        images = resp.json().get("images", [])
        if not images:
            return "❌ SD Forge не вернул изображений."
        return _save_image_bytes(state, base64.b64decode(images[0].split(",", 1)[-1]), "sd")
    except Exception as e:
        return f"❌ Не удалось сохранить изображение: {e}"


def _generate_image_comfy(state, user_request: str) -> str:
    """Локальная генерация через ComfyUI API. ComfyUI работает по схеме:
    POST воркфлоу на /prompt -> опрос /history/{id} до готовности -> картинка с /view.
    Используем минимальный txt2img-граф (checkpoint -> CLIP -> KSampler -> VAE -> Save)."""
    import requests
    comfy_url = (getattr(state, "comfy_url", "") or "http://127.0.0.1:8188").rstrip("/")
    ckpt = (getattr(state, "comfy_model", "") or "").strip()  # имя .safetensors, опц.

    # Минимальный воркфлоу в API-формате ComfyUI.
    seed = int(time.time()) % 2_000_000_000
    wf = {
        "3": {"class_type": "KSampler", "inputs": {
            "seed": seed, "steps": 20, "cfg": 7.0, "sampler_name": "euler",
            "scheduler": "normal", "denoise": 1.0,
            "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
        "4": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": ckpt or "v1-5-pruned-emaonly.safetensors"}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": user_request, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "lowres, bad anatomy, blurry", "clip": ["4", 1]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "CorePilot", "images": ["8", 0]}},
    }
    try:
        r = requests.post(f"{comfy_url}/prompt", json={"prompt": wf}, timeout=30)
    except requests.exceptions.ConnectionError:
        return (f"⚠️ ComfyUI недоступен по адресу {comfy_url}. Запустите ComfyUI "
                f"(он слушает порт 8188) и укажите ComfyUI API URL в настройках.")
    except Exception as e:
        return f"❌ Ошибка обращения к ComfyUI: {e}"
    if r.status_code != 200:
        return f"❌ ComfyUI вернул код {r.status_code}: {r.text[:200]}"

    try:
        prompt_id = r.json().get("prompt_id", "")
        if not prompt_id:
            return "❌ ComfyUI не вернул prompt_id."
        # Опрос истории до готовности (ComfyUI генерирует асинхронно).
        for _ in range(120):  # до ~2 минут
            time.sleep(1)
            h = requests.get(f"{comfy_url}/history/{prompt_id}", timeout=10)
            if not h.ok:
                continue
            hist = h.json().get(prompt_id, {})
            outputs = hist.get("outputs", {})
            for node in outputs.values():
                for img in node.get("images", []):
                    params = {"filename": img["filename"], "subfolder": img.get("subfolder", ""),
                              "type": img.get("type", "output")}
                    iv = requests.get(f"{comfy_url}/view", params=params, timeout=30)
                    if iv.ok:
                        return _save_image_bytes(state, iv.content, "comfy")
        return "⚠️ ComfyUI не вернул изображение за отведённое время (модель загружается?)."
    except Exception as e:
        return f"❌ Ошибка получения результата ComfyUI: {e}"


# Облачные провайдеры генерации изображений (бесплатные тарифы). Имя ключа —
# то же, что у текстовых (например, huggingface), плюс синонимы из init_api_keys.
_IMAGE_CLOUD = {
    "huggingface": {
        "url_tmpl": "https://api-inference.huggingface.co/models/{model}",
        "default_model": "black-forest-labs/FLUX.1-schnell",
    },
}


def _has_image_cloud_key(state) -> bool:
    """Есть ли ключ у выбранного (или дефолтного) облачного image-провайдера."""
    try:
        import agents
        provider = (getattr(state, "image_provider", "") or "huggingface").strip()
        return bool(agents.peek_api_key(provider))
    except Exception:
        return False


def _generate_image_cloud(state, user_request: str) -> str:
    """Облачная генерация (по умолчанию HuggingFace Inference API) с ротацией ключей.
    HF отдаёт бинарный PNG/JPEG. Ключи берутся из общего хранилища (secrets.toml)."""
    import agents
    provider = (getattr(state, "image_provider", "") or "huggingface").strip().lower()
    meta = _IMAGE_CLOUD.get(provider)
    if not meta:
        return f"❌ Неизвестный облачный image-провайдер: {provider}."
    model = (getattr(state, "image_cloud_model", "") or meta["default_model"]).strip()
    url = meta["url_tmpl"].format(model=model)

    keys = list(agents.API_KEYS.get(provider, []))
    if not keys:
        return (f"⚠️ Нет ключа для облачной генерации ({provider}). Добавьте его в "
                f"secrets.toml (например, HF_TOKEN) или выберите источник 'forge'.")

    import requests
    last_err = ""
    for key in keys:  # ротация: при исчерпании ключа пробуем следующий
        try:
            resp = requests.post(url, json={"inputs": user_request},
                                 headers={"Authorization": f"Bearer {key}",
                                          "Accept": "image/png"}, timeout=300)
        except Exception as e:
            last_err = str(e)[:200]
            continue
        if resp.status_code == 200 and resp.headers.get("Content-Type", "").startswith("image/"):
            return _save_image_bytes(state, resp.content, "cloud")
        if resp.status_code in (429, 401, 403, 402):
            last_err = f"HTTP {resp.status_code} (ключ исчерпан/не годен)"
            continue  # следующий ключ
        if resp.status_code == 503:
            return ("⚠️ Облачная модель загружается (HTTP 503). Подождите минуту и повторите.")
        last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
        break

    return f"❌ Облачная генерация не удалась ({provider}): {last_err}"


def _save_image_bytes(state, raw: bytes, prefix: str) -> str:
    """Сохраняет байты изображения в проект/generated_images и возвращает путь."""
    try:
        out_dir = os.path.join(getattr(state, "project_path", ".") if state else ".",
                               "generated_images")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"{prefix}_{int(time.time())}.png")
        with open(out_path, "wb") as f:
            f.write(raw)
        return f"✅ Изображение сохранено: {out_path}"
    except Exception as e:
        return f"❌ Не удалось сохранить изображение: {e}"

@tool("Search Code")
def search_code(query: str, file_extensions: str = "") -> str:
    """Searches the project source tree for a literal substring (case-insensitive),
    returning matching files with line numbers. file_extensions is an optional
    comma-separated filter, e.g. 'py,txt'."""
    if not query or not query.strip():
        return "❌ Пустой поисковый запрос."
    try:
        ctx = get_runtime_context()
        # Поиск по РЕАЛЬНОМУ проекту (root), а не по пустой песочнице.
        base = ctx.overlay.root if ctx.overlay else ctx.state.project_path
        lim = getattr(ctx.state, "max_tool_output_chars", 15000)
    except RuntimeError:
        return "❌ RuntimeContext не установлен."

    exts = {("." + e.strip().lstrip(".")).lower() for e in file_extensions.split(",") if e.strip()}
    needle = query.lower()
    ignore = {".git", "__pycache__", "node_modules", "venv", ".idea", ".vscode", ".ai_backups"}
    results: list[str] = []

    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in ignore]
        for fname in files:
            if exts and os.path.splitext(fname)[1].lower() not in exts:
                continue
            fp = os.path.join(root, fname)
            try:
                if os.path.getsize(fp) > 2 * 1024 * 1024:  # пропускаем файлы > 2 МБ
                    continue
                rel = os.path.relpath(fp, base)
                with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                    for ln, line in enumerate(fh, 1):
                        if needle in line.lower():
                            results.append(f"{rel}:{ln}: {line.strip()[:200]}")
            except OSError:
                continue
            if len(results) >= 200:
                break
        if len(results) >= 200:
            break

    if not results:
        return f"🔍 Совпадений для «{query}» не найдено."
    return (f"🔍 Найдено совпадений: {len(results)}\n" + "\n".join(results))[:lim]
