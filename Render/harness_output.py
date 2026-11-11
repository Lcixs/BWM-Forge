from __future__ import annotations

import json
from pathlib import Path
import re

import asyncio

from input_pipeline.common import save_json
from utils.prompts import load_prompt
from utils.gpt_client import request_gpt
from config import (RENDER_MODEL, RENDER_OPENAI_BASE_URL, RENDER_OPENAI_AUTH_FILE,
                    RENDER_REASONING_EFFORT, RENDER_CONNECT_TIMEOUT_SECONDS, RENDER_MAX_OUTPUT_TOKENS)


SUMMARY_FILENAME = "render_stage_summary.json"
SUMMARY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["stage_summary", "lessons_candidates"],
    "properties": {
        "stage_summary": {"type": "string"},
        "lessons_candidates": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["kind", "rule", "evidence", "tags"],
            "properties": {
                "kind": {"type": "string", "enum": ["general", "specific"]},
                "rule": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "string"}},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
        }},
    },
}


def _clean(value, state):
    if isinstance(value, dict):
        excluded = {"path", "url", "file", "artifacts", "sampling", "headers", "api_key", "base_url"}
        return {k: _clean(v, state) for k, v in value.items()
                if k not in excluded and not k.endswith(("_path", "_url", "_file", "_api_key"))}
    if isinstance(value, list):
        return [_clean(v, state) for v in value]
    if not isinstance(value, str):
        return value
    for secret in (state.get("openai_api_key"),
                   state.get("headers", {}).get("Authorization", "").removeprefix("Bearer ")):
        if secret:
            value = value.replace(secret, "[secret omitted]")
    for path in [state.get("initial_output"), state.get("current_output"), state.get("evaluation_white"),
                 getattr(state.get("base_args"), "image_1", None),
                 *[row.get("video") for row in state.get("history", [])]]:
        if path:
            value = value.replace(str(path), "[media location omitted]")
    value = re.sub(r"data:[^\s]+", "[media data omitted]", value)
    value = re.sub(r"https?://[^\s<>]+", "[URL omitted]", value)
    return re.sub(r"[A-Za-z]:[\\/][^\r\n\"<>]*", "[local path omitted]", value)


def collect_record(state: dict) -> dict:
    if not state.get("confirmed") or not state.get("history"):
        raise ValueError("Confirm the final video before exporting stage records.")
    rounds, checks, feedback = [], [], []
    original = state.get("original_prompt") or state["base_args"].prompt
    inputs = [{"role": "white_model", "description": "Provides motion, timing, and camera view"},
              {"role": "reference_image", "description": "Provides real-world scene appearance"},
              {"role": "initial_prompt", "text": original}]
    for index, row in enumerate(state["history"]):
        version = row["version"]
        if row.get("user_feedback"):
            feedback.append({"version": version, "role": "user", "text": row["user_feedback"]})
        metrics_path = Path(row["video"]).with_suffix("").with_name(Path(row["video"]).stem + "_evaluation") / "metrics.json"
        if index == len(state["history"]) - 1 and state.get("current_evaluation"):
            result = state["current_evaluation"]
        elif metrics_path.is_file():
            result = json.loads(metrics_path.read_text(encoding="utf-8-sig"))
        elif row.get("evaluation_status") in {"complete", "objective_complete"}:
            raise ValueError(f"The evaluation report for version {version} is missing; a complete export is not possible.")
        else:
            result = {}
        if not isinstance(result, dict):
            raise ValueError(f"The evaluation report for version {version} must contain a JSON object.")
        vlm = result.get("vlm") or {}
        rounds.append({
            "version": version, "kind": row.get("version_kind", "initial" if version == 0 else "user_revision"),
            "prompt": row.get("task_prompt", ""),
            "generation_status": row.get("generation_status"),
            "evaluation_status": row.get("evaluation_status"),
            "scores": result.get("scores", row.get("scores", {})),
            "aggregates": result.get("aggregates", {"overall_score": row.get("overall_score")}),
            "objective_gate_score": row.get("objective_gate_score"),
            "decision": result.get("decision"), "decision_reasons": result.get("decision_reasons", []),
            "findings": result.get("findings", []), "gpt_review": vlm.get("details", {}),
            "action_review": result.get("action_review", {}),
            "correction_prompt": result.get("gpt_seedance_correction_prompt", ""),
            "evaluation_error": row.get("evaluation_error"),
        })
        if "retrieved_experiences" in row:
            rounds[-1]["retrieved_experiences"] = row["retrieved_experiences"]
        checks.append({"version": version, "name": "objective_validation",
                       "result": result.get("hard_check", {}),
                       "auto_revision_triggered": bool(row.get("objective_auto_feedback_prompt"))})
    final = state["history"][-1]
    feedback.append({"version": final["version"], "role": "user", "action": "confirm"})
    record = {
        "stage": "render", "revision": 1, "status": "completed", "inputs": inputs,
        "outputs": [{"role": "final_video", "version": final["version"], "accepted": True}],
        "checks": checks,
        "evaluation": {"final_version": final["version"], "rounds": rounds},
        "stage_summary": "", "lessons_candidates": [],
        "retrieved_experiences": state.get("retrieved_experiences", []),
        "user_feedback": feedback,
    }
    return _clean(record, state)


def validate_summary(value: dict) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("stage_summary"), str) or not value["stage_summary"].strip():
        raise ValueError("The stage summary is missing a valid stage_summary field.")
    lessons = value.get("lessons_candidates")
    if not isinstance(lessons, list) or len(lessons) > 3:
        raise ValueError("Invalid lesson candidate format.")
    for item in lessons:
        if (not isinstance(item, dict) or item.get("kind") not in {"general", "specific"}
                or not isinstance(item.get("rule"), str) or not item["rule"].strip()
                or not isinstance(item.get("evidence"), list) or not all(isinstance(v, str) for v in item["evidence"])
                or not isinstance(item.get("tags"), list) or not all(isinstance(v, str) for v in item["tags"])):
            raise ValueError("Lesson candidate fields are incomplete.")
    return {"stage_summary": value["stage_summary"], "lessons_candidates": lessons}



def summarize_record(record: dict, *, transport=None) -> dict:
    instructions = load_prompt("harness/render_summary.md")
    response = asyncio.run(request_gpt(
        model=RENDER_MODEL, base_url=RENDER_OPENAI_BASE_URL, auth_file=RENDER_OPENAI_AUTH_FILE,
        instructions=instructions,
        content=[{"type": "input_text", "text": json.dumps(record, ensure_ascii=False, allow_nan=False)}],
        response_schema=SUMMARY_SCHEMA, schema_name="render_summary_v1", validate=validate_summary,
        reasoning_effort=RENDER_REASONING_EFFORT, connect_timeout_seconds=RENDER_CONNECT_TIMEOUT_SECONDS,
        max_output_tokens=RENDER_MAX_OUTPUT_TOKENS, max_retries=0, transport=transport))
    return validate_summary(response.data)


def save_summary(record: dict, summary: dict, state: dict, destination: Path) -> Path:
    document = {**record, **_clean(validate_summary(summary), state)}
    validate_summary(document)
    destination.parent.mkdir(parents=True, exist_ok=True)
    save_json(destination, document)
    return destination


def summary_matches(record: dict, destination: Path) -> bool:
    try:
        document = json.loads(destination.read_text(encoding="utf-8-sig"))
        validate_summary(document)
        return all(document.get(key) == value for key, value in record.items()
                   if key not in {"stage_summary", "lessons_candidates"})
    except (OSError, ValueError, TypeError):
        return False


def summary_ready(state: dict) -> bool:
    if not state.get("confirmed") or (state.get("harness_export") or {}).get("status") != "completed":
        return False
    try:
        path = Path(state["initial_output"]).parent / SUMMARY_FILENAME
        if state["harness_export"].get("file") != path.relative_to(path.parent.parent).as_posix():
            return False
        return summary_matches(collect_record(state), path)
    except (OSError, ValueError, KeyError, TypeError):
        return False
