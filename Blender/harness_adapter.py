from __future__ import annotations

from pathlib import Path
from typing import Any

from Harness.experience_adapter import ExperienceAdapter
from config import (BLENDER_MODEL, BLENDER_OPENAI_BASE_URL, BLENDER_OPENAI_AUTH_FILE,
                    BLENDER_REASONING_EFFORT)


class BlenderHarnessAdapter:

    def __init__(self, task_dir: Path, input_manifest: dict[str, Any], *, project_root: Path):
        self.task_dir = Path(task_dir).resolve()
        self.core = ExperienceAdapter(project_root, input_manifest, {
            "model": BLENDER_MODEL, "base_url": BLENDER_OPENAI_BASE_URL,
            "auth_file": BLENDER_OPENAI_AUTH_FILE, "reasoning_effort": BLENDER_REASONING_EFFORT})
        assets = input_manifest.get("assets") or {}
        assets = assets if isinstance(assets, dict) else {}
        robot = input_manifest.get("robot") or assets.get("urdf") or assets.get("root") or "unknown_robot"
        self.context = {
            "text": str(input_manifest.get("instruction") or ""),
            "robot": Path(str(robot)).stem,
            "input_type": input_manifest.get("input_type"),
        }
        for key in ("task_type", "scene", "gripper", "asset"):
            if input_manifest.get(key):
                self.context[key] = input_manifest[key]

    def begin(self, stage: str, round_number: int, *, feedback: dict[str, Any] | None = None,
              folder: Path) -> dict[str, Any]:
        context = {
            **self.context,
            "stage": stage,
            "round": int(round_number),
            "tags": ["blender", stage],
            "text": "\n".join(value for value in (
                self.context["text"],
                (feedback or {}).get("text", "") if isinstance(feedback, dict) else "",
            ) if value),
        }
        return self.core.query(Path(folder), context)

    @staticmethod
    def prompt_text(result: dict[str, Any]) -> str:
        return ExperienceAdapter.prompt_text(result)

    @staticmethod
    def retrieved(result: dict[str, Any]) -> list[dict[str, Any]]:
        rows = result.get("experiences", []) if isinstance(result, dict) else []
        return [dict(row) for row in rows if isinstance(row, dict)]
