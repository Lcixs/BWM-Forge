from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import shutil
from types import SimpleNamespace
from typing import Any

from input_pipeline.common import now, read_json
from utils.prompts import load_prompt
from .common import hash_path, log_progress, plain, progress_heartbeat, run_logged_command, write_json
from .history import (STAGES, STAGE_BLEND_FILES, load_run, save_run, local_path, artifact, verify_artifact,
                      append_event, round_context)


@dataclass
class Feedback:
    action: str = "accept"
    text: str = ""
    images: list[str] | None = None
    source: str = "user"


@dataclass
class StageSession:
    thread: Any
    task_dir: Path
    common_prompt: str
    prompt_root: Path
    blender_exe: Path
    render_script: Path
    first_frame: Path
    text_input: Any
    image_input: Any
    validate_blend: Any
    input_type: int
    state: dict
    active: dict | None = None
    experience_adapter: Any = None


def load_mode_prompt(path: Path, input_type: int) -> str:
    if type(input_type) is not int or input_type not in (0, 1, 2):
        raise ValueError("A valid input_type (0, 1, or 2) is required to load the prompt.")
    selected = []
    include = True
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        mode_heading = re.match(r"^#{2,3} (?:Type|类型) ([012])(?:：|:)", line)
        if mode_heading:
            include = int(mode_heading.group(1)) == input_type
        elif re.match(r"^#{1,2} ", line):
            include = True
        if include:
            selected.append(line)
    return "".join(selected).strip()


def _run_codex_with_progress(thread, inputs, label: str, emit):
    turn_factory = getattr(thread, "turn", None)
    if not callable(turn_factory):
        with progress_heartbeat(label):
            result = thread.run(inputs)
            for item in getattr(result, "items", []):
                data = plain(item)
                data = data.get("root", data) if isinstance(data, dict) else {}
                if data.get("type") != "reasoning":
                    emit("item.completed", data)
            return result
    handle = turn_factory(inputs)
    items = []
    last_response = ""
    final_response = ""
    completed = None
    output_buffers: dict[str, str] = {}
    with progress_heartbeat(label):
        for event in handle.stream():
            method = getattr(event, "method", "")
            payload = getattr(event, "payload", None)
            if method == "item/started" and hasattr(payload, "item"):
                data = plain(payload.item)
                if isinstance(data, dict):
                    data = data.get("root", data)
                if isinstance(data, dict):
                    kind = data.get("type", "unknown")
                    if kind != "reasoning":
                        emit("item.started", data)
                    detail = str(data.get("command") or data.get("name") or "").replace("\n", " ")[:180]
                    log_progress(f"{label}: starting {kind}" + (f"; {detail}" if detail else ""))
            elif method == "item/commandExecution/outputDelta" and hasattr(payload, "delta"):
                item_id = str(getattr(payload, "item_id", ""))
                output_buffers[item_id] = output_buffers.get(item_id, "") + payload.delta
                while "\n" in output_buffers[item_id]:
                    line, output_buffers[item_id] = output_buffers[item_id].split("\n", 1)
                    log_progress(f"{label} command output | {line.rstrip()}")
                if len(output_buffers[item_id]) > 4096:
                    log_progress(f"{label} command output | {output_buffers[item_id][:4096]}")
                    output_buffers[item_id] = output_buffers[item_id][4096:]
            elif method == "item/completed" and hasattr(payload, "item"):
                item = payload.item
                items.append(item)
                data = plain(item)
                if isinstance(data, dict):
                    data = data.get("root", data)
                if not isinstance(data, dict):
                    data = {}
                if data.get("type") == "reasoning":
                    continue
                emit("item.completed", data)
                item_id = str(data.get("id", ""))
                if output_buffers.get(item_id):
                    log_progress(f"{label} command output | {output_buffers.pop(item_id).rstrip()}")
                kind = data.get("type", type(item).__name__)
                status = data.get("status")
                detail = data.get("command") or data.get("name") or data.get("text") or ""
                detail = str(detail).replace("\n", " ")[:180]
                log_progress(f"{label}: operation {len(items)} completed; type: {kind}" +
                             (f"; status: {status}" if status else "") +
                             (f"; {detail}" if detail else ""))
                if kind in ("agentMessage", "agent_message"):
                    text = data.get("text") or ""
                    phase = data.get("phase")
                    if text and phase == "final_answer":
                        final_response = text
                    elif text and phase is None:
                        last_response = text
            elif method == "turn/completed" and hasattr(payload, "turn"):
                completed = payload.turn
    if completed is None:
        raise RuntimeError("The Codex session did not receive a turn/completed event")
    status = getattr(completed, "status", None)
    status_text = getattr(status, "value", str(status))
    if status_text != "completed":
        error = getattr(completed, "error", None)
        raise RuntimeError(getattr(error, "message", None) or f"Codex session ended with status: {status_text}")
    return SimpleNamespace(items=items, final_response=final_response or last_response)


def prepare_workflow(*, thread, task_dir, common_prompt, prompt_root, blender_exe,
                     render_script, first_frame, text_input, image_input,
                     validate_blend, input_type, experience_adapter=None) -> StageSession:
    state = load_run(task_dir)
    for row in state["rounds"]:
        if row.get("completed_at"):
            for record in row["artifacts"].values():
                verify_artifact(task_dir, record)
        elif row["status"] == "running":
            row.update(status="interrupted", error="The previous process ended before the round completed.")
    state["sessions"].append({"thread_id": thread.id, "started_at": now()})
    save_run(task_dir, state)
    return StageSession(thread, task_dir, common_prompt, prompt_root, blender_exe,
                        render_script, first_frame, text_input, image_input,
                        validate_blend, input_type, state, experience_adapter=experience_adapter)


def _bounded(value):
    if isinstance(value, str) and len(value) > 16000:
        return value[:8000] + f"\n[{len(value)-16000} characters omitted]\n" + value[-8000:]
    if isinstance(value, dict):
        return {k: _bounded(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_bounded(v) for v in value[:200]] + (
            [{"omitted_items": len(value)-200}] if len(value) > 200 else [])
    return value


def _operation_failure(data):
    if not isinstance(data, dict) or data.get("type") not in {"commandExecution", "command_execution", "mcpToolCall", "fileChange"}:
        return ""
    result = data.get("result") or {}
    parts = [str(data.get("error") or ""),
             str(data.get("aggregated_output") or data.get("aggregatedOutput") or "")]
    if isinstance(result, dict):
        parts.extend(str(c.get("text", "")) for c in (result.get("content") or [])
                     if isinstance(c, dict) and c.get("type") == "text")
    output = "\n".join(parts).strip()
    if (data.get("status") not in {"failed", "declined"}
            and not any(marker in output for marker in
                        ("Traceback (most recent call last)", "Automatic approval review failed",
                         "Failed to create unified exec process"))):
        return ""
    name = data.get("tool") or data.get("type")
    return f"{name} [{data.get('status', 'unknown')}]: {output[-1600:] or 'Operation did not execute; see the event log'}"


def _run_stage_turn(session, row, prompt, images, phase):
    task_dir = session.task_dir
    label = f"{row['stage']} round {row['round']} {phase}"
    tool_failures = []

    def emit(event, data):
        append_event(task_dir, row, event, _bounded(data))
        if event == "item.completed":
            failure = _operation_failure(data)
            if failure:
                tool_failures.append(failure)
                del tool_failures[:-3]
                log_progress(f"{label}: tool execution error\n{failure}")

    append_event(task_dir, row, f"{phase}.started", {"prompt": prompt, "images": images})
    protected = {str(verify_artifact(task_dir, r["artifacts"]["blend"])):
                 verify_artifact(task_dir, r["artifacts"]["blend"]).read_bytes()
                 for r in session.state["rounds"] if r.get("completed_at")}
    try:
        result = _run_codex_with_progress(
            session.thread, [session.text_input(prompt), *[session.image_input(p) for p in images]],
            label, emit)
    finally:
        altered = []
        for name, payload in protected.items():
            path = Path(name)
            if not path.is_file() or path.read_bytes() != payload:
                path.write_bytes(payload)
                altered.append(name)
        if altered:
            raise RuntimeError("The model modified completed projects; originals have been restored: " + ", ".join(altered))
    return result, tool_failures


def _render_stage_preview(session, row, command, result, tool_failures, log_name):
    folder = local_path(session.task_dir, row["folder"])
    blend, preview = folder / STAGE_BLEND_FILES[row["stage"]], folder / "preview.png"
    label = f"{row['stage']} round {row['round']}"
    try:
        session.validate_blend(blend)
    except (OSError, ValueError, RuntimeError) as exc:
        response = str(getattr(result, "final_response", "") or "(The model did not return a final explanation)")
        details = "\n".join(tool_failures) or "No recognizable tool error was received. Check this round's events and execution logs."
        raise RuntimeError(
            f"{label} was not completed; project validation failed: {exc}\n"
            f"Model final explanation:\n{response[:3000]}\nRecent tool errors (some may have been resolved later):\n{details}\n"
            f"Complete record: {folder / 'events.jsonl'}\nExecution logs: {folder / 'evidence'}"
        ) from exc
    # 先清除旧预览，避免渲染失败后展示旧图。
    preview.unlink(missing_ok=True)
    log_path = folder / "evidence" / log_name
    code, success = run_logged_command(command, log_path, f"{label} {log_name}")
    if code or not success or not preview.is_file() or not preview.stat().st_size:
        raise RuntimeError(f"Project validation or preview failed for this round; exit code: {code}; log: {log_path}")
    from PIL import Image
    with Image.open(preview) as image:
        image.verify()


def run_stage(session: StageSession, stage: str) -> dict:
    if stage not in STAGES:
        raise ValueError("Unknown modeling stage")
    state, task_dir = session.state, session.task_dir
    rows = [r for r in state["rounds"] if r["stage"] == stage]
    complete = [r for r in rows if r.get("completed_at")]
    previous = complete[-1] if complete else None
    if previous and previous.get("decision", {}).get("action") != "revise":
        session.active = previous
        return round_context(task_dir, previous)

    stage_root = task_dir / "blender/stages" / stage
    occupied = [int(p.name) for p in stage_root.iterdir() if p.is_dir() and p.name.isdigit()] if stage_root.exists() else []
    number = max([r["round"] for r in rows] + occupied, default=-1) + 1
    folder = stage_root / f"{number:03d}"
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "scripts").mkdir()
    (folder / "evidence").mkdir()
    feedback = previous.get("decision") if previous else None
    row = {"id": f"{stage}/{number:03d}", "stage": stage, "round": number,
           "folder": folder.relative_to(task_dir).as_posix(), "status": "running",
           "started_at": now(), "thread_id": session.thread.id,
           "previous_round": previous["id"] if previous else None,
           "restart_of": rows[-1]["id"] if rows and not rows[-1].get("completed_at") else None,
           "feedback": feedback, "decisions": []}
    state["rounds"].append(row)
    session.active = row
    save_run(task_dir, state)
    previous_blend = verify_artifact(task_dir, previous["artifacts"]["blend"]) if previous else None
    previous_preview = verify_artifact(task_dir, previous["artifacts"]["preview"]) if previous else None
    blend, preview = folder / STAGE_BLEND_FILES[stage], folder / "preview.png"
    # 首轮可复用资产缓存，修改轮沿用本任务上一轮结果。
    cache = None
    if stage == "robot_only" and previous is None:
        inputs = read_json(task_dir / "input/input.json")
        assets = inputs["assets"]
        cache = Path(assets["root"]) / "blender" / "robot.blend"
    reused_cache = cache is not None and cache.is_file() and cache.stat().st_size > 0
    if reused_cache:
        shutil.copy2(cache, blend)
        result = SimpleNamespace(final_response="Reused the standalone robot model from the asset library.")
        tool_failures = []
        append_event(task_dir, row, "cache.loaded", {"source": str(cache)})
        write_json(folder / "report.json", {"changes": [], "checks": [],
                                           "limitations": ["Reused model; task placement and motion are handled by composed."]})
        log_progress(f"Reused cached robot model: {cache}")
    else:
        upstream = {name: str(verify_artifact(task_dir, record))
                    for name, record in state["accepted"].items()}
        policy = load_mode_prompt(session.prompt_root / f"{stage}.md", session.input_type)
        if feedback:
            policy += "\n" + load_mode_prompt(session.prompt_root / "feedback.md", session.input_type)
        experience_text = ""
        if session.experience_adapter is not None:
            log_progress(f"{stage} round {number}: retrieving relevant experience")
            with progress_heartbeat("Experience retrieval"):
                experience_result = session.experience_adapter.begin(
                    stage, number, feedback=feedback, folder=folder)
            experience_text = session.experience_adapter.prompt_text(experience_result)
            row["retrieved_experiences"] = session.experience_adapter.retrieved(experience_result)
            save_run(task_dir, state)
        prompt = load_prompt(
            "blender/round.md",
            common_prompt=session.common_prompt, stage=stage, number=number,
            upstream=json.dumps(upstream, ensure_ascii=False),
            previous_blend=previous_blend or "None; initial generation",
            previous_preview=previous_preview or "None; initial generation",
            feedback=feedback.get("text", "") if feedback else "None",
            blend=blend, folder=folder,
            scripts_dir=folder / "scripts", evidence_dir=folder / "evidence",
            report_path=folder / "report.json", first_script=folder / "scripts/build_001.py",
            execution_args=json.dumps([
                str(session.blender_exe), "--background", "--python-exit-code", "1",
                "--python", str(folder / "scripts/build_001.py"),
            ], ensure_ascii=False),
            policy=policy, experience_text=experience_text,
        )
        images = [str(session.first_frame)] + ([str(previous_preview)] if previous_preview else []) + [
            str(local_path(task_dir, p)) for p in (feedback or {}).get("images", [])]
        result, tool_failures = _run_stage_turn(session, row, prompt, images, "round")
    command = [str(session.blender_exe), "--background", str(blend),
               "--python-exit-code", "1", "--python", str(session.render_script), "--", "--mode", "frame",
               "--output", str(preview), "--camera-role",
               "robot_preview" if stage == "robot_only" else "input",
               "--fast-white-model", "--validate-stage", stage]
    _render_stage_preview(session, row, command, result, tool_failures, "preview.log")

    review = None
    if not reused_cache:
        blend_hash, preview_hash = hash_path(blend), hash_path(preview)
        review_prompt = load_prompt(
            "blender/review.md", common_prompt=session.common_prompt, stage=stage, number=number,
            blend=blend, preview=preview, reference=session.first_frame,
            upstream=json.dumps(upstream, ensure_ascii=False),
            feedback=feedback.get("text", "") if feedback else "None",
            scripts_dir=folder / "scripts", evidence_dir=folder / "evidence",
            report_path=folder / "report.json", preview_command=json.dumps(command, ensure_ascii=False),
            policy=policy,
        )
        review_images = [str(session.first_frame), str(preview)] + [
            str(local_path(task_dir, p)) for p in (feedback or {}).get("images", [])]
        review_result, review_failures = _run_stage_turn(session, row, review_prompt, review_images, "review")
        modified = not blend.is_file() or hash_path(blend) != blend_hash
        if modified or not preview.is_file() or hash_path(preview) != preview_hash:
            _render_stage_preview(session, row, command, review_result, review_failures, "review_preview.log")
        review = {"modified": modified, "response": getattr(review_result, "final_response", "") or ""}
        append_event(task_dir, row, "review.completed", {"modified": modified})

    report_path = folder / "report.json"
    agent_report = read_json(report_path) if report_path.exists() else {
        "changes": [], "checks": [], "limitations": ["The model did not submit a structured report; see the event log for change details."]}
    if not isinstance(agent_report, dict):
        raise ValueError("report.json must contain a JSON object")
    write_json(report_path, {"agent_report": agent_report,
                            "response": getattr(result, "final_response", "") or "",
                            "review": review,
                            "host_checks": {"blend_valid": True, "preview_rendered": True}})
    row.update(status="ready", completed_at=now(),
               report=report_path.relative_to(task_dir).as_posix(),
               artifacts={"blend": artifact(task_dir, blend), "preview": artifact(task_dir, preview),
                          "report": artifact(task_dir, report_path)})
    append_event(task_dir, row, "round.completed",
                 {"host_checks": {"blend_valid": True, "preview_rendered": True}})
    save_run(task_dir, state)
    for backup in folder.glob(blend.name + "[0-9]*"):
        if (re.fullmatch(re.escape(blend.name) + r"\d+", backup.name) and backup.is_file()
                and backup.resolve().is_relative_to(folder.resolve()) and not backup.is_symlink()):
            backup.unlink()
    return round_context(task_dir, row)


def save_feedback(session: StageSession, decision) -> Feedback:
    if isinstance(decision, dict):
        decision = Feedback(**decision)
    if not isinstance(decision, Feedback) or decision.action not in {"accept", "revise", "quit"}:
        raise ValueError("Feedback action must be accept, revise, or quit")
    if decision.source not in {"user", "auto"}:
        raise ValueError("Feedback source must be user or auto")
    if decision.action == "revise" and not decision.text.strip() and not decision.images:
        raise ValueError("Revision feedback must not be empty")
    row = session.active
    if row is None or not row.get("completed_at"):
        raise ValueError("Feedback can only be submitted for a completed round")
    round_context(session.task_dir, row)
    record = asdict(decision)
    record["images"] = []
    for index, name in enumerate(decision.images or []):
        source = Path(name).expanduser().resolve(strict=True)
        if source.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise ValueError("Feedback references must be images")
        from PIL import Image
        with Image.open(source) as image:
            image.verify()
        target = local_path(session.task_dir, row["folder"]) / "evidence" / (
            f"feedback_{len(row['decisions'])}_{index}{source.suffix.lower()}")
        if source != target.resolve():
            shutil.copy2(source, target)
        record["images"].append(target.relative_to(session.task_dir).as_posix())
    record["time"] = now()
    if decision.action == "accept":
        if row["stage"] == "robot_only":
            # 用户确认预览后才更新资产库缓存。
            inputs = read_json(session.task_dir / "input/input.json")
            assets = inputs["assets"]
            asset_root = assets.get("root")
            if not isinstance(asset_root, str) or not asset_root.strip():
                raise ValueError("The input handoff is missing the robot asset root directory.")
            source = verify_artifact(session.task_dir, row["artifacts"]["blend"])
            cache = Path(asset_root).expanduser().resolve() / "blender" / "robot.blend"
            cache.parent.mkdir(parents=True, exist_ok=True)
            pending = cache.with_name(".robot.blend.pending")
            try:
                shutil.copy2(source, pending)
                pending.replace(cache)
            finally:
                pending.unlink(missing_ok=True)
        row["status"] = "accepted"
        session.state["accepted"][row["stage"]] = row["artifacts"]["blend"]
    elif decision.action == "revise":
        row["status"] = "revised"
    row["decisions"].append(record)
    row["decision"] = record
    save_run(session.task_dir, session.state)
    return decision


def finalize(session: StageSession) -> Path:
    if set(session.state["accepted"]) != set(STAGES):
        raise ValueError("All three modeling stages must be accepted before finalizing")
    task_dir = session.task_dir
    source_record = session.state["accepted"]["composed"]
    source = verify_artifact(task_dir, source_record)
    final = task_dir / "blender/final.blend"
    if source != final:
        temporary = final.with_suffix(".pending.blend")
        shutil.copy2(source, temporary)
        session.validate_blend(temporary)
        temporary.replace(final)
        record = artifact(task_dir, final)
        # 先提交引用，再删除内容相同的轮次副本。
        session.state["accepted"]["composed"] = record
        for row in session.state["rounds"]:
            if row.get("artifacts", {}).get("blend") == source_record:
                row["artifacts"]["blend"] = record
        session.state["final"]["blend"] = record
        save_run(task_dir, session.state)
        if source.resolve().is_relative_to((task_dir / "blender/stages").resolve()):
            source.unlink()
    else:
        session.state["final"]["blend"] = artifact(task_dir, final)
        save_run(task_dir, session.state)
    return final


def record_failure(session: StageSession, exc: BaseException) -> None:
    row = session.active
    if row is not None and not row.get("completed_at"):
        row.update(status="interrupted", error=str(exc) or "Interrupted by the user", ended_at=now())
        try:
            append_event(session.task_dir, row, "round.interrupted", {"error": row["error"]})
        finally:
            save_run(session.task_dir, session.state)
