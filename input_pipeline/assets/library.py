from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import uuid

from config import ASSET_ROOT, INPUT_ASSET_MAX_FILE_BYTES, INPUT_ASSET_MAX_TOTAL_BYTES
from ..common import copy_input, file_hash, json_bytes


SCHEMA_VERSION = 1
EMBODIMENTS = {
    "robot_dog": "机械狗",
    "robot": "机器人",
    "robot_arm": "机械臂",
    "dexterous_hand": "灵巧手",
}
NAME_PATTERN = re.compile(r"[\w-]{1,80}\Z")


def _relative_path(relative):
    if (not isinstance(relative, str) or not relative or "\\" in relative
            or ":" in relative or "\x00" in relative):
        raise ValueError(f"Invalid local asset path: {relative!r}")
    parsed = PurePosixPath(relative)
    if parsed.is_absolute() or ".." in parsed.parts or parsed.as_posix() != relative:
        raise ValueError(f"Non-canonical asset path: {relative!r}")
    return parsed


def _local_file(root, relative):
    _relative_path(relative)
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"Missing file or path outside asset root: {relative}")
    return path


def _metadata(metadata):
    if not isinstance(metadata, dict):
        raise ValueError("Robot metadata must be an object.")
    required = ("name", "embodiment")
    for key in required:
        if not isinstance(metadata.get(key), str) or not metadata[key].strip():
            raise ValueError(f"{key} is required and must be a non-empty string.")
    if (not metadata["name"][0].isalnum() or not NAME_PATTERN.fullmatch(metadata["name"])
            or Path(metadata["name"]).is_reserved()):
        raise ValueError("Asset name must start with a letter or digit, contain 1 to 80 letters, digits, underscores or hyphens, and must not be a reserved filename.")
    if metadata["embodiment"] not in EMBODIMENTS:
        raise ValueError("Unsupported asset embodiment.")
    if metadata.get("format", "urdf") != "urdf":
        raise ValueError("Only expanded URDF assets are supported.")
    result = {key: metadata[key] for key in required}
    json_bytes(result)
    return result


def _check_entry(entry):
    _metadata(entry)
    if set(entry) != {"name", "embodiment", "format", "entrypoint"}:
        raise ValueError("Each index entry must contain only name, embodiment, format and entrypoint.")
    if entry.get("format") != "urdf":
        raise ValueError("Each index entry must have format='urdf'.")
    path = _relative_path(entry.get("entrypoint"))
    if (len(path.parts) < 3 or path.parts[:2] != (entry["embodiment"], entry["name"])
            or path.suffix.lower() != ".urdf"):
        raise ValueError("Entrypoint must be a URDF inside <embodiment>/<asset_name>/.")


def _license_files(package_root):
    folder = package_root / "licenses"
    if not folder.resolve().is_relative_to(package_root.resolve()):
        raise ValueError("License path escapes asset root.")
    # 一并保留共同源目录内的许可声明。
    files = []
    candidates = [path for path in package_root.iterdir() if path.is_file() and
                  re.match(r"^(license|licence|copying|notice)([._-]|$)", path.name, re.I)]
    if folder.is_dir():
        candidates.extend(path for path in folder.rglob("*") if path.is_file())
    for path in sorted(candidates):
        relative = path.relative_to(package_root).as_posix()
        _local_file(package_root, relative)
        files.append(relative)
    return files


def _package_files(package_root, asset):
    return sorted({Path(path).relative_to(package_root).as_posix()
                   for path in asset["resource_files"]}
                  | set(_license_files(package_root)))


def _signatures(root, files):
    return {relative: file_hash(_local_file(root, relative)) for relative in files}


class AssetLibrary:

    def __init__(self, root=None):
        self.root = Path(root if root is not None else ASSET_ROOT).resolve()

    def _index(self):
        path = self.root / "manifest.json"
        if not path.exists():
            return {"schema_version": SCHEMA_VERSION, "robots": []}
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION
                or not isinstance(data.get("robots"), list)):
            raise ValueError("Expected a schema 1 manifest with a robots list.")
        for item in data["robots"]:
            _check_entry(item)
        names = [item["name"].casefold() for item in data["robots"]]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate asset names in manifest.json.")
        return data

    @contextmanager
    def _writer(self):
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.root / ".write.lock"
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise ValueError("Library is being updated (.write.lock exists).") from exc
        try:
            os.close(descriptor)
            yield
        finally:
            lock.unlink()

    def _write_index(self, index):
        index["robots"].sort(key=lambda item: item["name"])
        target = self.root / "manifest.json"
        with tempfile.NamedTemporaryFile(dir=self.root, prefix=".manifest-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json_bytes(index))
        try:
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def list_assets(self, query="", *, embodiment=None):
        query = query.casefold().strip()
        return [deepcopy(item) for item in self._index()["robots"]
                if (embodiment is None or item["embodiment"] == embodiment)
                and (not query or query in json.dumps(item, ensure_ascii=False).casefold())]

    def _lookup(self, asset_name):
        entry = next((item for item in self._index()["robots"] if item["name"] == asset_name), None)
        if entry is None:
            raise KeyError(f"Unknown asset: {asset_name}")
        parts = PurePosixPath(entry["entrypoint"]).parts
        package_root = (self.root / parts[0] / asset_name).resolve()
        if not package_root.is_relative_to(self.root):
            raise ValueError("Robot directory escapes library root.")
        relative = PurePosixPath(*parts[2:]).as_posix()
        _local_file(package_root, relative)
        return entry, package_root, relative

    def resolve(self, asset_name):
        entry, package_root, relative = self._lookup(asset_name)
        return {**deepcopy(entry), "assets": {
            "root": str(package_root), "urdf": str(package_root / relative)}}

    def register(self, asset, metadata):
        description = _metadata(metadata)
        if set(metadata) != {"name", "embodiment"}:
            raise ValueError("Asset metadata must contain only name and embodiment.")
        if (asset.get("passed") is not True or asset.get("asset_name") != description["name"]
                or asset.get("embodiment") != description["embodiment"]):
            raise ValueError("Provide the matching, validated asset before registration.")
        source_root = Path(asset["assets"]["root"])
        relative = Path(asset["assets"]["urdf"]).relative_to(source_root).as_posix()
        asset_name = description["name"]
        folder = description["embodiment"]
        entry = {**description, "format": "urdf", "entrypoint": f"{folder}/{asset_name}/{relative}"}
        _check_entry(entry)
        destination = (self.root / folder / asset_name).resolve()
        if not destination.is_relative_to(self.root):
            raise ValueError("Destination escapes library root.")
        with self._writer():
            index = self._index()
            existing = next((item for item in index["robots"]
                             if item["name"].casefold() == asset_name.casefold()), None)
            if existing is not None:
                raise ValueError(f"Asset name already exists: {asset_name}. Choose a different name.")
            elif destination.exists():
                raise ValueError(f"Unregistered asset directory already exists: {destination}")
            else:
                files = _package_files(source_root, asset)
                sizes = [_local_file(source_root, path).stat().st_size for path in files]
                if any(size > INPUT_ASSET_MAX_FILE_BYTES for size in sizes) or sum(sizes) > INPUT_ASSET_MAX_TOTAL_BYTES:
                    raise ValueError("Asset files exceed the per-file or total size limit.")
                signatures = _signatures(source_root, files)
                destination.parent.mkdir(parents=True, exist_ok=True)
                # 用普通目录继承资产库权限，避免保留临时目录的私有权限。
                staging = destination.parent / f".stage-{uuid.uuid4().hex}"
                staging.mkdir()
                try:
                    for path in files:
                        target = staging / path
                        copy_input(_local_file(source_root, path), target, INPUT_ASSET_MAX_FILE_BYTES)
                    if sum((staging / path).stat().st_size for path in files) > INPUT_ASSET_MAX_TOTAL_BYTES:
                        raise ValueError("Copied asset exceeds the total size limit.")
                    if _signatures(staging, files) != signatures:
                        raise ValueError("Source files changed during registration; retry with stable inputs.")
                    if not staging.resolve().is_relative_to(self.root) or not destination.resolve().is_relative_to(self.root):
                        raise ValueError("Staging or destination escapes library root.")
                    os.replace(staging, destination)
                    index["robots"].append(entry)
                    try:
                        self._write_index(index)
                    except Exception:
                        if not destination.resolve().is_relative_to(self.root):
                            raise ValueError("Rollback destination escapes library root.")
                        shutil.rmtree(destination)
                        raise
                finally:
                    if staging.exists():
                        shutil.rmtree(staging)
        return asset_name
