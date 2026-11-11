from __future__ import annotations

import argparse
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import threading
from typing import Any

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import RENDER_AUTO_CORRECTION_SCORE
from Blender.common import progress_heartbeat
from input_pipeline.common import read_json
from Render import service as backend, seedance as api, harness_output as harness
from Render.harness_adapter import RenderHarnessAdapter
from Render.service import prepare_render_input
from Render.session import prepare_render, load_session, save_progress, get_rendered_video
from Render.seedance_settings import SeedanceSettings
from Render.video_host import LocalVideoPublisher
from Render.evaluation import EvaluationConfig, build_feedback_prompt, run_objective_evaluation
from Render.vision import evaluate_openai_vlm, merge_objective_and_vlm
from Render.prompts import build_user_revision_prompt


_RENDER_LOG: ContextVar[Path | None] = ContextVar("render_progress_log", default=None)
_RENDER_LOG_LOCK = threading.Lock()


@contextmanager
def capture_render_log(path: Path):
    token = _RENDER_LOG.set(Path(path))
    try:
        yield
    finally:
        _RENDER_LOG.reset(token)


def _log(message, *, path: Path | None = None, log_source: str = "Render"):
    print(f"[{log_source}] {message}", flush=True)
    destination = path if path is not None else _RENDER_LOG.get()
    if destination is None:
        return
    try:
        timestamp = datetime.now(timezone.utc).isoformat()
        with _RENDER_LOG_LOCK:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"time": timestamp, "source": log_source, "message": str(message)}, ensure_ascii=False) + "\n")
    except OSError:
        pass  # A logging failure must not interrupt Render.


def _checkpoint(source, state, status, step, message=None, error=None, *, log_source="Render"):
    save_progress(source, state, status=status, step=step, error=error)
    if message:
        _log(message, log_source=log_source)


def get_status(state) -> dict:
    row = (state.get("history") or [{}])[-1]
    actions = ["q"]
    if state.get("confirmed"):
        phase = "confirmed" if harness.summary_ready(state) else "export_pending"
        if phase == "export_pending":
            actions.insert(0, "x")
        if Path(state["current_output"]).is_file():
            actions.insert(0, "p")
    elif not state.get("history"):
        phase, actions = "not_started", ["c", "q"]
    elif row.get("generation_status") != "downloaded" or not Path(row["video"]).is_file():
        phase = "generation_pending" if row.get("task_id") else "blocked"
        if row.get("task_id"):
            actions.insert(0, "c")
    else:
        actions.insert(0, "p")
        if state.get("initial_phase") == "objective":
            phase = "objective_pending"
            actions.insert(0, "c" if row.get("evaluation_status") == "objective_complete" else "e")
        elif (row.get("evaluation_status") not in ("complete", "disabled")
              or state.get("run_evaluation") and not state.get("current_evaluation")):
            phase = "evaluation_pending"
            actions.insert(0, "e")
        else:
            phase, actions = "awaiting_feedback", ["", "r", "e", *actions]
    return {"phase": phase, "actions": actions, "version": row.get("version"),
            "task_id": row.get("task_id"), "generation_status": row.get("generation_status"),
            "evaluation_status": row.get("evaluation_status"), "confirmed": state.get("confirmed", False)}


def _settle(source, state):
    phase = get_status(state)["phase"]
    row = (state.get("history") or [{}])[-1]
    status = {"confirmed": "completed", "export_pending": "awaiting_export",
              "awaiting_feedback": "awaiting_feedback", "evaluation_pending": "awaiting_evaluation",
              "blocked": "interrupted", "generation_pending": "interrupted"}.get(phase, "running")
    if phase == "objective_pending" and row.get("evaluation_status") == "failed":
        status = "awaiting_evaluation"
    if phase == "not_started" and state.get("render_error"):
        status = "interrupted"
    error = None
    if phase == "export_pending":
        error = (state.get("harness_export") or {}).get("error")
    elif status in {"interrupted", "awaiting_evaluation"}:
        error = row.get("generation_error") or row.get("evaluation_error") or state.get("render_error")
    _checkpoint(source, state, status, phase, error=error)


def _save_pause(source, state):
    if state.get("confirmed"):
        _settle(source, state)
    else:
        _checkpoint(source, state, "interrupted", state.get("render_step", "preparing"), error=state.get("render_error"))


def _load_or_prepare(source, action):
    settings = SeedanceSettings.load()
    if source.session_file.exists():
        state = load_session(source.session_file, settings=settings)
        if (Path(state["initial_output"]).resolve() != source.output
                or Path(state["evaluation_white"]).resolve() != source.white_video
                or Path(state["base_args"].image_1).resolve() != source.reference_image):
            raise ValueError("The session output, white-model video, or first frame does not match the current task.")
        return state
    if action != "continue":
        raise ValueError("The task has not started; this action is unavailable.")
    if source.output.parent.exists() and any(source.output.parent.iterdir()):
        raise FileExistsError("render/ contains files but no session; existing files cannot be overwritten and generation cannot be resubmitted.")
    state = prepare_render(white_video=source.white_video, reference_image=source.reference_image,
                           output=source.output, prompt=source.prompt, settings=settings)
    _checkpoint(source, state, "running", "preparing", "Starting the Render stage.")
    return state


def _generate_version(source, state, output, number, publisher, experience_adapter=None, *, prompt=None,
                      feedback="", kind="user_revision", auto_feedback=None):
    if output.exists():
        raise FileExistsError(f"Video already exists; it cannot be overwritten or resubmitted: {output}")
    _checkpoint(source, state, "running", "preparing_media", "Preparing generation inputs.")
    payload = backend.prepare_generation(state, publisher, prompt=prompt, notify=_log)
    retrieved = {"experiences": []}
    if experience_adapter is not None:
        _log(f"Retrieving relevant experience for version {number}.")
        retrieval_feedback = feedback or (prompt if kind == "objective_auto_revision" else "")
        log_path = _RENDER_LOG.get()
        with progress_heartbeat("Experience retrieval", notify=lambda message: _log(message, path=log_path)):
            retrieved = experience_adapter.begin(number, feedback=retrieval_feedback,
                evaluation=state.get("current_evaluation") or state.get("objective_evaluation"))
        payload = experience_adapter.inject_payload(payload, retrieved)
    experiences = [row for row in retrieved.get("experiences", []) if isinstance(row, dict)]
    row = {"version": number, "video": str(output.resolve()), "task_id": None,
           "version_kind": "initial" if number == 0 else kind, "user_feedback": feedback,
           "task_prompt": payload["prompt"], "retrieved_experiences": experiences,
           "generation_status": "submitting", "evaluation_status": "pending"}
    seen = {item.get("id") for item in state.get("retrieved_experiences", []) if isinstance(item, dict)}
    for item in experiences:
        if item.get("id") and item["id"] not in seen:
            state.setdefault("retrieved_experiences", []).append(item)
            seen.add(item["id"])
    if kind == "objective_auto_revision":
        state["initial_phase"] = "review"
        state["history"][-1]["objective_auto_feedback_prompt"] = str(auto_feedback)
    state["history"].append(row)
    state.update(objective_evaluation=None, current_evaluation={})
    _checkpoint(source, state, "running", "generating", f"Generating version {number}.")

    def submitted(response):
        row["submission_response"] = response
        _checkpoint(source, state, "running", "generating")

    def created(task_id):
        row.update(task_id=task_id, generation_status="running")
        _checkpoint(source, state, "running", "generating", f"Task ID: {task_id}")

    try:
        row["task_id"] = backend.generate_video(state, payload, output,
            on_submitted=submitted, on_created=created, on_status=_log)
    except (Exception, KeyboardInterrupt) as exc:
        row.update(generation_status="interrupted", generation_error=str(exc))
        _checkpoint(source, state, "interrupted", "generating", error=str(exc))
        raise
    row["generation_status"] = "downloaded"
    state.update(current_output=output, revision_number=number)
    _checkpoint(source, state, "running", "generated", f"Version {number} downloaded.")


def _resume_generation(source, state):
    row = state["history"][-1]
    output = Path(row["video"])
    if not backend.video_is_readable(output):
        _checkpoint(source, state, "running", "generating", "Resuming the original video task.")
        try:
            backend.resume_video(state, row.get("task_id"), output, on_status=_log)
        except (Exception, KeyboardInterrupt) as exc:
            row.update(generation_status="interrupted", generation_error=str(exc))
            _checkpoint(source, state, "interrupted", "generating", error=str(exc))
            raise
        state.update(objective_evaluation=None, current_evaluation={})
        row["evaluation_status"] = "pending"
    row["generation_status"] = "downloaded"
    row.pop("generation_error", None)
    state.update(current_output=output, revision_number=row["version"])
    _checkpoint(source, state, "running", "generated")


def _evaluate_version(source, state, *, objective_only=False):
    row = state["history"][-1]
    if not state["run_evaluation"]:
        row["evaluation_status"] = "disabled"
        state["initial_phase"] = "review"
        return True
    row.pop("skip_initial_vlm", None)
    row["evaluation_status"] = "running"
    output = Path(row["video"])
    directory = output.with_name(output.stem + "_evaluation")
    _checkpoint(source, state, "running", "objective_evaluation", "Running objective evaluation.")
    try:
        objective = state.get("objective_evaluation")
        if objective is None:
            objective = run_objective_evaluation(state["evaluation_white"], output, row["task_prompt"], directory)
        state.update(objective_evaluation=objective, current_evaluation=objective)
        row["scores"] = objective.get("scores", {})
        row["overall_score"] = objective.get("aggregates", {}).get("overall_score")
        # 先保存客观评估，模型请求失败后重试可复用。
        _checkpoint(source, state, "running", "objective_evaluation", "Objective evaluation complete.")
        if state["run_gpt_vlm"] and not objective_only:
            _checkpoint(source, state, "running", "gpt_evaluation", "Running GPT evaluation.")
            vlm = evaluate_openai_vlm(state["evaluation_white"], output, row["task_prompt"], directory,
                objective_scores=objective["scores"], reference_image=Path(state["base_args"].image_1), on_retry=_log)
            state["current_evaluation"] = merge_objective_and_vlm(objective, vlm, directory)
        row["evaluation_status"] = "objective_complete" if objective_only else "complete"
        row.pop("evaluation_error", None)
    except (Exception, KeyboardInterrupt) as exc:
        row["evaluation_status"] = "gpt_unavailable" if state.get("objective_evaluation") and not objective_only else "failed"
        row["evaluation_error"] = str(exc)
        _checkpoint(source, state, "interrupted", state["render_step"], f"Evaluation is incomplete: {exc}", error=str(exc))
        if isinstance(exc, KeyboardInterrupt):
            raise
        return False
    result = state["current_evaluation"]
    row["scores"] = result.get("scores", {})
    row["overall_score"] = result.get("aggregates", {}).get("overall_score")
    _checkpoint(source, state, "running", "evaluated", "Evaluation complete for this round.")
    return True


def _finish_generation(source, state, publisher, experience_adapter=None):
    if state["run_evaluation"] and state.get("initial_objective_gate") and state.get("initial_phase") == "objective":
        if not _evaluate_version(source, state, objective_only=True):
            return
        row = state["history"][-1]
        score = float(state["objective_evaluation"]["aggregates"]["objective_score"])
        if not math.isfinite(score):
            raise RuntimeError("Invalid objective score; automatic correction was not submitted.")
        row["objective_gate_score"] = score
        if score < RENDER_AUTO_CORRECTION_SCORE:
            _log(f"Objective score {score:g} is below {RENDER_AUTO_CORRECTION_SCORE:g}; generating one automatic correction.")
            prompt = build_feedback_prompt(row["task_prompt"], state["objective_evaluation"]["scores"],
                EvaluationConfig(pass_score=RENDER_AUTO_CORRECTION_SCORE, motion_pass_score=RENDER_AUTO_CORRECTION_SCORE)).strip()
            output = source.output.with_name(source.output.stem + "_objective_corrected.mp4")
            directory = Path(row["video"]).with_name(Path(row["video"]).stem + "_evaluation")
            directory.mkdir(parents=True, exist_ok=True)
            feedback_file = directory / "objective_auto_feedback_prompt.txt"
            feedback_file.write_text(prompt + "\n", encoding="utf-8")
            _generate_version(source, state, output, 1, publisher, experience_adapter, prompt=prompt,
                              kind="objective_auto_revision", auto_feedback=feedback_file.resolve())
        else:
            state["initial_phase"] = "review"
            _checkpoint(source, state, "running", "gpt_evaluation")
    _evaluate_version(source, state)


def _export_summary(source, state, experience_adapter=None):
    state["harness_export"] = {"status": "pending", "file": None, "error": None}
    _checkpoint(source, state, "awaiting_export", "summarizing", "Preparing final stage records.", log_source="Harness")
    try:
        record = harness.collect_record(state)
        destination = source.output.parent / harness.SUMMARY_FILENAME
        if not harness.summary_matches(record, destination):
            summary = harness.summarize_record(record)
            harness.save_summary(record, summary, state, destination)
        state["harness_export"] = {"status": "completed",
            "file": destination.relative_to(source.task_dir).as_posix(), "error": None}
    except (Exception, KeyboardInterrupt) as exc:
        state["harness_export"] = {"status": "failed", "file": None, "error": str(exc) or "Summary interrupted"}
        _checkpoint(source, state, "awaiting_export", "summarizing", f"The stage summary is incomplete: {exc}. Export can be retried separately.", error=str(exc), log_source="Harness")
        if isinstance(exc, KeyboardInterrupt):
            raise
        return
    _checkpoint(source, state, "completed", "completed", f"Harness stage record: {destination}", log_source="Harness")


def _run_action(source, state, publisher, action, feedback, experience_adapter=None):
    status = get_status(state)
    if state.get("confirmed"):
        if action not in {"continue", "confirm", "export"}:
            raise RuntimeError("The video is confirmed; only the stage summary remains available.")
        if action in {"confirm", "export"} and status["phase"] != "confirmed":
            _export_summary(source, state, experience_adapter)
        return
    if action == "export":
        raise RuntimeError("Confirm the final video before exporting stage records.")
    if action == "confirm":
        if status["phase"] != "awaiting_feedback":
            raise RuntimeError("The workflow or evaluation is incomplete; completion cannot be confirmed yet.")
        state["confirmed"] = True
        _checkpoint(source, state, "awaiting_export", "confirmed", "Final video confirmed.")
        _export_summary(source, state, experience_adapter)
        return
    if action == "evaluate":
        if "e" not in status["actions"] or not state["run_evaluation"]:
            raise RuntimeError("Evaluation is unavailable. Resume the video task first.")
        _evaluate_version(source, state, objective_only=state.get("initial_phase") == "objective")
        return  # 单独重评不触发自动视频修正。
    if action == "revise":
        if status["phase"] != "awaiting_feedback":
            raise RuntimeError("Complete generation and evaluation of the current version first.")
        if not isinstance(feedback, str) or not feedback.strip():
            raise ValueError("Revision instructions must not be empty.")
        number = max(row["version"] for row in state["history"]) + 1
        while api.revision_output_path(source.output, number).exists():
            number += 1
        previous = [row["user_feedback"] for row in state["history"]
                    if row.get("user_feedback") and row.get("generation_status") == "downloaded"]
        prompt = build_user_revision_prompt(state["original_prompt"], "\n".join([*previous, feedback.strip()]), state["current_evaluation"])
        _generate_version(source, state, api.revision_output_path(source.output, number), number,
                          publisher, experience_adapter, prompt=prompt, feedback=feedback.strip())
    elif not state["history"]:
        _log(f"Generating the initial version; one automatic correction is allowed if the seven-metric objective score is below {RENDER_AUTO_CORRECTION_SCORE:g}.")
        _generate_version(source, state, source.output, 0, publisher, experience_adapter)
    else:
        row = state["history"][-1]
        if status["phase"] == "blocked":
            return
        needs_resume = (row.get("generation_status") != "downloaded"
                        or row.get("evaluation_status") in {"pending", "running", "objective_complete"}
                        or state.get("initial_phase") == "objective"
                        and row.get("evaluation_status") not in {"failed", "gpt_unavailable"}
                        or not Path(row["video"]).is_file())
        if not needs_resume:
            return
        _resume_generation(source, state)
    _finish_generation(source, state, publisher, experience_adapter)


def process_render(task_dir: str | Path, *, publisher=None, action="continue", feedback=None) -> dict:
    if action not in {"continue", "revise", "evaluate", "confirm", "export"}:
        raise ValueError("Unknown Render action.")
    source = prepare_render_input(task_dir)
    state = _load_or_prepare(source, action)
    manifest = read_json(source.task_dir / "input" / "input.json")
    experience_adapter = RenderHarnessAdapter(
        source.task_dir, manifest, project_root=Path(__file__).resolve().parents[1])
    owned = publisher is None
    publisher = publisher or LocalVideoPublisher()
    try:
        _run_action(source, state, publisher, action, feedback, experience_adapter)
        _settle(source, state)
        return state
    except (Exception, KeyboardInterrupt) as exc:
        if state.get("confirmed"):
            _settle(source, state)
        else:
            _checkpoint(source, state, "interrupted", state.get("render_step", "preparing"), error=str(exc))
        raise
    finally:
        if owned:
            publisher.close()


def _show_review(state: dict[str, Any]) -> None:
    if not state.get("history"):
        print("No video task has been submitted. c: retry preparation and generate; q: save and exit.")
        return
    row = state["history"][-1]
    evaluation = state.get("current_evaluation") or {}
    print(f"\nRender version {row['version']}: {row.get('version_kind', 'initial')}")
    print(f"Seedance Task ID: {row.get('task_id') or 'not yet available'}")
    print(f"Generation: {row.get('generation_status')}; evaluation: {row.get('evaluation_status')}")
    video = Path(row["video"])
    if video.is_file():
        print(f"Video: {video}")
    if row.get("objective_gate_score") is not None:
        print(f"Initial seven-metric objective score: {row['objective_gate_score']:.2f}")
    scores = evaluation.get("scores") or row.get("scores") or {}
    for name, score in scores.items():
        print(f"  {name}: {float(score):.2f}")
    total = (evaluation.get("aggregates") or {}).get("overall_score", row.get("overall_score"))
    if total is not None:
        print(f"Overall score: {float(total):.2f}; decision: {evaluation.get('decision', 'pending evaluation')}")
    action = evaluation.get("action_review") or {}
    if action:
        print(f"Action review: {action.get('label', action.get('status', 'pending confirmation'))}")
    vlm = evaluation.get("vlm") or {}
    review_summary = (vlm.get("details") or {}).get("summary") or vlm.get("summary")
    if review_summary:
        print(f"GPT review: {review_summary}")
    if row.get("generation_error"):
        print(f"Generation interrupted: {row['generation_error']}")
    if row.get("evaluation_error"):
        print(f"Evaluation error: {row['evaluation_error']}")

    exported = state.get("harness_export") or {}
    if state.get("confirmed"):
        print(f"Final video confirmed; stage summary: {exported.get('status', 'pending')}")
        if exported.get("error"):
            print(f"Stage summary error: {exported['error']}")
    status = get_status(state)
    if status['phase'] == 'blocked':
        print("\nSubmission status is unknown and no task ID is available, so generation cannot continue. Check this task in the provider dashboard, "
              "retrieve the original task ID, and resume. Do not delete records and rerun, as this may incur duplicate charges.")
    labels = {'': 'Enter: accept and confirm', 'r': 'r: enter revision instructions', 'e': 'e: retry evaluation only',
              'x': 'x: retry stage summary export', 'c': 'c: resume the original task', 'p': 'p: open the video', 'q': 'q: save and exit'}
    print('\n' + '; '.join(labels[action] for action in status['actions']) + '.')


def run_render_task(task_dir: str | Path) -> int:
    source = prepare_render_input(task_dir)
    publisher = LocalVideoPublisher()
    print(f"\nTask directory: {source.task_dir}\nBlender white-model video: {source.white_video}\nFirst frame: {source.reference_image}")
    try:
        try:
            state = process_render(source.task_dir, publisher=publisher)
        except Exception as exc:
            if not source.session_file.is_file():
                raise
            state = load_session(source.session_file)
            print(f"Rendering is incomplete: {exc}")
        while get_status(state)["phase"] != "confirmed":
            _show_review(state)
            command = input("Action: ").strip().lower()
            if command in {"q", "quit", "exit"}:
                _settle(source, state)
                print(f"Paused. Resume later using the same task directory: {source.task_dir}")
                return 0
            if command not in get_status(state)["actions"]:
                print("This action is unavailable in the current state.")
                continue
            try:
                if command == "p":
                    video = get_rendered_video(state)
                    if hasattr(os, "startfile"):
                        os.startfile(str(video))
                    else:
                        print(f"Open this file in a local video player: {video}")
                    continue
                feedback = input("Revision instructions: ").strip() if command == "r" else None
                if command == "r" and not feedback:
                    print("Revision instructions must not be empty.")
                    continue
                action = {"c": "continue", "e": "evaluate", "r": "revise", "": "confirm", "x": "export"}[command]
                state = process_render(source.task_dir, publisher=publisher, action=action, feedback=feedback)
            except Exception as exc:
                state = load_session(source.session_file)
                print(f"Action is incomplete: {exc}")
        print(f"Confirmed final video: {get_rendered_video(state)}")
        print(f"Harness stage record: {source.task_dir / state['harness_export']['file']}")
        return 0
    except (KeyboardInterrupt, EOFError):
        if source.session_file.is_file():
            state = load_session(source.session_file)
            _save_pause(source, state)
        print(f"\nPaused. Task records preserved at: {source.task_dir}")
        return 130
    finally:
        publisher.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run or resume the interactive Render stage")
    parser.add_argument("task_dir", type=Path, help="Task directory")
    raise SystemExit(run_render_task(parser.parse_args().task_dir))
