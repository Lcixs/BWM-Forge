#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np
from utils.prompts import load_prompt
from utils.gpt_client import image_content, request_gpt
from config import (RENDER_MODEL, RENDER_OPENAI_BASE_URL, RENDER_OPENAI_AUTH_FILE,
                    RENDER_REASONING_EFFORT, RENDER_CONNECT_TIMEOUT_SECONDS, RENDER_MAX_OUTPUT_TOKENS,
                    RENDER_MAX_TOKEN_BUDGET, RENDER_MAX_RETRIES, RENDER_RETRY_BASE_DELAY,
                    RENDER_FRAME_COUNT, RENDER_SHEET_COLUMNS, RENDER_CELL_WIDTH,
                    RENDER_JPEG_QUALITY, RENDER_VLM_WEIGHT,
                    RENDER_VLM_WEIGHTS as VLM_INTERNAL_WEIGHTS,
                    RENDER_VLM_PASS_THRESHOLDS as VLM_PASS_THRESHOLDS)


VLM_METRICS = (
    "visual_realism",
    "physical_temporal_coherence",
    "task_semantic_consistency",
)

ACTION_PHASES = {"initial": "Initial state", "task_motion": "Task behavior", "final_state": "Final state"}
ACTION_LABELS = {"pass": "Passed", "fail": "Failed", "uncertain": "Needs human review"}


def normalize_action_review(value: Any) -> dict[str, Any]:
    phases = {}
    for name in ACTION_PHASES:
        item = value.get(name, {}) if isinstance(value, dict) else {}
        item = item if isinstance(item, dict) else {}
        evidence = str(item.get("evidence") or "").strip()
        times = str(item.get("timestamps") or "").strip()
        status = item.get("status")
        if not isinstance(status, str) or status not in ACTION_LABELS or not evidence or not times:
            status = "uncertain"
        phases[name] = {"status": status, "timestamps": times,
                        "evidence": evidence or "No verifiable action evidence provided"}
    statuses = [item["status"] for item in phases.values()]
    status = "fail" if "fail" in statuses else "uncertain" if "uncertain" in statuses else "pass"
    return {"status": status, "label": ACTION_LABELS[status], "phases": phases}

VLM_LABELS = {
    "visual_realism": "Visual realism",
    "physical_temporal_coherence": "Physical and temporal coherence",
    "task_semantic_consistency": "Task semantic consistency",
}

VLM_FEEDBACK = {key: value for key, value in json.loads(
    load_prompt("render/metric_feedback.json")).items() if key in VLM_METRICS}

REFERENCE_APPEARANCE_GUARD = load_prompt("render/references.md")


@dataclass(slots=True)
class VLMConfig:
    frame_count: int = RENDER_FRAME_COUNT
    sheet_columns: int = RENDER_SHEET_COLUMNS
    cell_width: int = RENDER_CELL_WIDTH
    jpeg_quality: int = RENDER_JPEG_QUALITY

    def validate(self) -> None:
        if not 4 <= self.frame_count <= 16:
            raise ValueError("frame_count must be between 4 and 16")
        if self.sheet_columns < 1 or self.cell_width < 96:
            raise ValueError("Invalid contact-sheet layout")
        if not 30 <= self.jpeg_quality <= 95:
            raise ValueError("jpeg_quality must be between 30 and 95")


def _read_uniform_frames(video_path: Path, frame_count: int) -> tuple[list[np.ndarray], list[float]]:
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise ValueError(f"Cannot open video for OpenAI VLM sampling: {video_path}")
    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = float(capture.get(cv2.CAP_PROP_FPS))
    if total < 1:
        capture.release()
        raise ValueError(f"Video contains no readable frames: {video_path}")

    indices = np.linspace(0, total - 1, min(frame_count, total), dtype=np.int64)
    frames: list[np.ndarray] = []
    times: list[float] = []
    for index in indices:
        capture.set(cv2.CAP_PROP_POS_FRAMES, int(index))
        ok, frame = capture.read()
        if not ok or frame is None:
            continue
        frames.append(frame)
        times.append(float(index / fps) if fps > 0 else float(index))
    capture.release()
    if len(frames) < 2:
        raise ValueError(f"Too few frames decoded for OpenAI VLM sampling: {video_path}")
    return frames, times


def build_contact_sheet(
    video_path: Path,
    output_path: Path,
    *,
    frame_count: int = 8,
    columns: int = 4,
    cell_width: int = 224,
    jpeg_quality: int = 78,
) -> dict[str, Any]:

    frames, times = _read_uniform_frames(Path(video_path), frame_count)
    first_height, first_width = frames[0].shape[:2]
    cell_height = int(round(cell_width * first_height / max(first_width, 1)))
    cell_height = max(96, min(cell_height, 180))
    header_height = 25
    rows = int(np.ceil(len(frames) / columns))
    sheet = np.full(
        (rows * (cell_height + header_height), columns * cell_width, 3),
        238,
        dtype=np.uint8,
    )

    for position, (frame, timestamp) in enumerate(zip(frames, times)):
        row, column = divmod(position, columns)
        x = column * cell_width
        y = row * (cell_height + header_height)
        resized = cv2.resize(frame, (cell_width, cell_height), interpolation=cv2.INTER_AREA)
        sheet[y + header_height : y + header_height + cell_height, x : x + cell_width] = resized
        cv2.putText(
            sheet,
            f"Frame {position + 1}/{len(frames)}  t={timestamp:.1f}s",
            (x + 5, y + 17),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            (25, 25, 25),
            1,
            cv2.LINE_AA,
        )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(
        ".jpg",
        sheet,
        [cv2.IMWRITE_JPEG_QUALITY, int(jpeg_quality)],
    )
    if not ok:
        raise RuntimeError(f"Failed to encode OpenAI VLM contact sheet: {output_path}")
    encoded.tofile(output_path)
    return {
        "path": str(output_path.resolve()),
        "sample_count": len(frames),
        "sample_times": times,
        "width": int(sheet.shape[1]),
        "height": int(sheet.shape[0]),
        "jpeg_quality": int(jpeg_quality),
    }


def build_action_sheets(white: Path, generated: Path, directory: Path) -> list[dict[str, Any]]:
    fractions = [0, .04, .08, .12, .18, .25, .35, .45, .58, .72, .86, 1.0]
    captures = [cv2.VideoCapture(str(path)) for path in (white, generated)]
    sheets = []
    try:
        metadata = [(int(c.get(cv2.CAP_PROP_FRAME_COUNT)), c.get(cv2.CAP_PROP_FPS)) for c in captures]
        if any(n < 2 or fps <= 0 for n, fps in metadata):
            raise ValueError("Cannot decode action comparison videos")
        for group in range(3):
            canvas = np.full((4 * 205, 640, 3), 238, np.uint8)
            pairs = []
            for row, fraction in enumerate(fractions[group * 4:group * 4 + 4]):
                times = []
                for column, (capture, (count, fps)) in enumerate(zip(captures, metadata)):
                    index = round(fraction * (count - 1))
                    capture.set(cv2.CAP_PROP_POS_FRAMES, index)
                    ok, frame = capture.read()
                    if not ok:
                        raise ValueError("Missing frame in action comparison")
                    h, w = frame.shape[:2]
                    scale = min(320 / w, 180 / h)
                    resized = cv2.resize(frame, (max(1, round(w * scale)), max(1, round(h * scale))))
                    rh, rw = resized.shape[:2]
                    x, y = column * 320 + (320 - rw) // 2, row * 205 + 25 + (180 - rh) // 2
                    canvas[y:y+rh, x:x+rw] = resized
                    times.append(round(index / fps, 3))
                    cv2.putText(canvas, f'{"WHITE" if column == 0 else "GENERATED"} t={index/fps:.2f}s',
                                (column * 320 + 4, row * 205 + 18), cv2.FONT_HERSHEY_SIMPLEX, .45, (20,20,20), 1)
                pairs.append({"progress": fraction, "white_seconds": times[0], "generated_seconds": times[1]})
            path = directory / f"vlm_action_pairs_{group + 1}.jpg"
            ok, encoded = cv2.imencode('.jpg', canvas, [cv2.IMWRITE_JPEG_QUALITY, 82])
            if not ok:
                raise RuntimeError("Cannot encode action comparison")
            encoded.tofile(path)
            sheets.append({"path": str(path.resolve()), "pairs": pairs, "alignment": "normalized_time_not_event_alignment"})
    finally:
        for capture in captures:
            capture.release()
    return sheets


def prepare_reference_image(
    source_path: Path,
    output_path: Path,
    *,
    max_side: int = 640,
    jpeg_quality: int = 82,
) -> dict[str, Any]:

    source_path = Path(source_path)
    if not source_path.is_file():
        raise FileNotFoundError(f"Appearance reference image not found: {source_path}")
    encoded = np.fromfile(source_path, dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"Cannot decode appearance reference image: {source_path}")
    height, width = image.shape[:2]
    scale = min(1.0, float(max_side) / max(height, width))
    if scale < 1.0:
        image = cv2.resize(
            image,
            (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
            interpolation=cv2.INTER_AREA,
        )
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ok, output = cv2.imencode(
        ".jpg",
        image,
        [cv2.IMWRITE_JPEG_QUALITY, int(jpeg_quality)],
    )
    if not ok:
        raise RuntimeError(f"Failed to encode appearance reference image: {output_path}")
    output.tofile(output_path)
    return {
        "source_path": str(source_path.resolve()),
        "path": str(output_path.resolve()),
        "width": int(image.shape[1]),
        "height": int(image.shape[0]),
        "jpeg_quality": int(jpeg_quality),
    }


def _color_signature(image: np.ndarray) -> dict[str, float]:

    resized = cv2.resize(image, (256, 256), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(resized, cv2.COLOR_BGR2HSV).astype(np.float32)
    lab = cv2.cvtColor(resized, cv2.COLOR_BGR2LAB).astype(np.float32)
    saturation = hsv[:, :, 1] / 255.0
    return {
        "mean_saturation": round(float(np.mean(saturation)), 4),
        "p90_saturation": round(float(np.percentile(saturation, 90)), 4),
        "lightness": round(float(np.mean(lab[:, :, 0]) * 100.0 / 255.0), 3),
        "lab_a": round(float(np.mean(lab[:, :, 1]) - 128.0), 3),
        "lab_b": round(float(np.mean(lab[:, :, 2]) - 128.0), 3),
    }


def analyze_reference_appearance(
    reference_image: Path,
    generated_video: Path,
    *,
    frame_count: int = 8,
) -> dict[str, Any]:

    encoded = np.fromfile(Path(reference_image), dtype=np.uint8)
    reference = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if reference is None:
        raise ValueError(f"Cannot decode appearance reference image: {reference_image}")
    frames, _ = _read_uniform_frames(Path(generated_video), frame_count)
    reference_signature = _color_signature(reference)
    frame_signatures = [_color_signature(frame) for frame in frames]
    generated_signature = {
        key: round(float(np.median([item[key] for item in frame_signatures])), 4)
        for key in reference_signature
    }
    deltas = {
        key: round(generated_signature[key] - reference_signature[key], 4)
        for key in reference_signature
    }
    warnings: list[str] = []
    if abs(deltas["mean_saturation"]) >= 0.10:
        warnings.append(
            "generated video is more saturated than the reference"
            if deltas["mean_saturation"] > 0
            else "generated video is less saturated than the reference"
        )
    if abs(deltas["lab_b"]) >= 6.0:
        warnings.append(
            "generated video has a stronger yellow/warm cast than the reference"
            if deltas["lab_b"] > 0
            else "generated video has a stronger blue/cool cast than the reference"
        )
    if abs(deltas["lab_a"]) >= 6.0:
        warnings.append(
            "generated video has a stronger red/magenta cast than the reference"
            if deltas["lab_a"] > 0
            else "generated video has a stronger green cast than the reference"
        )
    if abs(deltas["lightness"]) >= 15.0:
        warnings.append(
            "generated video is substantially brighter than the reference"
            if deltas["lightness"] > 0
            else "generated video is substantially darker than the reference"
        )
    return {
        "reference": reference_signature,
        "generated_median": generated_signature,
        "generated_minus_reference": deltas,
        "warnings": warnings,
        "note": "Global color statistics are supporting evidence only; differences in scene content may affect the results.",
    }


def build_review_request(
    task_prompt: str,
    white_sheet: Path,
    generated_sheet: Path,
    objective_scores: dict[str, float] | None = None,
    reference_image: Path | None = None,
    reference_appearance: dict[str, Any] | None = None,
    action_sheets: list[dict[str, Any]] | None = None,
) -> tuple[str, list[dict], dict]:
    rubric = load_prompt("render/review.md", task_prompt=task_prompt.strip(),
                         objective_scores=json.dumps(objective_scores or {}, ensure_ascii=False),
                         appearance_statistics=json.dumps(reference_appearance or {}, ensure_ascii=False))
    metric_schema = {
        "type": "object",
        "properties": {
            "score": {"type": "integer", "minimum": 0, "maximum": 100},
            "evidence": {"type": "string"},
            "correction": {"type": "string"},
        },
        "required": ["score", "evidence", "correction"],
        "additionalProperties": False,
    }
    response_schema = {
        "type": "object",
        "properties": {
            "visual_realism": metric_schema,
            "physical_temporal_coherence": metric_schema,
            "task_semantic_consistency": metric_schema,
            "summary": {"type": "string"},
            "seedance_correction_prompt": {"type": "string"},
        },
        "required": [
            "visual_realism",
            "physical_temporal_coherence",
            "task_semantic_consistency",
            "summary",
            "seedance_correction_prompt",
        ],
        "additionalProperties": False,
    }
    phase_schema = {"type": "object", "properties": {
        "status": {"type": "string", "enum": ["pass", "fail", "uncertain"]},
        "timestamps": {"type": "string"}, "evidence": {"type": "string"}},
        "required": ["status", "timestamps", "evidence"], "additionalProperties": False}
    response_schema["properties"]["action_review"] = {
        "type": "object", "properties": {name: phase_schema for name in ACTION_PHASES},
        "required": list(ACTION_PHASES), "additionalProperties": False}
    response_schema["required"].append("action_review")
    content: list[dict[str, Any]] = [{"type": "input_text", "text": rubric}]
    if reference_image is not None:
        content.extend([
            {"type": "input_text", "text": "Scoped appearance / source first-frame reference:"},
            image_content(reference_image, detail="low"),
        ])
    content.extend([
        {"type": "input_text", "text": "Chronological white-model video samples:"},
        image_content(white_sheet, detail="low"),
        {"type": "input_text", "text": "Chronological generated video samples:"},
        image_content(generated_sheet, detail="low"),
    ])
    for sheet in action_sheets or []:
        content.extend([
            {"type": "input_text", "text": "White model left, generated right; relative-time mapping:" + " " + json.dumps(sheet["pairs"])},
            image_content(Path(sheet["path"]), detail="high"),
        ])
    instructions = "\n\n".join((load_prompt("render/review_instructions.md"),
                                    REFERENCE_APPEARANCE_GUARD))
    return instructions, content, response_schema


def _normalize_vlm_response(value: dict[str, Any]) -> tuple[dict[str, float], dict[str, Any]]:
    scores: dict[str, float] = {}
    details: dict[str, Any] = {}
    for metric in VLM_METRICS:
        item = value.get(metric)
        if not isinstance(item, dict):
            raise RuntimeError(f"OpenAI VLM response is missing object field: {metric}")
        try:
            score = float(item.get("score"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"OpenAI VLM returned an invalid score for {metric}") from exc
        scores[metric] = round(max(0.0, min(100.0, score)), 4)
        details[metric] = {
            "score": scores[metric],
            "evidence": str(item.get("evidence") or "No evidence provided").strip()[:1000],
            "correction": str(item.get("correction") or "").strip()[:1000],
        }
    details["summary"] = str(value.get("summary") or "").strip()[:1500]
    details["action_review"] = normalize_action_review(value.get("action_review"))
    details["seedance_correction_prompt"] = str(
        value.get("seedance_correction_prompt") or ""
    ).strip()[:2000]
    return scores, details


def evaluate_openai_vlm(
    white_model_video: Path,
    generated_video: Path,
    task_prompt: str,
    output_dir: Path,
    *,
    config: VLMConfig | None = None,
    objective_scores: dict[str, float] | None = None,
    transport=None,
    on_retry: Callable[[str], None] | None = None,
    reference_image: Path | None = None,
) -> dict[str, Any]:

    config = config or VLMConfig()
    config.validate()
    if not task_prompt.strip():
        raise ValueError("task_prompt cannot be empty for OpenAI VLM evaluation")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    white_sheet = output_dir / "vlm_white_contact_sheet.jpg"
    generated_sheet = output_dir / "vlm_generated_contact_sheet.jpg"
    prepared_reference = output_dir / "vlm_reference_image.jpg"
    white_sampling = build_contact_sheet(
        white_model_video,
        white_sheet,
        frame_count=config.frame_count,
        columns=config.sheet_columns,
        cell_width=config.cell_width,
        jpeg_quality=config.jpeg_quality,
    )
    generated_sampling = build_contact_sheet(
        generated_video,
        generated_sheet,
        frame_count=config.frame_count,
        columns=config.sheet_columns,
        cell_width=config.cell_width,
        jpeg_quality=config.jpeg_quality,
    )
    reference_sampling: dict[str, Any] | None = None
    reference_appearance: dict[str, Any] | None = None
    if reference_image is not None:
        reference_sampling = prepare_reference_image(
            Path(reference_image),
            prepared_reference,
            jpeg_quality=config.jpeg_quality,
        )
        reference_appearance = analyze_reference_appearance(
            Path(reference_image),
            generated_video,
            frame_count=config.frame_count,
        )
    action_sheets = build_action_sheets(white_model_video, generated_video, output_dir)
    instructions, content, schema = build_review_request(
        task_prompt,
        white_sheet,
        generated_sheet,
        objective_scores=objective_scores,
        reference_image=prepared_reference if reference_sampling is not None else None,
        reference_appearance=reference_appearance,
        action_sheets=action_sheets,
    )
    diagnostics: list[dict[str, Any]] = []

    def save_diagnostic(item: dict[str, Any]) -> None:
        diagnostics.append(item)
        (output_dir / "openai_vlm_diagnostics.json").write_text(
            json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    response = asyncio.run(request_gpt(
        model=RENDER_MODEL, base_url=RENDER_OPENAI_BASE_URL, auth_file=RENDER_OPENAI_AUTH_FILE,
        instructions=instructions, content=content,
        response_schema=schema, schema_name="robot_video_quality_review",
        validate=_normalize_vlm_response,
        reasoning_effort=RENDER_REASONING_EFFORT, connect_timeout_seconds=RENDER_CONNECT_TIMEOUT_SECONDS,
        max_output_tokens=RENDER_MAX_OUTPUT_TOKENS, max_token_budget=RENDER_MAX_TOKEN_BUDGET,
        max_retries=RENDER_MAX_RETRIES, retry_base_delay=RENDER_RETRY_BASE_DELAY,
        on_retry=on_retry, on_diagnostic=save_diagnostic, transport=transport))
    scores, details = _normalize_vlm_response(response.data)

    result = {
        "schema_version": "1.0-openai-vlm",
        "model": response.model,
        "base_url": RENDER_OPENAI_BASE_URL,
        "metric_count": len(VLM_METRICS),
        "scores": scores,
        "details": details,
        "seedance_correction_prompt": details["seedance_correction_prompt"],
        "reference_image_used": reference_sampling is not None,
        "reference_appearance": reference_appearance,
        "usage": response.usage,
        "sampling": {
            "action_pairs": action_sheets,
            "white_model": white_sampling,
            "generated": generated_sampling,
            "appearance_reference": reference_sampling,
        },
        "artifacts": {
            "white_contact_sheet": str(white_sheet.resolve()),
            "generated_contact_sheet": str(generated_sheet.resolve()),
            **(
                {"reference_image": str(prepared_reference.resolve())}
                if reference_sampling is not None
                else {}
            ),
            "review": str((output_dir / "openai_vlm_review.json").resolve()),
        },
    }
    (output_dir / "openai_vlm_review.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return result


def _combined_decision(
    objective: dict[str, Any],
    vlm_scores: dict[str, float],
    overall_score: float,
) -> tuple[str, list[str]]:
    config = objective["config"]
    if objective["decision"] == "REJECT":
        return "REJECT", [f"objective:{reason}" for reason in objective["decision_reasons"]]
    retry_reasons: list[str] = []
    if objective["decision"] == "RETRY":
        retry_reasons.extend(f"objective:{reason}" for reason in objective["decision_reasons"])
    if overall_score < float(config["pass_score"]):
        retry_reasons.append(f"overall_score<{config['pass_score']:g}")
    low_vlm = [
        metric
        for metric, score in vlm_scores.items()
        if score < VLM_PASS_THRESHOLDS[metric]
    ]
    if low_vlm:
        retry_reasons.extend(
            f"{metric}<{VLM_PASS_THRESHOLDS[metric]:g}" for metric in low_vlm
        )
    if retry_reasons:
        return "RETRY", list(dict.fromkeys(retry_reasons))
    return "PASS", ["All objective and OpenAI VLM acceptance conditions passed"]


def _sanitize_seedance_correction(value: Any) -> str:
    return _filter_seedance_correction(value)[0]


_UNSAFE_STYLE_TERMS = (
    "warm lighting",
    "warmer lighting",
    "warm indoor",
    "golden lighting",
    "golden tone",
    "orange tint",
    "yellow tint",
    "orange cast",
    "yellow cast",
    "warm cast",
    "warm color",
    "warm colour",
    "cinematic lighting",
    "cinematic grading",
    "cinematic color",
    "colour grading",
    "color grading",
    "more saturated",
    "higher saturation",
    "boost saturation",
    "increase saturation",
    "enhance saturation",
    "vibrant color",
    "vibrant colour",
    "richer color",
    "richer colour",
    "saturated palette",
    "rich color palette",
    "rich colour palette",
    "make colors pop",
    "make colours pop",
    "punchier color",
    "punchier colour",
    "filter effect",
)

_SAFE_STYLE_SENTENCE_START = re.compile(
    r"^(?:do not|don't|never|avoid|without|remove|reduce|lower|decrease|correct|"
    r"neutralize|neutralise|prevent|match(?:es|ing)?\s+(?:the\s+)?reference|"
    r"preserve\s+(?:the\s+)?reference)\b",
    re.IGNORECASE,
)

_UNSAFE_STYLE_ACTION = re.compile(
    r"\b(?:increase|boost|enhance|raise|intensify|amplify)\b.{0,40}"
    r"\b(?:saturation|color|colour|warmth|vibrancy)\b|"
    r"\b(?:make|render)\b.{0,30}\b(?:warmer|more saturated|more vibrant|richer)\b|"
    r"\b(?:use|add|apply|introduce)\b.{0,30}"
    r"\b(?:warm|golden|cinematic|filter|grading|vibrant)\b",
    re.IGNORECASE,
)

_MIXED_POSITIVE_STYLE_ACTION = re.compile(
    r"\b(?:and|but|then)\s+(?:use|add|apply|introduce|increase|boost|enhance|raise|"
    r"intensify|amplify|make|render)\b.{0,40}"
    r"\b(?:warm|warmer|golden|cinematic|filter|grading|vibrant|saturation|"
    r"saturated|color|colour|warmth|vibrancy|richer)\b",
    re.IGNORECASE,
)

_WHITE_MODEL_APPEARANCE_INSTRUCTION = re.compile(
    r"\b(?:white[- ]model|clay model|reference video)\b.{0,140}"
    r"\b(?:appearance|material|texture|color|colour|lighting|object count|object inventory|"
    r"object placement|scene layout|background|props?)\b",
    re.IGNORECASE,
)

_SCENE_INVENTORY_LOCK = re.compile(
    r"\b(?:object count|object inventory|object locations?|scene layout)\b",
    re.IGNORECASE,
)


def _is_unsafe_style_sentence(sentence: str) -> bool:
    chinese_clauses = re.split(r"[，,。；;！？!?]|但是|但|并且|同时", sentence)
    for clause in chinese_clauses:
        clause = clause.strip()
        unsafe = bool(re.search(r"(?:提高|提升|增强|增加|加大|拉高).{0,8}(?:饱和度|鲜艳度|暖色)|(?:添加|加入|增加|使用|采用|应用).{0,8}(?:暖光|暖色|金色|电影调色|滤镜)|(?:继承|复制|采用|匹配).{0,8}白模.{0,12}(?:材质|外观|颜色|纹理|光照)", clause))
        safe = bool(re.match(r"^(?:请)?(?:不要|不得|禁止|避免|不应|不能|切勿|无需|不增加|不提高|不使用|不添加)", clause))
        if unsafe and not safe:
            return True
    if (
        _WHITE_MODEL_APPEARANCE_INSTRUCTION.search(sentence)
        or _SCENE_INVENTORY_LOCK.search(sentence)
    ):
        return True
    lowered = sentence.lower().strip()
    mentions_unsafe_style = any(term in lowered for term in _UNSAFE_STYLE_TERMS)
    mentions_unsafe_action = bool(_UNSAFE_STYLE_ACTION.search(sentence))
    if not (mentions_unsafe_style or mentions_unsafe_action):
        return False
    starts_with_safety_instruction = bool(_SAFE_STYLE_SENTENCE_START.search(sentence.strip()))
    if starts_with_safety_instruction:
        return bool(_MIXED_POSITIVE_STYLE_ACTION.search(sentence))
    return True


def _filter_seedance_correction(value: Any) -> tuple[str, list[str]]:

    text = str(value or "").replace("```", "").replace("\x00", " ").strip()
    normalized = " ".join(text.split())
    if not normalized:
        return "", []
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[。！？；])|(?<=[.!?;])\s+|\n+", normalized)
        if sentence.strip()
    ]
    accepted: list[str] = []
    rejected: list[str] = []
    for sentence in sentences:
        if _is_unsafe_style_sentence(sentence):
            rejected.append(sentence)
        else:
            accepted.append(sentence)
    return "\n".join(accepted)[:2000].strip(), rejected


def merge_objective_and_vlm(
    objective: dict[str, Any],
    vlm: dict[str, Any],
    output_dir: Path,
    *,
    vlm_weight: float = RENDER_VLM_WEIGHT,
) -> dict[str, Any]:

    if not 0.0 <= vlm_weight <= 1.0:
        raise ValueError("vlm_weight must be between 0 and 1")
    output_dir = Path(output_dir)
    combined = copy.deepcopy(objective)
    vlm_scores = {metric: float(vlm["scores"][metric]) for metric in VLM_METRICS}
    combined_scores = {**objective["scores"], **vlm_scores}
    vlm_score = sum(VLM_INTERNAL_WEIGHTS[metric] * vlm_scores[metric] for metric in VLM_METRICS)
    objective_score = float(objective["aggregates"]["objective_score"])
    overall_score = (1.0 - vlm_weight) * objective_score + vlm_weight * vlm_score
    decision, reasons = _combined_decision(objective, vlm_scores, overall_score)
    action = normalize_action_review(vlm.get("details", {}).get("action_review", {}).get("phases"))
    if action["status"] == "fail":
        decision = "REJECT"
        reasons = [*reasons, "action_review:fail"]
    elif action["status"] == "uncertain":
        if decision != "REJECT":
            decision = "RETRY"
        reasons = [*reasons, "action_review:manual_confirmation_required"]
    combined["action_review"] = action

    config = objective["config"]
    vlm_findings: list[dict[str, str]] = []
    for metric in VLM_METRICS:
        score = vlm_scores[metric]
        threshold = VLM_PASS_THRESHOLDS[metric]
        if score >= threshold:
            continue
        severity = "major" if score < max(35.0, threshold - 20.0) else "minor"
        vlm_findings.append(
            {
                "category": metric,
                "severity": severity,
                "evidence": str(vlm["details"][metric]["evidence"]),
                "suggested_prompt": VLM_FEEDBACK[metric],
            }
        )

    from .evaluation import METRIC_FEEDBACK, OBJECTIVE_INTERNAL_WEIGHTS

    objective_effective_weights = {
        metric: (1.0 - vlm_weight) * weight
        for metric, weight in OBJECTIVE_INTERNAL_WEIGHTS.items()
    }
    vlm_effective_weights = {
        metric: vlm_weight * weight for metric, weight in VLM_INTERNAL_WEIGHTS.items()
    }
    effective_weights = {**objective_effective_weights, **vlm_effective_weights}

    combined["schema_version"] = "3.2-action-gated-openai-vlm"
    combined["evaluator_mode"] = "objective_plus_openai_vlm"
    combined["objective_metric_count"] = len(objective["scores"])
    combined["vlm_metric_count"] = len(VLM_METRICS)
    combined["total_metric_count"] = len(combined_scores)
    combined["scores"] = combined_scores
    combined["weights"] = {
        "objective_share": round(1.0 - vlm_weight, 4),
        "vlm_share": round(vlm_weight, 4),
        "objective_internal": OBJECTIVE_INTERNAL_WEIGHTS,
        "vlm_internal": VLM_INTERNAL_WEIGHTS,
        "effective_overall": effective_weights,
    }
    combined["aggregates"]["vlm_score"] = round(vlm_score, 4)
    combined["aggregates"]["objective_weight"] = round(1.0 - vlm_weight, 4)
    combined["aggregates"]["vlm_weight"] = round(vlm_weight, 4)
    combined["aggregates"]["overall_score"] = round(overall_score, 4)
    combined["decision"] = decision
    combined["decision_reasons"] = reasons
    combined["findings"] = [*objective["findings"], *vlm_findings]
    combined["vlm"] = vlm
    combined["artifacts"].update(vlm["artifacts"])

    feedback_path = output_dir / "feedback_prompt.txt"
    feedback_map = {**METRIC_FEEDBACK, **VLM_FEEDBACK}
    feedback_thresholds = {
        metric: float(config["pass_score"]) for metric in OBJECTIVE_INTERNAL_WEIGHTS
    }
    feedback_thresholds["motion_fidelity"] = float(config["motion_pass_score"])
    feedback_thresholds.update(VLM_PASS_THRESHOLDS)
    feedback_priorities = []
    for metric, score in combined_scores.items():
        threshold = feedback_thresholds[metric]
        if score >= threshold or metric not in feedback_map:
            continue
        normalized_deficit = (threshold - score) / max(threshold, 1.0)
        feedback_priorities.append(
            {
                "metric": metric,
                "score": round(float(score), 4),
                "target": round(threshold, 4),
                "priority": round(effective_weights[metric] * normalized_deficit, 6),
            }
        )
    feedback_priorities.sort(key=lambda item: item["priority"], reverse=True)
    selected_metrics = [item["metric"] for item in feedback_priorities[:3]]
    combined["feedback_selection"] = feedback_priorities[:3]
    raw_dynamic_correction = (
        vlm.get("seedance_correction_prompt")
        or vlm.get("details", {}).get("seedance_correction_prompt")
    )
    dynamic_correction, rejected_style_instructions = _filter_seedance_correction(
        raw_dynamic_correction
    )
    if dynamic_correction:
        targeted_feedback = [dynamic_correction]
        feedback_source = "gpt_dynamic"
    else:
        targeted_feedback = [feedback_map[metric] for metric in selected_metrics]
        feedback_source = "fixed_fallback"
    if vlm.get("reference_image_used"):
        targeted_feedback.append(REFERENCE_APPEARANCE_GUARD)
    feedback_lines = [str(objective["inputs"]["task_prompt"]).strip(),
                      json.loads(load_prompt("render/metric_feedback.json"))["preserve"],
                      *targeted_feedback]
    combined["feedback_source"] = feedback_source
    combined["gpt_seedance_correction_prompt_raw"] = str(raw_dynamic_correction or "").strip()
    combined["gpt_seedance_correction_prompt"] = dynamic_correction
    combined["gpt_prompt_safety_filter"] = {
        "applied": bool(rejected_style_instructions),
        "rejected_sentences": rejected_style_instructions,
    }
    feedback_path.write_text("\n".join(feedback_lines).strip() + "\n", encoding="utf-8")

    review = {
        "action_review": action,
        "evaluator_mode": combined["evaluator_mode"],
        "decision": decision,
        "decision_reasons": reasons,
        "overall_score": combined["aggregates"]["overall_score"],
        "objective_score": round(objective_score, 4),
        "vlm_score": round(vlm_score, 4),
        "weights": combined["weights"],
        "feedback_selection": combined["feedback_selection"],
        "feedback_source": feedback_source,
        "gpt_seedance_correction_prompt_raw": str(raw_dynamic_correction or "").strip(),
        "gpt_seedance_correction_prompt": dynamic_correction,
        "gpt_prompt_safety_filter": combined["gpt_prompt_safety_filter"],
        "scores": combined_scores,
        "findings": combined["findings"],
        "vlm_model": vlm["model"],
        "reference_appearance": vlm.get("reference_appearance"),
    }
    (output_dir / "metrics.json").write_text(
        json.dumps(combined, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "review.json").write_text(
        json.dumps(review, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_combined_report(output_dir / "report.md", combined)
    return combined


def _write_combined_report(path: Path, result: dict[str, Any]) -> None:
    metadata = result["metadata"]
    objective_rows = ["| Metric | Score | Final weight |", "| --- | ---: | ---: |"]
    from .evaluation import METRIC_LABELS, OBJECTIVE_METRICS

    for metric in OBJECTIVE_METRICS:
        objective_rows.append(
            f"| {METRIC_LABELS[metric]} | {result['scores'][metric]:.2f} | "
            f"{result['weights']['effective_overall'][metric]:.1%} |"
        )
    vlm_rows = [
        "| VLM metric | Score | Final weight | Pass threshold | Evidence |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for metric in VLM_METRICS:
        evidence = str(result["vlm"]["details"][metric]["evidence"]).replace("|", "\\|")
        vlm_rows.append(
            f"| {VLM_LABELS[metric]} | {result['scores'][metric]:.2f} | "
            f"{result['weights']['effective_overall'][metric]:.1%} | "
            f"{VLM_PASS_THRESHOLDS[metric]:.0f} | {evidence} |"
        )
    findings = [
        f"- **{item['severity']} / {item['category']}**: {item['evidence']}"
        for item in result["findings"]
    ] or ["- No issues fall below the current thresholds."]
    feedback_selection = [
        f"- {METRIC_LABELS.get(item['metric'], VLM_LABELS.get(item['metric'], item['metric']))}: "
        f"{item['score']:.2f} / target {item['target']:.2f}"
        for item in result["feedback_selection"]
    ] or ["- No low-scoring metrics currently need to be added to the correction prompt."]
    feedback_source_label = (
        "Generated by GPT from the current visual evidence"
        if result.get("feedback_source") == "gpt_dynamic"
        else "Local fixed-text fallback"
    )
    dynamic_prompt = result.get("gpt_seedance_correction_prompt") or "(No dynamic correction text was returned)"
    reference_appearance = result["vlm"].get("reference_appearance")
    if reference_appearance:
        appearance_warnings = reference_appearance.get("warnings") or []
        appearance_lines = [f"- {warning}" for warning in appearance_warnings]
        if not appearance_lines:
            appearance_lines = ["- No significant global saturation, brightness, or color-cast warnings were triggered."]
        appearance_summary = "\n".join(appearance_lines)
        sampling_description = (
            "GPT receives a compressed first-frame appearance reference and one chronological low-resolution contact sheet from each of the white-model and generated videos. "
            "Full videos are not uploaded. "
        )
    else:
        appearance_summary = "- No local first-frame appearance reference was provided."
        sampling_description = (
            "GPT receives one chronological low-resolution contact sheet from each of the white-model "
            "and generated videos. Full videos are not uploaded. "
        )
    content = f"""# Robot Video Quality Report

## Conclusion

- Review decision: **{result['decision']}**
- Overall score: **{result['aggregates']['overall_score']:.2f}**
- Seven-metric objective score: **{result['aggregates']['objective_score']:.2f}** (weight: {result['aggregates']['objective_weight']:.0%})
- Three-metric GPT vision score: **{result['aggregates']['vlm_score']:.2f}** (weight: {result['aggregates']['vlm_weight']:.0%})
- VLM model: `{result['vlm']['model']}` (minimum reasoning effort)
- Decision rationale: {', '.join(result['decision_reasons'])}
- Action review: **{result['action_review']['label']}** (independent gate; overall scores cannot offset action errors)

## Action Comparison by Phase

{chr(10).join(f"- {ACTION_PHASES[name]}: {ACTION_LABELS[item['status']]}; timestamps: {item['timestamps']}; evidence: {item['evidence']}" for name, item in result['action_review']['phases'].items())}

Action review uses 12 additional frame pairs, sampled more densely near the start and paired by relative time rather than exact event alignment. Unclear evidence requires human review; this is not physics-simulation validation.

## Task Prompt

{result['inputs']['task_prompt']}

## Inputs

- White-model video: `{result['inputs']['white_model_video']}`
- Generated video: `{result['inputs']['generated_video']}`
- White-model metadata: {metadata['white_model']['width']}x{metadata['white_model']['height']}, {metadata['white_model']['duration']:.3f} s
- Generated video metadata: {metadata['generated']['width']}x{metadata['generated']['height']}, {metadata['generated']['duration']:.3f} s

## Seven Objective Metrics

{chr(10).join(objective_rows)}

## Three GPT Vision Metrics

{chr(10).join(vlm_rows)}

## Supporting First-Frame Appearance Drift Checks

{appearance_summary}

## Findings

{chr(10).join(findings)}

## Correction Prompt Priorities

{chr(10).join(feedback_selection)}

## Dynamic Corrections for the Second Generation

- Source: {feedback_source_label}

```text
{dynamic_prompt}
```

## VLM Sampling Notes

{sampling_description}This low-cost method only approximates temporal relationships. VLM scores carry a lower weight and do not replace human review.
"""
    path.write_text(content, encoding="utf-8")
