from __future__ import annotations

import hashlib
import json
from pathlib import Path
import uuid

from Blender.common import hash_path, progress_heartbeat, task_lock
from Blender.history import local_path
from Harness.experience_adapter import ExperienceAdapter
from config import PROJECT_ROOT
from input_pipeline.common import now, read_json, save_json


def _log(message):
    print(f"[Harness] {message}", flush=True)


def _record(task_dir, status, **updates):
    task = read_json(task_dir / "task.json")
    task.update(status=f"harness_{status}", harness_status=status,
                harness_updated_at=now(), **updates)
    save_json(task_dir / "task.json", task)


def _sources(task_dir, task):
    documents, fingerprints = [], {}
    for stage in ("blender", "render"):
        if task.get(f"{stage}_status") != "completed":
            raise ValueError(f"The {stage} stage is incomplete; Harness cannot run yet")
        path = local_path(task_dir, task.get(f"{stage}_summary_file") or
                          f"{stage}/{stage}_stage_summary.json")
        document = read_json(path)
        if not isinstance(document, dict) or document.get("stage") != stage:
            raise ValueError(f"Invalid {stage} stage record: {path}")
        if not str(document.get("stage_summary") or "").strip():
            raise ValueError(f"The {stage} stage summary is empty")
        fingerprints[path.relative_to(task_dir).as_posix()] = hash_path(path)
        # Blender 的单个输入、输出对象需补为 Harness 使用的路径数组。
        document = dict(document)
        document.setdefault("inputs", [{"path": "input/input.json"}])
        outputs = list(document.get("outputs", []))
        keys = ("blender_blend_file", "blender_video_file", "blender_preview_file") if stage == "blender" else ("render_video_file",)
        for key in keys:
            value = task.get(key)
            if not value:
                if key.endswith("video_file"):
                    raise ValueError(f"Task record is missing {key}")
                continue
            artifact = local_path(task_dir, value)
            if not artifact.is_file() or not artifact.stat().st_size:
                raise ValueError(f"Stage artifact is missing or empty: {artifact}")
            digest = hash_path(artifact)
            output_key = {"blender_blend_file": "blend", "blender_video_file": "video",
                          "blender_preview_file": "preview"}.get(key)
            expected = (document.get("output", {}).get("sha256", {}).get(output_key)
                        if output_key else None)
            if expected and digest != expected:
                raise ValueError(f"Completed artifact has been modified: {artifact}")
            fingerprints[artifact.relative_to(task_dir).as_posix()] = digest
            outputs.append({"role": key, "path": artifact.relative_to(task_dir).as_posix()})
        document["outputs"] = outputs
        document.setdefault("retrieved_experiences", [])
        documents.append(document)
    return documents, fingerprints


def run_harness_task(task_dir: str | Path) -> int:
    task_dir = Path(task_dir).expanduser().resolve(strict=True)
    root = task_dir / "harness"
    with task_lock(root):
        try:
            _log("Checking Blender and Render stage records")
            task = read_json(task_dir / "task.json")
            documents, sources = _sources(task_dir, task)
            input_path = task_dir / "input/input.json"
            inputs = read_json(input_path)
            sources["input/input.json"] = hash_path(input_path)
            adapter = ExperienceAdapter(PROJECT_ROOT, inputs)
            signature = {"sources": sources, "experience_store": str(adapter.store),
                         "learning_enabled": adapter.enabled}
            fingerprint = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
            run_dir = root / "runs" / fingerprint
            receipt = run_dir / "result.json"
            harness = adapter.harness
            _record(task_dir, "running", harness_error=None)
            if receipt.is_file():
                result = read_json(receipt)
                if result.get("source_fingerprint") != fingerprint:
                    raise ValueError("The Harness completion record does not match the inputs")
                _log("Reusing the completed experience-processing record")
            else:
                run_dir.mkdir(parents=True, exist_ok=True)
                manifest_path = run_dir / "manifest.json"
                if not manifest_path.exists():
                    # 重试沿用同一个 run_id，避免部分写入后重复计入经验。
                    assets = inputs.get("assets") or inputs.get("robot_assets") or {}
                    robot = inputs.get("robot") or assets.get("urdf") or assets.get("root") or "unknown_robot"
                    manifest = {
                        "schema_version": harness.SCHEMA,
                        "run_id": f"{task_dir.name}-{uuid.uuid4().hex}", "task_id": task_dir.name,
                        "created_at": now(), "source_dir": str(task_dir),
                        "task": {"robot": Path(str(robot)).stem, "task_type": inputs.get("task_type"),
                                 "scene": inputs.get("scene")},
                        "inputs": {"files": [str(input_path)], "description": inputs.get("instruction", "")},
                        "stages": [], "artifacts": [], "experience_refs": [],
                        # Render 评估与 Harness 的 metrics/threshold 格式不同，保留在阶段文档中。
                        "quality": None, "evaluation": None, "feedback": None,
                    }
                    for document in documents:
                        path = run_dir / "stages" / f"{document['stage']}.json"
                        path.parent.mkdir(parents=True, exist_ok=True)
                        save_json(path, document)
                        manifest["stages"].append(harness.normalize_stage_summary(document, path))
                    harness.save_manifest(run_dir, manifest)
                _log("Archiving both stage records and final artifacts")
                archive = harness.archive_run(run_dir)
                if archive["skipped_missing"]:
                    raise ValueError("Stage records reference missing archive files")
                learned_path = run_dir / "learning_result.json"
                if learned_path.is_file():
                    learned = read_json(learned_path)
                else:
                    _log("Updating the experience library from both stage records; waiting for completion")
                    with progress_heartbeat("Experience processing", notify=_log):
                        learned = harness.learn_run(
                            run_dir, experience_store=adapter.store if adapter.enabled else None,
                            **adapter.request_options)
                    save_json(learned_path, learned)
                report = harness.report_run(run_dir)
                result = {"source_fingerprint": fingerprint, **signature, "learning": learned,
                          "archive": archive, "report": report.relative_to(task_dir).as_posix(),
                          "completed_at": now()}
                save_json(receipt, result)
            report = local_path(task_dir, result["report"])
            if not report.is_file():
                harness.report_run(run_dir)
            save_json(root / "result.json", result)
            _record(task_dir, "completed", harness_error=None, harness_finished_at=now(),
                    harness_result_file="harness/result.json", harness_report_file=result["report"],
                    harness_decision_mode=result["learning"]["decision_mode"])
            _log(f"Experience processing complete ({result['learning']['decision_mode']}); report: {report}")
            return 0
        except (KeyboardInterrupt, EOFError):
            _record(task_dir, "interrupted", harness_error="Experience processing interrupted by the user")
            _log(f"Records saved. Resume experience processing for this task to continue. Task directory: {task_dir}")
            return 130
        except Exception as exc:
            _record(task_dir, "failed", harness_error=str(exc))
            _log(f"Experience processing is incomplete: {exc}\nRetry experience processing for this task. Task directory: {task_dir}")
            return 1
