from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
import uuid


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def save_json(path: Path, value) -> None:
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(json_bytes(value))
        for attempt in range(10):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.02)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_input(source: Path, destination: Path, max_bytes: int) -> dict:
    source, destination = Path(source), Path(destination)
    if not source.is_file() or not 0 < source.stat().st_size <= max_bytes:
        raise ValueError(f"Input file is empty, missing, or larger than {max_bytes // (1024 * 1024)} MiB: {source.name}")
    digest, size = hashlib.sha256(), 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader, destination.open("xb") as writer:
        while chunk := reader.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise ValueError("The input file exceeded the size limit during copying.")
            writer.write(chunk)
            digest.update(chunk)
    if size == 0:
        raise ValueError("The input file is empty.")
    return {"sha256": digest.hexdigest(), "size": size}
