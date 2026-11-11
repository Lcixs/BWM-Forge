from string import Template

from config import PROMPT_ROOT


def load_prompt(name: str, **values) -> str:
    path = PROMPT_ROOT / name
    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        raise ValueError(f"Prompt file is empty: {path}")
    return Template(text).substitute(values)
