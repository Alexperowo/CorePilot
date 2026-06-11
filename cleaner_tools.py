import os
import json
import shutil
import time
import hashlib
from datetime import datetime
from utils import _get_dir_size_mb

try:
    import winreg
except ImportError:  # на не-Windows winreg отсутствует
    winreg = None

_CACHE_HINTS = ("cache", "temp", "tmp", "logs", "crashdumps", "webcache", "gpucache", "code cache")

def _quick_hash(path: str, chunk: int = 524288) -> str:
    h = hashlib.md5()
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            h.update(f.read(chunk))
            if size > chunk * 2:
                f.seek(size - chunk)
                h.update(f.read(chunk))
    except OSError:
        return ""
    return h.hexdigest()

def _full_hash(path: str, chunk: int = 1048576) -> str:
    """Полный MD5 файла — для подтверждения дубликатов-финалистов."""
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(chunk), b""):
                h.update(block)
    except OSError:
        return ""
    return h.hexdigest()

_QUARANTINE_LEAF = ".ai_cleaner_quarantine"
QUARANTINE_DIR = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), _QUARANTINE_LEAF)
_PROTECTED: frozenset[str] = frozenset({
    "windows", "system32", "syswow64", "program files", "program files (x86)",
    "programdata", "users", "boot", "efi", "$recycle.bin",
    "system volume information", "recovery", "perflogs", "winsxs",
})

def _quarantine_root_for(path: str, same_drive: bool = True) -> str:
    """Корень карантина: на том же диске, что и источник (мгновенный rename на SSD),
    либо единый в USERPROFILE, если same_drive=False."""
    if not same_drive:
        return QUARANTINE_DIR
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if drive:
        return os.path.join(drive + os.sep, _QUARANTINE_LEAF)
    return QUARANTINE_DIR

def _all_quarantine_roots() -> list[str]:
    """Все возможные корни карантина: USERPROFILE + по одному на каждый фикс. диск."""
    import string
    roots = {QUARANTINE_DIR}
    for d in string.ascii_uppercase:
        drv = f"{d}:\\"
        if os.path.exists(drv):
            roots.add(os.path.join(drv, _QUARANTINE_LEAF))
    return [r for r in roots if os.path.isdir(r)]

def _is_protected(path: str) -> bool:
    parts = os.path.normpath(path).lower().split(os.sep)
    if len(parts) == 2 and parts[1] == "users": return True
    # Карантинную папку Cleaner НЕЛЬЗЯ сканировать/класть в карантин (самопоедание).
    if _QUARANTINE_LEAF.lower() in parts: return True
    return bool((set(parts) - {"users"}) & _PROTECTED)

def move_to_quarantine(paths_json: str, same_drive: bool = True) -> str:
    try: items = json.loads(paths_json).get("items", [])
    except Exception: return json.dumps({"error": "Invalid JSON"})
    sid = datetime.now().strftime("%Y%m%d_%H%M%S")
    created_at = datetime.now().isoformat()

    # session_dir на каждый задействованный диск (мгновенный rename на SSD того же тома)
    session_dirs: dict[str, str] = {}
    manifests: dict[str, dict] = {}
    moved, failed = [], []

    for it in items:
        src = it.get("path", "")
        if not os.path.exists(src): failed.append({"path": src, "error": "Not found"}); continue
        if _is_protected(src): failed.append({"path": src, "error": "Protected"}); continue

        root = _quarantine_root_for(src, same_drive)
        sdir = session_dirs.get(root)
        if sdir is None:
            sdir = os.path.join(root, sid)
            try: os.makedirs(sdir, exist_ok=True)
            except Exception as e: failed.append({"path": src, "error": f"mkdir quarantine: {e}"}); continue
            session_dirs[root] = sdir
            manifests[root] = {"session_id": sid, "created_at": created_at, "moves": []}

        dest = os.path.join(sdir, os.path.basename(src) + f"__{sid}")
        try:
            shutil.move(src, dest)
            sz = _get_dir_size_mb(dest) if os.path.isdir(dest) else round(os.path.getsize(dest)/1048576, 2)
            en = {"original": src, "quarantine": dest, "size_mb": sz, "reason": it.get("reason", "")}
            manifests[root]["moves"].append(en)
            moved.append(en)
        except Exception as e: failed.append({"path": src, "error": str(e)})

    manifest_paths = []
    for root, man in manifests.items():
        mpath = os.path.join(session_dirs[root], "manifest.json")
        try:
            with open(mpath, "w", encoding="utf-8") as f: json.dump(man, f, ensure_ascii=False)
            manifest_paths.append(mpath)
        except Exception: pass

    return json.dumps({
        "session_id": sid, "quarantine_dirs": list(session_dirs.values()), "manifest_paths": manifest_paths,
        "moved_count": len(moved), "failed_count": len(failed),
        "freed_mb": round(sum(x["size_mb"] for x in moved), 1),
        "moved": moved, "failed": failed
    })

def _session_dirs(session_id: str) -> list[str]:
    """Все каталоги сессии (session_id) по всем корням карантина."""
    sid = os.path.basename(session_id)
    out = []
    for root in _all_quarantine_roots():
        sdir = os.path.join(root, sid)
        if os.path.exists(os.path.join(sdir, "manifest.json")):
            out.append(sdir)
    return out

def undo_quarantine(session_id: str) -> str:
    dirs = _session_dirs(session_id)
    if not dirs: return json.dumps({"error": "Manifest not found"})
    restored, failed = [], []
    for sdir in dirs:
        # Битый manifest одной сессии не должен рушить всё восстановление.
        try:
            with open(os.path.join(sdir, "manifest.json"), "r", encoding="utf-8") as f:
                man = json.load(f)
        except Exception as e:
            failed.append({"path": sdir, "error": f"manifest нечитаем: {e}"})
            continue
        ok_dir = True
        for mv in man.get("moves", []):
            src, dest = mv["quarantine"], mv["original"]
            if not os.path.exists(src):
                failed.append({"path": dest, "error": "Already deleted"}); ok_dir = False; continue
            # ЗАЩИТА ОТ ПЕРЕЗАПИСИ: если по исходному пути уже появился новый файл
            # (пользователь создал его после карантина) — НЕ затираем, кладём рядом.
            if os.path.exists(dest):
                base, ext = os.path.splitext(dest)
                alt = f"{base}_restored_{datetime.now().strftime('%H%M%S')}{ext}"
                failed.append({"path": dest, "error": f"путь занят — восстановлено как {os.path.basename(alt)}"})
                dest = alt
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.move(src, dest)
                restored.append(dest)
            except Exception as e: failed.append({"path": dest, "error": str(e)}); ok_dir = False
        if ok_dir: shutil.rmtree(sdir, ignore_errors=True)
    return json.dumps({"session_id": os.path.basename(session_id), "restored_count": len(restored), "failed_count": len(failed), "failed": failed})

def execute_permanent_deletion(session_id: str) -> str:
    dirs = _session_dirs(session_id)
    if not dirs: return json.dumps({"error": "Session not found"})
    del_mb, del_cnt = 0.0, 0
    for sdir in dirs:
        with open(os.path.join(sdir, "manifest.json"), "r", encoding="utf-8") as f: man = json.load(f)
        for mv in man.get("moves", []):
            qp = mv["quarantine"]
            if os.path.isdir(qp):
                del_mb += _get_dir_size_mb(qp); shutil.rmtree(qp, ignore_errors=True); del_cnt += 1
            elif os.path.isfile(qp):
                del_mb += os.path.getsize(qp)/1048576; os.remove(qp); del_cnt += 1
        shutil.rmtree(sdir, ignore_errors=True)
    return json.dumps({"session_id": os.path.basename(session_id), "permanently_deleted_count": del_cnt, "freed_mb": round(del_mb, 1), "status": "completed"})

def list_quarantine_sessions() -> str:
    # Агрегируем сессии по всем дискам: одна session_id может жить на нескольких корнях.
    agg: dict[str, dict] = {}
    for root in _all_quarantine_roots():
        for sid in os.listdir(root):
            mpath = os.path.join(root, sid, "manifest.json")
            if not os.path.exists(mpath): continue
            try:
                with open(mpath, "r", encoding="utf-8") as f: m = json.load(f)
                sz = _get_dir_size_mb(os.path.join(root, sid))
                rec = agg.setdefault(sid, {"session_id": sid, "created_at": m.get("created_at", ""), "items_count": 0, "size_mb": 0.0})
                rec["items_count"] += len(m.get("moves", []))
                rec["size_mb"] = round(rec["size_mb"] + sz, 1)
            except Exception: pass
    sessions = sorted(agg.values(), key=lambda x: x["created_at"], reverse=True)
    return json.dumps({"sessions": sessions, "total_mb": round(sum(s["size_mb"] for s in sessions), 1)})

# Сканеры — детерминированные функции (вызываются напрямую из cleaner_flow, без LLM).
def scan_disk_intelligent(root: str = "", min_size_mb: float = 5.0) -> str:
    """Scans the immediate sub-folders of a Windows directory (skipping protected
    system paths) and returns JSON {"items":[...]} with size_mb, category and a
    risk_hint (safe|warn|danger) for each folder above min_size_mb."""
    root = (root or os.environ.get("LOCALAPPDATA") or "C:\\").strip()
    if not os.path.isdir(root):
        return json.dumps({"error": f"Папка не найдена: {root}", "items": []}, ensure_ascii=False)

    items = []
    try:
        children = list(os.scandir(root))
    except OSError as e:
        return json.dumps({"error": str(e), "items": []}, ensure_ascii=False)

    for entry in children:
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        full = entry.path
        if _is_protected(full):
            continue
        size_mb = _get_dir_size_mb(full)
        if size_mb < min_size_mb:
            continue
        is_cache = any(h in entry.name.lower() for h in _CACHE_HINTS)
        items.append({
            "path": full, "size_mb": size_mb,
            "category": "cache" if is_cache else "folder",
            "risk_hint": "safe" if is_cache else "warn",
            "explanation": "Кэш/временные файлы — безопасно удалять." if is_cache
                           else "Папка пользователя — требует проверки.",
        })
    items.sort(key=lambda x: x["size_mb"], reverse=True)
    return json.dumps({
        "scanned_root": root, "count": len(items),
        "total_mb": round(sum(i["size_mb"] for i in items), 1),
        "items": items[:100],
    }, ensure_ascii=False)


def scan_downloads_folder(min_size_mb: float = 30.0, older_than_days: int = 30) -> str:
    """Scans the Windows Downloads folder and returns JSON {"items":[...]} of files
    larger than min_size_mb or older than older_than_days (risk_hint=warn)."""
    downloads = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), "Downloads")
    if not os.path.isdir(downloads):
        return json.dumps({"error": f"Папка не найдена: {downloads}", "items": []}, ensure_ascii=False)
    cutoff = time.time() - older_than_days * 86400
    items = []
    for entry in os.scandir(downloads):
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            st = entry.stat()
            size_mb = round(st.st_size / 1048576, 2)
            old = st.st_mtime < cutoff
            if size_mb < min_size_mb and not old:
                continue
            items.append({
                "path": entry.path, "size_mb": size_mb, "category": "download",
                "risk_hint": "warn",
                "explanation": ("Старый файл" if old else "Крупный файл") + " в Загрузках.",
            })
        except OSError:
            continue
    items.sort(key=lambda x: x["size_mb"], reverse=True)
    return json.dumps({
        "scanned_root": downloads, "count": len(items),
        "total_mb": round(sum(i["size_mb"] for i in items), 1),
        "items": items[:200],
    }, ensure_ascii=False)


def find_duplicate_files(root: str = "", min_size_mb: float = 10.0, timeout_sec: int = 60, full_hash: bool = True) -> str:
    """Finds duplicate files under a Windows directory. Pipeline: group by size →
    quick head/tail MD5 to prune → (if full_hash) full MD5 to CONFIRM finalists.
    Returns JSON {"items":[...]} where each item is one redundant copy
    (risk_hint=warn); the first copy of each confirmed group is kept."""
    root = (root or os.environ.get("USERPROFILE") or "C:\\").strip()
    if not os.path.isdir(root):
        return json.dumps({"error": f"Папка не найдена: {root}", "items": []}, ensure_ascii=False)
    deadline = time.time() + max(10, timeout_sec)
    by_size: dict[int, list[str]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        if time.time() > deadline:
            break
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in _PROTECTED and d != _QUARANTINE_LEAF]
        for name in filenames:
            fp = os.path.join(dirpath, name)
            try:
                sz = os.path.getsize(fp)
            except OSError:
                continue
            if sz >= min_size_mb * 1048576:
                by_size.setdefault(sz, []).append(fp)

    items = []
    for sz, paths in by_size.items():
        if len(paths) < 2 or time.time() > deadline:
            continue
        # Этап 1: быстрый хэш головы/хвоста — отсев очевидно разных.
        by_quick: dict[str, list[str]] = {}
        for p in paths:
            if h := _quick_hash(p):
                by_quick.setdefault(h, []).append(p)
        size_mb = round(sz / 1048576, 2)
        for quick_group in by_quick.values():
            if len(quick_group) < 2:
                continue
            # Этап 2: ПОЛНЫЙ хэш финалистов — подтверждение настоящих дубликатов
            # (защита от ложного совпадения при одинаковых краях, разной середине).
            if full_hash:
                by_full: dict[str, list[str]] = {}
                for p in quick_group:
                    if h := _full_hash(p):
                        by_full.setdefault(h, []).append(p)
                confirmed_groups = [g for g in by_full.values() if len(g) >= 2]
            else:
                confirmed_groups = [quick_group]
            for group in confirmed_groups:
                for dup in group[1:]:
                    items.append({
                        "path": dup, "size_mb": size_mb, "category": "duplicate",
                        "risk_hint": "warn", "explanation": f"Дубликат файла: {group[0]}",
                    })
    items.sort(key=lambda x: x["size_mb"], reverse=True)
    return json.dumps({
        "scanned_root": root, "count": len(items),
        "total_mb": round(sum(i["size_mb"] for i in items), 1),
        "items": items[:200],
    }, ensure_ascii=False)


def get_disk_usage_report(root: str = "") -> str:
    """Returns JSON disk-usage stats (total/used/free GB and percent) for all fixed
    Windows drives, or for the drive containing `root` if provided."""
    import string
    if root and os.path.exists(root):
        targets = [os.path.splitdrive(os.path.abspath(root))[0] + os.sep]
    else:
        targets = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
    drives = []
    for drv in targets:
        try:
            u = shutil.disk_usage(drv)
            drives.append({
                "drive": drv,
                "total_gb": round(u.total / 1073741824, 1),
                "used_gb": round(u.used / 1073741824, 1),
                "free_gb": round(u.free / 1073741824, 1),
                "percent_used": round(u.used / u.total * 100, 1) if u.total else 0.0,
            })
        except OSError:
            continue
    return json.dumps({"drives": drives}, ensure_ascii=False)


def scan_startup_entries() -> str:
    """Lists Windows auto-start entries from HKCU/HKLM Run keys and the user Startup
    folder. Returns JSON {"items":[...]} with risk_hint=danger — startup items must
    never be auto-deleted, only reviewed."""
    items = []
    if winreg is not None:
        run_keys = [
            (winreg.HKEY_CURRENT_USER,  r"Software\Microsoft\Windows\CurrentVersion\Run", "HKCU"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run", "HKLM"),
        ]
        for hive, subkey, label in run_keys:
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    i = 0
                    while True:
                        try:
                            name, value, _ = winreg.EnumValue(key, i)
                        except OSError:
                            break
                        items.append({
                            "path": str(value), "name": name, "size_mb": 0.0,
                            "category": "startup_registry", "hive": label,
                            "risk_hint": "danger",
                            "explanation": "Автозапуск (реестр) — не удалять автоматически.",
                        })
                        i += 1
            except OSError:
                continue
    startup_dir = os.path.join(os.environ.get("APPDATA", ""),
                               r"Microsoft\Windows\Start Menu\Programs\Startup")
    if startup_dir and os.path.isdir(startup_dir):
        for entry in os.scandir(startup_dir):
            items.append({
                "path": entry.path, "name": entry.name, "size_mb": 0.0,
                "category": "startup_folder", "risk_hint": "danger",
                "explanation": "Автозапуск (папка) — не удалять автоматически.",
            })
    return json.dumps({"count": len(items), "items": items}, ensure_ascii=False)
