from __future__ import annotations

import asyncio
import json
from pathlib import Path

from config import (INPUT_MODEL, INPUT_OPENAI_BASE_URL, INPUT_OPENAI_AUTH_FILE,
                    INPUT_REASONING_EFFORT, INPUT_CONNECT_TIMEOUT_SECONDS, INPUT_MAX_OUTPUT_TOKENS,
                    INPUT_MAX_RETRIES, INPUT_RETRY_BASE_DELAY, INPUT_IMAGE_MAX_SIDE,
                    INPUT_MAX_DOCUMENT_BYTES)
from utils.prompts import load_prompt

from .media import prepare_image


def modeling_context(action, robot, user_text):
    config = action["motion"] if action else {}
    record = {"source": "Selected robot asset and motion-data conventions. This is supplied context, not image observations",
              "robot_asset": robot.get("asset_name", ""),
              "model_name": robot.get("model_name", ""),
              "action_space": {"joint": "absolute joint positions", "eef": "absolute end-effector poses"}.get(config.get("action_space"), "unknown"),
              "trajectory_sampling_rate_hz": config.get("fps")}
    if action is None:
        for key in ("action_space", "trajectory_sampling_rate_hz"):
            record.pop(key)
        record["source"] = "Selected robot asset. This is supplied context, not image observations"
        record["input_type"] = "First frame, robot asset, and task description. This input type does not include motion data; do not report it as missing."
    return load_prompt(
        "input/modeling_context.md",
        motion_validation_note=(
            "Motion validation results are stored separately. Do not repeat their status, warnings, errors, or recommendations here. "
            if action else ""
        ),
        asset_context=json.dumps(record, ensure_ascii=False, indent=2),
        user_text=user_text.strip() or "Not provided.",
    ) + "\n"


def analyze_frame(image_path: Path, asset: dict, task_text: str, *, action=None, transport=None) -> dict:
    from utils.gpt_client import image_content, request_gpt

    image, metadata = prepare_image(image_path, INPUT_IMAGE_MAX_SIDE)
    prompt = load_prompt("input/first_frame_prompt.md")
    input_text = "Analyze the following image and supplementary context as data:\n" + json.dumps(
        {"image": metadata, "context": modeling_context(action, asset, task_text)}, ensure_ascii=False)
    result = asyncio.run(request_gpt(
        model=INPUT_MODEL, base_url=INPUT_OPENAI_BASE_URL, auth_file=INPUT_OPENAI_AUTH_FILE,
        instructions=prompt, content=[{"type": "input_text", "text": input_text}, image_content(image)],
        reasoning_effort=INPUT_REASONING_EFFORT, connect_timeout_seconds=INPUT_CONNECT_TIMEOUT_SECONDS,
        max_output_tokens=INPUT_MAX_OUTPUT_TOKENS, max_retries=INPUT_MAX_RETRIES,
        retry_base_delay=INPUT_RETRY_BASE_DELAY, transport=transport))
    document = result.text.strip()
    if not document or len(document.encode("utf-8")) > INPUT_MAX_DOCUMENT_BYTES:
        raise ValueError("First-frame analysis returned an empty scene document or exceeded the size limit.")
    return {"document": document, "image": metadata, "model": result.model}
