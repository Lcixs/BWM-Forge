from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import threading
from typing import Callable
from types import SimpleNamespace
import uuid

from config import ASSET_ROOT, OUTPUT_ROOT, INPUT_MAX_IMAGE_BYTES
from input_pipeline.common import now, read_json, save_json
from input_pipeline.input_stage import InputRequest, load_input_request, run_input_stage
from gui.task_store import load_task
from Blender.blender_stage import run_blender_task
from Blender.blender_modeling import Feedback
from Blender.common import capture_progress, log_progress, task_lock
from Blender.history import copy_task_results
from Render.render_stage import capture_render_log, process_render, get_status
from Harness.harness_stage import run_harness_task


class Paused(Exception):
    pass


def completed(task_dir: Path, stage: str) -> bool:
    if stage in {"input", "blender", "render"}:
        task = load_task(task_dir)
        return task is not None and task.stage_states[stage] == "completed"
    task = read_json(task_dir / "task.json")
    return (task.get(f"{stage}_status") == "completed"
            and (task_dir / stage / "result.json").is_file())


def render_view(task_dir: Path) -> dict:
    session = task_dir / "render/rendered_review_session.json"
    if not session.exists():
        return {"phase": "not_started", "actions": ["c"], "evaluation": {}}
    state = read_json(session)
    state = {**state.get("context", {}), **state}
    state["base_args"] = SimpleNamespace(**state.get("base_args", {}))
    row = (state.get("history") or [{}])[-1]
    if not row.get("task_id") and row.get("submission_response"):
        from Render.seedance import get_task_id
        try:
            row["task_id"] = get_task_id(row["submission_response"])
        except RuntimeError:
            pass
    result = get_status(state)
    result["evaluation"] = state.get("current_evaluation") or {}
    result["error"] = state.get("render_error")
    return result


@dataclass
class Job:
    task_dir: Path
    automatic: bool = False
    stage: str = ""
    state: str = "running"
    message: str = "Starting…"
    pause_requested: bool = False
    review: dict = field(default_factory=dict)
    decision: Feedback | None = None
    thread: threading.Thread | None = None


class PipelineManager:
    def __init__(self, output_root=OUTPUT_ROOT, asset_root=ASSET_ROOT, *,
                 input_runner=run_input_stage, blender_runner=run_blender_task,
                 render_runner=process_render, harness_runner=run_harness_task):
        self.output_root = Path(output_root).resolve()
        self.asset_root = Path(asset_root).resolve()
        self.input_runner, self.blender_runner = input_runner, blender_runner
        self.render_runner, self.harness_runner = render_runner, harness_runner
        self._condition = threading.Condition(threading.RLock())
        self._job: Job | None = None

    def task_path(self, value) -> Path:
        if not value:
            raise ValueError("Select a task first.")
        path = Path(value).resolve(strict=True)
        if path.parent != self.output_root or not (path / "task.json").is_file():
            raise ValueError("Select a valid task directory under output.")
        return path

    def _ensure_task_mutable(self, path: Path):
        if self._job is not None:
            raise ValueError("A task is running or awaiting review. It cannot be deleted yet.")
        task = read_json(path / "task.json")
        for stage in ("input", "render", "harness"):
            state = str(task.get(f"{stage}_status") or "")
            if state in {"running", "submitting", "evaluating"}:
                raise ValueError(f"{stage.title()} is still marked as running and cannot be deleted.")
        blender_root = path / "blender"
        if blender_root.is_dir():
            with task_lock(blender_root):
                pass

    def rename_task(self, value, display_name: str) -> Path:
        name = " ".join(str(display_name or "").split())
        if not name:
            raise ValueError("Task name cannot be empty.")
        if len(name) > 80:
            raise ValueError("Task name cannot exceed 80 characters.")
        path = self.task_path(value)
        with self._condition:
            guard = self._reserve()
            try:
                task = read_json(path / "task.json")
                task["display_name"] = name
                task["display_name_updated_at"] = now()
                save_json(path / "task.json", task)
            finally:
                guard.__exit__(None, None, None)
        return path

    def delete_task(self, value) -> Path:
        path = self.task_path(value)
        with self._condition:
            guard = task_lock(self.output_root)
            guard.__enter__()
            try:
                self._ensure_task_mutable(path)
                trash = (self.output_root / ".trash").resolve()
                trash.mkdir(parents=True, exist_ok=True)
                suffix = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
                target = (trash / f"{path.name}_{suffix}").resolve()
                if target.parent != trash:
                    raise ValueError("Invalid task recycle path.")
                if target.exists():
                    target = (trash / f"{path.name}_{suffix}_{uuid.uuid4().hex[:6]}").resolve()
                path.replace(target)
            finally:
                guard.__exit__(None, None, None)
        return target

    def _set(self, job: Job, **changes):
        with self._condition:
            for name, value in changes.items():
                setattr(job, name, value)

    def view(self, value=None) -> dict:
        with self._condition:
            job = self._job
            busy = job is not None
            if job and value and job.task_dir == Path(value).resolve():
                return {"busy": True, "active_here": True, "stage": job.stage,
                        "state": job.state, "message": job.message,
                        "pause_requested": job.pause_requested, "review": dict(job.review)}
            info = {"busy": busy, "active_here": False, "review": {}, "state": "idle"}
            if busy:
                info["message"] = f"Task {job.task_dir.name} is currently running. Wait for it to finish or switch to that task."
            if value and not busy:
                task = load_task(self.task_path(value))
                info["stage"] = task.current_stage
                stage_state = task.stage_states.get(task.current_stage)
                if stage_state == "running":
                    info.update(state="interrupted", message="The previous run was interrupted. You can resume this task.")
                elif stage_state == "attention":
                    info.update(state="failed", message=task.error or "This stage needs attention. Review its files and continue.")
            return info

    def _reserve(self):
        if self._job is not None:
            raise ValueError("A task is already running or awaiting review. Finish or pause it first.")
        guard = task_lock(self.output_root)
        guard.__enter__()
        return guard

    def _launch(self, job: Job, action: Callable, guard):
        self._job = job
        try:
            job.thread = threading.Thread(target=self._work, args=(job, action, guard), daemon=True,
                                          name=f"codewm-{job.task_dir.name}")
            job.thread.start()
        except BaseException:
            self._job = None
            guard.__exit__(None, None, None)
            raise

    def _work(self, job: Job, action: Callable, guard):
        try:
            with capture_progress(job.task_dir / "blender/progress.jsonl"):
                with capture_render_log(job.task_dir / "render/progress.jsonl"):
                    action(job)
        except Paused:
            pass
        except (Exception, KeyboardInterrupt) as exc:
            if job.stage in {"input", "blender", "render", "harness"}:
                task = read_json(job.task_dir / "task.json")
                interrupted = isinstance(exc, KeyboardInterrupt)
                status = "interrupted" if interrupted else "failed"
                task.update(status=f"{job.stage}_{status}", **{
                    f"{job.stage}_status": status,
                    f"{job.stage}_updated_at": now(),
                })
                error_key = f"{job.stage}_error" if job.stage in {"render", "harness"} else "error"
                task[error_key] = task.get(error_key) or str(exc) or status
                save_json(job.task_dir / "task.json", task)
        finally:
            with self._condition:
                try:
                    guard.__exit__(None, None, None)
                finally:
                    self._job = None
                    self._condition.notify_all()

    def create(self, request: InputRequest, automatic=True) -> Path:
        request.validate()
        with self._condition:
            guard = self._reserve()
            try:
                task_dir = self.output_root / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
                task_dir.mkdir()
                save_json(task_dir / "task.json", {
                    "task_id": task_dir.name, "start_stage": "input", "created_at": now(),
                    "status": "input_running", "input_status": "running", "input_type": request.input_type,
                    "asset_name": request.asset_name, "asset_source": "library",
                    "embodiment": request.embodiment,
                })
            except BaseException:
                guard.__exit__(None, None, None)
                raise
            job = Job(task_dir, automatic=automatic, stage="input", message="Saving inputs and creating the task…")
            self._launch(job, lambda j: self._input(j, request), guard)
            return task_dir

    def load_history(self, value) -> Path:
        source = self.task_path(value)
        with self._condition:
            guard = self._reserve()
            try:
                original = read_json(source / "task.json")
                task_dir = self.output_root / (
                    datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
                task_dir.mkdir()
                save_json(task_dir / "task.json", {
                    "task_id": task_dir.name, "start_stage": "blender", "created_at": now(),
                    "display_name": task_dir.name,
                    "source_task": source.name, "status": "blender_running", "blender_status": "running",
                    **{key: original[key] for key in ("input_type", "asset_name", "embodiment") if key in original},
                })
            except BaseException:
                guard.__exit__(None, None, None)
                raise
            job = Job(task_dir, stage="blender", message="Loading historical Blender results…")
            self._launch(job, lambda j: self._load_history(j, source), guard)
            return task_dir

    def _load_history(self, job, source):
        try:
            log_progress(f"Loading historical task {source.name}")
            with task_lock(source / "blender"):
                copy_task_results(source, job.task_dir)
        except Exception as exc:
            task = read_json(job.task_dir / "task.json")
            task.update(status="failed", blender_status="failed", error=str(exc))
            save_json(job.task_dir / "task.json", task)
            raise
        task = read_json(job.task_dir / "task.json")
        loaded_at = now()
        task.update(input_status="completed", input_finished_at=loaded_at,
                    history_loaded_at=loaded_at)
        save_json(job.task_dir / "task.json", task)
        self._blender(job)

    def _checkpoint(self, job):
        with self._condition:
            if job.pause_requested:
                raise Paused()

    def _input(self, job, request=None):
        self._set(job, stage="input", message="Running Input…")
        try:
            if request is None:
                request = load_input_request(job.task_dir)

            def progress(step):
                self._checkpoint(job)
                task = read_json(job.task_dir / "task.json")
                task.update(status="input_running", input_status="running", input_step=step, input_updated_at=now())
                task.pop("error", None)
                task.pop("stopped_at", None)
                save_json(job.task_dir / "task.json", task)
                self._set(job, message=f"Input · {step}")

            handoff = self.input_runner(request, job.task_dir, asset_root=self.asset_root, on_progress=progress)
            task = read_json(job.task_dir / "task.json")
            task.update(status="input_completed", input_status="completed", input_finished_at=now(),
                        handoff_file=Path(handoff).relative_to(job.task_dir).as_posix())
            task.pop("error", None)
            save_json(job.task_dir / "task.json", task)
        except (Exception, KeyboardInterrupt) as exc:
            task = read_json(job.task_dir / "task.json")
            paused = isinstance(exc, (Paused, KeyboardInterrupt))
            task.update(status="input_interrupted" if paused else "failed",
                        input_status="interrupted" if paused else "failed", stopped_at=now(), error=str(exc))
            save_json(job.task_dir / "task.json", task)
            raise
        self._set(job, message="Input completed.")
        self._checkpoint(job)
        if job.automatic:
            self._blender(job)

    def start(self, value, stage="all", *, action="continue", feedback="", automatic=False):
        path = self.task_path(value)
        if stage not in {"all", "blender", "render"}:
            raise ValueError("Unknown stage.")
        with self._condition:
            guard = self._reserve()
            job = Job(path, automatic=automatic or stage == "all", stage=stage)
            self._launch(job, lambda j: self._resume(j, stage, action, feedback), guard)

    def _resume(self, job, stage, action, feedback):
        if stage == "all":
            if not (job.task_dir / "input/input.json").is_file():
                self._input(job)
            elif not completed(job.task_dir, "blender"):
                self._blender(job)
            elif not completed(job.task_dir, "render"):
                self._set(job, state="finished",
                          message="Blender results are ready. Open Render and click Continue generation to start it manually.")
            elif not completed(job.task_dir, "harness"):
                self._harness(job)
            else:
                self._set(job, message="All task stages are complete.")
        elif stage == "blender":
            self._blender(job)
        else:
            self._render(job, action, feedback)

    def _blender(self, job):
        if not (job.task_dir / "input/input.json").is_file():
            raise ValueError("Complete Input first.")
        self._checkpoint(job)
        self._set(job, stage="blender", message="Running Blender. A review preview will appear after the current round finishes.")
        code = self.blender_runner(job.task_dir, feedback_provider=lambda context: self._review(job, context))
        if code == 130:
            raise Paused()
        if code != 0:
            raise RuntimeError(f"Blender returned error code {code}")
        if not completed(job.task_dir, "blender"):
            self._set(job, state="attention", message="Blender is not complete. Check the error or retry the stage summary.")
            return
        self._checkpoint(job)
        message = "Blender completed. Open Render and click Continue generation to start it manually."
        self._set(job, state="finished", message=message)
        log_progress(message)

    def _review(self, job, context):
        with self._condition:
            if job.pause_requested:
                return Feedback("quit")
            token = uuid.uuid4().hex
            self._set(job, state="awaiting_feedback", review={
                "token": token, "stage": context["stage"], "round": context["round"],
                "preview": context["preview"],
                "video": context.get("video"),
            }, message=f"Awaiting review: {context['stage']} · Round {context['round']}")
            log_progress(f"Awaiting review: {context['stage']} round {context['round']}")
            self._condition.wait_for(lambda: job.decision is not None or job.pause_requested)
            decision = Feedback("quit") if job.pause_requested else job.decision
            job.decision = None
            self._set(job, state="running", review={}, message="Review feedback received. Blender is continuing…")
            log_progress("Review feedback received; Blender is continuing")
            return decision

    def feedback(self, value, token, action, text="", images=None):
        path = self.task_path(value)
        if action not in {"accept", "revise"}:
            raise ValueError("Unknown feedback action.")
        text, images = (text or "").strip(), images or []
        if action == "revise" and not text and not images:
            raise ValueError("Enter revision instructions or upload reference images.")
        with self._condition:
            job = self._job
            if (not job or job.task_dir != path or job.state != "awaiting_feedback"
                    or job.review.get("token") != token or job.decision is not None or job.pause_requested):
                raise ValueError("The review state changed. Wait for the page to refresh and try again.")
            references = []
            if action == "revise":
                for value in images:
                    source = Path(value).resolve(strict=True)
                    if source.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                        raise ValueError("Unsupported reference image format.")
                    if not source.is_file() or not 0 < source.stat().st_size <= INPUT_MAX_IMAGE_BYTES:
                        raise ValueError("Reference image is empty or exceeds the size limit.")
                    references.append(str(source))
            job.decision = Feedback(action, text if action == "revise" else "", references)
            self._set(job, state="running", review={}, message="Review feedback submitted.")
            self._condition.notify_all()

    def pause(self, value):
        path = self.task_path(value)
        with self._condition:
            job = self._job
            if not job or job.task_dir != path:
                raise ValueError("This task has no active background operation.")
            self._set(job, pause_requested=True, state="pausing",
                      message="Pause requested. Processing will stop at the next review checkpoint or when the current stage finishes.")
            self._condition.notify_all()

    def _render(self, job, action, feedback):
        self._checkpoint(job)
        self._set(job, stage="render", message="Checking the Render handoff and current status…")
        if not completed(job.task_dir, "blender"):
            raise ValueError("Render can start only after Blender and its stage summary are complete.")
        codes = {"continue": "c", "evaluate": "e", "revise": "r", "confirm": "", "export": "x"}
        view = render_view(job.task_dir)
        if action == "continue" and view["phase"] in {"awaiting_feedback", "confirmed"}:
            if view["phase"] == "confirmed":
                self._harness(job)
            else:
                self._set(job, state="attention", message="Review the current video, then confirm it or submit a revision.")
            return
        if action == "continue" and view["phase"] == "export_pending":
            action = "export"
        if action not in codes or codes[action] not in view["actions"]:
            if view["phase"] == "confirmed" and action == "continue":
                return
            raise ValueError(f"Render cannot perform {action} while its current phase is {view['phase']}.")
        if action == "revise" and not (feedback or "").strip():
            raise ValueError("Enter video revision instructions.")
        self._set(job, stage="render", message=f"Render · {action} in progress…")
        state = self.render_runner(job.task_dir, action=action, feedback=feedback or None)
        status = get_status(state)
        messages = {"confirmed": "The final video and stage summary are complete.",
                    "awaiting_feedback": "The video is ready. Review the evaluation, then confirm it or submit a revision.",
                    "export_pending": "The video is confirmed, but the stage summary is incomplete. Retry the summary.",
                    "blocked": "The video submission result is unknown and no task ID is available. Check the provider records; the request will not be resubmitted automatically."}
        message = messages.get(status["phase"], f"Render stopped at {status['phase']}. Review the status and continue.")
        if status["phase"] == "confirmed":
            self._harness(job)
        else:
            self._set(job, state="attention", message=message)

    def _harness(self, job):
        if not completed(job.task_dir, "render"):
            raise ValueError("Harness can start only after Render and its stage summary are complete.")
        if completed(job.task_dir, "harness"):
            self._set(job, state="finished", message="All task stages are complete.")
            return
        self._checkpoint(job)
        self._set(job, stage="harness", state="running", message="Running Harness experience archiving and learning…")
        code = self.harness_runner(job.task_dir)
        if code == 130:
            raise Paused()
        if code != 0 or not completed(job.task_dir, "harness"):
            raise RuntimeError(f"Harness returned error code {code}")
        self._set(job, state="finished", message="Render and Harness completed. The task is finished.")

    def wait(self, timeout=5):
        with self._condition:
            return self._condition.wait_for(lambda: self._job is None, timeout=timeout)


manager = PipelineManager()
