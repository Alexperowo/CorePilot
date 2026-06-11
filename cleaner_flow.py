from __future__ import annotations

import asyncio
import json
import logging
import os

import chainlit as cl
from crewai import Crew, Task

from agents import safe_kickoff
from cleaner_agents import make_cleaner_analyzer
from pipeline_parser import _extract_json as _parse_json_from_agent
from utils import SessionState, RuntimeContext, DummyInteractionHandler, set_runtime_context, reset_runtime_context
import cleaner_tools as ct

logger = logging.getLogger("CLEANER_FLOW")
_STEP_TIMEOUT = 300

def _get_safe_state() -> SessionState:
    from utils import SessionState
    kwargs = {k: cl.user_session.get(k) for k in SessionState.model_fields if cl.user_session.get(k) is not None}
    return SessionState.model_validate(kwargs)

def _fmt_mb(mb: float) -> str: return f"{mb/1024:.1f} ГБ" if mb >= 1024 else f"{mb:.1f} МБ"

def _render_risk_group(title: str, emoji: str, items: list[dict]) -> str:
    if not items: return ""
    total_mb = sum(x.get("size_mb", 0) for x in items)
    lines = [f"\n{emoji} **{title}** — {len(items)} объектов, {_fmt_mb(total_mb)}\n"]
    for item in items[:15]:
        detail = item.get("explanation", "") or item.get("description", "") or item.get("category", "")
        lines.append(f"  `{item.get('path', '')}` — {_fmt_mb(item.get('size_mb', 0))}")
        if detail: lines.append(f"  *{detail}*")
    if len(items) > 15: lines.append(f"  _...и ещё {len(items) - 15} объектов_")
    return "\n".join(lines)

async def _show_risk_report(report: dict) -> str:
    summary = report.get("summary", {})
    safe_items, warn_items, danger_items = report.get("safe", []), report.get("warn", []), report.get("danger", [])
    safe_mb = summary.get("safe_mb", sum(x.get("size_mb", 0) for x in safe_items))
    warn_mb = summary.get("warn_mb", sum(x.get("size_mb", 0) for x in warn_items))

    return (
        f"## 🧹 Отчёт AI Cleaner\n\n"
        f"| Уровень риска | Объектов | Размер |\n|---|---|---|\n"
        f"| ✅ Безопасно удалить | {len(safe_items)} | {_fmt_mb(safe_mb)} |\n"
        f"| ⚠️ Требует внимания | {len(warn_items)} | {_fmt_mb(warn_mb)} |\n"
        f"| 🚨 Не трогать | {len(danger_items)} | — |\n"
        + _render_risk_group("БЕЗОПАСНО УДАЛИТЬ", "✅", safe_items)
        + _render_risk_group("ТРЕБУЕТ ВНИМАНИЯ", "⚠️", warn_items)
        + _render_risk_group("НЕ ТРОГАТЬ (системные/важные)", "🚨", danger_items)
        + (f"\n\n---\n**💡 Вывод аналитика:** {report.get('analyzer_notes', '')}" if report.get("analyzer_notes") else "")
    )

async def run_cleaner_flow(request: str, scan_params: dict | None = None):
    state = _get_safe_state()
    ctx = RuntimeContext(state=state, ui=DummyInteractionHandler(), overlay=None)
    token = set_runtime_context(ctx)
    try: await _run_cleaner_crew(request, state, scan_params)
    finally: reset_runtime_context(token)

def _gather_items_deterministic(scan_params: dict | None) -> dict:
    """Детерминированный сбор: вызывает нужные сканеры напрямую (без LLM) и
    объединяет items. Какие сканеры запускать — задаётся scan_params['scanners']
    (по умолчанию 'disk'). Полностью настраиваемо, без хардкода набора."""
    prm = scan_params or {}
    scanners = prm.get("scanners") or ["disk"]
    root = prm.get("root", os.environ.get("USERPROFILE", "C:\\"))
    min_size = prm.get("min_size_mb", 5.0)
    dup_full = prm.get("dup_full_hash", True)

    items: list[dict] = []
    roots_scanned: list[str] = []
    for s in scanners:
        try:
            if s == "disk":
                raw = ct.scan_disk_intelligent(root, min_size)
            elif s == "downloads":
                raw = ct.scan_downloads_folder(min_size, prm.get("older_than_days", 30))
            elif s == "dups":
                raw = ct.find_duplicate_files(root, min_size, prm.get("timeout_sec", 60), dup_full)
            elif s == "startup":
                raw = ct.scan_startup_entries()
            else:
                continue
            data = json.loads(raw)
            items.extend(data.get("items", []))
            if data.get("scanned_root"):
                roots_scanned.append(data["scanned_root"])
        except Exception as e:
            logger.warning("Сканер %s упал: %s", s, e)
    return {"items": items, "scanned_roots": roots_scanned, "count": len(items)}

async def _run_cleaner_crew(request: str, state: SessionState, scan_params: dict | None):
    analyzer = make_cleaner_analyzer(state)

    async with cl.Step(name="🔍 Сбор данных") as step:
        # Сбор — детерминированный прямой вызов сканеров (без LLM, без нагрузки на VRAM).
        scan_params = dict(scan_params or {})
        scan_params.setdefault("dup_full_hash", getattr(state, "dup_full_hash", True))
        gathered_data = await asyncio.to_thread(_gather_items_deterministic, scan_params)
        raw_gather = json.dumps(gathered_data, ensure_ascii=False)
        await step.stream_token(f"\n✅ Собрано объектов: {gathered_data.get('count', 0)}.")

    items = gathered_data.get("items", [])

    async with cl.Step(name="🧠 Анализ рисков") as step:
        analyze_task = Task(
            description=f"Данные:\n{raw_gather}\nПроанализируй неизвестные папки и классифицируй риски (safe/warn/danger). Верни JSON-отчёт с ключами safe, warn, danger, summary.",
            agent=analyzer, expected_output="JSON-отчёт"
        )
        try:
            res = await asyncio.wait_for(asyncio.to_thread(safe_kickoff, Crew(agents=[analyzer], tasks=[analyze_task]), state), timeout=getattr(state, "ui_step_timeout", 300))
            raw_analysis = getattr(analyze_task.output, "raw", None) or str(res)
        except asyncio.TimeoutError: raise RuntimeError("⏱ Перевышен лимит RiskAnalyzer.")
        await step.stream_token("\n✅ Анализ завершён.")

    report_data = _parse_json_from_agent(raw_analysis)
    if not isinstance(report_data, dict) or "safe" not in report_data:
        report_data = {
            "safe": [x for x in items if x.get("risk_hint") == "safe"],
            "warn": [x for x in items if x.get("risk_hint") in ("warn", "unknown")],
            "danger": [x for x in items if x.get("risk_hint") == "danger"],
            "summary": {"safe_mb": sum(x.get("size_mb", 0) for x in items if x.get("risk_hint") == "safe")},
        }

    cl.user_session.set("cleaner_report", report_data)
    md = await _show_risk_report(report_data)
    safe_mb = report_data.get("summary", {}).get("safe_mb", 0)
    warn_mb = report_data.get("summary", {}).get("warn_mb", 0)

    actions = []
    if report_data.get("safe"): actions.append(cl.Action(name="cleaner_delete_safe", label=f"✅ Удалить БЕЗОПАСНОЕ ({_fmt_mb(safe_mb)})", payload={"value": "safe"}))
    if report_data.get("warn"): actions.append(cl.Action(name="cleaner_delete_warn", label=f"⚠️ Удалить ОСТОРОЖНО ({_fmt_mb(warn_mb)})", payload={"value": "warn"}))
    if report_data.get("safe") and report_data.get("warn"): actions.append(cl.Action(name="cleaner_delete_all", label=f"🗑️ Удалить ВСЁ ({_fmt_mb(safe_mb + warn_mb)})", payload={"value": "all"}))
    actions.append(cl.Action(name="cleaner_quarantine_status", label="📦 Карантин (управление)", payload={"value": "quarantine"}))

    await cl.Message(content=md, actions=actions).send()

async def _execute_deletion(category: str):
    if cl.user_session.get("is_running"): return await cl.Message(content="⏳ Система занята.").send()
    cl.user_session.set("is_running", True)
    state = _get_safe_state()
    ctx = RuntimeContext(state=state, ui=DummyInteractionHandler(), overlay=None)
    token = set_runtime_context(ctx)

    try:
        report = cl.user_session.get("cleaner_report") or {}
        items_to_delete = []
        if category in ("safe", "all"): items_to_delete.extend(report.get("safe", []))
        if category in ("warn", "all"): items_to_delete.extend(report.get("warn", []))
        if not items_to_delete: return await cl.Message(content="ℹ️ Нечего удалять.").send()

        confirm = await cl.AskActionMessage(
            content=f"Подтверждаете карантин {len(items_to_delete)} объектов?",
            actions=[cl.Action(name="y", label="Да", payload={"value": "yes"}), cl.Action(name="n", label="Нет", payload={"value": "no"})],
            timeout=300
        ).send()

        if not confirm or confirm.get("payload", {}).get("value") != "yes": return await cl.Message(content="🚫 Отменено.").send()

        async with cl.Step(name="🗑️ Выполнение") as step:
            # Перенос в карантин — детерминированная операция, выполняется напрямую
            # (без LLM): надёжно и не нагружает VRAM.
            items_payload = json.dumps(
                {"items": [{"path": i["path"], "reason": i.get("description", "")} for i in items_to_delete]},
                ensure_ascii=False,
            )
            try:
                raw_exec = await asyncio.wait_for(
                    asyncio.to_thread(ct.move_to_quarantine, items_payload, getattr(state, "quarantine_same_drive", True)),
                    timeout=getattr(state, "ui_step_timeout", 300),
                )
                exec_data = _parse_json_from_agent(raw_exec) or {}
            except Exception as e:
                return await cl.Message(content=f"❌ Ошибка execution: {e}").send()
            
            session_id = exec_data.get("session_id")
            if not session_id: return await cl.Message(content="❌ Ошибка: нет session_id.").send()

            freed_mb = exec_data.get("freed_mb", 0.0)
            cl.user_session.set("last_cleaner_session", session_id)
            if db := cl.user_session.get("db"):
                if hasattr(db, "log_cleaner_run"): db.log_cleaner_run(freed_mb, len(items_to_delete))

            await cl.Message(
                content=f"✅ Успешно. Карантин ID: `{session_id}`. Освобождено: {_fmt_mb(freed_mb)}",
                actions=[
                    cl.Action(name="cleaner_undo_last", label="↩️ Отменить", payload={"value": session_id}),
                    cl.Action(name="cleaner_permanent_delete", label="💀 Удалить навсегда", payload={"value": session_id})
                ]
            ).send()
    finally:
        reset_runtime_context(token)
        cl.user_session.set("is_running", False)

async def _preset_scan(req: str, prm: dict):
    if cl.user_session.get("is_running"): return await cl.Message(content="⏳ Система занята.").send()
    cl.user_session.set("is_running", True)
    try: await run_cleaner_flow(req, prm)
    finally: cl.user_session.set("is_running", False)

async def action_clean_cache_deep(a=None):
    if a:
        try: await a.remove()
        except Exception: pass
    await _preset_scan("Глубокий кэш", {"scanners": ["disk"], "root": os.environ.get("LOCALAPPDATA", "C:\\"), "min_size_mb": 2.0})

async def action_clean_orphans(a=None):
    if a:
        try: await a.remove()
        except Exception: pass
    await _preset_scan("Остатки программ", {"scanners": ["disk"], "root": "C:\\", "min_size_mb": 10.0})

async def action_clean_downloads(a=None):
    if a:
        try: await a.remove()
        except Exception: pass
    await _preset_scan("Загрузки", {"scanners": ["downloads"], "min_size_mb": 30.0})

async def action_find_dups(a=None):
    if a:
        try: await a.remove()
        except Exception: pass
    await _preset_scan("Дубликаты", {"scanners": ["dups"], "root": os.environ.get("USERPROFILE", "C:\\"), "min_size_mb": 10.0})

async def action_cleaner_delete_safe(a): await _execute_deletion("safe")
async def action_cleaner_delete_warn(a): await _execute_deletion("warn")
async def action_cleaner_delete_all(a): await _execute_deletion("all")

async def action_cleaner_undo_last(a):
    sid = (a.payload or {}).get("value") or cl.user_session.get("last_cleaner_session")
    if not sid: return await cl.Message(content="❌ Session ID не найден.").send()
    raw = ct.undo_quarantine(sid)
    res = _parse_json_from_agent(raw) or {}
    await cl.Message(content=f"↩️ Отменено. Восстановлено: {res.get('restored_count',0)}, Ошибок: {res.get('failed_count',0)}").send()

async def action_cleaner_permanent_delete(a):
    sid = (a.payload or {}).get("value")
    if not sid: return
    conf = await cl.AskActionMessage(content=f"Удалить НАВСЕГДА карантин `{sid}`?", actions=[cl.Action(name="y", label="Да", payload={"value":"yes"}), cl.Action(name="n", label="Нет", payload={"value":"no"})]).send()
    if conf and conf.get("payload", {}).get("value") == "yes":
        res = _parse_json_from_agent(ct.execute_permanent_deletion(sid)) or {}
        await cl.Message(content=f"💀 Удалено. Освобождено: {_fmt_mb(res.get('freed_mb',0))}").send()
    else: await cl.Message(content="🚫 Отменено.").send()

async def action_cleaner_quarantine_status(a=None):
    try:
        raw = ct.list_quarantine_sessions()
        data = _parse_json_from_agent(raw) or {}
        sessions = data.get("sessions", [])
        if not sessions: return await cl.Message(content="📦 Карантин пуст.").send()
        
        import time
        salt = str(time.time())
        actions, lines = [], [f"📦 **Карантин** — суммарно {_fmt_mb(data.get('total_mb', 0))}\n"]
        for s in sessions:
            sid = s.get("session_id", "")
            lines.append(f"`{sid}` — {s.get('items_count','?')} файлов, {_fmt_mb(s.get('size_mb',0))}")
            if sid:
                actions.append(cl.Action(name="cleaner_undo_last", label=f"↩️ Восстановить {sid[:8]}", payload={"value": sid, "salt": salt}))
                actions.append(cl.Action(name="cleaner_permanent_delete", label=f"💀 Удалить {sid[:8]}", payload={"value": sid, "salt": salt}))
        await cl.Message(content="\n".join(lines), actions=actions).send()
    except Exception as e:
        await cl.Message(content=f"❌ Ошибка карантина: {e}").send()
