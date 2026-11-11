from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from config import (ASSET_ROOT as DEFAULT_ASSET_ROOT, INPUT_MAX_IMAGE_BYTES,
                    INPUT_MAX_MOTION_BYTES, INPUT_MAX_VIDEO_BYTES)
from .assets.library import AssetLibrary, EMBODIMENTS
from .assets.urdf_validation import validate_urdf
from .assets.arm_validation import validate_arm
from .common import copy_input, file_hash, now, read_json, save_json
from .first_frame import analyze_frame
from .media import inspect_video
from .motion import check_action, trajectory_columns, validate_fps


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


@dataclass(frozen=True)
class InputRequest:
    input_type: int
    asset_name: str
    task_text: str
    image: Path | None = None
    trajectory: Path | None = None
    video: Path | None = None
    action_space: str | None = None
    fps: float | None = None
    joint_names: list[str] | None = None
    embodiment: str = "robot_arm"

    def validate(self) -> None:
        if self.embodiment not in EMBODIMENTS:
            raise ValueError("Select a supported embodiment.")
        if type(self.input_type) is not int or self.input_type not in (0, 1, 2):
            raise ValueError("Input type must be 0, 1, or 2.")
        if not isinstance(self.asset_name, str) or not self.asset_name.strip():
            raise ValueError("A robot asset name is required.")
        if (not isinstance(self.task_text, str) or not self.task_text.strip()
                or len(self.task_text) > 8000 or "\x00" in self.task_text):
            raise ValueError("Task description must contain 1 to 8,000 characters and must not contain NUL.")
        required = {0: {"image"}, 1: {"image", "trajectory"}, 2: {"video"}}[self.input_type]
        supplied = {name for name in ("image", "trajectory", "video") if getattr(self, name) is not None}
        if supplied != required:
            raise ValueError(f"Input type {self.input_type} requires exactly these files: {', '.join(sorted(required))}.")
        for name in required:
            path = Path(getattr(self, name))
            extensions = IMAGE_EXTENSIONS if name == "image" else {".parquet", ".npz"} if name == "trajectory" else {".mp4", ".mov"}
            if not path.is_file() or path.suffix.lower() not in extensions:
                raise ValueError(f"{name} file does not exist or has an unsupported extension: {path}")
        if self.input_type != 1:
            if self.action_space is not None or self.fps is not None or self.joint_names is not None:
                raise ValueError("Only input type 1 accepts an action space, sampling rate and joint names.")
        else:
            if self.action_space not in {"joint", "eef"}:
                raise ValueError("Input type 1 requires joint or eef.")
            if self.embodiment != "robot_arm" and self.action_space != "joint":
                raise ValueError("Only robot_arm accepts EEF trajectories; use joint for other embodiments.")
            validate_fps(self.fps)
            trajectory_columns(self.action_space, self.joint_names)
            if self.action_space == "eef" and self.joint_names is not None:
                raise ValueError("joint_names applies only to joint trajectories.")


def read_path(prompt: str) -> Path:
    return Path(input(prompt).strip().strip('"').strip("'")).expanduser().resolve()


def choose_embodiment() -> str:
    print("\nCWM System - Input Stage")
    for index, (key, label) in enumerate(EMBODIMENTS.items(), 1):
        print(f"{index}: {label} [{key}]")
    selected = input("Select an embodiment [1/2/3/4]: ").strip()
    if selected not in {"1", "2", "3", "4"}:
        raise ValueError("Select an embodiment from 1 to 4.")
    return list(EMBODIMENTS)[int(selected) - 1]


def validate_asset(urdf, embodiment, *, asset_name, package_root=None,
                   base_frame=None, tool_frame=None, tcp_xyz=None, tcp_rpy=None):
    if embodiment not in EMBODIMENTS:
        raise ValueError("Select a supported embodiment.")
    if embodiment != "robot_arm" and any(value is not None for value in
                                         (base_frame, tool_frame, tcp_xyz, tcp_rpy)):
        raise ValueError("Base/tool frames and TCP offsets apply only to robot_arm.")
    if package_root is not None and Path(urdf).is_absolute():
        urdf = Path(urdf).relative_to(Path(package_root))
    asset = validate_urdf(package_root, urdf, asset_name=asset_name)
    if embodiment == "robot_arm":
        asset.update(validate_arm(asset["joints"], asset["root_link"],
                                  base_frame=base_frame, tool_frame=tool_frame,
                                  tcp_xyz=tcp_xyz, tcp_rpy=tcp_rpy))
    asset.update(embodiment=embodiment)
    return asset


def choose_input_type() -> int:
    print("\nInput mode:")
    print("0: First-frame image + robot asset + task description")
    print("1: First-frame image + robot asset + task description + trajectory (Parquet/NPZ) and sampling rate")
    print("2: Robot asset + task description + demonstration video")
    kind = int(input("Select an input type [0/1/2]: "))
    if kind not in (0, 1, 2):
        raise ValueError("Select input type 0, 1, or 2.")
    return kind


def select_asset(library: AssetLibrary, embodiment: str) -> tuple[str, str]:
    print("\nRobot asset source:\n1: Use the local asset library\n2: Import a local URDF")
    source = input("Select an asset source [1/2] (Enter to use the library): ").strip() or "1"
    if source == "2":
        urdf = read_path("Path to the expanded robot URDF (resources must use valid relative paths): ")
        metadata = {
            "name": input("Asset name (letters, digits, underscores or hyphens): ").strip(),
            "embodiment": embodiment,
        }
        print("[Asset] Validating the URDF and selected embodiment...", flush=True)
        asset = validate_asset(urdf, embodiment, asset_name=metadata["name"])
        print("[Asset] Validation passed. Saving to the asset library...", flush=True)
        asset_name = library.register(asset, metadata)
        print(f"[Asset] Added to the library: {asset_name}")
        print("This asset will be used for the current task and will remain in the library if the task later fails.")
        return asset_name, "urdf"
    if source != "1":
        raise ValueError("Select asset source 1 or 2.")

    assets = library.list_assets(embodiment=embodiment)
    if not assets:
        raise ValueError("No assets for this embodiment. Run again and select the option to import a local URDF.")
    print("\nAvailable robot assets:")
    for index, asset in enumerate(assets, 1):
        print(f"{index}. {asset['name']}")
    selected = input("Select a robot asset (name or list number): ").strip()
    if selected not in {asset["name"] for asset in assets} and selected.isdigit():
        index = int(selected) - 1
        if not 0 <= index < len(assets):
            raise ValueError("Robot asset selection is outside the list range.")
        selected = assets[index]["name"]
    if selected not in {asset["name"] for asset in assets}:
        raise ValueError("Select an asset belonging to the chosen embodiment.")
    entry = library.resolve(selected)
    validate_asset(entry["assets"]["urdf"], embodiment, asset_name=selected,
                   package_root=entry["assets"]["root"])
    return selected, "library"


def collect_request(kind: int, asset_name: str, embodiment: str) -> InputRequest:
    task_text = input("Task description: ").strip()
    options = {}
    if kind in (0, 1):
        options["image"] = read_path("First-frame image path: ")
    if kind == 1:
        options["trajectory"] = read_path("Trajectory file path (Parquet/NPZ): ")
        options["action_space"] = "joint"
        if embodiment == "robot_arm":
            mode = input("Action space [1 joint positions / 2 EEF end-effector poses]: ").strip()
            if mode not in {"1", "2"}:
                raise ValueError("Select action space 1 or 2.")
            options["action_space"] = {"1": "joint", "2": "eef"}[mode]
        if options["action_space"] == "joint":
            options["joint_names"] = [name.strip() for name in input(
                "URDF joint names in trajectory-column order (comma-separated, including tool joints if supplied): "
            ).split(",")]
        try:
            options["fps"] = validate_fps(float(input("Trajectory sampling rate fps (Hz, e.g. 30; required): ").strip()))
        except ValueError as exc:
            raise ValueError("Trajectory sampling rate fps must be a finite positive number in Hz.") from exc
    if kind == 2:
        options["video"] = read_path("Demonstration video path (MP4/MOV): ")
    return InputRequest(kind, asset_name, task_text, embodiment=embodiment, **options)


def load_input_request(task_dir: Path) -> InputRequest:
    task_dir = Path(task_dir).resolve()
    document = read_json(task_dir / "input/request.json")
    for name in ("image", "trajectory", "video"):
        if document.get(name):
            source = (task_dir / document[name]).resolve(strict=True)
            if not source.is_relative_to(task_dir / "input"):
                raise ValueError("The input file is outside the input directory.")
            document[name] = source
    return InputRequest(**document)


def _save_input_request(request: InputRequest, task_dir: Path) -> dict:
    input_dir = task_dir / "input"
    if not input_dir.resolve().is_relative_to(task_dir):
        raise ValueError("The input directory is outside the task directory.")
    input_dir.mkdir(parents=True, exist_ok=True)
    document, checks = asdict(request), {}
    for field, basename, limit in (
        ("image", "first_frame", INPUT_MAX_IMAGE_BYTES),
        ("trajectory", "trajectory", INPUT_MAX_MOTION_BYTES),
        ("video", "human_video", INPUT_MAX_VIDEO_BYTES),
    ):
        source = getattr(request, field)
        if source is None:
            continue
        source = Path(source).resolve(strict=True)
        target = input_dir / "files" / (basename + source.suffix.lower())
        if not target.resolve().is_relative_to(input_dir):
            raise ValueError("The input file is outside the input directory.")
        if source == target.resolve():
            size = source.stat().st_size
            if not 0 < size <= limit:
                raise ValueError(f"Input file is empty or exceeds the size limit: {source.name}")
            checks[field] = {"sha256": file_hash(source), "size": size}
        else:
            checks[field] = copy_input(source, target, limit)
        document[field] = target.relative_to(task_dir).as_posix()
    save_json(input_dir / "request.json", document)
    return checks


def run_input_stage(request: InputRequest, task_dir: Path, *,
                    asset_root: Path = DEFAULT_ASSET_ROOT,
                    base_frame=None, tool_frame=None,
                    tcp_xyz=None, tcp_rpy=None,
                    frame_analyzer=analyze_frame, on_progress=None) -> Path:
    task_dir = Path(task_dir).resolve()
    task_dir.mkdir(parents=True, exist_ok=True)
    input_dir = task_dir / "input"
    if (input_dir / "input.json").exists():
        raise FileExistsError(f"Input handoff already exists: {input_dir / 'input.json'}")

    def progress(step):
        if on_progress is not None:
            on_progress(step)

    request.validate()
    file_checks = _save_input_request(request, task_dir)
    request = load_input_request(task_dir)
    progress("Validating request")
    progress("Validating robot asset")
    entry = AssetLibrary(asset_root).resolve(request.asset_name)
    if entry["embodiment"] != request.embodiment:
        raise ValueError("The selected asset does not belong to the task embodiment.")
    asset = validate_asset(entry["assets"]["urdf"], request.embodiment,
                           asset_name=request.asset_name,
                           package_root=entry["assets"]["root"], base_frame=base_frame,
                           tool_frame=tool_frame, tcp_xyz=tcp_xyz, tcp_rpy=tcp_rpy)
    checks = {"asset": {key: asset[key] for key in
                       ("passed", "asset_name", "model_name", "embodiment")},
              "files": file_checks}
    handoff = {"instruction": request.task_text, "assets": asset["assets"]}
    action = None
    if request.input_type == 1:
        progress("Validating motion data")
        action = check_action(request.trajectory, asset,
                              action_space=request.action_space, fps=request.fps,
                              joint_names=request.joint_names)
        action["trajectory"] = str(request.trajectory)
        checks["action"] = action
    if request.input_type == 2:
        progress("Validating demonstration video")
        checks["video"] = inspect_video(request.video)
        handoff["human_video"] = str(request.video)
    else:
        progress("Analyzing the first frame")
        frame = frame_analyzer(request.image, asset, request.task_text, action=action)
        if not isinstance(frame.get("document"), str) or not frame["document"].strip():
            raise ValueError("First-frame analysis did not return scene modeling information.")
        checks["frame"] = {key: value for key, value in frame.items() if key != "document"}
        handoff.update(first_frame=str(request.image), scene_supplement_text=frame["document"])
        if request.input_type == 1:
            handoff["trajectory"] = str(request.trajectory)
            handoff["motion"] = action["motion"]

    progress("Publishing input handoff file")
    handoff.update(input_type=request.input_type, embodiment=request.embodiment,
                   asset_name=request.asset_name)
    save_json(input_dir / "checks.json", checks)
    # Publish the handoff last; request.json and original files remain available after an interruption.
    save_json(input_dir / "input.json", handoff)
    return input_dir / "input.json"


def run_input_task(task_dir: Path, asset_root: Path = DEFAULT_ASSET_ROOT) -> int:
    task_dir = Path(task_dir).resolve()
    task_dir.mkdir(parents=True, exist_ok=True)
    task = {"task_id": task_dir.name, "start_stage": "input",
            "created_at": now(), "status": "input_running"}
    save_json(task_dir / "task.json", task)
    print(f"\nTask directory: {task_dir}", flush=True)
    try:
        embodiment = choose_embodiment()
        task["embodiment"] = embodiment
        save_json(task_dir / "task.json", task)

        asset_name, asset_source = select_asset(AssetLibrary(asset_root), embodiment)
        task.update(asset_name=asset_name, asset_source=asset_source)
        save_json(task_dir / "task.json", task)

        kind = choose_input_type()
        task["input_type"] = kind
        save_json(task_dir / "task.json", task)
        request = collect_request(kind, asset_name, embodiment)

        def progress(step):
            task["input_step"] = step
            save_json(task_dir / "task.json", task)
            print(f"[Input] {step}", flush=True)

        handoff = run_input_stage(request, task_dir, asset_root=asset_root, on_progress=progress)
        task.update(status="input_completed", asset_name=asset_name, asset_source=asset_source,
                    handoff_file=handoff.relative_to(task_dir).as_posix(), input_finished_at=now())
        save_json(task_dir / "task.json", task)
        print(f"\nInput processing complete.\nHandoff file: {handoff}")
        return 0
    except (KeyboardInterrupt, EOFError):
        task.update(status="cancelled", stopped_at=now())
        save_json(task_dir / "task.json", task)
        print("\nCancelled. Existing task files have been preserved.")
        return 130
    except Exception as exc:
        task.update(status="failed", error=str(exc), stopped_at=now())
        save_json(task_dir / "task.json", task)
        raise
