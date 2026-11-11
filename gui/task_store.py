from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import json
from typing import Any


@dataclass(frozen=True)
class TaskSnapshot:
    task_id: str
    display_name: str
    task_dir: Path
    status: str
    current_stage: str
    stage_states: dict[str, str]
    created_at: str
    updated_at: str
    input_type: int | None
    asset_name: str
    error: str
    task: dict[str, Any]
    preview_image: Path | None
    white_video: Path | None
    final_video: Path | None
    console_log: str


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _inside_file(task_dir: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        raw = Path(value)
        path = (raw if raw.is_absolute() else task_dir / raw).resolve()
        root = task_dir.resolve()
    except OSError:
        return None
    return path if path.is_relative_to(root) and path.is_file() else None


def _newest(task_dir: Path, pattern: str) -> Path | None:
    try:
        candidates = [path for path in task_dir.glob(pattern) if path.is_file()]
        return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None
    except OSError:
        return None


def _stage_states(task_dir: Path, task: dict[str, Any]) -> dict[str, str]:
    overall = str(task.get("status") or "unknown")
    states: dict[str, str] = {}
    specs = {
        "input": task_dir / "input" / "input.json",
        "blender": task_dir / "blender" / "run.json",
        "render": task_dir / "render" / "rendered_review_session.json",
    }
    for stage, evidence in specs.items():
        if not (task_dir / stage).is_dir():
            states[stage] = "pending"
            continue
        explicit = str(task.get(f"{stage}_status") or "")
        if stage == "input" and evidence.is_file():
            states[stage] = "completed"
        elif stage == "render" and evidence.is_file():
            session = _read_json(evidence)
            state = session.get("render_status", explicit)
            if state == "completed" and _inside_file(task_dir, session.get("current_output")):
                states[stage] = "completed"
            elif state == "running":
                states[stage] = "running"
            else:
                states[stage] = "attention"
        elif (explicit == "completed" or overall == f"{stage}_completed") and evidence.is_file() and (
                stage != "blender" or all((task_dir / relative).is_file() for relative in (
                    "blender/final.blend", "blender/white_model.mp4", "blender/blender_stage_summary.json"))):
            states[stage] = "completed"
        elif explicit in {"running", "submitting", "evaluating"} or overall.startswith(f"{stage}_running"):
            states[stage] = "running"
        elif explicit in {"failed", "interrupted", "cancelled", "awaiting_summary", "awaiting_export", "awaiting_evaluation"} or overall in {
            f"{stage}_failed", f"{stage}_interrupted", f"{stage}_cancelled"
        }:
            states[stage] = "attention"
        elif evidence.is_file():
            states[stage] = "ready"
        else:
            states[stage] = "pending"

    if overall in {"failed", "cancelled"}:
        active = next((stage for stage in ("input", "blender", "render")
                       if states[stage] != "completed"), None)
        if active and (task_dir / active).is_dir():
            states[active] = "attention"
    return states


def _current_stage(states: dict[str, str]) -> str:
    for stage in ("input", "blender", "render"):
        if states[stage] != "completed":
            return stage
    return "completed"


def _tail_file_lines(path: Path, *, max_bytes: int = 256 * 1024, max_lines: int = 700) -> list[str]:
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            start = max(0, size - max_bytes)
            stream.seek(start)
            payload = stream.read()
    except OSError:
        return []
    if start and b"\n" in payload:
        payload = payload.split(b"\n", 1)[1]
    return payload.decode("utf-8", errors="replace").splitlines()[-max_lines:]


def _log_clock(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
        return parsed.strftime("%H:%M:%S")
    except (AttributeError, ValueError):
        return "--:--:--"


def _log_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _compact_log_detail(value: Any, limit: int = 220) -> str:
    return " ".join(str(value or "").split())[:limit]


def _structured_console(task_dir: Path, task: dict[str, Any], limit: int = 500) -> list[str]:
    rows: list[tuple[str, int, str]] = []
    order = 0
    source_task = str(task.get("source_task") or "").strip()
    try:
        all_event_files = sorted(
            task_dir.glob("blender/stages/*/*/events.jsonl"),
            key=lambda path: path.stat().st_mtime,
        )
    except OSError:
        all_event_files = []
    cutoff = _log_time(task.get("history_loaded_at")) if source_task else None
    if source_task and cutoff is None and (task_dir / "blender/run.json").is_file():
        # Older history copies have no task-level marker; their copied rounds carry this event.
        for path in all_event_files:
            for raw in _tail_file_lines(path, max_lines=10):
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict) and record.get("event") == "history.loaded":
                    marker = _log_time(record.get("time"))
                    if marker is not None and (cutoff is None or marker > cutoff):
                        cutoff = marker
    header = None
    if source_task:
        if cutoff is None:
            header = (f"[Blender] [{_log_clock(str(task.get('created_at') or ''))}] "
                      f"Loading scene and robot models from historical task {source_task}.")
            error = task.get("error") or task.get("blender_error")
            return [header] + ([f"[Blender] [--:--:--] Loading failed: {error}"] if error else [])
        header = (f"[Blender] [{_log_clock(cutoff.isoformat())}] "
                  f"Scene and robot models loaded from historical task {source_task}. New activity follows.")

    def include(timestamp: str) -> bool:
        if not source_task:
            return True
        parsed = _log_time(timestamp)
        return parsed is not None and parsed > cutoff

    for path in all_event_files[-8:]:
        stage = path.parent.parent.name
        round_name = path.parent.name
        try:
            round_number = int(round_name)
        except ValueError:
            round_number = round_name
        label = f"{stage} Round {round_number}"
        completed = 0
        for raw in _tail_file_lines(path, max_lines=180):
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            timestamp = str(record.get("time") or "")
            if not include(timestamp):
                continue
            stamp = _log_clock(timestamp)
            event = str(record.get("event") or "event")
            data = record.get("data") if isinstance(record.get("data"), dict) else {}
            prefix = f"[Blender] [{stamp}] {label}"
            messages: list[str] = []
            if event == "round.started":
                messages.append(f"{prefix}: Started")
            elif event == "item.started":
                kind = data.get("type", "unknown")
                detail = _compact_log_detail(data.get("command") or data.get("name"))
                messages.append(f"{prefix}: Started {kind}" + (f"; {detail}" if detail else ""))
            elif event == "item.completed":
                completed += 1
                output = str(data.get("aggregated_output") or "")
                for line in output.splitlines()[-24:]:
                    if line.strip():
                        messages.append(f"{prefix} Command output | {line.rstrip()[:500]}")
                kind = data.get("type", "unknown")
                status = _compact_log_detail(data.get("status"), 60)
                detail = _compact_log_detail(data.get("command") or data.get("name") or data.get("text"))
                messages.append(
                    f"{prefix}: Item {completed} completed; type: {kind}"
                    + (f"; status: {status}" if status else "")
                    + (f"; {detail}" if detail else "")
                )
            elif event == "round.completed":
                messages.append(f"{prefix}: Round completed")
            elif event == "round.interrupted":
                detail = _compact_log_detail(data.get("error"), 500)
                messages.append(f"{prefix}: Round interrupted" + (f"; {detail}" if detail else ""))
            for message in messages:
                rows.append((timestamp, order, message))
                order += 1

    for raw in _tail_file_lines(task_dir / "blender/progress.jsonl", max_lines=400):
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or not isinstance(record.get("message"), str):
            continue
        timestamp = str(record.get("time") or "")
        if not include(timestamp):
            continue
        if source_task and record["message"].startswith(("Loading historical ", "Historical ")):
            continue
        stamp = _log_clock(timestamp)
        for line in record["message"].splitlines():
            if line.strip():
                rows.append((timestamp, order, f"[Blender] [{stamp}] {line.rstrip()[:500]}"))
                order += 1

    for raw in _tail_file_lines(task_dir / "render/progress.jsonl", max_lines=400):
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or not isinstance(record.get("message"), str):
            continue
        timestamp = str(record.get("time") or "")
        if not include(timestamp):
            continue
        stamp = _log_clock(timestamp)
        for line in record["message"].splitlines():
            if line.strip():
                rows.append((timestamp, order, f"[Render] [{stamp}] {line.rstrip()[:500]}"))
                order += 1

    for path in sorted(task_dir.glob("blender/stages/*/*/evidence/video_render.log"))[-2:]:
        timestamp = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat()
        if not include(timestamp):
            continue
        for line in _tail_file_lines(path, max_lines=40):
            rows.append((timestamp, order, f"[Blender] [{_log_clock(timestamp)}] {line}"))
            order += 1

    for stage, state in _stage_states(task_dir, task).items():
        if state == "pending" or (source_task and stage == "input"):
            continue
        timestamp = str(task.get(f"{stage}_updated_at") or task.get(f"{stage}_finished_at")
                        or task.get("created_at") or "")
        if not include(timestamp):
            continue
        step = task.get(f"{stage}_step") or state
        error = task.get(f"{stage}_error") or task.get(f"{stage}_summary_error")
        if stage == "render":
            session = _read_json(task_dir / "render/rendered_review_session.json")
            step = session.get("render_step") or step
            error = session.get("render_error") or error
            for row in session.get("history", []):
                details = [f"Version {row.get('version', 0)}: {row.get('generation_status', 'pending')}"]
                if row.get("task_id"):
                    details.append(f"Task ID: {row['task_id']}")
                if row.get("user_feedback"):
                    details.append(f"Revision instructions: {row['user_feedback']}")
                if row.get("evaluation_error"):
                    details.append(str(row["evaluation_error"]))
                for detail in details:
                    rows.append((timestamp, order, f"[Render] [{_log_clock(timestamp)}] {detail}"))
                    order += 1
        elif state == "attention":
            error = error or task.get("error")
        message = f"{state}: {step}" + (f" | {error}" if error else "")
        rows.append((timestamp, order, f"[{stage.title()}] [{_log_clock(timestamp)}] {message}"))
        order += 1
    rows.sort(key=lambda item: (item[0], item[1]))
    messages = [message for _, _, message in rows]
    return ([header, *messages[-(limit - 1):]] if limit > 1 else [header]) if header else messages[-limit:]


def load_task(task_dir: Path) -> TaskSnapshot | None:
    task_dir = Path(task_dir).resolve()
    task_file = task_dir / "task.json"
    if not task_file.is_file():
        return None
    task = _read_json(task_file)
    states = _stage_states(task_dir, task)
    current = _current_stage(states)
    error = ""
    if states.get(current) in {"attention", "running"}:
        error = task.get(f"{current}_error")
        if current == "render":
            session = _read_json(task_dir / "render/rendered_review_session.json")
            error = session.get("render_error") or error
        else:
            error = error or task.get("error") or task.get(f"{current}_summary_error")
    preview = _inside_file(task_dir, task.get("blender_preview_file")) or _newest(
        task_dir, "blender/stages/*/*/preview.png"
    )
    white = _inside_file(task_dir, task.get("blender_video_file"))
    final = _inside_file(task_dir, task.get("render_video_file"))
    timestamps = [
        str(task.get(key) or "")
        for key in ("render_updated_at", "blender_updated_at", "input_updated_at", "input_finished_at", "stopped_at", "created_at")
    ]
    timestamps.append(str(task.get("display_name_updated_at") or ""))
    updated = max(timestamps, default="")
    task_id = str(task.get("task_id") or task_dir.name)
    return TaskSnapshot(
        task_id=task_id,
        display_name=str(task.get("display_name") or task_id),
        task_dir=task_dir,
        status=str(task.get("status") or "unknown"),
        current_stage=current,
        stage_states=states,
        created_at=str(task.get("created_at") or ""),
        updated_at=updated,
        input_type=task.get("input_type") if type(task.get("input_type")) is int else None,
        asset_name=str(task.get("asset_name") or ""),
        error=str(error or ""),
        task=task,
        preview_image=preview,
        white_video=white,
        final_video=final,
        console_log="\n".join(_structured_console(task_dir, task)) or "No runtime logs are available for this task yet.",
    )


def list_tasks(output_root: Path) -> list[TaskSnapshot]:
    root = Path(output_root).resolve()
    if not root.is_dir():
        return []
    tasks = [snapshot for child in root.iterdir() if child.is_dir() if (snapshot := load_task(child))]
    tasks.sort(
        key=lambda item: (item.updated_at, item.task_dir.stat().st_mtime),
        reverse=True,
    )
    return tasks
