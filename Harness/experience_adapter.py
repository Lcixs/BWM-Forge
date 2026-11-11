from __future__ import annotations

import json
import os
from pathlib import Path
from utils.prompts import load_prompt
from typing import Any

from input_pipeline.common import save_json
from . import harness


class ExperienceAdapter:

    def __init__(self, project_root: Path, input_manifest: dict[str, Any],
                 request_options: dict[str, Any] | None = None):
        self.project_root = Path(project_root).resolve()
        config = input_manifest.get("experience") or {}
        if not isinstance(config, dict):
            raise ValueError("experience configuration must be an object")
        self.enabled = bool(config.get("enabled", True)) and os.getenv(
            "CWM_ENABLE_EXPERIENCE_RETRIEVAL", "1"
        ).lower() not in {"0", "false", "no"}
        raw_store = config.get("store") or os.getenv("CWM_EXPERIENCE_STORE") or "assets/experience-store"
        self.store = Path(raw_store).expanduser()
        if not self.store.is_absolute():
            self.store = self.project_root / self.store
        self.store = self.store.resolve()
        self.limit = max(0, min(100, int(config.get("limit", 8))))
        self.request_options = dict(request_options or {})
        self.harness = harness

    def query(self, folder: Path, context: dict[str, Any]) -> dict[str, Any]:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        result_path = folder / "experience_result.json"
        if result_path.is_file():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if not isinstance(result, dict) or not isinstance(result.get("experiences"), list):
                raise ValueError(f"invalid cached experience result: {result_path}")
            return result
        result = {"experiences": [], "mode": "disabled", "error": None}
        if self.enabled and self.limit:
            try:
                result = self.harness.query_experiences(
                    experience_store=self.store, context=context, limit=self.limit, **self.request_options
                )
                if not isinstance(result, dict) or not isinstance(result.get("experiences"), list):
                    raise ValueError("invalid Harness query result")
            except Exception as exc:
                result = {"experiences": [], "mode": "unavailable", "error": type(exc).__name__}
        save_json(folder / "experience_query.json", {
            "context": context, "experience_store": str(self.store),
            "limit": self.limit, "enabled": self.enabled,
        })
        save_json(result_path, result)
        return result

    @staticmethod
    def prompt_text(result: dict[str, Any]) -> str:
        rows = result.get("experiences", []) if isinstance(result, dict) else []
        if not rows:
            return ""
        guidance = [{key: row[key] for key in ("id", "rule", "scope") if key in row}
                    for row in rows if isinstance(row, dict)]
        return "\n\n" + load_prompt("harness/retrieved_experience.md",
                                     experiences=json.dumps(guidance, ensure_ascii=False))

    @classmethod
    def inject_payload(cls, payload: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        suffix = cls.prompt_text(result)
        if not suffix:
            return payload
        if suffix.strip() not in str(payload.get("prompt") or ""):
            payload["prompt"] = str(payload.get("prompt") or "").rstrip() + suffix
        for item in payload.get("content", []):
            if isinstance(item, dict) and item.get("type") == "text":
                if suffix.strip() not in str(item.get("text") or ""):
                    item["text"] = str(item.get("text") or "").rstrip() + suffix
                break
        return payload
