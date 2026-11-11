from __future__ import annotations

import asyncio
import hashlib
import json
import math
from pathlib import Path
import re

from config import (BLENDER_SUMMARY_MODEL, BLENDER_OPENAI_BASE_URL, BLENDER_OPENAI_AUTH_FILE,
                    BLENDER_SUMMARY_REASONING_EFFORT, BLENDER_SUMMARY_CONNECT_TIMEOUT_SECONDS,
                    BLENDER_SUMMARY_MAX_OUTPUT_TOKENS, BLENDER_SUMMARY_MAX_INPUT_CHARS)
from input_pipeline.common import now, read_json, save_json
from utils.prompts import load_prompt
from utils.gpt_client import request_gpt
from .history import STAGES, load_run, local_path, artifact, verify_artifact
from .common import hash_path, log_progress

SUMMARY_FILENAME = "blender_stage_summary.json"


def _object(properties):
    return {"type": "object", "additionalProperties": False,
            "required": list(properties), "properties": properties}


TEXT = {"type": "string"}
STRINGS = {"type": "array", "items": TEXT}
CHANGE = _object({"reason": TEXT, "modification": TEXT, "result": TEXT,
                  "checks": STRINGS, "unresolved": STRINGS, "evidence": STRINGS})
LESSON = _object({"kind": {"type": "string", "enum": ["general", "specific"]},
                  "rule": TEXT, "applies_when": TEXT,
                  "evidence": STRINGS, "tags": STRINGS})
SUMMARY_SCHEMA = _object({
    "stage_summary": TEXT,
    "round_summaries": {"type": "array", "items": _object({
        "round_id": TEXT, "summary": TEXT,
        "changes": {"type": "array", "items": CHANGE}})},
    "stage_summaries": {"type": "array", "items": _object({"stage": TEXT, "summary": TEXT})},
    "limitations": STRINGS,
    "lessons_candidates": {"type": "array", "items": LESSON},
})


def collect_record(task_dir):
    task_dir = Path(task_dir).resolve()
    state = load_run(task_dir)
    final = state["final"]
    for key in ("blend", "video", "preview"):
        verify_artifact(task_dir, final[key])
    if final.get("rendered_blend_sha256") != final["blend"]["sha256"]:
        raise ValueError("The video does not match the final project")
    inputs = read_json(task_dir / "input/input.json")
    if not isinstance(inputs, dict):
        raise ValueError("input.json must contain an object")
    if state.get("input_fingerprints", {}).get("input") not in (None, hash_path(task_dir / "input/input.json")):
        raise ValueError("The Input handoff file was modified after Blender ran")
    process, evidence, retrieved_experiences = [], {}, []
    retrieved_ids = set()
    for stage in STAGES:
        rows = []
        for row in state["rounds"]:
            if row["stage"] != stage:
                continue
            item = {key: row.get(key) for key in
                    ("id", "round", "status", "previous_round", "restart_of", "feedback",
                     "decision", "decisions", "started_at", "completed_at", "error", "artifacts")}
            if "retrieved_experiences" in row:
                item["retrieved_experiences"] = row["retrieved_experiences"]
            for experience in row.get("retrieved_experiences") or []:
                if isinstance(experience, dict) and experience.get("id") and experience["id"] not in retrieved_ids:
                    retrieved_experiences.append(experience)
                    retrieved_ids.add(experience["id"])
            if row.get("completed_at"):
                for ref in row["artifacts"].values():
                    verify_artifact(task_dir, ref)
            folder = local_path(task_dir, row["folder"])
            for path in sorted(folder.rglob("*")):
                if path.is_file() and path.suffix.lower() not in {".blend", ".blend1", ".blend2"}:
                    if not path.resolve().is_relative_to(folder.resolve()):
                        raise ValueError("An evidence reference for this round is outside the allowed scope")
                    evidence[row["id"] + "/" + path.relative_to(folder).as_posix()] = artifact(task_dir, path)
            rows.append(item)
        accepted = state["accepted"].get(stage)
        process.append({"stage": stage, "accepted_blend": accepted, "rounds": rows})
    for row in state["renders"]:
        log = row.get("log")
        if log and local_path(task_dir, log).is_file():
            evidence[row["id"] + "/log"] = artifact(task_dir, local_path(task_dir, log))
    record = {
        "stage": "blender", "task_id": task_dir.name, "input_type": state.get("input_type"),
        "input": inputs,
        "output": {"blend_file": final["blend"]["file"], "white_model_video": final["video"]["file"],
                   "preview_image": final["preview"]["file"], "video_metadata": final["video_metadata"],
                   "sha256": {key: final[key]["sha256"] for key in ("blend", "video", "preview")}},
        "process": process, "rendering": state["renders"], "evidence_index": evidence,
        "history_complete": all(p["accepted_blend"] and p["rounds"] for p in process)
                            and not state.get("imported_history"),
        "record_limitations": state.get("record_limitations", []),
    }
    if retrieved_experiences:
        record["retrieved_experiences"] = retrieved_experiences
    record["source_fingerprint"] = hashlib.sha256(
        json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return record


def _clean(value):
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()
                if not re.search(r"api.?key|authorization|headers|password|secret|token", k, re.I)}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if not isinstance(value, str):
        return value
    value = re.sub(r"data:[^\s]+", "[media data omitted]", value)
    value = re.sub(r"(?i)bearer\s+[^\s\"']+|sk-[A-Za-z0-9_-]+", "[credentials omitted]", value)
    value = re.sub(r"(?i)((?:api[_-]?key|authorization|password|secret)\s*[\"']?\s*[:=]\s*[\"']?)[^\s\"',;]+",
                   r"\1[credentials omitted]", value)
    value = re.sub(r"https?://[^\s\"<>]+", "[URL omitted]", value)
    return re.sub(r"[A-Za-z]:[\\/][^\r\n\"<>]*", "[local path omitted]", value)


def _excerpt(text, limit):
    if len(text) <= limit:
        return text
    half = max(1, (limit-80)//2)
    return text[:half] + f"\n[{len(text)-2*half} characters omitted here; full content is preserved in local evidence]\n" + text[-half:]


def build_context(record, task_dir):
    rounds = [r for s in record["process"] for r in s["rounds"]]
    budget = max(800, min(12000, BLENDER_SUMMARY_MAX_INPUT_CHARS // max(1, len(rounds)) // 2))
    rows = []
    state_rows = {r["id"]: r for r in load_run(task_dir)["rounds"]}
    for row in rounds:
        state_row = state_rows[row["id"]]
        folder = local_path(task_dir, state_row["folder"])
        sources = []
        report = folder / "report.json"
        if report.exists():
            sources.append({"evidence": row["id"] + "/report.json",
                            "text": _excerpt(report.read_text(encoding="utf-8"), budget)})
        events = folder / "events.jsonl"
        operations = []
        if events.exists():
            with events.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        operations.append({"event": "truncated_record"})
                        continue
                    if event["event"] in {"item.completed", "round.interrupted", "round.completed"}:
                        operations.append(event)
        per_event = max(120, budget // max(1, len(operations)))
        if operations:
            sources.append({"evidence": row["id"] + "/events.jsonl",
                            "events": [{"id": e.get("id"), "event": e["event"],
                                        "data": _excerpt(json.dumps(_clean(e.get("data")), ensure_ascii=False), per_event)}
                                       for e in operations]})
        rows.append({**row, "sources": sources})
    context = _clean({"stage": "blender", "input": record["input"], "rounds": rows,
                      "rendering": record["rendering"], "output": record["output"]["video_metadata"],
                      "history_complete": record["history_complete"],
                      "record_limitations": record["record_limitations"],
                      "evidence_ids": list(record["evidence_index"])})
    text = json.dumps(context, ensure_ascii=False, allow_nan=False)
    if len(text) > BLENDER_SUMMARY_MAX_INPUT_CHARS:
        raise ValueError("Round records exceed the summary budget. No rounds were discarded; increase BLENDER_SUMMARY_MAX_INPUT_CHARS")
    return text


def _check_shape(value, schema, label="summary"):
    kind = schema["type"]
    if kind == "object":
        if not isinstance(value, dict) or set(value) != set(schema["properties"]):
            raise ValueError(label + " has missing or unknown fields")
        for key, child in schema["properties"].items():
            _check_shape(value[key], child, label+"."+key)
    elif kind == "array":
        if not isinstance(value, list):
            raise ValueError(label + " must be a list")
        for child in value:
            _check_shape(child, schema["items"], label)
    elif kind == "string":
        if not isinstance(value, str):
            raise ValueError(label + " must be text")
    elif kind == "number":
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(label + " must be a finite number")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(label + " has an invalid value")


def validate_summary(value, record):
    _check_shape(value, SUMMARY_SCHEMA)
    rows = {r["id"] for s in record["process"] for r in s["rounds"]}
    ids = [r["round_id"] for r in value["round_summaries"]]
    stages = [s["stage"] for s in value["stage_summaries"]]
    if len(ids) != len(set(ids)) or set(ids) != rows:
        raise ValueError("The summary contains missing, duplicate, or invented rounds")
    if len(stages) != 3 or set(stages) != set(STAGES):
        raise ValueError("The summary must cover all three modeling stages")
    evidence = set(record["evidence_index"]) | rows | {r["id"] for r in record["rendering"]}
    if not value["stage_summary"].strip():
        raise ValueError("The stage summary is empty")
    for item in value["round_summaries"]:
        if not item["summary"].strip():
            raise ValueError("The round summary is empty")
        for change in item["changes"]:
            if not change["modification"].strip() or not change["evidence"] or not set(change["evidence"]) <= evidence:
                raise ValueError("A change is missing content or valid evidence")
    if len(value["lessons_candidates"]) > 5:
        raise ValueError("At most 5 lesson candidates are allowed")
    for lesson in value["lessons_candidates"]:
        if (not lesson["rule"].strip() or not lesson["applies_when"].strip()
                or not lesson["evidence"] or not set(lesson["evidence"]) <= evidence):
            raise ValueError("Invalid lesson fields or evidence references")
    return value


def summarize_record(record, task_dir, *, transport=None):
    instructions = load_prompt("harness/blender_summary.md")
    response = asyncio.run(request_gpt(
        model=BLENDER_SUMMARY_MODEL, base_url=BLENDER_OPENAI_BASE_URL,
        auth_file=BLENDER_OPENAI_AUTH_FILE, instructions=instructions,
        content=[{"type": "input_text", "text": build_context(record, task_dir)}],
        response_schema=SUMMARY_SCHEMA, schema_name="blender_stage_summary",
        validate=lambda v: validate_summary(v, record),
        reasoning_effort=BLENDER_SUMMARY_REASONING_EFFORT,
        connect_timeout_seconds=BLENDER_SUMMARY_CONNECT_TIMEOUT_SECONDS,
        on_progress=log_progress,
        max_output_tokens=BLENDER_SUMMARY_MAX_OUTPUT_TOKENS, max_retries=0, transport=transport))
    return validate_summary(response.data, record)


def save_summary(record, summary, destination):
    summary = validate_summary(_clean(summary), record)
    rounds = {r["round_id"]: r for r in summary["round_summaries"]}
    stages = {s["stage"]: s["summary"] for s in summary["stage_summaries"]}
    process = [{**s, "summary": stages[s["stage"]],
                "rounds": [{**r, "summary": rounds[r["id"]]["summary"],
                            "changes": rounds[r["id"]]["changes"]} for r in s["rounds"]]}
               for s in record["process"]]
    document = {**record, "process": process, "stage_summary": summary["stage_summary"],
                "limitations": summary["limitations"], "lessons_candidates": summary["lessons_candidates"],
                "summary_model": BLENDER_SUMMARY_MODEL, "summarized_at": now()}
    save_json(Path(destination), document)
    return Path(destination)


def summary_matches(record, destination):
    try:
        doc = read_json(destination)
        if (doc["source_fingerprint"] != record["source_fingerprint"]
                or doc["input"] != record["input"] or doc["output"] != record["output"]):
            return False
        candidate = {"stage_summary": doc["stage_summary"],
                     "stage_summaries": [{"stage": s["stage"], "summary": s["summary"]} for s in doc["process"]],
                     "round_summaries": [{"round_id": r["id"], "summary": r["summary"], "changes": r["changes"]}
                                         for s in doc["process"] for r in s["rounds"]],
                     "limitations": doc["limitations"], "lessons_candidates": doc["lessons_candidates"]}
        validate_summary(candidate, record)
        for actual, expected in zip(doc["process"], record["process"]):
            if {k:v for k,v in actual.items() if k not in {"summary","rounds"}} != {k:v for k,v in expected.items() if k != "rounds"}:
                return False
            if [{k:v for k,v in r.items() if k not in {"summary","changes"}} for r in actual["rounds"]] != expected["rounds"]:
                return False
        return all(doc.get(k) == v for k,v in record.items() if k != "process")
    except (OSError, ValueError, KeyError, TypeError):
        return False
