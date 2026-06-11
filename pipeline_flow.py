from __future__ import annotations

import asyncio
import logging
import os

import chainlit as cl
from crewai import Crew, Task

from agents import safe_kickoff, maybe_unload_between
from pipeline_agents import make_pipeline_agents
from pipeline_parser import (
    parse_gatherer_output,
    parse_architect_output,
    parse_fixer_output,
    parse_auditor_verdict,
    fixer_output_to_patch_models,
    render_pipeline_status,
    GathererManifest,
    ArchitectPlan,
    FixerOutput,
)
from utils import SessionState, RuntimeContext, ProjectOverlay, DummyInteractionHandler, set_runtime_context, reset_runtime_context, generate_unified_diff, apply_fixes, PipelineCheckpoint

logger = logging.getLogger("PIPELINE_FLOW")
_STEP_TIMEOUT = 300  # фолбэк; фактически берётся из state.ui_step_timeout

def _get_safe_state() -> SessionState:
    kwargs = {k: cl.user_session.get(k) for k in SessionState.model_fields if cl.user_session.get(k) is not None}
    return SessionState.model_validate(kwargs)

def _get_raw(task: Task, fallback: str = "") -> str:
    return getattr(task.output, "raw", None) or fallback

def _make_ui_handler():
    try:
        import importlib
        return getattr(importlib.import_module("app"), "ChainlitInteractionHandler", DummyInteractionHandler)()
    except Exception: return DummyInteractionHandler()

async def run_pipeline_v2(msg: cl.Message):
    state = _get_safe_state()
    ui = _make_ui_handler()
    overlay = ProjectOverlay(state.project_path) if state.strict_sandbox else None
    token = None

    try:
        ctx = RuntimeContext(state=state, ui=ui, overlay=overlay)
        token = set_runtime_context(ctx)

        gatherer, architect, fixer, auditor = make_pipeline_agents(state, None)
        chat_context = f"Контекст:\n{msg.content}\n\n"

        # Чекпойнт: при сбое (исчерпаны ключи/таймаут) повторный запуск той же
        # задачи продолжит с незавершённого этапа, а не с нуля.
        ckpt = PipelineCheckpoint(state.project_path, msg.content)
        resumed = bool(ckpt.get("gather") or ckpt.get("architect"))
        if resumed:
            await cl.Message(content="↩️ Найден незавершённый прогон этой задачи — продолжаю с места обрыва.").send()

        # 1: CodeGatherer
        manifest = await _step_gather(msg.content, chat_context, gatherer, state, ckpt)
        await asyncio.to_thread(maybe_unload_between, state, "gatherer", "architect")

        # 2: SystemArchitect
        plan = await _step_architect(msg.content, manifest, architect, state, ckpt)
        if not plan.execution_steps:
            ckpt.clear()
            return await cl.Message(content="ℹ️ Архитектор не определил изменений.").send()
        await asyncio.to_thread(maybe_unload_between, state, "architect", "coder")

        # 3: CodeFixer
        fixer_output = await _step_fix(manifest, plan, fixer, overlay, state, ckpt)
        if fixer_output.no_changes_needed or not fixer_output.patches:
            ckpt.clear()
            return await cl.Message(content="ℹ️ Fixer: изменений не требуется.").send()
        await asyncio.to_thread(maybe_unload_between, state, "coder", "auditor")

        # 4: QAAuditor
        audit = await _step_audit(plan, fixer_output, auditor, overlay, state)

        # Задача дошла до конца — чекпойнт больше не нужен.
        ckpt.clear()

        # Отчёт и применение
        await cl.Message(content=render_pipeline_status(manifest, plan, fixer_output, audit)).send()
        await _handle_patch_application(fixer_output_to_patch_models(fixer_output), audit, state, overlay, ui)

    except Exception as e:
        logger.exception("Pipeline crash: %s", e)
        await cl.Message(content=f"❌ Критическая ошибка: `{e}`").send()
    finally:
        if overlay: overlay.cleanup()
        if token: reset_runtime_context(token)

async def _step_gather(req: str, ctx: str, agent, state, ckpt=None) -> GathererManifest:
    async with cl.Step(name="🔍 Структура") as step:
        cached = ckpt.get("gather") if ckpt else None
        if cached:
            await step.stream_token("✅ Из чекпойнта.")
            return parse_gatherer_output(cached)
        task = Task(description=f"{ctx}Собери манифест по задаче: {req}", agent=agent, expected_output="JSON GathererManifest")
        try: res = await asyncio.wait_for(asyncio.to_thread(safe_kickoff, Crew(agents=[agent], tasks=[task]), state), timeout=getattr(state, "ui_step_timeout", 300))
        except asyncio.TimeoutError: raise RuntimeError("⏱ Gatherer timeout.")
        raw = _get_raw(task, str(res))
        if ckpt: ckpt.save("gather", raw)
        await step.stream_token(f"✅ Найдено.")
        return parse_gatherer_output(raw)

async def _step_architect(req: str, manifest: GathererManifest, agent, state, ckpt=None) -> ArchitectPlan:
    async with cl.Step(name="🧠 План") as step:
        cached = ckpt.get("architect") if ckpt else None
        if cached:
            await step.stream_token("✅ Из чекпойнта.")
            return parse_architect_output(cached)
        task = Task(description=f"План для: {req}\nМанифест: {manifest.model_dump_json(indent=2)}", agent=agent, expected_output="JSON ArchitectPlan")
        try: res = await asyncio.wait_for(asyncio.to_thread(safe_kickoff, Crew(agents=[agent], tasks=[task]), state), timeout=getattr(state, "ui_step_timeout", 300))
        except asyncio.TimeoutError: raise RuntimeError("⏱ Architect timeout.")
        raw = _get_raw(task, str(res))
        if ckpt: ckpt.save("architect", raw)
        return parse_architect_output(raw)

async def _step_fix(manifest: GathererManifest, plan: ArchitectPlan, agent, overlay, state, ckpt=None) -> FixerOutput:
    async with cl.Step(name="🔧 Код") as step:
        cached = ckpt.get("fix") if ckpt else None
        if cached:
            out = parse_fixer_output(cached)
            if overlay and not out.no_changes_needed: overlay.apply_dry_fixes(fixer_output_to_patch_models(out))
            await step.stream_token("✅ Из чекпойнта.")
            return out
        task = Task(description=f"План: {plan.model_dump_json(indent=2)}", agent=agent, expected_output="JSON FixerOutput")
        try: res = await asyncio.wait_for(asyncio.to_thread(safe_kickoff, Crew(agents=[agent], tasks=[task]), state), timeout=getattr(state, "ui_step_timeout", 300))
        except asyncio.TimeoutError: raise RuntimeError("⏱ Fixer timeout.")
        raw = _get_raw(task, str(res))
        out = parse_fixer_output(raw)
        if ckpt: ckpt.save("fix", raw)
        if overlay and not out.no_changes_needed: overlay.apply_dry_fixes(fixer_output_to_patch_models(out))
        return out

async def _step_audit(plan: ArchitectPlan, fixer_out: FixerOutput, agent, overlay, state):
    async with cl.Step(name="🔬 Аудит") as step:
        task = Task(description=f"План: {plan.model_dump_json(indent=2)}\nПроверь реализацию.", agent=agent, expected_output="Вердикт: ОК или ОТКЛОНЕНО")
        try: res = await asyncio.wait_for(asyncio.to_thread(safe_kickoff, Crew(agents=[agent], tasks=[task]), state), timeout=getattr(state, "ui_step_timeout", 300))
        except asyncio.TimeoutError: raise RuntimeError("⏱ Auditor timeout.")
        return parse_auditor_verdict(_get_raw(task, str(res)))

async def _handle_patch_application(patches, audit, state, overlay, ui):
    if not patches: return
    if state.auto_apply:
        if audit.verdict_ok:
            if overlay: overlay.commit_if_success(state.backup_retention_days)
            await cl.Message(content="💾 Авто-применение.").send()
        else: await cl.Message(content="⚠️ Аудитор отклонил. Отмена.").send()
        return

    diffs = []
    for p in patches:
        orig = os.path.join(state.project_path, p.filepath)
        old_code = open(orig, "r", encoding="utf-8").read() if os.path.exists(orig) else ""
        diffs.append(generate_unified_diff(old_code, p.code, p.filepath))
    
    diff_text = "\n".join(diffs)[:3000]
    conf = await cl.AskActionMessage(
        content=f"**📄 Патч**\n{('✅ ОК' if audit.verdict_ok else '⚠️ ВНИМАНИЕ')}\n```diff\n{diff_text}\n```",
        actions=[cl.Action(name="y", label="Применить", payload={"value":"yes"}), cl.Action(name="n", label="Отклонить", payload={"value":"no"})],
        timeout=600
    ).send()
    
    if conf and conf.get("payload", {}).get("value") == "yes":
        if overlay: overlay.commit_if_success(state.backup_retention_days)
        else: apply_fixes(patches, state.project_path)
        await cl.Message(content="💾 Применено.").send()
    else: await cl.Message(content="🚫 Отклонено.").send()

async def run_image_crew(prompt: str, state, llm):
    """Генерация контента через SD Forge."""
    import chainlit as cl, asyncio
    async with cl.Step(name="🎨 Генерация") as step:
        from tools import generate_image
        try:
            result = await asyncio.to_thread(generate_image, prompt)
            await cl.Message(content=f"🖼 Результат генерации:\n{result}").send()
        except Exception as e:
            await cl.Message(content=f"❌ Ошибка генерации: {e}").send()
