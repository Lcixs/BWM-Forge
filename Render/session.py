from __future__ import annotations

from dataclasses import fields
import json
from pathlib import Path
from typing import Any

from input_pipeline.common import now, read_json, save_json
from . import seedance as api
from .prompts import DEFAULT_INITIAL_PROMPT
from .seedance_settings import SeedanceSettings


def prepare_render(*, white_video: str | Path, reference_image: str | Path,
                   output: str | Path, prompt: str | None = None,
                   settings: SeedanceSettings | None = None) -> dict:
    white, reference, output = (Path(p).expanduser().resolve() for p in
                                (white_video, reference_image, output))
    if not white.is_file() or white.suffix.lower() not in ('.mp4', '.mov') or not white.stat().st_size:
        raise ValueError('The white-model video must be a non-empty MP4/MOV file.')
    if not reference.is_file():
        raise FileNotFoundError(f'First-frame reference image not found: {reference}')
    if prompt is None or isinstance(prompt, str) and not prompt.strip():
        prompt = DEFAULT_INITIAL_PROMPT
    if not isinstance(prompt, str):
        raise ValueError('prompt must be a string.')
    if output.exists() or output.with_name(output.stem + '_review_session.json').exists():
        raise FileExistsError('A video or session already exists. Load the existing session to continue.')
    settings = settings or SeedanceSettings.load()
    args = api.GenerationOptions(model=settings.seedance_model, prompt=prompt.strip(),
                                 video=str(white), image_1=str(reference))
    return {
        'base_args': args, 'headers': api.auth_headers(settings.seedance_api_key),
        'base_url': settings.seedance_base_url, 'poll_interval': settings.poll_interval,
        'timeout_seconds': settings.timeout_seconds,
        'initial_output': output, 'current_output': output, 'current_evaluation': {},
        'original_prompt': api.effective_generation_prompt(args), 'evaluation_white': white,
        'run_evaluation': True, 'run_gpt_vlm': True,
        'revision_number': 0, 'history': [], 'initial_objective_gate': True,
        'initial_phase': 'objective', 'auto_local_media': True,
        'harness_export': {'status': 'pending', 'file': None, 'error': None},
        'retrieved_experiences': [],
    }


def save_session(state: dict[str, Any]) -> Path:
    initial = Path(state['initial_output'])
    path = initial.with_name(initial.stem + '_review_session.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        'initial_output': str(initial.resolve()),
        'current_output': str(Path(state['current_output']).resolve()),
        'revision_count': state['revision_number'],
        'history': state['history'],
        'confirmed': state.get('confirmed', False),
        'harness_export': state.get('harness_export', {'status': 'pending', 'file': None, 'error': None}),
        'retrieved_experiences': state.get('retrieved_experiences', []),
        'render_status': state.get('render_status', 'running'),
        'render_step': state.get('render_step', 'preparing'),
        'render_error': state.get('render_error'),
        'current_evaluation': state.get('current_evaluation', {}),
        'objective_evaluation': state.get('objective_evaluation'),
        'initial_objective_gate': state.get('initial_objective_gate', False),
        'initial_phase': state.get('initial_phase', 'review'),
        'context': {
            'auto_local_media': state.get('auto_local_media', False),
            'base_url': state.get('base_url'),
            'evaluation_white': str(state.get('evaluation_white', '')),
            'original_prompt': state.get('original_prompt', ''),
            'run_evaluation': state.get('run_evaluation', True),
            'run_gpt_vlm': state.get('run_gpt_vlm', False),
            'base_args': {k: v for k, v in vars(state['base_args']).items()
                          if k in ('model', 'prompt', 'video', 'image_1', 'image_2', 'image_1_type', 'image_2_type',
                                   'image_1_role', 'image_2_role', 'duration', 'ratio', 'resolution', 'watermark',
                                   'generate_audio', 'seed', 'return_last_frame', 'execution_expires_after', 'output_format')}
                         if state.get('base_args') else {},
        },
    }
    save_json(path, document)
    return path


def load_session(path: str | Path, *, settings: SeedanceSettings | None = None) -> dict:
    document = json.loads(Path(path).read_text(encoding='utf-8'))
    history = document['history']
    context = document.get('context') or {}
    if not isinstance(history, list) or not context.get('base_args') or not context.get('evaluation_white'):
        raise ValueError('The session is missing its version list or input configuration.')
    settings = settings or SeedanceSettings.load()
    if api.gateway_root(context['base_url']) != api.gateway_root(settings.seedance_base_url):
        raise ValueError('The session API URL differs from the current configuration. Use the matching service configuration.')
    names = {field.name for field in fields(api.GenerationOptions)}
    args = api.GenerationOptions(**{k: v for k, v in context['base_args'].items() if k in names})
    args.model = settings.seedance_model
    state = {
        'base_args': args, 'headers': api.auth_headers(settings.seedance_api_key),
        'base_url': settings.seedance_base_url, 'poll_interval': settings.poll_interval,
        'timeout_seconds': settings.timeout_seconds,
        'initial_output': Path(document['initial_output']),
        'current_output': Path(document.get('current_output') or document['initial_output']),
        'current_evaluation': document.get('current_evaluation') or {},
        'objective_evaluation': document.get('objective_evaluation'),
        'evaluation_white': Path(context['evaluation_white']),
        'original_prompt': context.get('original_prompt', args.prompt),
        'run_evaluation': context.get('run_evaluation', True),
        'run_gpt_vlm': context.get('run_gpt_vlm', False),
        'history': history, 'revision_number': history[-1]['version'] if history else 0,
        'initial_objective_gate': document.get('initial_objective_gate', False),
        'initial_phase': document.get('initial_phase', 'review'),
        'auto_local_media': context.get('auto_local_media', False),
        'confirmed': document.get('confirmed', False),
        'harness_export': document.get('harness_export') or {'status': 'pending', 'file': None, 'error': None},
        'retrieved_experiences': document.get('retrieved_experiences', []),
        'render_status': document.get('render_status', 'running'),
        'render_step': document.get('render_step', 'preparing'),
        'render_error': document.get('render_error'),
    }
    row = history[-1] if history else {}
    if not row.get('task_id') and row.get('submission_response'):
        try:
            row['task_id'] = api.get_task_id(row['submission_response'])
        except RuntimeError:
            pass
    return state


def get_rendered_video(state: dict[str, Any]) -> Path:
    path = Path(state['current_output'])
    if not path.is_file():
        raise FileNotFoundError('The video download is not complete')
    return path.resolve()


def save_progress(source, state: dict[str, Any], *, status: str, step: str,
                  error: str | None = None) -> None:
    state.update(render_status=status, render_step=step, render_error=error)
    save_session(state)
    task = read_json(source.task_dir / 'task.json')
    if not isinstance(task, dict):
        raise ValueError('task.json must contain a JSON object.')
    task.setdefault('render_started_at', now())
    task.update(status='render_' + status, render_status=status, render_step=step,
                render_session_file=source.session_file.relative_to(source.task_dir).as_posix(),
                render_updated_at=now())
    row = (state.get('history') or [{}])[-1]
    for key in ('version', 'generation_status', 'evaluation_status'):
        task['render_' + key] = row.get(key)
    if row.get('task_id'):
        task['render_provider_task_id'] = row['task_id']
    else:
        task.pop('render_provider_task_id', None)
    video = Path(state['current_output']).resolve()
    if video.is_file() and video.is_relative_to(source.task_dir):
        task['render_video_file'] = video.relative_to(source.task_dir).as_posix()
    exported = state.get('harness_export') or {}
    task['render_export_status'] = exported.get('status', 'pending')
    if status == 'completed':
        task.setdefault('render_finished_at', now())
        task['render_summary_file'] = exported['file']
    else:
        task.pop('render_finished_at', None)
        task.pop('render_summary_file', None)
    if error:
        task['render_error'] = error
    else:
        task.pop('render_error', None)
    save_json(source.task_dir / 'task.json', task)
