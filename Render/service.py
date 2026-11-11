from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import math
from pathlib import Path
import uuid

import requests

from input_pipeline.common import read_json
from . import seedance as api
from .prompts import DEFAULT_INITIAL_PROMPT


REFERENCE_MIN_SIDE = 300
REFERENCE_MAX_SIDE = 6000
REFERENCE_UPSCALE_SHORT_SIDE = 512


def prepare_reference_image(value, directory: Path, *, notify=None):
    if not value or str(value).startswith(("http://", "https://", "asset://", "data:")):
        return value
    from PIL import Image, ImageOps

    source = Path(value).expanduser().resolve(strict=True)
    with Image.open(source) as image:
        image.load()
        width, height = image.size
        if min(width, height) >= REFERENCE_MIN_SIDE and max(width, height) <= REFERENCE_MAX_SIDE:
            return value
        if max(width, height) > REFERENCE_MAX_SIDE:
            raise ValueError(f"Reference image dimensions {width}x{height} exceed the {REFERENCE_MAX_SIDE}-pixel limit: {source}")
        minimum_scale = REFERENCE_MIN_SIDE / min(width, height)
        maximum_scale = REFERENCE_MAX_SIDE / max(width, height)
        if minimum_scale > maximum_scale:
            raise ValueError(f"Reference image dimensions {width}x{height} cannot be scaled proportionally to meet the "
                             f"{REFERENCE_MIN_SIDE}-{REFERENCE_MAX_SIDE}-pixel requirement: {source}")
        image = ImageOps.exif_transpose(image)
        width, height = image.size
        scale = min(REFERENCE_UPSCALE_SHORT_SIDE / min(width, height), maximum_scale)
        size = tuple(min(REFERENCE_MAX_SIDE, math.ceil(side * scale)) for side in (width, height))
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"reference_{digest}_{size[0]}x{size[1]}.png"
        reusable = False
        if destination.is_file():
            try:
                with Image.open(destination) as cached:
                    reusable = cached.size == size and cached.format == "PNG"
                    cached.verify()
            except (OSError, ValueError, SyntaxError):
                reusable = False
        if not reusable:
            mode = "RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB"
            resized = image.convert(mode).resize(size, Image.Resampling.LANCZOS)
            temporary = directory / f".{destination.stem}.{uuid.uuid4().hex}.tmp"
            try:
                resized.save(temporary, format="PNG")
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
        if notify:
            notify(f"Reference image is too small; using an upscaled copy: {width}x{height} -> {size[0]}x{size[1]}; {destination}")
        return str(destination)


@dataclass(frozen=True)
class RenderStageInput:
    task_dir: Path
    white_video: Path
    reference_image: Path
    prompt: str
    output: Path
    session_file: Path


def _task_path(task_dir: Path, value: str, *, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Task record is missing {name}.")
    raw = Path(value)
    path = (raw if raw.is_absolute() else task_dir / raw).resolve(strict=True)
    if not path.is_relative_to(task_dir) or not path.is_file():
        raise ValueError(f"{name} must be a file within the current task directory: {path}")
    return path


def prepare_render_input(task_dir: str | Path) -> RenderStageInput:
    task_dir = Path(task_dir).expanduser().resolve(strict=True)
    if not task_dir.is_dir():
        raise ValueError(f"Task root path is not a directory: {task_dir}")
    task_file = task_dir / "task.json"
    if not task_file.is_file():
        raise FileNotFoundError(f"Task record not found: {task_file}")
    task = read_json(task_file)
    if not isinstance(task, dict):
        raise ValueError("task.json must contain a JSON object.")
    white = _task_path(task_dir, task.get("blender_video_file"), name="blender_video_file")
    if white.suffix.lower() not in {".mp4", ".mov"} or white.stat().st_size == 0:
        raise ValueError("The Blender white-model video must be a non-empty MP4/MOV file.")
    handoff_path = task_dir / "input" / "input.json"
    handoff = read_json(handoff_path)
    if not isinstance(handoff, dict):
        raise ValueError("input/input.json must contain a JSON object.")
    first_frame = ("blender/derived/reference_first_frame.png" if handoff.get("input_type") == 2
                   else handoff.get("first_frame"))
    image = _task_path(task_dir, first_frame, name="first_frame")
    if image.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise ValueError("The first-frame reference image must be JPG, PNG, or WebP.")
    try:
        import cv2
        import numpy
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Render dependencies are missing. Install the dependencies in requirements.txt.") from exc
    try:
        with Image.open(image) as reference:
            reference.verify()
    except (OSError, ValueError) as exc:
        raise ValueError(f"Unable to decode the first-frame reference image: {image}") from exc
    prompt = DEFAULT_INITIAL_PROMPT
    output = task_dir / "render" / "rendered.mp4"
    return RenderStageInput(task_dir, white, image, prompt.strip(), output,
                            output.with_name("rendered_review_session.json"))


def prepare_generation(state, publisher, *, prompt=None, notify=None) -> dict:
    args = replace(state["base_args"])
    if prompt is not None:
        args.prompt = prompt
    directory = Path(state["initial_output"]).parent / "prepared_media"
    for name in ("image_1", "image_2"):
        original = getattr(args, name)
        prepared = prepare_reference_image(original, directory, notify=notify)
        if prepared != original:
            setattr(args, name, prepared)
            setattr(args, name + "_type", "image/png")
    if state.get("auto_local_media"):
        args.video = publisher.publish(state["evaluation_white"], notify=notify or (lambda message: None))
    return api.build_payload(args)


def generate_video(state, payload, output, *, on_submitted, on_created, on_status=None) -> str:
    with requests.Session() as client:
        return api.generate_and_download(
            client, state["base_url"], state["headers"], payload, Path(output),
            interval=state["poll_interval"], timeout=state["timeout_seconds"],
            on_submitted=on_submitted, on_created=on_created, on_status=on_status)


def resume_video(state, task_id, output, *, on_status=None) -> str:
    if not task_id:
        raise ValueError("Cannot resume without a task ID. No new task will be submitted.")
    with requests.Session() as client:
        return api.resume_and_download(
            client, state["base_url"], state["headers"], task_id, Path(output),
            interval=state["poll_interval"], timeout=state["timeout_seconds"],
            on_status=on_status)


def video_is_readable(path) -> bool:
    import cv2
    path = Path(path)
    if not path.is_file():
        return False
    capture = cv2.VideoCapture(str(path))
    try:
        valid, _ = capture.read()
        return bool(valid)
    finally:
        capture.release()
