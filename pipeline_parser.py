import json
import logging
import re
from pydantic import BaseModel, Field

logger = logging.getLogger("PIPELINE_PARSER")

class GatheredFile(BaseModel):
    filepath: str
    reason: str = ""
    current_code_snippet: str = ""
    size_kb: float = 0.0

class DependencyInfo(BaseModel):
    symbol: str
    defined_in: str = ""
    used_in: list[str] = Field(default_factory=list)
    usage_count: int = 0

class GathererManifest(BaseModel):
    task_summary: str = ""
    primary_file: str = ""
    files_to_modify: list[GatheredFile] = Field(default_factory=list)
    dependencies_found: list[DependencyInfo] = Field(default_factory=list)
    project_context: dict = Field(default_factory=dict)
    ambiguities: list[str] = Field(default_factory=list)
    is_structured: bool = True

class ExecutionStep(BaseModel):
    step_id: int = 0
    file: str
    action: str = "modify"
    instruction: str
    code_context: str = ""
    constraints: list[str] = Field(default_factory=list)

class ArchitectPlan(BaseModel):
    task_summary: str = ""
    risk_analysis: str = ""
    approach: str = ""
    execution_steps: list[ExecutionStep] = Field(default_factory=list)
    test_criteria: list[str] = Field(default_factory=list)
    is_structured: bool = True

class PatchEntry(BaseModel):
    filepath: str
    code: str
    change_summary: str = ""
    lines_changed: str = ""

class FixerOutput(BaseModel):
    patches: list[PatchEntry] = Field(default_factory=list)
    no_changes_needed: bool = False
    fixer_notes: str = ""
    is_structured: bool = True

class AuditResult(BaseModel):
    verdict_ok: bool
    raw_text: str
    reasons: list[str] = Field(default_factory=list)

def _extract_json(raw: str) -> dict | list | None:
    if not raw or not raw.strip(): return None
    from utils import extract_agent_reasoning
    raw, _ = extract_agent_reasoning(raw)
    if not raw: return None
    try: return json.loads(raw)
    except json.JSONDecodeError: pass
    for pat in [r"```json\s*([\s\S]+?)\s*```", r"```\s*([\s\S]+?)\s*```", r"(\{[\s\S]+\})", r"(\[[\s\S]+\])"]:
        if m := re.search(pat, raw):
            try: return json.loads(m.group(1))
            except json.JSONDecodeError: continue
    return None

def parse_gatherer_output(raw: str) -> GathererManifest:
    data = _extract_json(raw)
    if not isinstance(data, dict): return GathererManifest(task_summary=raw[:200] if raw else "Error", is_structured=False)
    try:
        files = [GatheredFile(filepath=f) if isinstance(f, str) else GatheredFile(**f) for f in data.get("files_to_modify", [])]
        deps = [DependencyInfo(**d) for d in data.get("dependencies_found", []) if isinstance(d, dict)]
        return GathererManifest(task_summary=str(data.get("task_summary", "")), files_to_modify=files, dependencies_found=deps, is_structured=True)
    except Exception: return GathererManifest(task_summary="Error", is_structured=False)

def parse_architect_output(raw: str) -> ArchitectPlan:
    data = _extract_json(raw)
    if not isinstance(data, dict): return ArchitectPlan(task_summary="Error", is_structured=False, execution_steps=[ExecutionStep(file="?", instruction=raw[:1000] if raw else "Error")])
    try:
        steps = [ExecutionStep(**s) for s in data.get("execution_steps", []) if isinstance(s, dict)]
        return ArchitectPlan(task_summary=str(data.get("task_summary", "")), execution_steps=steps, is_structured=True)
    except Exception: return ArchitectPlan(task_summary="Error", is_structured=False)

def parse_fixer_output(raw: str) -> FixerOutput:
    if raw.lower().strip().startswith('{"patches": [], "no_changes_needed": true'): return FixerOutput(no_changes_needed=True, is_structured=True)
    data = _extract_json(raw)
    if isinstance(data, dict):
        if data.get("no_changes_needed"): return FixerOutput(no_changes_needed=True, is_structured=True)
        patches = [PatchEntry(filepath=str(p["filepath"]).strip(), code=str(p["code"])) for p in data.get("patches", []) if isinstance(p, dict) and p.get("filepath") and p.get("code")]
        if patches: return FixerOutput(patches=patches, is_structured=True)
    
    from utils import strict_parse_fixes, PatchModel
    legacy = strict_parse_fixes(raw)
    if legacy.is_valid and legacy.patches: return FixerOutput(patches=[PatchEntry(filepath=p.filepath, code=p.code) for p in legacy.patches], is_structured=False)
    if "file: none" in raw.lower()[:100]: return FixerOutput(no_changes_needed=True, is_structured=False)
    return FixerOutput(is_structured=False)

def parse_auditor_verdict(raw: str) -> AuditResult:
    ok = "вердикт: ок" in raw.lower()
    reasons = [l.strip() for l in raw.splitlines() if l.strip() and (l.lower().startswith("причина") or l.startswith("- "))] if not ok else []
    return AuditResult(verdict_ok=ok, raw_text=raw, reasons=reasons)

def fixer_output_to_patch_models(output: FixerOutput):
    from utils import PatchModel
    return [PatchModel(filepath=p.filepath, code=p.code) for p in output.patches]

def render_pipeline_status(man: GathererManifest, plan: ArchitectPlan, fix: FixerOutput, aud: AuditResult) -> str:
    lines = ["## ⚙️ Pipeline v2.0 — Отчёт\n"]
    lines.append(f"{'✅' if man.is_structured else '⚠️'} **Gatherer**: {man.task_summary}")
    lines.append(f"{'✅' if plan.is_structured else '⚠️'} **Architect**: {len(plan.execution_steps)} шагов")
    lines.append("ℹ️ **Fixer**: Без изменений" if fix.no_changes_needed else f"{'✅' if fix.is_structured else '⚠️'} **Fixer**: {len(fix.patches)} файлов")
    lines.append(f"{'✅' if aud.verdict_ok else '❌'} **Auditor**: {'ОК' if aud.verdict_ok else 'ОТКЛОНЕНО'}")
    return "\n".join(lines)
