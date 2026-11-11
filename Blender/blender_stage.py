from __future__ import annotations

import argparse
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from Blender import runtime as backend
from Blender import blender_modeling as workflow
from Blender import harness_output as harness
from Blender.history import load_run, verify_artifact
from Blender.common import log_progress, progress_heartbeat, task_lock, validate_blend
from Blender.blender_modeling import Feedback
from Blender.inputs import load_blender_input
from input_pipeline.common import now, read_json, save_json


def terminal_feedback(context: dict) -> Feedback:
    print(f"\n[{context['stage']} / {context['round']}] Preview: {context['preview']}")
    if context.get("video"):
        print(f"Full white-model video: {context['video']}")
    if not sys.stdin.isatty():
        raise EOFError("For non-interactive input, explicitly use --no-feedback or provide feedback_provider")
    while True:
        action = input("accept / revise / quit: ").strip().lower()
        if action in ("accept", "quit"):
            return Feedback(action)
        if action != "revise":
            continue
        text = input("Revision feedback (one line; press Enter to submit): ").strip()
        images = []
        print("Enter one reference image path per line; submit an empty line to finish:")
        while True:
            value = input().strip().strip('"')
            if not value:
                break
            path = Path(value).expanduser().resolve()
            if not path.is_file() or path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
                print("Invalid image path. Try again.")
                continue
            images.append(str(path))
        if not text and not images:
            print("Revision feedback must not be empty.")
            continue
        return Feedback("revise", text, images)


def prepare_blender_input(path: str | Path, *, input_type: int | None = None) -> Path:
    return load_blender_input(path, input_type=input_type).input_json


def _read_task(task_dir: Path) -> dict[str, Any]:
    path = task_dir / "task.json"
    task = read_json(path) if path.is_file() else {}
    if not isinstance(task, dict):
        raise ValueError("task.json must contain a JSON object.")
    return task


def _record(task_dir: Path, status: str, **updates: Any) -> None:
    task = _read_task(task_dir)
    task.setdefault("task_id", task_dir.name)
    task.setdefault("start_stage", "blender")
    task.setdefault("created_at", now())
    task.setdefault("blender_started_at", now())
    task.update(status=status, blender_backend="pipeline",
                blender_run_dir="blender", blender_updated_at=now(), **updates)
    if status == "blender_running":
        for key in ("error", "stopped_at", "blender_finished_at"):
            task.pop(key, None)
    save_json(task_dir / "task.json", task)


def _export_summary(task_dir):
    destination = task_dir / "blender" / harness.SUMMARY_FILENAME
    try:
        log_progress("Checking the stage summary and existing artifacts", source="Harness")
        record = harness.collect_record(task_dir)
        if not harness.summary_matches(record, destination):
            _record(task_dir, "blender_awaiting_summary", blender_status="awaiting_summary",
                    blender_step="summarizing", blender_summary_error=None)
            log_progress(
                "Requesting the stage summary; waiting for the complete response after connecting"
                f" (model: {harness.BLENDER_SUMMARY_MODEL})", source="Harness"
            )
            with progress_heartbeat("Stage summary", source="Harness"):
                summary = harness.summarize_record(record, task_dir)
            harness.save_summary(record, summary, destination)
            log_progress(f"Stage summary saved: {destination}", source="Harness")
    except (Exception, KeyboardInterrupt) as exc:
        _record(task_dir, "blender_awaiting_summary", blender_status="awaiting_summary",
                blender_step="summarizing", blender_summary_error=str(exc) or "Summary interrupted")
        log_progress(f"White-model video preserved; summary is incomplete: {exc}", source="Harness")
        print(f'Retry the summary only: python -m Blender.blender_stage "{task_dir}" --summary-only')
        if isinstance(exc, KeyboardInterrupt):
            raise
        return False
    task = _read_task(task_dir)
    # 复用总结时保留已进入下游阶段的任务状态。
    if (task.get("blender_status") != "completed"
            or task.get("blender_summary_file") != destination.relative_to(task_dir).as_posix()):
        _record(task_dir, "blender_completed", blender_status="completed", blender_step="completed",
                blender_summary_file=destination.relative_to(task_dir).as_posix(),
                blender_summary_error=None, blender_finished_at=now(),
                blender_video_file=record["output"]["white_model_video"],
                blender_preview_file=record["output"]["preview_image"],
                blender_blend_file=record["output"]["blend_file"])
    return True


def run_blender_task(task_dir: str | Path, *, no_feedback=False,
                     feedback_provider=None, summary_only=False) -> int:
    task_dir = Path(task_dir).expanduser().resolve(strict=True)
    if not task_dir.is_dir():
        raise ValueError("Task directory does not exist")
    if no_feedback and feedback_provider is not None:
        raise ValueError("no_feedback and feedback_provider are mutually exclusive")
    log_progress(f"Loading Blender task: {task_dir}")
    with task_lock(task_dir / "blender"):
        try:
            state = load_run(task_dir)
            if summary_only:
                verify_artifact(task_dir, state["final"]["video"])
                backend.render_existing(task_dir)
                _export_summary(task_dir)
                return 0
            if state["final"].get("video"):
                log_progress("Completed white-model video found; checking artifacts and the stage summary before resuming")
                record = harness.collect_record(task_dir) if state["final"].get("preview") else None
                if record and harness.summary_matches(record, task_dir / "blender" / harness.SUMMARY_FILENAME):
                    _export_summary(task_dir)
                    log_progress("Reusing the completed white-model video and stage summary")
                    return 0
            _record(task_dir, "blender_running", blender_status="running", blender_step="modeling")
            if not state["final"].get("blend"):
                if (task_dir / "blender/workflow_state.json").exists():
                    raise ValueError("Legacy output layout found. Run --organize-only on this task first")
                input_type = _read_task(task_dir).get("input_type")
                prepare_blender_input(task_dir / "input/input.json", input_type=input_type)
                provider = (lambda context: Feedback(source="auto")) if no_feedback else (
                    feedback_provider or terminal_feedback)
                if state.get("source_task") and "composed" not in state["accepted"]:
                    rows = [row for row in state["rounds"] if row["stage"] == "composed" and row.get("completed_at")]
                    previous = rows[-1] if rows else None
                    if previous and previous.get("decision", {}).get("action") != "revise":
                        session = SimpleNamespace(task_dir=task_dir, state=state, active=previous)
                        _record(task_dir, "blender_running", blender_step="composed_rendering")
                        context = backend.prepare_composed_review(session)
                        _record(task_dir, "blender_running", blender_step="composed_feedback")
                        decision = workflow.save_feedback(session, provider(context))
                        if decision.action == "quit":
                            raise KeyboardInterrupt("Modeling stopped by the user")
                if set(state["accepted"]) == {"scene_only", "robot_only", "composed"}:
                    workflow.finalize(SimpleNamespace(task_dir=task_dir, state=state, validate_blend=validate_blend))
                else:
                    with backend.prepare(task_dir, input_type=input_type) as session:
                        for stage in ("scene_only", "robot_only", "composed"):
                            if stage in session.state["accepted"]:
                                continue
                            while True:
                                _record(task_dir, "blender_running", blender_step=f"{stage}_modeling")
                                result = workflow.run_stage(session, stage)
                                if stage == "composed":
                                    _record(task_dir, "blender_running", blender_step="composed_rendering")
                                    result = backend.prepare_composed_review(session)
                                    _record(task_dir, "blender_running", blender_step="composed_feedback")
                                decision = result.pop("decision")
                                if decision is None:
                                    decision = provider(result)
                                decision = workflow.save_feedback(session, decision)
                                if decision.action == "quit":
                                    raise KeyboardInterrupt("Modeling stopped by the user")
                                if decision.action == "accept":
                                    break
                        workflow.finalize(session)
            else:
                verify_artifact(task_dir, state["final"]["blend"])
            _record(task_dir, "blender_running", blender_status="running", blender_step="rendering")
            backend.render_existing(task_dir)
            _export_summary(task_dir)
            return 0
        except (KeyboardInterrupt, EOFError):
            if _read_task(task_dir).get("blender_status") != "awaiting_summary":
                _record(task_dir, "blender_interrupted", blender_status="interrupted", stopped_at=now())
            print(f'Completed rounds preserved. Resume with: python -m Blender.blender_stage "{task_dir}"')
            return 130
        except Exception as exc:
            _record(task_dir, "failed", blender_status="failed", error=str(exc), stopped_at=now())
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the three Blender modeling stages and white-model rendering; --render-only renders an existing project")
    parser.add_argument("task_dir", type=Path,
                        help="Task directory containing input/input.json")
    parser.add_argument("--summary-only", action="store_true", help="Generate the missing white-model stage summary without modeling or rendering again")
    parser.add_argument("--organize-only", action="store_true", help="Migrate legacy outputs once without calling a model")
    parser.add_argument("--render-only", action="store_true", help="Skip Codex and render the existing blender/final.blend")
    parser.add_argument("--no-feedback", action="store_true", help="Explicitly accept all stages automatically for testing without human feedback")
    parser.add_argument("--video-output", type=Path, help="Video output path for --render-only; defaults to blender/white_model.mp4")
    parser.add_argument("--resolution", nargs=2, type=int, metavar=("WIDTH", "HEIGHT"),
                        help="Output dimensions for --render-only; width and height must be even. By default, preserve the project aspect ratio with a minimum short side of 720 pixels")
    parser.add_argument("--frame-end", type=int, help="End frame for --render-only; use 2 for a short test")
    args = parser.parse_args(argv)
    if sum((args.render_only, args.summary_only, args.organize_only)) > 1:
        parser.error("render-only, summary-only, and organize-only are mutually exclusive")
    if (args.summary_only or args.organize_only) and (
            args.no_feedback or args.video_output or args.resolution or args.frame_end):
        parser.error("Summary and migration modes do not accept modeling/rendering options")
    if args.organize_only:
        from Blender.history import organize_existing
        with task_lock(args.task_dir.resolve() / "blender"):
            organize_existing(args.task_dir)
        return 0
    if args.summary_only:
        return run_blender_task(args.task_dir, summary_only=True)
    if args.render_only:
        if args.no_feedback:
            parser.error("--render-only does not accept --no-feedback")
        with task_lock(args.task_dir.resolve() / "blender"):
            backend.render_existing(args.task_dir, output_video=args.video_output,
                            resolution=tuple(args.resolution) if args.resolution else None,
                            frame_end=args.frame_end)
    else:
        if args.video_output or args.resolution or args.frame_end:
            parser.error("--video-output, --resolution, and --frame-end require --render-only")
        return run_blender_task(args.task_dir, no_feedback=args.no_feedback)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
