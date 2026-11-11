from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from .common import IMAGE_EXTS, resolve_input_path


@dataclass(frozen=True)
class BlenderInput:
    input_json: Path
    input_type: int
    manifest: dict
    instruction: str
    urdf: Path
    first_frame: Path | None = None
    trajectory: Path | None = None
    human_video: Path | None = None
    asset_root: Path | None = None
    mesh_root: Path | None = None


def _read_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object.")
    return value


def _directory(handoff: Path, value, fallback: Path, label: str) -> Path:
    if value is None:
        return fallback
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a directory path.")
    path = Path(value).expanduser()
    path = (path if path.is_absolute() else handoff.parent / path).resolve(strict=True)
    if not path.is_dir():
        raise ValueError(f"{label} is not a directory: {path}")
    return path


def load_blender_input(path: str | Path, *, input_type: int | None = None) -> BlenderInput:
    handoff = Path(path).expanduser().resolve(strict=True)
    manifest = _read_object(handoff)
    task_file = handoff.parent.parent / "task.json"
    task = _read_object(task_file) if task_file.is_file() else {}
    types = [value for value in (input_type, task.get("input_type"), manifest.get("input_type"))
             if value is not None]
    if any(type(value) is not int or value not in (0, 1, 2) for value in types):
        raise ValueError("input_type must be the integer 0, 1, or 2.")
    if len(set(types)) > 1:
        raise ValueError("Input types in the input_type argument, task.json, and input.json do not match.")
    if types:
        kind = types[0]
    else:
        raise ValueError("Blender requires input_type, provided through task.json, input.json, or an argument.")

    instruction = manifest.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("Input is missing the task description instruction.")
    assets = manifest.get("assets")
    if not isinstance(assets, dict):
        raise ValueError("assets must be an object.")
    urdf = resolve_input_path(handoff, assets.get("urdf"), "assets.urdf")
    if urdf.suffix.lower() != ".urdf":
        raise ValueError("assets.urdf must be a URDF file.")
    asset_root = _directory(handoff, assets.get("root"), urdf.parent, "assets.root")
    mesh_root = _directory(handoff, assets.get("meshes"), urdf.parent, "assets.meshes")

    if kind == 2:
        video = resolve_input_path(handoff, manifest.get("human_video"), "human_video")
        if video.suffix.lower() not in (".mp4", ".mov"):
            raise ValueError("human_video must be an MP4 or MOV video.")
        return BlenderInput(handoff, kind, manifest, instruction, urdf,
                            human_video=video, asset_root=asset_root, mesh_root=mesh_root)

    frame = resolve_input_path(handoff, manifest.get("first_frame"), "first_frame")
    if frame.suffix.lower() not in IMAGE_EXTS:
        raise ValueError("first_frame must be a PNG, JPG, or WebP image.")
    trajectory = None
    if kind == 1:
        trajectory = resolve_input_path(handoff, manifest.get("trajectory"), "trajectory")
        if trajectory.suffix.lower() not in {".parquet", ".npz"}:
            raise ValueError("trajectory for input type 1 must be a Parquet or NPZ file.")
        from input_pipeline.motion import trajectory_columns, validate_fps
        motion = manifest.get("motion")
        if not isinstance(motion, dict):
            raise ValueError("Input type 1 is missing the motion trajectory convention. Run the Input stage again.")
        validate_fps(motion.get("fps"))
        columns = trajectory_columns(motion.get("action_space"), motion.get("joint_names"))
        if motion.get("columns") != columns:
            raise ValueError("motion.columns does not match the action space or joint count.")
    return BlenderInput(handoff, kind, manifest, instruction, urdf, first_frame=frame,
                        trajectory=trajectory, asset_root=asset_root, mesh_root=mesh_root)
