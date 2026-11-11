from __future__ import annotations

import json
import os
from pathlib import Path
import uuid

from input_pipeline.common import now, read_json, save_json
from .common import hash_path

STAGES = ("scene_only", "robot_only", "composed")
STAGE_BLEND_FILES = {
    "scene_only": "scene.blend",
    "robot_only": "robot.blend",
    "composed": "composed.blend",
}


def load_run(task_dir):
    path = Path(task_dir) / "blender/run.json"
    if path.exists():
        state = read_json(path)
        if not isinstance(state, dict) or not isinstance(state.get("rounds"), list):
            raise ValueError("Invalid Blender run.json")
        return state
    return {"task_id": Path(task_dir).name, "created_at": now(),
            "rounds": [], "accepted": {}, "sessions": [], "final": {}, "renders": []}


def save_run(task_dir, state):
    state["updated_at"] = now()
    path = Path(task_dir) / "blender/run.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    save_json(path, state)


def local_path(task_dir, relative):
    root = Path(task_dir).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Artifact path is outside the task directory: " + str(relative))
    return path


def artifact(task_dir, path):
    path = Path(path).resolve()
    return {"file": path.relative_to(Path(task_dir).resolve()).as_posix(),
            "sha256": hash_path(path)}


def verify_artifact(task_dir, record):
    path = local_path(task_dir, record["file"])
    if not path.is_file() or hash_path(path) != record["sha256"]:
        raise ValueError("Completed artifact is missing or has been modified: " + str(path))
    return path


def append_event(task_dir, row, event, data):
    path = local_path(task_dir, row["folder"]) / "events.jsonl"
    record = {"id": uuid.uuid4().hex, "time": now(), "event": event, "data": data}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return record["id"]


def round_context(task_dir, row):
    return {"stage": row["stage"], "round": row["round"],
            "blend": str(verify_artifact(task_dir, row["artifacts"]["blend"])),
            "preview": str(verify_artifact(task_dir, row["artifacts"]["preview"])),
            "video": str(verify_artifact(task_dir, row["artifacts"]["video"])) if row["artifacts"].get("video") else None,
            "thread_id": row.get("thread_id"),
            "decision": row.get("decision") if row.get("decision", {}).get("action") != "quit" else None}


def recovery_context(state):
    return [{"id": row["id"], "stage": row["stage"], "round": row["round"],
             "feedback": row.get("feedback"), "decision": row.get("decision"),
             "artifacts": row.get("artifacts"), "report": row.get("report")}
            for row in state["rounds"] if row.get("completed_at")]


def copy_task_results(source, destination):
    import shutil
    import subprocess
    from .common import discover_blender_bin, log_progress, progress_heartbeat, validate_blend

    source, destination = Path(source).resolve(), Path(destination).resolve()
    saved = load_run(source)
    if set(saved["accepted"]) != set(STAGES) or not saved["final"].get("blend"):
        raise ValueError("Select a task with all three Blender stages completed.")
    selected = {}
    for stage in STAGES:
        blend = verify_artifact(source, saved["accepted"][stage])
        rows = [row for row in saved["rounds"] if row["stage"] == stage
                and row.get("completed_at") and row.get("artifacts", {}).get("blend") == saved["accepted"][stage]]
        if not rows:
            raise ValueError(f"The historical task has no completed {stage} record.")
        row = rows[-1]
        verify_artifact(source, row["artifacts"]["preview"])
        selected[stage] = (row, blend)

    blender = Path(discover_blender_bin())
    if not blender.is_file():
        raise FileNotFoundError(f"Blender executable not found: {blender}")
    # 新副本继承目标目录权限，避免沿用源目录或私有临时目录的权限。
    shutil.copytree(source / "input", destination / "input", copy_function=shutil.copyfile)
    derived = source / "blender/derived"
    if derived.is_dir():
        shutil.copytree(derived, destination / "blender/derived", copy_function=shutil.copyfile)
    handoff_path = destination / "input/input.json"
    handoff = read_json(handoff_path)
    for key in ("first_frame", "trajectory", "human_video"):
        if not handoff.get(key):
            continue
        original = Path(handoff[key])
        original = (original if original.is_absolute() else source / "input" / original).resolve(strict=True)
        copied = (destination / original.relative_to(source) if original.is_relative_to(source)
                  else destination / "input/files" / (key + original.suffix.lower()))
        if not copied.exists():
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, copied)
        handoff[key] = str(copied)
    save_json(handoff_path, handoff)

    state = load_run(destination)
    state.update(source_task=source.name, input_type=handoff["input_type"])
    rebase_script = Path(__file__).resolve().parent / "scripts/rebase_history.py"
    for stage, (original_row, original_blend) in selected.items():
        log_progress(f"Loading historical {stage} stage")
        folder = local_path(destination, original_row["folder"])
        # Keep the artifacts, but start the copied task with fresh execution logs.
        shutil.copytree(local_path(source, original_row["folder"]), folder,
                        copy_function=shutil.copyfile,
                        ignore=shutil.ignore_patterns("events.jsonl", "*.log"))
        blend = folder / STAGE_BLEND_FILES[stage]
        shutil.copyfile(original_blend, blend)
        evidence = folder / "evidence"
        evidence.mkdir(exist_ok=True)
        command = [str(blender), "--background", str(blend), "--python-exit-code", "1",
                   "--python", str(rebase_script), "--", str(source), str(destination), str(original_blend)]
        with (evidence / "history_copy.log").open("w", encoding="utf-8") as log:
            with progress_heartbeat(f"Loading historical {stage}", interval=5):
                subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        validate_blend(blend)
        preview = folder / "preview.png"
        shutil.copyfile(verify_artifact(source, original_row["artifacts"]["preview"]), preview)
        report = folder / "report.json"
        save_json(report, {"source_task": source.name, "source_round": original_row["id"],
                           "source_report": read_json(verify_artifact(source, original_row["artifacts"]["report"]))})
        row = {"id": original_row["id"], "stage": stage, "round": original_row["round"],
               "folder": original_row["folder"], "status": "ready" if stage == "composed" else "accepted",
               "started_at": now(), "completed_at": now(), "decisions": [],
               "report": report.relative_to(destination).as_posix(),
               "artifacts": {"blend": artifact(destination, blend), "preview": artifact(destination, preview),
                             "report": artifact(destination, report)}}
        if (stage == "composed" and saved["final"].get("video") and saved["final"].get("preview")
                and saved["final"].get("rendered_blend_sha256") == saved["accepted"][stage]["sha256"]):
            video, sheet = folder / "white_model.mp4", folder / "video_preview.jpg"
            shutil.copyfile(verify_artifact(source, saved["final"]["video"]), video)
            shutil.copyfile(verify_artifact(source, saved["final"]["preview"]), sheet)
            row["artifacts"].update(video=artifact(destination, video), video_preview=artifact(destination, sheet))
            row["video_metadata"] = saved["final"]["video_metadata"]
            row["video_render"] = {"status": "completed", "blend_sha256": row["artifacts"]["blend"]["sha256"],
                                   "source_task": source.name, "source_round": original_row["id"],
                                   "log": (evidence / "history_copy.log").relative_to(destination).as_posix()}
        if stage != "composed":
            row["decision"] = {"action": "accept", "source": "history"}
            state["accepted"][stage] = row["artifacts"]["blend"]
        state["rounds"].append(row)
        append_event(destination, row, "history.loaded", {"source_task": source.name, "source_round": original_row["id"]})
        log_progress(f"Historical {stage} stage loaded")
    save_run(destination, state)


def organize_existing(task_dir):
    import shutil
    import re
    from .runtime import build_video_preview
    task_dir = Path(task_dir).resolve()
    root = task_dir / "blender"
    ledger = root / "derived/organize_cleanup.json"

    def safe(path):
        path = Path(path)
        if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
            raise ValueError("Migration path is outside the Blender directory: " + str(path))
        return path

    def clean(manifest):
        removed = 0
        for entry in manifest:
            source = safe(local_path(task_dir, entry["file"]))
            if source.is_file():
                if hash_path(source) != entry["sha256"]:
                    raise ValueError("Source file changed during migration: " + str(source))
                source.unlink()
                removed += 1
        # 只删除空目录，保留未列入归档的用户文件。
        for directory in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if directory.is_dir() and not any(directory.iterdir()):
                safe(directory).rmdir()
        ledger.unlink(missing_ok=True)
        return removed

    if (root / "run.json").exists():
        if ledger.exists():
            clean(read_json(ledger))
        return load_run(task_dir)
    old_file = root / "workflow_state.json"
    if not old_file.exists():
        raise ValueError("No legacy workflow_state.json is available to migrate")
    old = read_json(old_file)
    state = load_run(task_dir)
    task = read_json(task_dir / "task.json")
    state.update(input_type=task.get("input_type"), imported_history=True,
                 model=old.get("model"),
                 input_fingerprints={**old.get("input_fingerprints", {}), "input_type": task.get("input_type")},
                 record_limitations=["One-time migration of historical outputs; missing intermediate operations, completion times, and acceptance sources have not been reconstructed."])
    deletion = {}
    def discard(source):
        source = safe(source)
        deletion[str(source)] = artifact(task_dir, source)
    def keep(source, destination):
        source, destination = safe(source), safe(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and hash_path(source) != hash_path(destination):
            raise ValueError("Migration destination already exists with different content: " + str(destination))
        if not destination.exists():
            shutil.copy2(source, destination)
        if hash_path(source) != hash_path(destination):
            raise ValueError("Migration copy verification failed")
        discard(source)
        return destination

    for stage in STAGES:
        directory = root / stage
        if not directory.exists():
            continue
        for source_folder in sorted((p for p in directory.iterdir() if p.is_dir() and p.name.isdigit()),
                                    key=lambda p: int(p.name)):
            number = int(source_folder.name)
            folder = root / "stages" / stage / f"{number:03d}"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "evidence").mkdir(exist_ok=True)
            (folder / "events.jsonl").write_text("", encoding="utf-8")
            row = {"id": f"{stage}/{number:03d}", "stage": stage, "round": number,
                   "folder": folder.relative_to(task_dir).as_posix(), "status": "interrupted",
                   "previous_round": None, "restart_of": None, "decisions": [],
                   "thread_id": old.get("thread_id"), "feedback": None}
            siblings = [r for r in state["rounds"] if r["stage"] == stage and r.get("completed_at")]
            if siblings:
                row["previous_round"] = siblings[-1]["id"]
            records, feedback_dir = source_folder / "records", source_folder / "feedback"
            if (feedback_dir / "feedback.json").exists():
                row["feedback"] = read_json(feedback_dir / "feedback.json")
            decision_file = feedback_dir / "decision.json"
            if decision_file.exists():
                row["decision"] = {**read_json(decision_file), "source": "unknown"}
                row["decisions"] = [row["decision"]]
            files = list(source_folder.rglob("*"))
            seen = {}
            agent_reports = {}
            blend = source_folder / "blends" / STAGE_BLEND_FILES[stage]
            preview = source_folder / "renders/preview.png"
            target_blend = None
            if blend.is_file():
                final = root / "final.blend"
                if stage == "composed" and final.is_file() and hash_path(final) == hash_path(blend):
                    target_blend = final
                    discard(blend)
                else:
                    target_blend = keep(blend, folder / STAGE_BLEND_FILES[stage])
            target_preview = keep(preview, folder / "preview.png") if preview.is_file() else None
            for source in sorted(p for p in files if p.is_file()):
                if source in {blend, preview}:
                    continue
                digest = hash_path(source)
                relative = source.relative_to(source_folder)
                if source.name in {"operations.json", "prompt.txt", "response.md"}:
                    if digest not in seen:
                        value = read_json(source) if source.suffix == ".json" else source.read_text(encoding="utf-8")
                        if source.name == "operations.json":
                            for operation in value:
                                data = operation.get("root", operation) if isinstance(operation, dict) else {"text":str(operation)}
                                if data.get("type") != "reasoning":
                                    append_event(task_dir, row, "item.completed", data)
                        else:
                            append_event(task_dir, row, "imported." + source.stem, {"text": value})
                        seen[digest] = "events"
                    discard(source)
                elif source.name in {"checkpoint.json", "artifacts.json"} or source.name.startswith("stage_summary"):
                    if digest not in seen:
                        append_event(task_dir, row, "imported.status", read_json(source))
                        seen[digest] = "events"
                    discard(source)
                elif source.name == "white_model.mp4" and (root / "white_model.mp4").exists() and digest == hash_path(root / "white_model.mp4"):
                    discard(source)
                elif source.name == "video_render.log" and (root / "render.log").exists() and digest == hash_path(root / "render.log"):
                    discard(source)
                else:
                    target = folder / "evidence" / relative
                    keep(source, target)
                    if source.name in {"session_summary.json", stage + "_report.json"}:
                        agent_reports[source.name] = read_json(source)
            report = folder / "report.json"
            write = {"agent_report": agent_reports,
                     "host_checks": {"imported_existing_files": True},
                     "limitations": ["Validation conclusions in historical reports come from the original modeling records; these checks were not rerun during migration."]}
            save_json(report, write)
            row["report"] = report.relative_to(task_dir).as_posix()
            if target_blend and target_preview:
                from PIL import Image
                with Image.open(target_preview) as image:
                    image.verify()
                row.update(status="ready", completed_at="imported",
                           artifacts={"blend": artifact(task_dir, target_blend),
                                      "preview": artifact(task_dir, target_preview),
                                      "report": artifact(task_dir, report)})
                accepted = old.get("accepted", {}).get(stage)
                if accepted and Path(accepted).resolve() == blend.resolve():
                    row["status"] = "accepted"
                    state["accepted"][stage] = row["artifacts"]["blend"]
            state["rounds"].append(row)

    scripts = root / "scripts"
    if scripts.exists():
        for source in scripts.rglob("*"):
            if source.is_file():
                match = re.match(r"(scene_only|robot_only|composed)_(\d+)_", source.name)
                row = next((r for r in state["rounds"] if match and r["stage"] == match[1]
                            and r["round"] == int(match[2])), None)
                destination = local_path(task_dir, row["folder"]) / "scripts" / source.name if row else root / "derived/legacy_scripts" / source.relative_to(scripts)
                keep(source, destination)
    final = root / "final.blend"
    if final.exists():
        state["final"]["blend"] = artifact(task_dir, final)
    video = root / "white_model.mp4"
    if video.exists() and final.exists():
        metadata = build_video_preview(video, root / "preview.jpg")
        state["final"].update(video=artifact(task_dir, video), preview=artifact(task_dir, root / "preview.jpg"),
                              video_metadata=metadata, rendered_blend_sha256=hash_path(final))
        for folder in root.glob(".white_model.*_frames"):
            safe(folder)
            entries = list(folder.iterdir())
            if entries and all(p.is_file() and re.fullmatch(r"frame_\d+\.png", p.name) for p in entries):
                for source in entries:
                    discard(source)
    if (root / "render.log").exists():
        log = keep(root / "render.log", root / "logs/render.log")
        state["renders"].append({"id":"render/000", "status":"completed" if video.exists() else "unknown",
                                 "log":log.relative_to(task_dir).as_posix(), "imported":True})
    if (root / "developer_instructions.txt").exists():
        keep(root / "developer_instructions.txt", root / "derived/instructions_imported.md")
    input_data = read_json(task_dir / "input/input.json")
    trajectory = input_data.get("trajectory")
    source = Path(trajectory) if trajectory else None
    if source and not source.is_absolute():
        source = task_dir / "input" / source
    if source and source.is_file():
        raw = root / "inputs/trajectory.parquet"
        if raw.exists() and hash_path(raw) == hash_path(source):
            discard(raw)
    legacy_inputs = root / "inputs"
    if legacy_inputs.exists():
        for source in legacy_inputs.rglob("*"):
            if source.is_file() and str(source) not in deletion:
                keep(source, root / "derived/imported_inputs" / source.relative_to(legacy_inputs))
    for name in ("workflow_state.json", "blender_history.json", "blender_history.md"):
        path = root / name
        if path.is_file():
            keep(path, root / "derived/imported_records" / name)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    save_json(ledger, list(deletion.values()))
    save_run(task_dir, state)
    clean(list(deletion.values()))
    task.update(blender_status="awaiting_summary", blender_step="summarizing")
    if not task.get("render_status"):
        task["status"] = "blender_awaiting_summary"
    save_json(task_dir / "task.json", task)
    return state
