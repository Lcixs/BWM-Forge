#!/usr/bin/env python3

from __future__ import annotations

import base64
import copy
import mimetypes
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, quote

import requests
from config import (SEEDANCE_REQUEST_TIMEOUT, SEEDANCE_DOWNLOAD_TIMEOUT,
                    SEEDANCE_QUERY_MAX_RETRIES, SEEDANCE_POLL_INTERVAL, SEEDANCE_TIMEOUT_SECONDS)


from .prompts import R2V_APPEARANCE_PRIORITY, effective_generation_prompt, build_user_revision_prompt


SUBMIT_PATH = "/v1/video/generations"
STATUS_PATH = "/v1/video/generations/{task_id}"
DOWNLOAD_PATH = "/v1/videos/{task_id}/content"
LEGACY_SUBMIT_PATH = "/v1/videos/generations"
LEGACY_STATUS_PATH = "/v1/videos/generations/task/{task_id}"


@dataclass
class GenerationOptions:
    model: str
    prompt: str
    video: str | None = None
    image_1: str | None = None
    image_2: str | None = None
    image_1_type: str | None = None
    image_2_type: str | None = None
    image_1_role: str | None = None
    image_2_role: str | None = None
    audio: str | None = None
    audio_type: str | None = None
    # duration 显式发送 -1，其他可选字段为 None 时省略。
    duration: int | None = -1
    ratio: str | None = None
    resolution: str | None = None
    generate_audio: bool | None = False
    watermark: bool | None = None
    seed: int | None = None
    return_last_frame: bool | None = None
    execution_expires_after: int | None = None
    output_format: str | None = None
    tools: list | None = None
    safety_identifier: str | None = None


def gateway_paths(base_url: str) -> tuple[str, str, str | None]:
    if urlsplit(base_url).hostname == "aigw.telecomjs.com":
        return LEGACY_SUBMIT_PATH, LEGACY_STATUS_PATH, None
    return SUBMIT_PATH, STATUS_PATH, DOWNLOAD_PATH


def gateway_root(base_url: str) -> str:
    root = base_url.rstrip('/')
    return root[:-3] if root.endswith('/v1') else root
SUCCESS_STATUSES = {"succeeded", "success", "completed", "done"}
FAILURE_STATUSES = {"failed", "failure", "error", "cancelled", "canceled", "expired"}
LOCAL_MEDIA_EXTENSIONS = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
}



def normalize_api_key(value: str | None) -> str:
    key = (value or "").strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    return key


def auth_headers(api_key: str) -> dict[str, str]:
    key = normalize_api_key(api_key)
    if not key:
        raise ValueError("API key is empty")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def data_url(file_path: Path, forced_type: str | None = None) -> str:
    if not file_path.is_file():
        raise FileNotFoundError(f"File not found: {file_path}")
    extension = file_path.suffix.lower()
    if not forced_type and extension not in LOCAL_MEDIA_EXTENSIONS:
        supported = ", ".join(sorted(LOCAL_MEDIA_EXTENSIONS))
        raise ValueError(
            f"Unsupported local media extension {extension or '(none)'} for {file_path.name}. "
            f"Use one of: {supported}"
        )
    mime = forced_type or LOCAL_MEDIA_EXTENSIONS.get(extension) or mimetypes.guess_type(file_path.name)[0]
    if not mime:
        raise ValueError(f"Cannot detect MIME type for {file_path}")
    encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def media_url(
    value: str | Path | None,
    forced_type: str | None = None,
    *,
    allow_local_data: bool = True,
) -> str | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    if text.startswith("data:") and not allow_local_data:
        raise ValueError(
            "Video/audio data URLs are not supported by the gateway. "
            "Use a public http(s) URL or an asset://<Id> reference instead."
        )
    if text.startswith(("http://", "https://", "asset://", "data:")):
        return text
    if not allow_local_data:
        raise ValueError(
            f"Local media file {text!r} cannot be sent as a video/audio data URL. "
            "Use a public http(s) URL or an asset://<Id> reference instead."
        )
    return data_url(Path(text), forced_type)


def effective_generation_prompt(args: GenerationOptions) -> str:

    prompt = str(getattr(args, "prompt", "") or "").strip()
    has_reference_image = bool(str(getattr(args, "image_1", "") or "").strip())
    has_reference_video = bool(str(getattr(args, "video", "") or "").strip())
    if has_reference_image and has_reference_video and R2V_APPEARANCE_PRIORITY not in prompt:
        prompt = f"{prompt}\n\n{R2V_APPEARANCE_PRIORITY}".strip()
    return prompt


def build_content(args: GenerationOptions) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    prompt = effective_generation_prompt(args)
    if prompt:
        content.append({"type": "text", "text": prompt})
    for value, forced_type, item_type, role, key, allow_local_data in (
        (getattr(args, "image_1", None), getattr(args, "image_1_type", None), "image_url", getattr(args, "image_1_role", None) or "reference_image", "image_url", True),
        (getattr(args, "image_2", None), getattr(args, "image_2_type", None), "image_url", getattr(args, "image_2_role", None) or "reference_image", "image_url", True),
        (getattr(args, "video", None), None, "video_url", "reference_video", "video_url", False),
        (getattr(args, "audio", None), getattr(args, "audio_type", None), "audio_url", "reference_audio", "audio_url", False),
    ):
        url = media_url(value, forced_type, allow_local_data=allow_local_data)
        if url:
            content.append({"type": item_type, "role": role, key: {"url": url}})
    return content


def build_payload(args: GenerationOptions) -> dict[str, Any]:
    if not isinstance(args.prompt, str) or not args.prompt.strip():
        raise ValueError("prompt must be a non-empty string.")
    payload: dict[str, Any] = {"model": args.model, "prompt": effective_generation_prompt(args),
                             "content": build_content(args)}
    optional = (
        ("duration", args.duration if args.duration is not None else -1),
        ("ratio", getattr(args, "ratio", None)),
        ("resolution", getattr(args, "resolution", None)),
        ("generate_audio", False if args.generate_audio is None else args.generate_audio),
        ("watermark", getattr(args, "watermark", None)),
        ("seed", getattr(args, "seed", None)),
        ("return_last_frame", getattr(args, "return_last_frame", None)),
        ("execution_expires_after", getattr(args, "execution_expires_after", None)),
        ("output_format", getattr(args, "output_format", None)),
        ("tools", getattr(args, "tools", None)),
        ("safety_identifier", getattr(args, "safety_identifier", None)),
    )
    for key, value in optional:
        if value is not None:
            payload[key] = value
    return payload


def build_gateway_payload(payload: dict[str, Any]) -> dict[str, Any]:
    request = copy.deepcopy(payload)
    request.pop("prompt", None)
    return request


class GatewayHTTPError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def json_request(session: requests.Session, method: str, url: str, headers: dict[str, str], **kwargs: Any) -> dict[str, Any]:
    response = session.request(method, url, headers=headers, timeout=SEEDANCE_REQUEST_TIMEOUT, **kwargs)
    if not response.ok:
        if response.status_code == 401:
            raise RuntimeError(
                f"{method} {url} failed (401): API key rejected by this gateway. "
                "Check that the key belongs to the configured API address and is not expired. "
                f"Server detail: {response.text[:1000]}"
            )
        raise GatewayHTTPError(response.status_code, f"{method} {url} failed ({response.status_code}): {response.text[:2000]}")
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError(f"API returned non-JSON response: {response.text[:1000]}") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"Unexpected API response: {result!r}")
    return result


def get_task_id(data: dict[str, Any]) -> str:
    output = data.get("output")
    task_id = output.get("task_id") if isinstance(output, dict) else None
    task_id = task_id or data.get("task_id") or data.get("id") or data.get('taskId')
    if not task_id and isinstance(data.get('data'), dict):
        return get_task_id(data['data'])
    if not isinstance(task_id, str) or not task_id.strip():
        raise RuntimeError(f"The submission response is missing a valid task ID: {submission_record(data)}")
    return task_id.strip()


def get_status(data: dict[str, Any]) -> str:
    for key in ('task_status', 'status'):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    for key in ('output', 'data', 'content'):
        nested = data.get(key)
        if isinstance(nested, dict):
            status = get_status(nested)
            if status:
                return status
    return ''


def get_video_url(data: dict[str, Any]) -> str | None:
    for body in (data.get('output'), data.get('data'), data):
        if isinstance(body, dict):
            for key in ('video_url', 'result_url'):
                value = body.get(key)
                if isinstance(value, str) and value.startswith(('http://', 'https://')):
                    return value
    content = data.get("content")
    if isinstance(content, dict):
        value = content.get("video_url")
        if isinstance(value, str) and value:
            return value
    return None


def wait_until_done(
    session: requests.Session,
    base_url: str,
    task_id: str,
    headers: dict[str, str],
    interval: int,
    timeout: int,
    on_status: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    _, status_path, _ = gateway_paths(base_url)
    url = gateway_root(base_url) + status_path.format(task_id=quote(task_id, safe=''))
    start = time.monotonic()
    failures = 0
    while True:
        try:
            response = json_request(session, "GET", url, headers)
            failures = 0
        except (GatewayHTTPError, requests.Timeout, requests.ConnectionError) as exc:
            if isinstance(exc, GatewayHTTPError) and exc.status_code not in (408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524):
                raise
            failures += 1
            remaining = timeout - (time.monotonic() - start)
            if failures > SEEDANCE_QUERY_MAX_RETRIES or remaining <= 0:
                raise
            if on_status:
                on_status(f'Temporary query failure; retrying the original task ({failures}/{SEEDANCE_QUERY_MAX_RETRIES}) without submitting a new generation')
            time.sleep(min(max(interval, 1), remaining))
            continue
        status = get_status(response)
        if on_status:
            on_status(status or "unknown")
        if status in SUCCESS_STATUSES:
            return response
        if status in FAILURE_STATUSES:
            detail = response.get("error", response)
            if isinstance(detail, dict) and detail.get("code") == "VIDEO_MEDIA_FORMAT_UNSUPPORTED":
                raise RuntimeError(
                    "Video task failed: a reference media format is unsupported. "
                    "Use JPG/PNG images and H.264 MP4 or MOV video (and MP3/WAV audio); "
                    f"server detail: {detail.get('message', detail)}"
                )
            raise RuntimeError(f"Video task failed: {detail}")
        if time.monotonic() - start >= timeout:
            raise TimeoutError(f"Task timed out after {timeout} seconds: {response}")
        time.sleep(interval)


def download_video(session: requests.Session, url: str, headers: dict[str, str] | None, output: Path) -> None:
    response = session.get(url, headers=headers or {}, timeout=SEEDANCE_DOWNLOAD_TIMEOUT, stream=True)
    if not response.ok:
        raise RuntimeError(f"Video download failed ({response.status_code}): {response.text[:2000]}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + '.part')
    with temporary.open("wb") as file:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                file.write(chunk)
    if not temporary.stat().st_size:
        raise RuntimeError('Downloaded video is empty')
    temporary.replace(output)


def generate_and_download(
    session: requests.Session,
    base_url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    output: Path,
    *,
    interval: int = SEEDANCE_POLL_INTERVAL,
    timeout: int = SEEDANCE_TIMEOUT_SECONDS,
    on_created: Callable[[str], None] | None = None,
    on_submitted: Callable[[dict], None] | None = None,
    on_status: Callable[[str], None] | None = None,
) -> str:
    if not isinstance(payload.get('prompt'), str) or not payload['prompt'].strip():
        raise ValueError('prompt must be a non-empty string.')
    submit_path, _, download_path = gateway_paths(base_url)
    created = json_request(
        session,
        "POST",
        gateway_root(base_url) + submit_path,
        headers,
        json=build_gateway_payload(payload),
    )
    record = submission_record(created, headers.get('Authorization', '').removeprefix('Bearer '))
    if on_submitted:
        on_submitted(record)
    task_id = get_task_id(record)
    if on_created:
        on_created(task_id)
    return resume_and_download(session, base_url, headers, task_id, output,
                               interval=interval, timeout=timeout, on_status=on_status)


def resume_and_download(session, base_url, headers, task_id, output, *, interval=SEEDANCE_POLL_INTERVAL, timeout=SEEDANCE_TIMEOUT_SECONDS, on_status=None):
    _, _, download_path = gateway_paths(base_url)
    completed = wait_until_done(
        session,
        base_url.rstrip("/"),
        task_id,
        headers,
        interval,
        timeout,
        on_status=on_status,
    )
    video_url = get_video_url(completed)
    if video_url:
        # 第三方下载地址不携带网关认证头，也不复用认证会话。
        with requests.Session() as media_session:
            download_video(media_session, video_url, {}, output)
    elif download_path:
        video_url = gateway_root(base_url) + download_path.format(task_id=quote(task_id, safe=''))
        download_video(session, video_url, headers, output)
    else:
        raise RuntimeError("The task succeeded, but the response contains no downloadable video URL.")
    return task_id


def submission_record(response: dict, api_key: str = '') -> dict:
    allowed = {'task_id', 'id', 'taskId', 'status', 'task_status', 'code', 'message',
               'error', 'fail_reason', 'success', 'output', 'data'}
    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k in allowed}
        if isinstance(value, str):
            return value.replace(api_key, '[omitted]')[:1000] if api_key else value[:1000]
        return value if value is None or isinstance(value, (bool, int, float)) else None
    return clean(response)


def build_corrected_payload(args: GenerationOptions, feedback_prompt: str) -> dict[str, Any]:

    corrected_args = replace(args)
    corrected_args.prompt = feedback_prompt.strip()
    if not corrected_args.prompt:
        raise ValueError("Evaluation feedback prompt is empty")
    return build_payload(corrected_args)



@dataclass(slots=True)
class UserRevisionPlan:

    revision_number: int
    prompt: str
    output: Path
    evaluation_dir: Path
    args: GenerationOptions
    payload: dict[str, Any]



def revision_output_path(initial_output: Path, revision_number: int) -> Path:

    if revision_number < 1:
        raise ValueError("revision_number must be at least 1")
    initial_output = Path(initial_output)
    suffix = initial_output.suffix or ".mp4"
    return initial_output.with_name(
        f"{initial_output.stem}_revision_{revision_number:02d}{suffix}"
    )



def prepare_user_revision(
    args: GenerationOptions,
    initial_output: Path,
    revision_number: int,
    user_feedback: str,
    evaluation: dict[str, Any],
    *,
    original_prompt: str | None = None,
) -> UserRevisionPlan:

    base_prompt = original_prompt if original_prompt is not None else args.prompt
    prompt = build_user_revision_prompt(base_prompt, user_feedback, evaluation)
    revision_args = replace(args)
    revision_args.prompt = prompt
    output = revision_output_path(initial_output, revision_number)
    return UserRevisionPlan(
        revision_number=revision_number,
        prompt=prompt,
        output=output,
        evaluation_dir=output.with_name(f"{output.stem}_evaluation"),
        args=revision_args,
        payload=build_payload(revision_args),
    )



