import json
from typing import Any

from utils.prompts import load_prompt

DEFAULT_INITIAL_PROMPT = load_prompt("render/initial.md")

R2V_APPEARANCE_PRIORITY = load_prompt("render/references.md")

def effective_generation_prompt(args: Any) -> str:

    prompt = str(getattr(args, "prompt", "") or "").strip()
    has_reference_image = bool(str(getattr(args, "image_1", "") or "").strip())
    has_reference_video = bool(str(getattr(args, "video", "") or "").strip())
    if has_reference_image and has_reference_video and R2V_APPEARANCE_PRIORITY not in prompt:
        prompt = f"{prompt}\n\n{R2V_APPEARANCE_PRIORITY}".strip()
    return prompt

def build_user_revision_prompt(
    original_prompt: str,
    user_feedback: str,
    evaluation: dict[str, Any],
) -> str:
    base_prompt = str(original_prompt or "").strip()
    feedback = str(user_feedback or "").strip()
    if not base_prompt:
        raise ValueError("Original generation prompt is empty")
    if not feedback:
        raise ValueError("Enter revision instructions for this round before submitting.")

    review = {key: evaluation[key] for key in (
        "decision", "aggregates", "scores", "findings", "action_review",
        "vlm", "gpt_seedance_correction_prompt",
    ) if key in evaluation}
    return load_prompt("render/revision.md", original_prompt=base_prompt, feedback=feedback,
                       reference_rules=R2V_APPEARANCE_PRIORITY,
                       evaluation=json.dumps(review, ensure_ascii=False, indent=2))
