from __future__ import annotations

import copy
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
import re
import shutil
import sys
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


from config import BLENDER_EXE, INPUT_MAX_IMAGE_BYTES as MAX_IMAGE_BYTES
STAGES = ("scene_only", "robot_only", "composed")
STAGE_INDEX = {name: index for index, name in enumerate(STAGES)}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
_PROGRESS_LOG: ContextVar[Path | None] = ContextVar("blender_progress_log", default=None)
_PROGRESS_WRITE_LOCK = threading.Lock()


def discover_blender_bin() -> str:
    override = os.getenv("CWM_BLENDER_EXE")
    candidates = [Path(override).expanduser()] if override else []
    candidates.append(Path(BLENDER_EXE).expanduser())
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    located = shutil.which("blender") or shutil.which("blender.exe")
    return str(Path(located).resolve()) if located else str(candidates[0])


@contextmanager
def task_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / "workflow.lock").open("a+b") as lock:
        lock.seek(0)
        if os.fstat(lock.fileno()).st_size == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Another process is already running this task; concurrent resumption is not allowed") from exc
        try:
            yield
        finally:
            lock.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


@contextmanager
def capture_progress(path: Path):
    token = _PROGRESS_LOG.set(Path(path))
    try:
        yield
    finally:
        _PROGRESS_LOG.reset(token)


def _emit_progress(message: str, path: Path | None) -> None:
    timestamp = datetime.now().astimezone()
    print(f"[Blender] [{timestamp:%H:%M:%S}] {message}", file=sys.stderr, flush=True)
    if path is not None:
        try:
            with _PROGRESS_WRITE_LOCK:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"time": timestamp.astimezone(timezone.utc).isoformat(),
                                             "message": message}, ensure_ascii=False) + "\n")
        except OSError:
            pass  # Progress logging must not interrupt the modeling task.


def log_progress(message: str) -> None:
    _emit_progress(message, _PROGRESS_LOG.get())


@contextmanager
def progress_heartbeat(label: str, interval: int = 20, *, notify=None):
    stop = threading.Event()
    started = time.monotonic()
    path = _PROGRESS_LOG.get()
    notify = notify or (lambda message: _emit_progress(message, path))

    def report():
        while not stop.wait(interval):
            notify(f"{label} is still running; elapsed time: {int(time.monotonic() - started)} seconds")

    worker = threading.Thread(target=report, daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join(timeout=1)


def run_logged_command(command: list[str], log_path: Path, label: str) -> tuple[int, bool]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_progress(f"{label} started; command: {subprocess.list2cmdline(command)}")
    log_progress(f"{label} log: {log_path}")
    found_success = False
    with log_path.open("w", encoding="utf-8") as log, progress_heartbeat(label):
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        try:
            assert process.stdout is not None
            for line in process.stdout:
                found_success |= "CWM_RENDER_OK" in line
                log.write(line)
                log.flush()
                log_progress(f"{label} | {line.rstrip()}")
            code = process.wait()
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
        finally:
            if process.stdout is not None:
                process.stdout.close()
    log_progress(f"{label} finished; exit code: {code}; success marker: {'detected' if found_success else 'not detected'}")
    return code, found_success


def utc_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def json_load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def hash_path(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    if path.is_dir():
        for child in sorted(p for p in path.rglob("*") if p.is_file()):
            digest.update(str(child.relative_to(path)).replace("\\", "/").encode())
            with child.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        return digest.hexdigest()
    raise FileNotFoundError(path)


def plain(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "__dict__"):
        return {k: plain(v) for k, v in vars(value).items() if not k.startswith("_")}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def as_dict(item: Any) -> Dict[str, Any]:
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        try:
            return item.model_dump(by_alias=True, exclude_none=True)
        except TypeError:
            return item.model_dump()
    if hasattr(item, "to_dict"):
        return item.to_dict()
    return {key: getattr(item, key) for key in dir(item) if not key.startswith("_")}


def item_value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def parse_json_text(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise ValueError("model did not return JSON")


def safe_child(root: Path, requested: str | Path) -> Path:

    candidate = (root / Path(requested)).resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise ValueError(f"path escapes run directory: {requested}")
    return candidate


def read_image_size(path: Path) -> Optional[tuple[int, int]]:
    data = path.read_bytes()
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data.startswith(b"\xff\xd8"):
        pos = 2
        while pos + 9 < len(data):
            if data[pos] != 0xFF:
                pos += 1
                continue
            marker = data[pos + 1]
            pos += 2
            if marker in (0xD8, 0xD9):
                continue
            if pos + 2 > len(data):
                break
            length = int.from_bytes(data[pos:pos + 2], "big")
            if marker in range(0xC0, 0xC4) or marker in range(0xC5, 0xC8) or marker in range(0xC9, 0xCC) or marker in range(0xCD, 0xD0):
                if pos + 7 <= len(data):
                    return int.from_bytes(data[pos + 5:pos + 7], "big"), int.from_bytes(data[pos + 3:pos + 5], "big")
                break
            pos += max(length, 2)
    return None


def resolve_input_path(input_json: Path, value: str, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"input.json is missing {label}")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = input_json.parent / path
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"{label} is not a file: {path}")
    return path


def load_input_json(path: Path) -> Dict[str, Any]:

    source = path.resolve()
    payload = json_load(source)
    if not isinstance(payload, dict):
        raise ValueError(f"input.json must contain a JSON object: {source}")
    missing = [key for key in ("first_frame", "instruction", "assets") if not payload.get(key)]
    if missing:
        raise ValueError(f"input.json is missing required fields {missing}: {source}")

    payload = copy.deepcopy(payload)
    payload.setdefault("_input_json", str(source))

    if not payload.get("task_id") and source.name.lower() == "input.json" and source.parent.name.lower() == "input":
        payload["task_id"] = source.parent.parent.name
    payload.setdefault("_source_manifest_dir", str(source.parent))
    return payload


def validate_blend(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f".blend project not found: {path}")
    with path.open("rb") as stream:
        header = stream.read(7)
    # Blender 压缩工程使用 Zstandard 或旧版 Gzip 文件头。
    if not (header == b"BLENDER" or header.startswith(b"\x28\xb5\x2f\xfd")
            or header.startswith(b"\x1f\x8b")):
        raise RuntimeError(f"File header does not match a Blender project or supported compressed format: {path}")


def materialize_trajectory(source: Path, run_dir: Path, *, motion: dict) -> Dict[str, Any]:
    import math
    import numpy as np
    from input_pipeline.motion import read_trajectory, validate_fps, trajectory_columns

    source = Path(source).resolve()
    fps = validate_fps(motion.get("fps"))
    columns = trajectory_columns(motion.get("action_space"), motion.get("joint_names"))
    if motion.get("columns") != columns:
        raise ValueError("Trajectory column conventions do not match")
    derived = Path(run_dir) / "derived"
    derived.mkdir(parents=True, exist_ok=True)
    report_path = derived / "trajectory.json"
    array_file = source if source.suffix.lower() == ".npz" else (derived / "trajectory.npz").resolve()
    fingerprint = hash_path(source)
    if report_path.exists():
        cached = json_load(report_path)
        if (cached.get("array_key") == "action"
                and cached.get("source_sha256") == fingerprint and cached.get("source_path") == str(source)
                and cached.get("motion") == motion and cached.get("array_file") == str(array_file)
                and array_file.is_file() and cached.get("array_sha256") == hash_path(array_file)):
            return cached
    values = read_trajectory(source, action_space=motion["action_space"], joint_names=motion["joint_names"])
    duration = (len(values) - 1) / fps
    if not math.isfinite(duration):
        raise ValueError("The trajectory time range is not finite. Check fps.")
    if source.suffix.lower() == ".parquet":
        pending = array_file.with_suffix(".pending")
        with pending.open("wb") as stream:
            np.savez_compressed(stream, action=values)
        pending.replace(array_file)
    report = {"source_path": str(source), "source_sha256": fingerprint,
              "motion": motion, "shape": list(values.shape),
              "duration_seconds": duration, "array_file": str(array_file),
              "array_key": "action", "array_sha256": hash_path(array_file),
              "report_json": "derived/trajectory.json",
              "blender_contract": "np.load(array_file, allow_pickle=False)['action']; row i occurs at i/fps seconds"}
    write_json(report_path, report)
    return report
