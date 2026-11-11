from __future__ import annotations

import asyncio
import base64
import codecs
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
from typing import Callable

import httpx

from config import (OPENAI_BASE_URL, OPENAI_AUTH_FILE, GPT_REASONING_EFFORT,
                    GPT_CONNECT_TIMEOUT_SECONDS, GPT_MAX_OUTPUT_TOKENS, GPT_MAX_RETRIES,
                    GPT_RETRY_BASE_DELAY, GPT_RESPONSE_LIMIT_BYTES, GPT_STREAM)
from .openai_config import load_openai_config, read_openai_api_key

RESPONSE_LIMIT = GPT_RESPONSE_LIMIT_BYTES
_RETRYABLE_HTTP_STATUSES = {408, 409, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}


class GPTError(RuntimeError):
    def __init__(self, message: str, **details):
        super().__init__(message)
        self.details = details


@dataclass(frozen=True)
class GPTResult:
    text: str
    data: dict | None
    model: str
    usage: dict


def image_content(image: bytes | Path, *, detail: str = "high") -> dict:
    data = image.read_bytes() if isinstance(image, Path) else image
    return {"type": "input_image", "image_url": "data:image/jpeg;base64," +
            base64.b64encode(data).decode("ascii"), "detail": detail}


def _service_error(status: int, headers, body: bytes, key: str) -> GPTError:
    request_id = None
    for name in ("x-request-id", "cf-ray", "x-oneapi-request-id"):
        value = headers.get(name, "")
        if (re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{5,127}", value)
                and key.lower() not in value.lower()
                and not re.search(r"sk-|bearer|token|secret|password|api.?key", value, re.I)):
            request_id = value
            break
    hints = {400: "Request rejected. Check whether this service supports the model and image inputs.",
             401: "Authentication failed. Check the API key in auth.json.",
             403: "Access denied. Check the provider's model permissions and access restrictions.",
             404: "Endpoint or model not found. Check base_url and model.",
             429: "Rate limit or quota exceeded. Check your account status."}
    hint = hints.get(status, "Server or gateway error. Contact the service provider." if status >= 500 else
                     "The API returned an unsuccessful status. Check the API URL; redirects are not followed.")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError):
        data = {}
    if (status == 502 and isinstance(data, dict) and data.get("cloudflare_error") is True
            and data.get("status") == 502):
        hint = "Cloudflare reported an origin response error. Ask the provider to check the origin server and upstream model service."
    message = f"The model API returned HTTP {status}. {hint}"
    if request_id:
        message += f" Request ID: {request_id}."
    return GPTError(message, http_status=status, request_id=request_id,
                    retryable=status in _RETRYABLE_HTTP_STATUSES,
                    retry_after=_retry_after_seconds(headers, data))


def _retry_after_seconds(headers, body) -> float | None:
    candidate = headers.get("Retry-After")
    if candidate is None and isinstance(body, dict):
        candidate = body.get("retry_after")
        if candidate is None and isinstance(body.get("error"), dict):
            candidate = body["error"].get("retry_after")
    try:
        delay = float(candidate)
        return max(0.0, delay) if math.isfinite(delay) else None
    except (TypeError, ValueError):
        return None


def _response_metadata(body) -> dict:
    body = body if isinstance(body, dict) else {}
    details = body.get("incomplete_details")
    reason = details.get("reason") if isinstance(details, dict) else None
    usage = body.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    return {
        "status": body.get("status") if body.get("status") in
            ("completed", "incomplete", "failed", "cancelled", "queued", "in_progress") else None,
        "incomplete_reason": reason if reason in ("max_output_tokens", "content_filter") else None,
        "usage": {k: v for k, v in usage.items() if k in
            ("input_tokens", "output_tokens", "total_tokens") and type(v) in (int, float)},
    }


def _parse_response(body, *, model: str, structured: bool) -> GPTResult:
    if not isinstance(body, dict):
        raise GPTError("Invalid Responses API response format.", retryable=True, invalid_response=True)
    if body.get("status") != "completed" or body.get("error"):
        meta = _response_metadata(body)
        if meta["incomplete_reason"] == "content_filter":
            raise GPTError("The model did not provide usable output.")
        raise GPTError("The model response is incomplete.", retryable=True, invalid_response=True)
    output = body.get("output")
    if not isinstance(output, list):
        raise GPTError("The Responses API response is missing output.", retryable=True, invalid_response=True)
    texts = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        if item.get("status", "completed") != "completed":
            raise GPTError("The model message is incomplete.", retryable=True, invalid_response=True)
        parts = item.get("content")
        if not isinstance(parts, list):
            raise GPTError("Invalid model message content.", retryable=True, invalid_response=True)
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "refusal":
                raise GPTError("The model declined this request.")
            if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
    text = "\n".join(texts).strip()
    if not text:
        raise GPTError("The model returned empty content.", retryable=True, invalid_response=True)
    data = None
    if structured:
        try:
            data = json.loads(text)
        except ValueError:
            raise GPTError("The model did not return valid JSON.", retryable=True, invalid_response=True) from None
        if not isinstance(data, dict):
            raise GPTError("The model's JSON output must be an object.", retryable=True, invalid_response=True)
    returned_model = body.get("model")
    return GPTResult(text, data, returned_model if isinstance(returned_model, str) and returned_model else model,
                     _response_metadata(body)["usage"])


async def request_gpt(
    *, model: str, instructions: str, content: list[dict],
    base_url: str = OPENAI_BASE_URL, auth_file: str | Path = OPENAI_AUTH_FILE,
    reasoning_effort: str = GPT_REASONING_EFFORT,
    connect_timeout_seconds: float = GPT_CONNECT_TIMEOUT_SECONDS,
    max_output_tokens: int = GPT_MAX_OUTPUT_TOKENS,
    max_retries: int = GPT_MAX_RETRIES, retry_base_delay: float = GPT_RETRY_BASE_DELAY,
    max_token_budget: int | None = None,
    response_schema: dict | None = None, schema_name: str = "response",
    validate: Callable[[dict], object] | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_retry: Callable[[str], None] | None = None,
    on_diagnostic: Callable[[dict], None] | None = None,
    transport=None,
) -> GPTResult:
    config = load_openai_config(model=model, base_url=base_url, auth_file=auth_file,
        reasoning_effort=reasoning_effort, connect_timeout_seconds=connect_timeout_seconds,
        max_output_tokens=max_output_tokens, max_retries=max_retries,
        retry_base_delay=retry_base_delay, max_token_budget=max_token_budget)
    key = read_openai_api_key(config.auth_file)
    if validate is not None and response_schema is None:
        raise ValueError("JSON application validation requires response_schema.")
    payload = {"model": config.model, "instructions": instructions,
               "input": [{"role": "user", "content": content}],
               "reasoning": {"effort": config.reasoning_effort},
               "max_output_tokens": config.max_output_tokens,
               "store": False, "stream": GPT_STREAM}
    if response_schema is not None:
        payload["text"] = {"format": {"type": "json_schema", "name": schema_name,
                                      "strict": True, "schema": response_schema}}
    for attempt in range(config.max_retries + 1):
        body = None
        try:
            body = await _send_gpt_request(config.base_url, key, payload,
                connect_timeout=config.connect_timeout_seconds, transport=transport, on_progress=on_progress)
            result = _parse_response(body, model=config.model, structured=response_schema is not None)
            if validate is not None:
                try:
                    validate(result.data)
                except (ValueError, KeyError, IndexError, TypeError, RuntimeError):
                    raise GPTError("The model's JSON output failed application validation.", retryable=True, invalid_response=True) from None
            return result
        except GPTError as exc:
            if exc.details.get("invalid_response"):
                meta = _response_metadata(body)
                if on_diagnostic is not None:
                    on_diagnostic({"attempt": attempt + 1, "max_output_tokens": payload["max_output_tokens"], **meta})
                if meta["incomplete_reason"] == "max_output_tokens":
                    payload["max_output_tokens"] = min(config.max_token_budget, payload["max_output_tokens"] * 2)
            if not exc.details.get("retryable") or attempt >= config.max_retries:
                raise
            delay = exc.details.get("retry_after")
            if delay is None:
                delay = config.retry_base_delay * (2 ** attempt)
            if on_retry is not None:
                on_retry(f"{exc} Retrying in {delay:g} seconds ({attempt + 1}/{config.max_retries})")
            await asyncio.sleep(delay)
    raise GPTError("The GPT request did not return a result.")


async def _send_gpt_request(base_url: str, key: str, payload: dict, *,
                           connect_timeout: float, transport=None, on_progress=None) -> dict:
    known_http_error = None
    try:
        async with httpx.AsyncClient(transport=transport, follow_redirects=False,
                timeout=httpx.Timeout(None, connect=connect_timeout)) as client:
            async with client.stream("POST", base_url + "/responses", json=payload,
                    headers={"Authorization": "Bearer " + key, "Accept": "text/event-stream, application/json"}) as response:
                if not 200 <= response.status_code < 300:
                    known_http_error = _service_error(response.status_code, response.headers, b"", key)
                    body = bytearray()
                    try:
                        async with asyncio.timeout(2):
                            async for chunk in response.aiter_bytes():
                                body.extend(chunk[:max(0, 16384 - len(body))])
                                if len(body) >= 16384:
                                    break
                    except (TimeoutError, httpx.HTTPError):
                        pass
                    raise _service_error(response.status_code, response.headers, bytes(body), key)
                if on_progress is not None:
                    on_progress("Response received; waiting for the complete result")
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower().strip()
                if content_type == "application/json" or content_type.endswith("+json"):
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > RESPONSE_LIMIT:
                            raise GPTError("The service response exceeded the size limit. Reading stopped.")
                    result = json.loads(body)
                    if not isinstance(result, dict):
                        raise GPTError("GPT response must be a JSON object", retryable=True, invalid_response=True)
                    return result
                if content_type != "text/event-stream":
                    raise GPTError("The service did not return the expected GPT response. Check the API URL and protocol.")
                async for event in _read_events(response):
                    kind = event["type"]
                    if kind in {"response.completed", "response.failed", "response.incomplete", "response.cancelled"}:
                        return event.get("response", {})
                    if kind == "error":
                        raise GPTError("The model service returned an error. Check the provider logs.")
                raise GPTError("The response connection closed before the complete result was received.", retryable=True)
    except httpx.ConnectTimeout:
        raise GPTError(f"Connection to the GPT service timed out after {connect_timeout:g} seconds", retryable=True) from None
    except (TimeoutError, httpx.TimeoutException):
        if known_http_error is not None:
            raise known_http_error from None
        raise GPTError("GPT service transfer timed out before the complete result was received", retryable=True) from None
    except httpx.HTTPError:
        raise GPTError("Cannot connect to GPT service", retryable=True) from None
    except (ValueError, UnicodeError):
        raise GPTError("GPT returned invalid JSON or text", retryable=True, invalid_response=True) from None


async def _read_events(response):
    decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
    pending, data_lines, received = "", [], 0
    async for chunk in response.aiter_bytes():
        received += len(chunk)
        if received > RESPONSE_LIMIT:
            raise GPTError("The service response exceeded the size limit. Reading stopped.")
        pending += decoder.decode(chunk)
        while match := re.search(r"\r\n|\r|\n", pending):
            if match.group() == "\r" and match.end() == len(pending):
                break  # 下一分片可能以 LF 开头。
            line, pending = pending[:match.start()], pending[match.end():]
            if line.startswith("data:"):
                data = line[5:]
                data_lines.append(data[1:] if data.startswith(" ") else data)
            elif not line and data_lines:
                text, data_lines = "\n".join(data_lines), []
                if text == "[DONE]":
                    return
                event = json.loads(text)
                if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                    raise GPTError("The service returned an invalid streaming event.")
                yield event
    decoder.decode(b"", final=True)
    # 末尾单独的 CR 可结束事件，未结束的数据行不能提交。
    if pending == "\r" and data_lines:
        text = "\n".join(data_lines)
        if text != "[DONE]":
            event = json.loads(text)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise GPTError("The service returned an invalid streaming event.")
            yield event
