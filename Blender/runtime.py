from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import re
import shutil
from pathlib import Path
import subprocess
import uuid

from .blender_modeling import load_mode_prompt, prepare_workflow, record_failure
from .history import (load_run, save_run, recovery_context, artifact, verify_artifact,
                      local_path, round_context, append_event)
from .common import (
    discover_blender_bin, materialize_trajectory,
    validate_blend, hash_path, write_json,
    log_progress, run_logged_command,
)

from utils.openai_config import load_openai_config, read_openai_api_key
from config import (BLENDER_MODEL, BLENDER_OPENAI_BASE_URL, BLENDER_OPENAI_AUTH_FILE,
                    BLENDER_REASONING_EFFORT, PROMPT_ROOT, BLENDER_RENDER_SAMPLES)
from .inputs import load_blender_input
from .harness_adapter import BlenderHarnessAdapter

RENDER_SCRIPT = Path(__file__).resolve().parent / "scripts/render_blend.py"


def _extract_video_first_frame(video: Path, output: Path) -> tuple[Path, Path]:
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise RuntimeError("Video inputs for Blender require imageio-ffmpeg. Install the dependencies in requirements.txt.") from exc
    ffmpeg = Path(imageio_ffmpeg.get_ffmpeg_exe())
    if not ffmpeg.is_file():
        raise FileNotFoundError(f"FFmpeg executable not found: {ffmpeg}")
    pending = output.with_name(f".{output.stem}.{uuid.uuid4().hex}.png")
    try:
        completed = subprocess.run(
            [str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error",
             "-i", str(video), "-frames:v", "1", "-an", str(pending)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if completed.returncode or not pending.is_file() or pending.stat().st_size == 0:
            raise RuntimeError(f"Unable to extract the first frame from the human demonstration video: {video}; {completed.stderr.strip()}")
        pending.replace(output)
    finally:
        pending.unlink(missing_ok=True)
    return output, ffmpeg


def _codex_config():
    from openai_codex import CodexConfig

    api = load_openai_config(model=BLENDER_MODEL, base_url=BLENDER_OPENAI_BASE_URL,
                             auth_file=BLENDER_OPENAI_AUTH_FILE,
                             reasoning_effort=BLENDER_REASONING_EFFORT, allow_http=True)
    api_key = read_openai_api_key(api.auth_file)
    provider_id = "cwm_assets_api"
    key_env = "CWM_CODEX_SDK_API_KEY"
    overrides = (
        f"model_provider={json.dumps(provider_id)}",
        f"model_reasoning_effort={json.dumps(api.reasoning_effort)}",
        f"model_providers.{provider_id}.name={json.dumps('CWM assets/api')}",
        f"model_providers.{provider_id}.base_url={json.dumps(api.base_url)}",
        f"model_providers.{provider_id}.env_key={json.dumps(key_env)}",
        f"model_providers.{provider_id}.wire_api=\"responses\"",
        f"model_providers.{provider_id}.requires_openai_auth=false",
    )
    return CodexConfig(config_overrides=overrides, env={key_env: api_key}), api.model


@contextmanager
def prepare(task_dir: str | Path, *, input_type: int | None = None):
    task_dir = Path(task_dir).expanduser().resolve(strict=True)
    input_json = task_dir / "input" / "input.json"
    source = load_blender_input(input_json, input_type=input_type)
    input_type, manifest = source.input_type, source.manifest
    first_frame, trajectory = source.first_frame, source.trajectory
    urdf, instruction = source.urdf, source.instruction
    human_video = source.human_video
    log_progress(f"Validating input: type {input_type}; handoff file: {input_json}")

    blender_dir = task_dir / "blender"
    final_blend = blender_dir / "final.blend"
    saved = load_run(task_dir)
    fingerprints = {"input_type": input_type, "input": hash_path(input_json), "urdf": hash_path(urdf)}
    if input_type in (0, 1):
        fingerprints["frame"] = hash_path(first_frame)
    if input_type == 1:
        fingerprints["trajectory"] = hash_path(trajectory)
    if input_type == 2:
        fingerprints["video"] = hash_path(human_video)
    previous_fingerprints = saved.get("input_fingerprints")
    if previous_fingerprints and previous_fingerprints != fingerprints:
        raise ValueError("Task inputs have changed. Inputs cannot be replaced when resuming; create a new task")
    if final_blend.exists() and not saved.get("rounds"):
        raise FileExistsError(f"A final project exists without workflow state. Use --render-only: {final_blend}")
    log_progress("Input validation passed; creating a new session and loading completed rounds")
    blender_exe = Path(discover_blender_bin())
    if not blender_exe.is_file():
        raise FileNotFoundError(f"Blender executable not found: {blender_exe}")
    blender_dir.mkdir(parents=True, exist_ok=True)
    (blender_dir / "derived").mkdir(exist_ok=True)
    instructions = load_mode_prompt(PROMPT_ROOT / "blender/developer_instructions.md", input_type)
    experience_adapter = BlenderHarnessAdapter(
        task_dir, manifest, project_root=Path(__file__).resolve().parents[1])

    if input_type == 1:
        # Blender 内置 Python 通常没有 pyarrow，Parquet 在主进程中转换。
        log_progress(f"Type 1: preparing trajectory {trajectory}")
        motion = materialize_trajectory(trajectory, blender_dir, motion=manifest["motion"])
        log_progress(f"Trajectory prepared: {blender_dir / motion['report_json']}")
        mode_context = f"""Scene supplement: {manifest.get('scene_supplement_text') or 'None'}
First frame: {first_frame}
Original trajectory: {trajectory}
Trajectory report: {blender_dir / motion['report_json']}
Blender-readable NPZ: {motion['array_file']}. Read only the action array as the trajectory.
Trajectory conventions: {json.dumps(manifest['motion'], ensure_ascii=False)}
Sample count: {motion['shape'][0]}; time from first to last sample: {motion['duration_seconds']} seconds.
Use row i at i/fps seconds. fps is the input sampling rate, separate from the output video frame rate. Interpolation must preserve the original duration.
If the declared motion includes a gripper channel and the target has that mechanism, preserve raw gripper values; they need not lie in [0,1] and must not be clipped or renormalized. Use 0 closed and 1 open only as convention references.
For applicable gripper motion, determine opening direction and mapping from URDF drives, mimic relationships, and geometry. Do not treat opening values directly as joint angles; document unknown calibration.
Motion evidence: prioritize the first frame and task constraints; use the trajectory as a motion reference. Read only the NPZ above; Blender Python does not need pyarrow."""
    elif input_type == 0:
        mode_context = f"""Scene supplement: {manifest.get('scene_supplement_text') or 'None'}
First frame: {first_frame}
Motion evidence: no input trajectory is provided. Design plausible robot and task-object animation from the task description, first frame, and scene constraints; explicitly label inferences."""
    else:
        log_progress(f"Type 2: extracting a reference frame from the demonstration video {human_video}")
        first_frame, ffmpeg_exe = _extract_video_first_frame(
            human_video, blender_dir / "derived/reference_first_frame.png"
        )
        log_progress(f"Reference frame saved: {first_frame}")
        mode_context = f"""Demonstration video: {human_video}
First-frame reference image: {first_frame} (extracted from the demonstration video and attached as an image)
Video frame extraction tool: {ffmpeg_exe}
Motion evidence: no robot trajectory is provided. Inspect the objects, scene, action order, and key moments in the demonstration video, then map observed behavior to plausible animation of the selected target. The first frame is only a reference; it does not establish that the full video has been analyzed. Use local tools to extract frames at relevant times, and record observations and inferences separately."""

    prompt = f"""This is the three-stage Codex SDK workflow for input type {input_type}. Use the input materials, task description, and robot assets to
create an animated white-model project in the local Blender installation. Write reports and user-facing explanations in English.

Task description: {instruction}
Robot URDF: {urdf}
Asset root directory: {source.asset_root}
Mesh directory: {source.mesh_root}
{mode_context}
Blender executable: {blender_exe}
Working directory: {blender_dir}

Write task-specific bpy scripts in the scripts/ directory assigned to this round. Execute them with the Blender executable above
and save to the output path specified by the current stage instructions. The final composed project must contain camera_input, the complete URDF model and scene,
the reference composition at frame 1, action keyframes, and valid frame_start/frame_end values, at a minimum of 30 FPS.
This call must generate only the assigned stage project. Do not render the video yourself;
the backend will render each composed round's full white-model video before user feedback using render_blend.py,
then publish the accepted project and video. If essential information is missing, state
the scope of your inferences and the results of checks actually performed.
"""

    try:
        from openai_codex import Codex, LocalImageInput, Sandbox, TextInput
    except ImportError as exc:
        raise RuntimeError("Codex Python SDK is missing. Run python -m pip install openai-codex") from exc

    codex_config, selected_model = _codex_config()
    log_progress(f"Codex API configuration: config.py; model: {selected_model}")
    log_progress(f"Modeling target: {final_blend}")
    with Codex(codex_config) as codex:
        thread_options = dict(
            cwd=str(blender_dir), model=selected_model, sandbox=Sandbox.workspace_write,
            developer_instructions=instructions,
        )
        thread = codex.thread_start(**thread_options)
        saved.update(input_fingerprints=fingerprints, model=selected_model, input_type=input_type)
        save_run(task_dir, saved)
        log_progress(f"New Codex session: {thread.id}; resuming from completed round records")
        import hashlib
        digest = hashlib.sha256(instructions.encode("utf-8")).hexdigest()[:16]
        instruction_file = blender_dir / "derived" / f"instructions_{digest}.md"
        instruction_file.write_text(instructions, encoding="utf-8")
        prompt += f"\nRelative paths in the history below are resolved against the task directory {task_dir}, not the current working directory."
        prompt += "\nCompleted rounds and feedback (read-only reference):\n" + json.dumps(recovery_context(saved), ensure_ascii=False)
        prompt += f"\nCommon instruction record for this session: {instruction_file}"
        session = prepare_workflow(
            thread=thread, task_dir=task_dir, common_prompt=prompt,
            prompt_root=PROMPT_ROOT / "blender", blender_exe=blender_exe,
            render_script=RENDER_SCRIPT, first_frame=first_frame,
            input_type=input_type,
            text_input=TextInput, image_input=LocalImageInput,
            validate_blend=validate_blend,
            experience_adapter=experience_adapter,
        )
        try:
            yield session
        except BaseException as exc:
            record_failure(session, exc)
            raise


def build_video_preview(video: Path, destination: Path) -> dict:
    import cv2
    from PIL import Image, ImageDraw
    import math

    cap = cv2.VideoCapture(str(video))
    try:
        fps = cap.get(cv2.CAP_PROP_FPS)
        expected = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if not cap.isOpened() or not math.isfinite(fps) or fps <= 0 or expected <= 0:
            raise ValueError("The white-model video cannot be decoded or contains no valid frames")
        indices = sorted({round((expected - 1) * i / 5) for i in range(6)})
        frames, count, size = [], 0, None
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            size = (frame.shape[1], frame.shape[0])
            if count in indices:
                image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                image.thumbnail((320, 240))
                frames.append((count / fps, image))
            count += 1
        if count != expected or len(frames) != len(indices):
            raise ValueError(f"The white-model video is incomplete: {expected} declared frames, {count} decoded frames")
    finally:
        cap.release()
    sheet = Image.new("RGB", (960, 2 * 270), "white")
    draw = ImageDraw.Draw(sheet)
    for index, (time, image) in enumerate(frames):
        x, y = index % 3 * 320, index // 3 * 270
        sheet.paste(image, (x, y))
        draw.text((x+8, y+245), f"{time:.3f} s", fill="black")
    destination.parent.mkdir(parents=True, exist_ok=True)
    pending = destination.with_suffix(".pending.jpg")
    sheet.save(pending, quality=85)
    pending.replace(destination)
    return {"frames": count, "fps": fps, "duration_seconds": count/fps,
            "width": size[0], "height": size[1],
            "preview_timestamps_seconds": [t for t, _ in frames]}


def _remove_render_frames(video: Path):
    folder = video.with_name("." + video.stem + ".pending_frames")
    if not folder.exists():
        return
    if not folder.resolve().is_relative_to(video.parent.resolve()) or folder.is_symlink():
        raise ValueError("The temporary render directory is outside the allowed location")
    if any(not p.is_file() or not re.fullmatch(r"frame_\d+\.png", p.name) for p in folder.iterdir()):
        raise ValueError("The temporary render directory contains unknown files; automatic deletion was skipped")
    shutil.rmtree(folder)


def _render_white_video(blend, video, preview, log, *, resolution=None, frame_end=None):
    if video.exists():
        metadata = build_video_preview(video, preview)
        _remove_render_frames(video)
        return metadata
    _remove_render_frames(video)
    blender = Path(discover_blender_bin())
    if not blender.is_file():
        raise FileNotFoundError(f"Blender executable not found: {blender}")
    import imageio_ffmpeg
    pending = video.with_name("." + video.stem + ".pending.mp4")
    command = [str(blender), "--background", str(blend), "--python-exit-code", "1", "--python", str(RENDER_SCRIPT),
               "--", "--output", str(pending), "--mode", "animation", "--camera-role", "input",
               "--fast-white-model", "--samples", str(BLENDER_RENDER_SAMPLES),
               "--ffmpeg", imageio_ffmpeg.get_ffmpeg_exe()]
    if resolution:
        command.extend(["--resolution", *map(str, resolution)])
    if frame_end is not None:
        command.extend(["--frame-end", str(frame_end)])
    try:
        code, success = run_logged_command(command, log, "White-model video render")
        if code or not success or not pending.is_file():
            raise RuntimeError(f"White-model rendering failed: {log}")
        metadata = build_video_preview(pending, preview)
        pending.replace(video)
    finally:
        pending.unlink(missing_ok=True)
    _remove_render_frames(video)
    return metadata


def prepare_composed_review(session):
    task_dir, row = session.task_dir, session.active
    if row["stage"] != "composed" or not row.get("completed_at"):
        raise ValueError("A completed composed round is required for video feedback")
    context = round_context(task_dir, row)
    blend = Path(context["blend"])
    blend_hash = row["artifacts"]["blend"]["sha256"]
    rendering = row.get("video_render", {})
    if row["artifacts"].get("video"):
        if rendering.get("blend_sha256") != blend_hash:
            raise ValueError("The composed video does not match this round's project")
        verify_artifact(task_dir, row["artifacts"]["video"])
        verify_artifact(task_dir, row["artifacts"]["video_preview"])
        return context
    folder = local_path(task_dir, row["folder"])
    video, preview = folder / "white_model.mp4", folder / "video_preview.jpg"
    log = folder / "evidence" / "video_render.log"
    if video.exists() and rendering.get("blend_sha256") != blend_hash:
        raise FileExistsError(f"The existing composed video has no matching render record: {video}")
    row["video_render"] = {"status": "running", "blend_sha256": blend_hash,
                           "started_at": rendering.get("started_at") or datetime.now(timezone.utc).isoformat(),
                           "log": log.relative_to(task_dir).as_posix()}
    save_run(task_dir, session.state)
    append_event(task_dir, row, "video.started", {"blend_sha256": blend_hash})
    log_progress(f"Composed round {row['round']}: rendering the full white-model video before user feedback")
    try:
        metadata = _render_white_video(blend, video, preview, log)
        verify_artifact(task_dir, row["artifacts"]["blend"])
    except BaseException as exc:
        row["video_render"].update(status="failed", error=str(exc) or "Video render interrupted")
        save_run(task_dir, session.state)
        raise
    row["artifacts"].update(video=artifact(task_dir, video), video_preview=artifact(task_dir, preview))
    row["video_metadata"] = metadata
    row["video_render"].update(status="completed", finished_at=datetime.now(timezone.utc).isoformat())
    append_event(task_dir, row, "video.completed", {"video": row["artifacts"]["video"]})
    save_run(task_dir, session.state)
    return round_context(task_dir, row)


def _record_rendered_video(task_dir, state, blend, video, preview, metadata):
    state["final"].update(blend=artifact(task_dir, blend), video=artifact(task_dir, video),
                          preview=artifact(task_dir, preview), video_metadata=metadata,
                          rendered_blend_sha256=hash_path(blend))
    if state["renders"]:
        state["renders"][-1].update(status="completed", finished_at=datetime.now(timezone.utc).isoformat())
    save_run(task_dir, state)
    from input_pipeline.common import read_json, save_json
    task_file = task_dir / "task.json"
    task = read_json(task_file) if task_file.exists() else {"task_id": task_dir.name}
    task.update(status="blender_awaiting_summary", blender_status="awaiting_summary",
                blender_step="summarizing", blender_video_file=video.relative_to(task_dir).as_posix(),
                blender_blend_file="blender/final.blend", blender_preview_file="blender/preview.jpg")
    save_json(task_file, task)


def _publish_composed_video(task_dir, state, blend, video):
    accepted = state["accepted"].get("composed")
    rows = [row for row in state["rounds"] if row["stage"] == "composed"
            and row.get("artifacts", {}).get("blend") == accepted and row.get("completed_at")]
    row = rows[-1] if rows else None
    if not row or not row["artifacts"].get("video"):
        return False
    rendering = row.get("video_render", {})
    if rendering.get("blend_sha256") != hash_path(blend):
        raise ValueError("The accepted composed video does not match final.blend")
    source = verify_artifact(task_dir, row["artifacts"]["video"])
    source_preview = verify_artifact(task_dir, row["artifacts"]["video_preview"])
    preview = task_dir / "blender/preview.jpg"
    for original, destination in ((source, video), (source_preview, preview)):
        if destination.exists():
            if hash_path(destination) != hash_path(original):
                raise FileExistsError(f"Existing output differs from the accepted composed video: {destination}")
            continue
        pending = destination.with_name("." + destination.name + ".pending")
        try:
            shutil.copyfile(original, pending)
            pending.replace(destination)
        finally:
            pending.unlink(missing_ok=True)
    state["renders"].append({"id": f"render/{len(state['renders']):03d}", "official": True,
                             "source_round": row["id"], **rendering})
    _record_rendered_video(task_dir, state, blend, video, preview, row["video_metadata"])
    log_progress("Published the accepted composed video; no additional render is needed")
    return True


def render_existing(task_dir: str | Path, *, output_video: str | Path | None = None,
                    resolution: tuple[int, int] | None = None,
                    frame_end: int | None = None) -> Path:
    task_dir = Path(task_dir).expanduser().resolve(strict=True)
    root = task_dir / "blender"
    final_blend = root / "final.blend"
    validate_blend(final_blend)
    video = Path(output_video).expanduser().resolve() if output_video else root / "white_model.mp4"
    official = video == root / "white_model.mp4"
    if frame_end is not None and (frame_end < 1 or official):
        raise ValueError("Partial-frame tests require a separate --video-output and a frame-end of at least 1")
    if resolution is not None and (len(resolution) != 2 or any(v < 2 or v % 2 for v in resolution)):
        raise ValueError("Render dimensions must be positive even integers")
    state = load_run(task_dir)
    blend_hash = hash_path(final_blend)
    if official and not state["final"].get("video") and resolution is None:
        if _publish_composed_video(task_dir, state, final_blend, video):
            return video
    if official and state["final"].get("video"):
        verify_artifact(task_dir, state["final"]["video"])
        if state["final"].get("rendered_blend_sha256") != blend_hash:
            raise ValueError("final.blend has changed since the final render; the previous video cannot be reused")
        if state["final"].get("preview"):
            verify_artifact(task_dir, state["final"]["preview"])
            _remove_render_frames(video)
            return video
    elif video.exists():
        last = state["renders"][-1] if state["renders"] else {}
        if not official or last.get("blend_sha256") != blend_hash or last.get("status") != "running":
            raise FileExistsError(f"Video already exists and its source cannot be verified; it will not be overwritten: {video}")
    video.parent.mkdir(parents=True, exist_ok=True)
    preview = root / "preview.jpg" if official else video.with_suffix(".preview.jpg")
    log = root / "logs" / f"render_{len(state['renders']):03d}.log" if official else video.with_suffix(".log")
    if not video.exists():
        row = {"id": f"render/{len(state['renders']):03d}", "status": "running",
               "started_at": datetime.now(timezone.utc).isoformat(), "blend_sha256": blend_hash,
               "official": official, "log": str(log.relative_to(task_dir)) if log.is_relative_to(task_dir) else str(log)}
        if official:
            state["renders"].append(row)
            save_run(task_dir, state)
        try:
            metadata = _render_white_video(final_blend, video, preview, log,
                                           resolution=resolution, frame_end=frame_end)
        except BaseException as exc:
            if official:
                row.update(status="failed", error=str(exc) or "Interrupted by the user")
                save_run(task_dir, state)
            raise
    else:
        metadata = build_video_preview(video, preview)
    if official:
        _record_rendered_video(task_dir, state, final_blend, video, preview, metadata)
    _remove_render_frames(video)
    return video
