from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from urllib.parse import urlsplit

from config import (OPENAI_BASE_URL, OPENAI_AUTH_FILE, GPT_REASONING_EFFORT,
                    GPT_CONNECT_TIMEOUT_SECONDS, GPT_MAX_OUTPUT_TOKENS, GPT_MAX_RETRIES,
                    GPT_RETRY_BASE_DELAY)


class OpenAIConfigError(ValueError):
    """不包含密钥或响应原文的配置错误。"""

@dataclass(frozen=True)
class OpenAIConfig:
    model: str
    base_url: str
    auth_file: Path
    reasoning_effort: str
    connect_timeout_seconds: float
    max_output_tokens: int
    max_retries: int
    retry_base_delay: float
    max_token_budget: int


def load_openai_config(*, model: str, base_url: str = OPENAI_BASE_URL,
                      auth_file: str | Path = OPENAI_AUTH_FILE,
                      reasoning_effort: str = GPT_REASONING_EFFORT,
                      connect_timeout_seconds: float = GPT_CONNECT_TIMEOUT_SECONDS,
                      max_output_tokens: int = GPT_MAX_OUTPUT_TOKENS,
                      max_retries: int = GPT_MAX_RETRIES,
                      retry_base_delay: float = GPT_RETRY_BASE_DELAY,
                      max_token_budget: int | None = None,
                      allow_http: bool = False) -> OpenAIConfig:
    for name, value in (("model", model), ("base_url", base_url),
                        ("reasoning_effort", reasoning_effort)):
        if not isinstance(value, str) or not value.strip():
            raise OpenAIConfigError(f"{name} must be a non-empty string.")
    url = base_url.strip().rstrip("/")
    try:
        parts = urlsplit(url)
        valid = (parts.scheme in ({"http", "https"} if allow_http else {"https"})
                 and bool(parts.hostname) and parts.username is None and parts.password is None
                 and not parts.query and not parts.fragment
                 and not any(c.isspace() or ord(c) < 32 for c in url)
                 and not parts.path.endswith(("/responses", "/chat/completions")))
        parts.port
    except ValueError:
        valid = False
    if not valid:
        raise OpenAIConfigError("base_url must be the API root URL, without credentials, query parameters, or an endpoint path.")
    for name, value, minimum in (("max_output_tokens", max_output_tokens, 1),
                                 ("max_retries", max_retries, 0)):
        if type(value) is not int or value < minimum:
            raise OpenAIConfigError(f"{name} must be an integer greater than or equal to {minimum}.")
    for name, value, positive in (("connect_timeout_seconds", connect_timeout_seconds, True),
                                   ("retry_base_delay", retry_base_delay, False)):
        if (type(value) not in (int, float) or not math.isfinite(value)
                or (value <= 0 if positive else value < 0)):
            raise OpenAIConfigError(f"Invalid numeric value for {name}.")
    budget = max_output_tokens if max_token_budget is None else max_token_budget
    if type(budget) is not int or budget < max_output_tokens:
        raise OpenAIConfigError("max_token_budget must be an integer greater than or equal to max_output_tokens.")
    if not isinstance(auth_file, (str, Path)) or not str(auth_file).strip():
        raise OpenAIConfigError("auth_file must specify a credentials file path.")
    return OpenAIConfig(model.strip(), url, Path(auth_file).expanduser().resolve(),
                        reasoning_effort.strip(), connect_timeout_seconds, max_output_tokens,
                        max_retries, retry_base_delay, budget)


def read_openai_api_key(auth_file: str | Path) -> str:
    try:
        auth = json.loads(Path(auth_file).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        raise OpenAIConfigError("Unable to read the OpenAI credentials file. Check AUTH_FILE in config.py.") from None
    if not isinstance(auth, dict):
        raise OpenAIConfigError("The OpenAI credentials file must contain a JSON object.")
    key = next((auth[name] for name in ("OPENAI_API_KEY", "CWM_API_KEY", "api_key") if auth.get(name)), None)
    if (not isinstance(key, str) or not key or key == "REPLACE_WITH_YOUR_API_KEY"
            or not key.isascii() or any(c.isspace() or ord(c) < 32 for c in key)):
        raise OpenAIConfigError("The credentials file does not contain a valid OpenAI API key.")
    return key
