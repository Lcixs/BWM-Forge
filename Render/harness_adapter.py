from __future__ import annotations

from pathlib import Path
from typing import Any

from Harness.experience_adapter import ExperienceAdapter
from config import (RENDER_MODEL, RENDER_OPENAI_BASE_URL, RENDER_OPENAI_AUTH_FILE,
                    RENDER_REASONING_EFFORT)


class RenderHarnessAdapter:

    def __init__(self, task_dir: Path, input_manifest: dict[str, Any], *, project_root: Path):
        self.task_dir = Path(task_dir).resolve()
        self.run_dir = self.task_dir / "render" / "harness"
        self.core = ExperienceAdapter(project_root, input_manifest, {
            "model": RENDER_MODEL, "base_url": RENDER_OPENAI_BASE_URL,
            "auth_file": RENDER_OPENAI_AUTH_FILE, "reasoning_effort": RENDER_REASONING_EFFORT})
        assets = input_manifest.get("assets") or input_manifest.get("robot_assets") or {}
        assets = assets if isinstance(assets, dict) else {}
        robot = input_manifest.get("robot") or assets.get("urdf") or assets.get("root") or "unknown_robot"
        self.context = {
            "text": str(input_manifest.get("instruction") or input_manifest.get("task_text") or ""),
            "robot": Path(str(robot)).stem,
        }
        for key in ("task_type", "scene", "gripper", "asset"):
            if input_manifest.get(key):
                self.context[key] = input_manifest[key]
    def _version_dir(self, version: int) -> Path:
        path = self.run_dir / "versions" / f"{int(version):03d}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _evaluation_text(evaluation: dict[str, Any] | None) -> str:
        if not isinstance(evaluation, dict):
            return ""
        parts = []
        if evaluation.get("decision"):
            parts.append(f"decision={evaluation['decision']}")
        aggregates = evaluation.get("aggregates")
        if isinstance(aggregates, dict) and aggregates.get("overall_score") is not None:
            parts.append(f"overall_score={aggregates['overall_score']}")
        for finding in (evaluation.get("findings") or [])[:8]:
            if isinstance(finding, dict):
                parts.append(str(finding.get("evidence") or finding.get("suggested_prompt") or ""))
        return "\n".join(part for part in parts if part)

    def begin(self, version: int, *, feedback: str = "", evaluation: dict[str, Any] | None = None) -> dict[str, Any]:
        folder = self._version_dir(version)
        context = {
            **self.context,
            "stage": "render",
            "tags": ["render"],
            "text": "\n".join(value for value in (self.context["text"], feedback, self._evaluation_text(evaluation)) if value),
        }
        return self.core.query(folder, context)

    @staticmethod
    def prompt_text(result: dict[str, Any]) -> str:
        return ExperienceAdapter.prompt_text(result)

    @classmethod
    def inject_payload(cls, payload: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        return ExperienceAdapter.inject_payload(payload, result)

