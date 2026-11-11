from __future__ import annotations

from html import escape
from dataclasses import replace
from pathlib import Path
import json
import re
import sys
import uuid

import gradio as gr


GUI_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = GUI_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import ASSET_ROOT, OUTPUT_ROOT  # noqa: E402
from input_pipeline.assets.library import AssetLibrary, EMBODIMENTS  # noqa: E402
from input_pipeline.input_stage import validate_asset  # noqa: E402
from input_pipeline.motion import validate_fps  # noqa: E402
from gui.task_store import TaskSnapshot, list_tasks, load_task  # noqa: E402
from gui.pipeline_adapter import InputRequest, manager, render_view  # noqa: E402


STAGE_LABELS = {"input": "Input", "blender": "Blender", "render": "Render"}
UPLOAD_ASSET_VALUE = "__upload_robot_asset__"
WORKSPACE_OUTPUT_COUNT = 38
REFRESH_OUTPUT_COUNT = 40
EMBODIMENT_CHOICES = [("Robot dog", "robot_dog"), ("Humanoid robot", "robot"),
                      ("Robot arm", "robot_arm"), ("Dexterous hand", "dexterous_hand")]
ACTION_CHOICES = [("Joint positions", "joint"), ("End-effector pose", "eef")]
STATE_LABELS = {
    "completed": "Completed",
    "running": "Running",
    "attention": "Needs attention",
    "ready": "Ready to resume",
    "pending": "Pending",
    "review": "Awaiting review",
}

THEME_SWITCH_JS = r"""
const button = element.querySelector('#cwm-theme-toggle');
const applyTheme = (theme) => {
  const dark = theme === 'dark';
  document.body.classList.toggle('dark', dark);
  button.textContent = dark ? '☾ Dark' : '☀ Light';
  button.title = dark ? 'Switch to light mode' : 'Switch to dark mode';
  button.setAttribute('aria-label', button.title);
};
const saved = localStorage.getItem('cwm-theme');
applyTheme(saved === 'light' ? 'light' : 'dark');
button.addEventListener('click', (event) => {
  event.preventDefault();
  const next = document.body.classList.contains('dark') ? 'light' : 'dark';
  localStorage.setItem('cwm-theme', next);
  applyTheme(next);
});

if (!document.querySelector('#cwm-value-tooltip')) {
  const tooltip = document.createElement('div');
  tooltip.id = 'cwm-value-tooltip';
  tooltip.className = 'cwm-value-tooltip';
  tooltip.setAttribute('role', 'tooltip');
  document.body.appendChild(tooltip);

  const builtinEnglish = new Map([
    ['将图像拖放到此处', 'Drop an image here'],
    ['将文件拖放到此处', 'Drop a file here'],
    ['将视频拖放到此处', 'Drop a video here'],
    ['将图像文件拖放到此处以上传', 'Drop an image file here to upload'],
    ['将视频文件拖放到此处以上传', 'Drop a video file here to upload'],
    ['点击上传', 'Click to upload'],
    ['- 或 -', '- or -'],
    ['或', 'or'],
    ['正在上传...', 'Uploading...'],
    ['清除', 'Clear'],
    ['下载', 'Download'],
    ['编辑', 'Edit'],
    ['移除', 'Remove'],
    ['分享', 'Share'],
    ['撤销', 'Undo'],
    ['提交', 'Submit'],
    ['取消', 'Cancel'],
    ['加载中', 'Loading'],
    ['设置', 'Settings'],
    ['语言', 'Language'],
    ['显示主题', 'Display theme'],
    ['下拉菜单', 'Dropdown'],
    ['单选框', 'Radio button'],
    ['复选框', 'Checkbox'],
    ['全屏', 'Fullscreen'],
  ]);
  const translateBuiltinChrome = (root) => {
    const scope = root instanceof Element || root instanceof Document ? root : root?.parentElement;
    if (!scope || scope.closest?.('#console-log')) return;
    const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
    const textNodes = [];
    while (walker.nextNode()) textNodes.push(walker.currentNode);
    for (const node of textNodes) {
      if (node.parentElement?.closest('#console-log')) continue;
      const trimmed = node.nodeValue.trim();
      const translated = builtinEnglish.get(trimmed);
      if (translated) node.nodeValue = node.nodeValue.replace(trimmed, translated);
    }
    const labelled = scope.querySelectorAll?.('[title], [aria-label]') || [];
    const elements = [scope, ...labelled];
    for (const item of elements) {
      if (!(item instanceof Element) || item.closest('#console-log')) continue;
      for (const attribute of ['title', 'aria-label']) {
        const value = item.getAttribute(attribute);
        if (builtinEnglish.has(value)) item.setAttribute(attribute, builtinEnglish.get(value));
      }
    }
  };
  translateBuiltinChrome(document.body);
  new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      translateBuiltinChrome(mutation.target);
      for (const node of mutation.addedNodes) translateBuiltinChrome(node);
    }
  }).observe(document.body, {childList: true, subtree: true, characterData: true});

  const selector = '#task-picker input, #asset-picker input';
  const hideTooltip = () => tooltip.classList.remove('is-visible');
  const showTooltip = (input) => {
    const value = input.value.trim();
    if (input.getAttribute('aria-expanded') === 'true' || !value || input.scrollWidth <= input.clientWidth + 1) {
      hideTooltip();
      return;
    }
    tooltip.textContent = value;
    tooltip.classList.add('is-visible');
    const inputRect = input.getBoundingClientRect();
    const tipRect = tooltip.getBoundingClientRect();
    const left = Math.max(12, Math.min(
      inputRect.left + inputRect.width / 2 - tipRect.width / 2,
      window.innerWidth - tipRect.width - 12,
    ));
    let top = inputRect.top - tipRect.height - 9;
    if (top < 8) top = inputRect.bottom + 9;
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
  };

  document.addEventListener('pointerover', (event) => {
    const input = event.target.closest?.(selector);
    if (input) showTooltip(input);
  });
  document.addEventListener('pointerout', (event) => {
    const input = event.target.closest?.(selector);
    if (input && !input.contains(event.relatedTarget)) hideTooltip();
  });
  let closeOnClick = null;
  const dropdownControlInput = (event) => {
    if (event.target.closest?.('[role="option"], li')) return null;
    const component = event.target.closest?.('#task-picker, #asset-picker');
    const input = component?.querySelector('input');
    if (!input) return null;
    const inputRect = input.getBoundingClientRect();
    const componentRect = component.getBoundingClientRect();
    const clickedControl = event.clientY >= inputRect.top - 2
      && event.clientY <= inputRect.bottom + 2
      && event.clientX >= inputRect.left - 2
      && event.clientX <= componentRect.right + 2;
    return clickedControl ? input : null;
  };
  document.addEventListener('pointerdown', (event) => {
    const input = dropdownControlInput(event);
    closeOnClick = input?.getAttribute('aria-expanded') === 'true' ? input : null;
    if (input) hideTooltip();
    if (closeOnClick) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, true);
  document.addEventListener('click', (event) => {
    const input = dropdownControlInput(event);
    if (!input || closeOnClick !== input) {
      closeOnClick = null;
      return;
    }
    closeOnClick = null;
    event.preventDefault();
    event.stopImmediatePropagation();
    hideTooltip();
    queueMicrotask(() => {
      input.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'Escape', code: 'Escape', bubbles: true, cancelable: true,
      }));
      setTimeout(() => input.blur(), 0);
    });
  }, true);
  window.addEventListener('resize', hideTooltip);
  window.addEventListener('scroll', hideTooltip, true);
  document.addEventListener('click', (event) => {
    const trigger = event.target.closest?.('.rename-task-trigger');
    if (!trigger || trigger.disabled) return;
    event.preventDefault();
    const bridge = document.querySelector('#rename-task-bridge button')
      || document.querySelector('button#rename-task-bridge');
    bridge?.click();
  });

  window.cwmDialog = (mode, data = {}) => {
    const dialog = document.querySelector('#cwm-delete-dialog');
    if (!dialog) return;
    if (dialog.parentElement !== document.body) document.body.appendChild(dialog);
    const confirming = mode === 'confirm';
    dialog.dataset.mode = mode;
    dialog.querySelector('[data-dialog-title]').textContent = data.title
      || (confirming ? 'Delete the current task?' : mode === 'error' ? 'Action failed' : mode === 'success' ? 'Action completed' : 'Notice');
    const name = dialog.querySelector('[data-dialog-name]');
    name.textContent = data.name || '';
    name.hidden = !data.name;
    dialog.querySelector('[data-dialog-message]').textContent = confirming
      ? 'The task will be moved to output/.trash/ and will not be permanently deleted immediately.' : (data.message || '');
    dialog.querySelector('[data-dialog-close]').hidden = !confirming;
    const action = dialog.querySelector('[data-dialog-action]');
    action.textContent = confirming ? 'Delete task' : 'Got it';
    action.disabled = false;
    if (dialog.open) dialog.close();
    dialog.showModal();
  };
  window.cwmDeleteDialog = window.cwmDialog;

  document.addEventListener('click', (event) => {
    const deleteTrigger = event.target.closest?.('#delete-task-button button, button#delete-task-button');
    if (deleteTrigger && !deleteTrigger.disabled) {
      event.preventDefault();
      const name = document.querySelector('.pipeline-name-copy h2')?.textContent?.trim()
        || document.querySelector('#task-picker input')?.value?.split(' · ')[0]?.trim()
        || 'Current task';
      window.cwmDialog('confirm', {name});
      return;
    }
    if (event.target.closest?.('[data-dialog-close]')) {
      event.preventDefault();
      document.querySelector('#cwm-delete-dialog')?.close();
      return;
    }
    const action = event.target.closest?.('[data-dialog-action]');
    if (action) {
      event.preventDefault();
      const dialog = action.closest('dialog');
      if (dialog.dataset.mode !== 'confirm') return dialog.close();
      action.disabled = true;
      action.textContent = 'Deleting…';
      dialog.close();
      const bridge = document.querySelector('#delete-confirm-bridge button')
        || document.querySelector('button#delete-confirm-bridge');
      bridge?.click();
    }
  });

  let inputReadinessTimer;
  document.addEventListener('input', (event) => {
    if (!event.target.closest?.('#task-text')) return;
    clearTimeout(inputReadinessTimer);
    inputReadinessTimer = setTimeout(() => {
      const bridge = document.querySelector('#input-readiness-bridge button')
        || document.querySelector('button#input-readiness-bridge');
      bridge?.click();
    }, 180);
  });

  let consoleFollowLatest = true;
  let consoleSavedTop = 0;
  let consolePointerActive = false;
  let consoleUserIntentUntil = 0;
  const consoleFromEvent = (event) => event.target instanceof Element
    ? event.target.closest?.('#console-log .console-view') : null;
  const markConsoleScrollIntent = (event) => {
    if (!consoleFromEvent(event)) return;
    consoleUserIntentUntil = performance.now() + 400;
  };
  document.addEventListener('wheel', markConsoleScrollIntent, {capture: true, passive: true});
  document.addEventListener('pointerdown', (event) => {
    if (!consoleFromEvent(event)) return;
    consolePointerActive = true;
    markConsoleScrollIntent(event);
  }, true);
  document.addEventListener('pointerup', () => {
    if (!consolePointerActive) return;
    consolePointerActive = false;
    consoleUserIntentUntil = performance.now() + 400;
  }, true);
  document.addEventListener('keydown', (event) => {
    if (!['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(event.key)) return;
    markConsoleScrollIntent(event);
  }, true);
  const restoreConsolePosition = () => {
    requestAnimationFrame(() => {
      const consoleView = document.querySelector('#console-log .console-view');
      if (!consoleView) return;
      if (consoleFollowLatest) {
        consoleView.scrollTop = consoleView.scrollHeight;
      } else {
        const maximum = Math.max(0, consoleView.scrollHeight - consoleView.clientHeight);
        consoleView.scrollTop = Math.min(consoleSavedTop, maximum);
      }
    });
  };
  document.addEventListener('scroll', (event) => {
    const consoleView = consoleFromEvent(event);
    if (!consoleView) return;
    const now = performance.now();
    if (!consolePointerActive && now > consoleUserIntentUntil) return;
    consoleSavedTop = consoleView.scrollTop;
    const remaining = consoleView.scrollHeight - consoleView.clientHeight - consoleView.scrollTop;
    consoleFollowLatest = remaining <= 48;
  }, true);
  const belongsToConsole = (node) => {
    const elementNode = node instanceof Element ? node : node?.parentElement;
    return Boolean(elementNode?.closest?.('#console-log') || elementNode?.querySelector?.('#console-log'));
  };
  new MutationObserver((mutations) => {
    if (mutations.some((mutation) => belongsToConsole(mutation.target)
        || Array.from(mutation.addedNodes).some(belongsToConsole))) {
      restoreConsolePosition();
    }
  }).observe(document.body, {childList: true, subtree: true, characterData: true});
  restoreConsolePosition();
}
"""

DELETE_RESULT_JS = r"""
(payload) => {
  let result;
  try {
    result = typeof payload === 'string' ? JSON.parse(payload || '{}') : (payload || {});
  } catch (_) {
    result = {ok: false, name: 'Current task', message: 'Could not parse the deletion result.'};
  }
  result.title = result.ok ? 'Task deleted' : 'Deletion failed';
  window.cwmDialog?.(result.ok ? 'success' : 'error', result);
}
"""

ACTION_NOTICE_JS = r"""
(payload) => {
  let notice;
  try {
    notice = typeof payload === 'string' ? JSON.parse(payload || '{}') : (payload || {});
  } catch (_) {
    notice = {kind: 'error', title: 'Notice', message: 'Could not parse the action result.'};
  }
  if (notice.message) window.cwmDialog?.(notice.kind || 'info', notice);
}
"""

DELETE_DIALOGS_HTML = """
<dialog id="cwm-delete-dialog" class="cwm-dialog" aria-labelledby="cwm-delete-dialog-title">
  <h2 id="cwm-delete-dialog-title" data-dialog-title></h2>
  <strong data-dialog-name></strong>
  <p data-dialog-message></p>
  <div class="cwm-dialog-actions">
    <button type="button" class="secondary" data-dialog-close>Cancel</button>
    <button type="button" class="primary" data-dialog-action></button>
  </div>
</dialog>
"""


def _notice(message: str, *, kind="error", title="Notice") -> dict[str, str]:
    return {
        "kind": kind,
        "title": title,
        "message": str(message),
        "nonce": uuid.uuid4().hex,
    }


def _assets(embodiment="robot_arm") -> list[tuple[str, str]]:
    try:
        rows = AssetLibrary(ASSET_ROOT).list_assets(embodiment=embodiment)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        rows = []
    choices = [
        (row["name"], row["name"])
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    ]
    choices.append(("＋ Import local robot asset", UPLOAD_ASSET_VALUE))
    return choices


def _default_asset(choices: list[tuple[str, str]] | None = None) -> str | None:
    return next((value for _, value in (choices or _assets()) if value != UPLOAD_ASSET_VALUE), None)


def asset_upload_visibility(asset):
    return gr.update(visible=asset == UPLOAD_ASSET_VALUE)


def import_robot_asset(embodiment, urdf_path, asset_name):
    if manager.view()["busy"]:
        return gr.skip(), gr.skip(), _notice("Finish or pause the running task before importing an asset.")
    if not urdf_path or not str(urdf_path).strip():
        return gr.skip(), gr.update(visible=True), _notice("Enter the local path to an expanded robot URDF.")
    metadata = {
        "name": str(asset_name or "").strip(),
        "embodiment": embodiment,
    }
    try:
        # 使用本机 URDF 原路径，单文件上传会丢失与 meshes 的相对关系。
        urdf = Path(str(urdf_path).strip().strip('"').strip("'")).expanduser()
        asset = validate_asset(urdf, embodiment, asset_name=metadata["name"])
        imported = AssetLibrary(ASSET_ROOT).register(asset, metadata)
    except Exception as exc:
        return gr.skip(), gr.update(visible=True), _notice(f"Robot asset import failed: {exc}")
    return (
        gr.update(choices=_assets(embodiment), value=imported),
        gr.update(visible=False),
        _notice(f"Robot asset imported and selected: {imported}", kind="success", title="Import completed"),
    )


def _task_choices(tasks: list[TaskSnapshot]) -> list[tuple[str, str]]:
    choices = []
    for item in tasks:
        stage = "All stages completed" if item.current_stage == "completed" else STAGE_LABELS[item.current_stage]
        choices.append((f"{item.display_name} · {stage} · {item.status}", str(item.task_dir)))
    return choices


def _history_choices(tasks: list[TaskSnapshot]) -> list[tuple[str, str]]:
    return [(item.display_name if item.display_name == item.task_id else
             f"{item.display_name} · {item.task_id[-8:]}", str(item.task_dir))
            for item in tasks]


def _stats_html(tasks: list[TaskSnapshot]) -> str:
    completed = sum(task.current_stage == "completed" for task in tasks)
    # 等待人工审核时仍占用任务执行名额。
    busy = manager.view()["busy"]
    execution = "Active" if busy else "Idle"
    tone = "accent-amber" if busy else "accent-green"
    return f"""
    <div class="status-strip">
      <div class="status-item"><span class="status-kicker">Total tasks</span><b>{len(tasks)}</b></div>
      <div class="status-item"><span class="status-kicker">Completed</span><b class="accent-green">{completed}</b></div>
      <div class="status-item"><span class="status-kicker">Execution status</span><b class="execution-badge {tone}"><i aria-hidden="true"></i>{execution}</b></div>
    </div>
    """


_EVALUATION_LABELS = {
    "sharpness": "Sharpness",
    "exposure": "Exposure",
    "color_stability": "Color stability",
    "temporal_flicker": "Temporal flicker",
    "motion_smoothness": "Motion smoothness",
    "motion_fidelity": "Motion fidelity",
    "scene_stability": "Scene stability",
    "visual_realism": "Visual realism",
    "physical_temporal_coherence": "Physical and temporal coherence",
    "task_semantic_consistency": "Task semantic consistency",
}

_RENDER_PHASE_LABELS = {
    "not_started": "Not started",
    "generating": "Generating",
    "evaluating": "Evaluating",
    "awaiting_feedback": "Awaiting human review",
    "awaiting_evaluation": "Awaiting evaluation",
    "export_pending": "Stage summary pending",
    "confirmed": "Confirmed",
    "blocked": "Needs attention",
}

def _evaluation_html(phase: str, evaluation: dict | None, error: str = "") -> str:
    evaluation = evaluation if isinstance(evaluation, dict) else {}
    phase_label = _RENDER_PHASE_LABELS.get(phase, phase or "Unknown status")
    if not evaluation:
        detail = escape(error) if error else "Overall scores, detailed metrics, and the action review will appear here after video generation."
        tone = " evaluation-error" if error else ""
        return f"""
        <section class="evaluation-view evaluation-empty{tone}">
          <div class="evaluation-heading"><b>Evaluation status</b><span>{escape(phase_label)}</span></div>
          <p>{detail}</p>
        </section>
        """

    decision = str(evaluation.get("decision") or "Pending")
    decision_labels = {"PASS": "Pass", "RETRY": "Revision recommended", "REJECT": "Rejected"}
    decision_label = decision_labels.get(decision.upper(), decision)
    decision_class = {"PASS": "pass", "RETRY": "retry", "REJECT": "reject"}.get(
        decision.upper(), "pending")
    aggregates = evaluation.get("aggregates") if isinstance(evaluation.get("aggregates"), dict) else {}

    score_cards = []
    for key, label in (("overall_score", "Overall score"), ("objective_score", "Objective score"),
                       ("vlm_score", "Visual score")):
        value = aggregates.get(key, evaluation.get(key))
        try:
            score = float(value)
        except (TypeError, ValueError):
            continue
        score_cards.append(
            f'<div class="evaluation-score"><span>{label}</span><b>{score:.1f}</b><small>/ 100</small></div>')

    metric_cards = []
    scores = evaluation.get("scores") if isinstance(evaluation.get("scores"), dict) else {}
    for key, value in scores.items():
        try:
            score = float(value)
        except (TypeError, ValueError):
            continue
        tone = "high" if score >= 80 else "medium" if score >= 60 else "low"
        label = _EVALUATION_LABELS.get(str(key), str(key).replace("_", " "))
        metric_cards.append(
            f'<div class="evaluation-metric score-{tone}"><span>{escape(label)}</span><b>{score:.1f}</b></div>')

    action = evaluation.get("action_review") if isinstance(evaluation.get("action_review"), dict) else {}
    action_label = str(action.get("label") or action.get("status") or "Pending")
    vlm = evaluation.get("vlm") if isinstance(evaluation.get("vlm"), dict) else {}
    details = vlm.get("details") if isinstance(vlm.get("details"), dict) else {}
    summary = str(details.get("summary") or evaluation.get("summary") or "").strip()
    error_html = f'<div class="evaluation-alert">{escape(error)}</div>' if error else ""
    summary_html = f'<p class="evaluation-summary">{escape(summary)}</p>' if summary else ""
    scores_html = (f'<div class="evaluation-scores">{"".join(score_cards)}</div>' if score_cards else "")
    metrics_html = (f'<div class="evaluation-metrics">{"".join(metric_cards)}</div>' if metric_cards else "")
    return f"""
    <section class="evaluation-view">
      <div class="evaluation-heading">
        <div><b>Evaluation result</b><small>{escape(phase_label)}</small></div>
        <span class="evaluation-decision decision-{decision_class}">{escape(decision_label)}</span>
      </div>
      {scores_html}
      {metrics_html}
      <div class="evaluation-action"><span>Action review</span><b>{escape(action_label)}</b></div>
      {summary_html}
      {error_html}
    </section>
    """


def _empty_pipeline() -> str:
    return """
    <section class="pipeline-shell empty-state">
      <h2>New task · Input</h2>
      <div class="progress-callout activity-ready" role="status">
        <span class="progress-label">Ready to start</span>
        <p class="pipeline-message">Choose the embodiment and asset, then add your task inputs.</p>
        <p class="pipeline-next"><b>Next</b> Click “Create task and run Input” when the form is complete.</p>
      </div>
    </section>
    """


def _pipeline_html(task: TaskSnapshot | None, *, editable: bool = False, message: str = "",
                   next_step: str = "", activity: str = "ready") -> str:
    if task is None:
        return _empty_pipeline()
    cards = []
    for index, stage in enumerate(("input", "blender", "render"), 1):
        state = task.stage_states[stage]
        cards.append(f"""
        <article class="stage-card state-{state}">
          <span class="stage-number">0{index} · {STAGE_LABELS[stage]}</span>
          <span class="stage-state">{STATE_LABELS[state]}</span>
        </article>
        """)
    identifier = (f'<small>{escape(task.task_id)}</small>'
                  if task.display_name != task.task_id else "")
    disabled = "" if editable else " disabled"
    return f"""
    <section class="pipeline-shell">
      <div class="pipeline-title-row">
        <div class="pipeline-name-row">
          <div class="pipeline-name-copy"><h2>{escape(task.display_name)}</h2>{identifier}</div>
          <button class="rename-task-trigger" type="button" title="Rename task" aria-label="Rename task"{disabled}>✎</button>
        </div>
        <div class="task-status">{"All stages completed" if task.current_stage == "completed" else "Current stage: " + STAGE_LABELS[task.current_stage]}</div>
      </div>
      <div class="progress-callout activity-{activity}" role="status">
        <span class="progress-label">{"In progress" if activity == "running" else "Your action is needed" if activity == "attention" else "Completed" if activity == "completed" else "Ready"}</span>
        <p class="pipeline-message">{escape(message)}</p>
        <p class="pipeline-next"><b>Next</b> {escape(next_step)}</p>
      </div>
      <div class="pipeline-grid">{''.join(cards)}</div>
    </section>
    """


_LOG_HEADER = re.compile(r"^\[([^\]]+)\]\s+\[([^\]]+)\]\s*(.*)$")


def _console_html(task: TaskSnapshot | None) -> str:
    raw = task.console_log if task else "Select a task to view its logs."
    lines = []
    for line in raw.splitlines():
        match = _LOG_HEADER.match(line)
        if match:
            source, timestamp, message = match.groups()
            source_class = ("blender" if "blender" in source.lower() else
                            "input" if source.lower().startswith("input") else
                            "render" if source.lower().startswith("render") else
                            "codewm" if source.lower() == "codewm" else "output")
            lines.append(
                f'<div class="log-line log-{source_class}">'
                f'<span class="log-source">[{escape(source)}]</span> '
                f'<span class="log-time">[{escape(timestamp)}]</span> '
                f'<span class="log-text">{escape(message)}</span></div>'
            )
        else:
            content = escape(line) if line else "&nbsp;"
            lines.append(f'<div class="log-line log-continuation"><span class="log-text">{content}</span></div>')
    if not lines:
        lines.append('<div class="log-line"><span class="log-text">No runtime logs are available for this task yet.</span></div>')
    return f'<div class="console-view" role="log" tabindex="0" aria-label="Task runtime logs">{"".join(lines)}</div>'


def _resolve_selected(value: str | None) -> TaskSnapshot | None:
    if not value:
        return None
    try:
        path = Path(value).resolve()
        root = OUTPUT_ROOT.resolve()
    except OSError:
        return None
    if not path.is_relative_to(root):
        return None
    return load_task(path)


def _task_outputs(task: TaskSnapshot | None, *, editable: bool = False, message: str = "",
                  next_step: str = "", activity: str = "ready"):
    previews = {"scene_only": None, "robot_only": None}
    selected = "scene_only"
    white_video = str(task.white_video) if task and task.white_video else None
    composed_video = white_video
    if task:
        try:
            history = json.loads((task.task_dir / "blender/run.json").read_text(encoding="utf-8"))
            for row in history.get("rounds", []):
                stage = row.get("stage")
                if stage not in (*previews, "composed"):
                    continue
                selected = stage
                if stage == "composed":
                    if row.get("completed_at"):
                        relative = row.get("artifacts", {}).get("video", {}).get("file")
                        path = (task.task_dir / relative).resolve() if relative else None
                        composed_video = (str(path) if path and path.is_relative_to(task.task_dir) and path.is_file()
                                          else white_video if task.stage_states["blender"] == "completed" else None)
                    continue
                relative = row.get("artifacts", {}).get("preview", {}).get("file")
                if row.get("completed_at") and relative:
                    path = (task.task_dir / relative).resolve()
                    if path.is_relative_to(task.task_dir) and path.is_file():
                        previews[stage] = str(path)
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    return (
        _stats_html(list_tasks(OUTPUT_ROOT)),
        _pipeline_html(task, editable=editable, message=message, next_step=next_step, activity=activity),
        _console_html(task),
        *[gr.update(value=preview) for preview in previews.values()],
        gr.update(value=composed_video),
        white_video,
        str(task.final_video) if task and task.final_video else None,
        gr.update(selected=selected),
    )


def input_visibility(input_type: str, embodiment, action_space):
    kind = int(input_type)
    return (
        gr.update(visible=kind in (0, 1)),
        gr.update(visible=kind == 1),
        gr.update(visible=kind == 2),
        gr.update(choices=ACTION_CHOICES if embodiment == "robot_arm" else ACTION_CHOICES[:1],
                  value=action_space if embodiment == "robot_arm" else "joint", visible=kind == 1),
        gr.update(visible=kind == 1),
        gr.update(visible=kind == 1 and (embodiment != "robot_arm" or action_space == "joint")),
    )


def embodiment_changed(embodiment, input_type):
    choices = _assets(embodiment)
    selected = _default_asset(choices)
    return (gr.update(choices=choices, value=selected or UPLOAD_ASSET_VALUE),
            gr.update(visible=selected is None),
            gr.update(choices=ACTION_CHOICES if embodiment == "robot_arm" else ACTION_CHOICES[:1],
                      value="joint", visible=str(input_type) == "1"),
            gr.update(value="", visible=str(input_type) == "1"))


def input_submit_state(mode, input_type, embodiment, asset, text, image, trajectory, video,
                       action_space, fps, joint_names):
    create_mode = mode == "create"
    try:
        kind = int(input_type)
    except (TypeError, ValueError):
        kind = -1
    supplied = {
        "image": bool(image),
        "trajectory": bool(trajectory),
        "video": bool(video),
    }
    required = {0: ("image",), 1: ("image", "trajectory"), 2: ("video",)}.get(kind, ())
    asset_choices = {value for _, value in _assets(embodiment)} if embodiment in EMBODIMENTS else set()
    complete = bool(asset in asset_choices and asset != UPLOAD_ASSET_VALUE
                    and isinstance(text, str) and text.strip() and required
                    and all(supplied[name] for name in required))
    if kind == 1:
        try:
            validate_fps(fps)
        except ValueError:
            complete = False
        if action_space == "joint":
            names = [name.strip() for name in (joint_names or "").split(",")]
            complete = complete and all(names) and len(names) == len(set(names))
        elif action_space != "eef" or embodiment != "robot_arm":
            complete = False
    # 显示状态由 workspace_values() 控制，避免清空表单的 change 事件用旧 mode 隐藏按钮。
    return gr.update(interactive=create_mode and complete and not manager.view()["busy"])


def _read_form_document(path: Path) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return document if isinstance(document, dict) else {}


def _task_input_file(task: TaskSnapshot, value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        raw = Path(value)
        path = (raw if raw.is_absolute() else task.task_dir / raw).resolve()
        root = task.task_dir.resolve()
    except OSError:
        return None
    return str(path) if path.is_relative_to(root) and path.is_file() else None


def _input_form_updates(task: TaskSnapshot | None, *, available: bool, reset_create: bool):
    if task is None:
        if reset_create:
            choices = _assets("robot_arm")
            default_asset = _default_asset(choices)
            return [
                gr.update(value="robot_arm", interactive=available),
                gr.update(choices=choices, value=default_asset or UPLOAD_ASSET_VALUE, interactive=available),
                gr.update(value="1", interactive=available),
                gr.update(value="", interactive=available),
                gr.update(value=None, visible=True, interactive=available),
                gr.update(value=None, visible=True, interactive=available),
                gr.update(value=None, visible=False, interactive=available),
                gr.update(choices=ACTION_CHOICES, value="joint", visible=True, interactive=available),
                gr.update(value=15, visible=True, interactive=available),
                gr.update(value="", visible=True, interactive=available),
                gr.update(value=True, visible=True, interactive=available),
                gr.update(visible=default_asset is None),
            ]
        return [
            gr.update(interactive=available),
            gr.update(interactive=available),
            gr.update(interactive=available),
            gr.update(interactive=available),
            gr.update(interactive=available),
            gr.update(interactive=available),
            gr.update(interactive=available),
            gr.update(interactive=available),
            gr.update(interactive=available),
            gr.update(interactive=available),
            gr.update(visible=True, interactive=available),
            gr.skip(),
        ]

    request = _read_form_document(task.task_dir / "input" / "request.json")
    handoff = _read_form_document(task.task_dir / "input" / "input.json")
    kind = handoff.get("input_type", request.get("input_type", task.input_type))
    kind = kind if type(kind) is int and kind in (0, 1, 2) else 1
    task_text = handoff.get("instruction")
    if not isinstance(task_text, str):
        task_text = request.get("task_text", "")
    embodiment = handoff.get("embodiment") or request.get("embodiment") or task.task.get("embodiment") or "robot_arm"
    choices = _assets(embodiment)
    asset_name = handoff.get("asset_name") or request.get("asset_name") or task.asset_name or None
    if asset_name and asset_name not in {value for _, value in choices}:
        choices.insert(0, (asset_name, asset_name))
    motion = handoff.get("motion") if isinstance(handoff.get("motion"), dict) else {}
    action_space = request.get("action_space") or motion.get("action_space") or "joint"
    fps = request.get("fps") or motion.get("fps") or 15
    joint_names = request.get("joint_names") or motion.get("joint_names") or []
    image = (_task_input_file(task, handoff.get("first_frame"))
             or _task_input_file(task, request.get("image")))
    trajectory = (_task_input_file(task, handoff.get("trajectory"))
                  or _task_input_file(task, request.get("trajectory")))
    video = (_task_input_file(task, handoff.get("human_video"))
             or _task_input_file(task, request.get("video")))
    return [
        gr.update(value=embodiment, interactive=False),
        gr.update(choices=choices, value=asset_name, interactive=False),
        gr.update(value=str(kind), interactive=False),
        gr.update(value=task_text or "", interactive=False),
        gr.update(value=image, visible=kind in (0, 1), interactive=False),
        gr.update(value=trajectory, visible=kind == 1, interactive=False),
        gr.update(value=video, visible=kind == 2, interactive=False),
        gr.update(choices=ACTION_CHOICES if embodiment == "robot_arm" else ACTION_CHOICES[:1],
                  value=action_space, visible=kind == 1, interactive=False),
        gr.update(value=fps, visible=kind == 1, interactive=False),
        gr.update(value=", ".join(joint_names), visible=kind == 1 and action_space == "joint", interactive=False),
        gr.update(visible=False, interactive=False),
        gr.update(visible=False),
    ]


def workspace_values(selected, mode=None, *, reset_create=False):
    task = _resolve_selected(selected)
    mode = "task" if task else "create"
    create_mode = mode == "create"
    runtime = manager.view(str(task.task_dir) if task else None)
    available = not runtime["busy"]
    if task and runtime.get("state") in {"awaiting_feedback", "interrupted", "failed"}:
        states = dict(task.stage_states)
        stage = runtime.get("stage") or task.current_stage
        if stage in states and states[stage] != "completed":
            states[stage] = "review" if runtime["state"] == "awaiting_feedback" else "attention"
            task = replace(task, stage_states=states)
    review = runtime.get("review", {})
    has_input = bool(task and (task.task_dir / "input/input.json").is_file())
    blender_done = bool(task and task.stage_states["blender"] == "completed")
    render = {"phase": "not_started", "actions": [], "evaluation": {}}
    read_error = ""
    if blender_done:
        try:
            render = render_view(task.task_dir)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            read_error = f"Render status is temporarily unavailable: {exc}"
    actions = render["actions"]
    awaiting = runtime.get("state") == "awaiting_feedback" and bool(review)
    message = runtime.get("message") or read_error or "Choose an action to continue this task."
    next_step, activity = "Choose a stage below to view its results.", "ready"
    if task:
        if runtime.get("active_here") and runtime.get("state") == "running":
            activity = "running"
            stage = runtime.get("stage") or task.current_stage
            if stage == "input":
                message = task.task.get("input_step") or message
                next_step = "Wait for input validation and preparation to finish."
            elif stage == "blender":
                step = task.task.get("blender_step")
                message = {"scene_only_modeling": "Building the scene.",
                           "robot_only_modeling": "Preparing the robot model.",
                           "composed_modeling": "Composing the scene and animation.",
                           "composed_rendering": "Rendering the full white-model video for your review.",
                           "composed_feedback": "The composed video is ready for review.",
                           "rendering": "Publishing the accepted Blender result.",
                           "summarizing": "Saving the Blender stage summary."}.get(step, message)
                next_step = "Review the scene and robot images, then watch the composed video to check the scene and motion."
            elif stage == "render":
                step = task.task.get("render_step", "")
                message = {"preparing": "Preparing the video generation session.",
                           "preparing_media": "Preparing references and making the white-model video accessible.",
                           "generating": "Seedance is generating the video; waiting for the result and download.",
                           "generated": "Video downloaded. Preparing quality checks.",
                           "objective_evaluation": "Checking video quality and motion against the white-model video.",
                           "gpt_evaluation": "GPT is reviewing realism, motion and task consistency.",
                           "summarizing": "Saving the confirmed video's stage summary."}.get(step, message)
                next_step = "Wait for generation and evaluation. Then review the video and confirm it or request changes."
            elif stage == "harness":
                next_step = "Wait for experience archiving to finish. No input is needed."
        elif awaiting:
            activity = "attention"
            stage = {"scene_only": "Scene", "robot_only": "Robot", "composed": "Composed"}.get(review["stage"], review["stage"])
            message = f"{stage} · Round {review['round']} is ready for your review."
            next_step = ("Watch the full video in Composed. Accept it, or submit scene and motion changes."
                         if review["stage"] == "composed" else
                         "Review the image in the matching Blender subtab. Accept it, or submit changes.")
        elif render["phase"] == "awaiting_feedback":
            activity, message = "attention", "Video generation and evaluation are complete. Review the current result."
            next_step = "Open Render, then confirm the final video or submit revision instructions."
        elif render["phase"] == "export_pending":
            activity, message = "attention", "The video is confirmed; its stage summary still needs to finish."
            next_step = "Open Render and click “Retry stage summary”."
        elif runtime.get("state") in {"interrupted", "failed"} or "attention" in task.stage_states.values():
            activity = "attention"
            next_step = ("Open Render and retry evaluation or continue the saved generation. Check the runtime logs for the cause."
                         if blender_done else "Check the runtime logs, then click “Start / resume Blender” to continue.")
        elif task.current_stage == "completed":
            activity, message = "completed", "All three stages are complete. Your generated video is ready."
            next_step = "View the results below, or load this historical task to create another camera view."
        elif blender_done:
            message, next_step = "Blender results are ready.", "Open Render and click “Continue generation”."
        elif has_input:
            message, next_step = "Input preparation is complete. Blender is ready.", "Open Blender and click “Start / resume Blender”."
        if runtime["busy"] and not runtime.get("active_here"):
            message = runtime.get("message") or message
            next_step = "You can inspect this task's results. Switch to the active task to respond, or wait for it to finish."
    values = list(_task_outputs(task, editable=task is not None, message=message,
                                next_step=next_step, activity=activity))
    if task and review:
        media = review.get("video") if review.get("stage") == "composed" else review.get("preview")
        path = Path(media).resolve() if media else None
        if path and path.is_relative_to(task.task_dir) and path.is_file():
            stage_index = {"scene_only": 3, "robot_only": 4, "composed": 5}.get(review.get("stage"))
            if stage_index is not None:
                values[stage_index] = gr.update(value=str(path))
                values[8] = gr.update(selected=review["stage"])
    review_text = ((f"Composed · Round {review['round']}: watch the full video, then accept or request scene and motion changes."
                    if review["stage"] == "composed" else
                    f"{stage} · Round {review['round']}: review the image, then accept or request changes.") if awaiting
                   else "Scene → Robot → Composed · Review images first, then the full animation.")
    # 返回顺序须与 build_app 的 control_outputs 一致。
    input_updates = _input_form_updates(task, available=available, reset_create=reset_create)
    values.extend([
        review_text, review.get("token", ""),
        _evaluation_html(render["phase"], render.get("evaluation", {}), render.get("error") or read_error),
        gr.update(value="New task · Selected" if create_mode else "＋ Start new task",
                  interactive=available and not create_mode,
                  elem_classes=["sidebar-action", "is-selected"] if create_mode else ["sidebar-action"]),
        *input_updates,
        (gr.update(visible=True, interactive=False) if create_mode and (reset_create or not available) else
         gr.update(visible=True) if create_mode else
         gr.update(visible=False, interactive=False)),
        gr.update(interactive=available and has_input and not blender_done),
        gr.update(interactive=awaiting), gr.update(interactive=awaiting),
        gr.update(interactive=available and blender_done and "c" in actions),
        gr.update(interactive=available and blender_done and "e" in actions),
        gr.update(interactive=available and blender_done and "r" in actions),
        gr.update(interactive=available and blender_done and "" in actions),
        gr.update(interactive=available and blender_done and "x" in actions),
        gr.update(interactive=available and task is not None),
        gr.update(interactive=available and task is not None),
        gr.update(interactive=available),
    ])
    return values


def poll_workspace(selected, previous, mode):
    values = workspace_values(selected, mode)
    signatures = []
    for index, value in enumerate(values):
        signature = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if index in (3, 4, 5, 6, 7):
            media = value.get("value") if isinstance(value, dict) else value
            try:
                if media:
                    signature += str(Path(media).stat().st_mtime_ns)
            except (OSError, TypeError):
                pass
        signatures.append(signature)
    previous = previous or []
    updates = [gr.skip() if index < len(previous) and signature == previous[index] else values[index]
               for index, signature in enumerate(signatures)]
    return (*updates, signatures)


def refresh_workspace(selected=None):
    tasks = list_tasks(OUTPUT_ROOT)
    paths = {str(task.task_dir) for task in tasks}
    selected = selected if selected in paths else (str(tasks[0].task_dir) if tasks else None)
    mode = "task" if selected else "create"
    return (gr.update(choices=_task_choices(tasks), value=selected),
            *workspace_values(selected, mode, reset_create=not selected), [], mode)


def create_from_form(embodiment, asset, kind, text, image, trajectory, video, action_space, fps, joint_names, automatic):
    try:
        kind = int(kind)
        path = lambda value: Path(value) if value else None
        request = InputRequest(
            input_type=kind, asset_name=asset or "", task_text=(text or "").strip(),
            embodiment=embodiment,
            image=path(image) if kind in (0, 1) else None,
            trajectory=path(trajectory) if kind == 1 else None,
            video=path(video) if kind == 2 else None,
            action_space=action_space if kind == 1 else None,
            fps=fps if kind == 1 else None,
            joint_names=[name.strip() for name in (joint_names or "").split(",")]
                        if kind == 1 and action_space == "joint" else None,
        )
        selected = manager.create(request, automatic=automatic)
    except (ValueError, OSError, RuntimeError) as exc:
        return (*[gr.skip() for _ in range(REFRESH_OUTPUT_COUNT)], _notice(str(exc)))
    return (*refresh_workspace(str(selected)), {})


def load_history(selected):
    try:
        task = _resolve_selected(selected)
        if task is None or task.stage_states["blender"] != "completed":
            raise ValueError("Select a task with completed Blender results.")
        path = manager.load_history(selected)
    except (ValueError, OSError, RuntimeError) as exc:
        return (*[gr.skip() for _ in range(REFRESH_OUTPUT_COUNT)], _notice(str(exc)),
                gr.skip(), gr.skip())
    return (*refresh_workspace(str(path)), {}, gr.update(selected="blender"), gr.update(visible=False))


def execute_action(selected, operation, text="", images=None, token=""):
    try:
        if operation == "pause":
            manager.pause(selected)
        elif operation in {"accept", "revise_blender"}:
            manager.feedback(selected, token, "revise" if operation == "revise_blender" else "accept", text, images)
        elif operation in {"all", "blender"}:
            manager.start(selected, stage=operation)
        else:
            manager.start(selected, stage="render", action=operation, feedback=text)
    except (ValueError, OSError, RuntimeError) as exc:
        return (*[gr.skip() for _ in range(WORKSPACE_OUTPUT_COUNT)], _notice(str(exc)))
    return (*workspace_values(selected, "task"), [], {})


def open_rename_editor(selected):
    task = _resolve_selected(selected)
    if task is None:
        return gr.update(visible=False), "", "", _notice("Select a task first.")
    return (gr.update(visible=True), task.display_name, selected,
            {})


def apply_task_rename(target, display_name):
    try:
        selected = manager.rename_task(target, display_name)
    except (ValueError, OSError, RuntimeError) as exc:
        return (*[gr.skip() for _ in range(REFRESH_OUTPUT_COUNT)], _notice(str(exc)),
                gr.skip(), gr.skip(), gr.skip())
    return (*refresh_workspace(str(selected)),
            _notice(f"Task renamed to: {' '.join(str(display_name).split())}",
                    kind="success", title="Rename completed"),
            gr.update(visible=False), "", "")


def confirm_task_delete(target):
    task = _resolve_selected(target)
    display_name = task.display_name if task is not None else "Current task"
    try:
        manager.delete_task(target)
    except (ValueError, OSError, RuntimeError) as exc:
        result = {"ok": False, "name": display_name, "message": str(exc)}
        return (*[gr.skip() for _ in range(REFRESH_OUTPUT_COUNT)], {},
                result)
    result = {
        "ok": True,
        "name": display_name,
        "message": "Moved to the recycle directory. You can restore it from output/.trash/.",
    }
    return (*refresh_workspace(None),
            {},
            result)


def build_theme() -> gr.Theme:
    return gr.themes.Base(
        primary_hue="indigo",
        secondary_hue="violet",
        neutral_hue="slate",
        text_size="md",
        radius_size="lg",
    ).set(
        body_background_fill="#f3f5f9",
        body_background_fill_dark="#0d1220",
        body_text_color="#243044",
        body_text_color_dark="#edf1f8",
        body_text_color_subdued="#68758b",
        body_text_color_subdued_dark="#a2acc0",
        background_fill_primary="#ffffff",
        background_fill_primary_dark="#151c2c",
        background_fill_secondary="#f8f9fc",
        background_fill_secondary_dark="#1c2538",
        block_background_fill="#f8f9fc",
        block_background_fill_dark="#1c2538",
        block_border_color="#dce2ec",
        block_border_color_dark="#34405a",
        block_label_background_fill="#f8f9fc",
        block_label_background_fill_dark="#1c2538",
        block_label_text_color="#243044",
        block_label_text_color_dark="#edf1f8",
        input_background_fill="#ffffff",
        input_background_fill_dark="#111827",
        input_border_color="#dce2ec",
        input_border_color_dark="#34405a",
        input_placeholder_color="#8490a4",
        input_placeholder_color_dark="#7f8aa3",
        button_primary_background_fill="linear-gradient(135deg, #536ff0, #6e59dd)",
        button_primary_background_fill_dark="linear-gradient(135deg, #536ff0, #6e59dd)",
        button_primary_text_color="#ffffff",
        button_primary_text_color_dark="#ffffff",
        button_secondary_background_fill="#f8f9fc",
        button_secondary_background_fill_dark="#1c2538",
        button_secondary_border_color="#dce2ec",
        button_secondary_border_color_dark="#34405a",
        button_secondary_text_color="#243044",
        button_secondary_text_color_dark="#edf1f8",
    )


def build_app() -> gr.Blocks:
    assets = _assets()
    default_asset = _default_asset(assets)
    with gr.Blocks(title="CodeWM Studio", fill_height=True) as demo:
        display_cache = gr.State([])
        page_mode = gr.State("create")
        review_token = gr.State("")
        rename_target = gr.State("")
        delete_result = gr.JSON(value={}, visible="hidden", elem_id="delete-result-payload")
        gr.HTML("""
        <header class="app-header">
          <div class="brand-mark">CW</div>
          <div class="brand-copy">
            <h1>CodeWM Studio</h1>
            <p>Robot Video Generation Workspace</p>
          </div>
          <button id="cwm-theme-toggle" class="theme-toggle" type="button">☾ Dark</button>
        </header>
        """, js_on_load=THEME_SWITCH_JS)
        gr.HTML(DELETE_DIALOGS_HTML, elem_id="delete-dialog-host")
        delete_confirm_bridge = gr.Button(
            "", elem_id="delete-confirm-bridge", size="sm"
        )
        input_readiness_bridge = gr.Button(
            "", elem_id="input-readiness-bridge", size="sm"
        )
        stats_html = gr.HTML(_stats_html(list_tasks(OUTPUT_ROOT)), elem_id="stats-html")
        with gr.Group(visible=False, elem_classes="history-modal") as history_panel:
            gr.Markdown("### Load Historical Task")
            gr.Markdown("Choose a task with completed Blender results. A new copy will open at composed feedback.")
            history_picker = gr.Dropdown(label="Completed Blender task", choices=[],
                                         filterable=False, allow_custom_value=False,
                                         elem_id="history-picker")
            with gr.Row():
                history_cancel = gr.Button("Cancel", size="sm")
                history_confirm = gr.Button("Load as new task", variant="primary", interactive=False)

        with gr.Row(elem_classes="workspace-row"):
            with gr.Column(scale=3, min_width=250, elem_classes="workspace-panel left-panel"):
                gr.HTML("<div class='panel-heading'><span>Task Management</span></div>")
                task_picker = gr.Dropdown(label="Task", choices=[], interactive=True, filterable=False,
                                          allow_custom_value=False, elem_id="task-picker")
                new_task = gr.Button(
                    "New task · Selected", variant="primary", size="md", interactive=False,
                    elem_id="new-task-button", elem_classes=["sidebar-action", "is-selected"],
                )
                load_history_button = gr.Button(
                    "Load historical task", size="md", interactive=False,
                    elem_id="load-history-button", elem_classes="sidebar-action",
                )
                delete_button = gr.Button(
                    "Delete current task", variant="stop", size="md", interactive=False,
                    elem_id="delete-task-button", elem_classes="sidebar-action",
                )

            with gr.Column(scale=9, min_width=620, elem_classes="workspace-panel center-panel"):
                action_notice = gr.JSON(value={}, visible="hidden")
                pipeline_html = gr.HTML(_empty_pipeline(), elem_id="pipeline-html")
                rename_bridge = gr.Button("Open rename editor", interactive=False, elem_id="rename-task-bridge")
                with gr.Group(visible=False, elem_id="rename-task-editor") as rename_editor:
                    rename_name = gr.Textbox(label="Task name", max_length=80)
                    with gr.Row():
                        rename_save = gr.Button("Save name", variant="primary", size="sm")
                        rename_cancel = gr.Button("Cancel", size="sm")
                with gr.Tabs(elem_id="stage-tabs") as stage_tabs:
                    with gr.Tab("01 · Input", id="input"):
                        with gr.Row():
                            embodiment = gr.Dropdown(
                                choices=EMBODIMENT_CHOICES, value="robot_arm",
                                label="Embodiment", filterable=False,
                                allow_custom_value=False, scale=1,
                            )
                            asset_name = gr.Dropdown(
                                choices=assets,
                                value=default_asset or UPLOAD_ASSET_VALUE,
                                label="Robot asset",
                                allow_custom_value=False,
                                filterable=False,
                                scale=2,
                                elem_id="asset-picker",
                            )
                        with gr.Group(
                            visible=default_asset is None,
                            elem_id="asset-upload-panel",
                            elem_classes="asset-upload-panel",
                        ) as asset_upload_panel:
                            gr.Markdown("### Import Local Robot Asset")
                            asset_urdf_path = gr.Textbox(
                                label="URDF path on the computer running CodeWM",
                                placeholder="C:/robots/my_robot/urdf/robot.urdf",
                                info="Use an expanded URDF with valid relative resource paths. Keep its source files in place.",
                            )
                            asset_import_name = gr.Textbox(
                                label="Asset name",
                                placeholder="franka_panda_robotiq_2f85",
                                info="Start with a letter or digit; use 1 to 80 letters, digits, underscores or hyphens. The name is also used for the asset folder.",
                                max_length=80,
                            )
                            with gr.Row():
                                asset_import_cancel = gr.Button("Cancel", size="sm")
                                asset_import_button = gr.Button("Import and use", variant="primary", size="sm")
                        input_type = gr.Radio(
                            choices=[("Type 0 · First frame", "0"), ("Type 1 · First frame + trajectory", "1"), ("Type 2 · Demonstration video", "2")],
                            value="1", label="Input type", elem_id="input-type-picker",
                        )
                        task_text = gr.Textbox(
                            label="Task description",
                            placeholder="Describe the action the robot should perform…",
                            lines=3,
                            elem_id="task-text",
                        )
                        with gr.Row():
                            image_file = gr.Image(
                                label="First-frame image", type="filepath", height=176,
                                sources=["upload", "clipboard"], buttons=["fullscreen"],
                                placeholder="Click or drop an image",
                                elem_id="input-image-file", elem_classes="compact-upload",
                            )
                            trajectory_file = gr.File(
                                label="Trajectory · NPZ / Parquet",
                                file_types=[".parquet", ".npz"], height=176,
                                elem_id="input-trajectory-file", elem_classes="compact-upload",
                            )
                            video_file = gr.Video(
                                label="Demonstration video", visible=False, height=176,
                                sources=["upload"], elem_id="input-video-file",
                                elem_classes="compact-upload",
                            )
                        with gr.Row():
                            action_space = gr.Radio(
                                ACTION_CHOICES,
                                value="joint", label="Action space", elem_id="input-action-space",
                            )
                            fps = gr.Number(
                                value=15, minimum=0.01, label="Trajectory sampling rate (Hz)",
                                elem_id="input-fps",
                            )
                        joint_names = gr.Textbox(
                            label="URDF joint names in trajectory-column order",
                            placeholder="joint1, joint2, joint3",
                            info="Comma-separated. Include tool joints if their positions are supplied.",
                            elem_id="input-joint-names",
                        )
                        automatic = gr.Checkbox(
                            value=True,
                            label="Continue to Blender after Input (keep human review)",
                            info="Render always starts manually after Blender is complete.",
                            elem_id="automatic-continue",
                        )
                        input_button = gr.Button(
                            "Create task and run Input",
                            variant="primary",
                            interactive=False,
                        )

                    with gr.Tab("02 · Blender", id="blender"):
                        blender_start = gr.Button("Start / resume Blender", variant="primary", interactive=False)
                        review_status = gr.Textbox(value="Scene → Robot → Composed · Review images first, then the full animation.", show_label=False, interactive=False, elem_id="blender-review-status")
                        with gr.Tabs(elem_id="blender-stage-tabs") as blender_stage_tabs:
                            with gr.Tab("Scene", id="scene_only"):
                                scene_preview = gr.Image(
                                    label="Scene preview", height=320, interactive=False,
                                    buttons=["download", "fullscreen"],
                                    elem_id="blender-scene-preview",
                                )
                            with gr.Tab("Robot", id="robot_only"):
                                robot_preview = gr.Image(
                                    label="Robot preview", height=320, interactive=False,
                                    buttons=["download", "fullscreen"],
                                    elem_id="blender-robot-preview",
                                )
                            with gr.Tab("Composed", id="composed"):
                                blender_white_preview = gr.Video(
                                    label="Scene and motion · Full white-model video", height=360, interactive=False,
                                    elem_id="blender-white-preview",
                                )
                        with gr.Row():
                            blender_feedback = gr.Textbox(
                                label="Revision instructions",
                                lines=3,
                                placeholder="Describe scene, robot, or motion changes…",
                            )
                        with gr.Row():
                            blender_refs = gr.File(
                                label="Revision reference images",
                                file_count="multiple",
                                file_types=["image"],
                                height=112, elem_classes="compact-upload",
                                elem_id="blender-reference-files",
                            )
                        with gr.Row():
                            blender_accept = gr.Button("Accept and continue", variant="primary", interactive=False)
                            blender_revise = gr.Button("Submit revision", interactive=False)

                    with gr.Tab("03 · Render", id="render"):
                        with gr.Row():
                            white_video = gr.Video(label="Blender white model", height=300, interactive=False)
                            final_video = gr.Video(label="Current generated video", height=300, interactive=False)
                        with gr.Accordion("Video Evaluation Results", open=False):
                            evaluation_report = gr.HTML(
                                _evaluation_html("not_started", {}),
                                elem_id="evaluation-report",
                            )
                        render_feedback = gr.Textbox(label="Video revision instructions", lines=2, placeholder="For example: keep the camera stable and improve the realism of the wood texture…")
                        with gr.Row():
                            render_continue = gr.Button("Continue generation", variant="primary", interactive=False)
                            render_evaluate = gr.Button("Evaluate again", interactive=False)
                            render_revise = gr.Button("Submit revision", interactive=False)
                            render_confirm = gr.Button("Confirm final video", interactive=False)
                        render_export = gr.Button("Retry stage summary", interactive=False)

            with gr.Column(scale=4, min_width=310, elem_classes="workspace-panel right-panel"):
                gr.HTML("<div class='panel-heading'><span>Runtime Logs</span></div>")
                console_log = gr.HTML(_console_html(None), elem_id="console-log")

        task_outputs = [stats_html, pipeline_html, console_log,
                        scene_preview, robot_preview,
                        blender_white_preview, white_video, final_video, blender_stage_tabs]
        input_form_outputs = [embodiment, asset_name, input_type, task_text, image_file, trajectory_file,
                              video_file, action_space, fps, joint_names, automatic, asset_upload_panel]
        control_outputs = [review_status, review_token, evaluation_report,
                           new_task, *input_form_outputs, input_button,
                           blender_start,
                           blender_accept, blender_revise, render_continue, render_evaluate,
                           render_revise, render_confirm, render_export, delete_button, rename_bridge,
                           load_history_button]
        workspace_outputs = [*task_outputs, *control_outputs, display_cache]
        action_outputs = [*workspace_outputs, action_notice]
        refresh_outputs = [task_picker, *workspace_outputs, page_mode]
        def select_task(value):
            task = _resolve_selected(value)
            stage = task.current_stage if task else "input"
            if stage == "completed":
                stage = "render"
            return (*workspace_values(value), [], {}, gr.update(visible=False), "",
                    "task" if task else "create", gr.update(selected=stage),
                    gr.update(visible=False))

        demo.load(refresh_workspace, inputs=[task_picker], outputs=refresh_outputs,
                  queue=False, show_progress="hidden").then(
            select_task, inputs=[task_picker],
            outputs=[*action_outputs, rename_editor, rename_target, page_mode, stage_tabs,
                     history_panel],
            queue=False, show_progress="hidden",
        )
        task_picker.input(
            select_task,
            inputs=[task_picker],
            outputs=[*action_outputs, rename_editor, rename_target, page_mode, stage_tabs,
                     history_panel],
            queue=False,
            show_progress="hidden",
        )
        timer = gr.Timer(2)
        timer.tick(poll_workspace, inputs=[task_picker, display_cache, page_mode], outputs=workspace_outputs,
                   queue=False, show_progress="hidden")
        submit_inputs = [page_mode, input_type, embodiment, asset_name, task_text, image_file,
                         trajectory_file, video_file, action_space, fps, joint_names]
        embodiment.input(
            embodiment_changed, inputs=[embodiment, input_type],
            outputs=[asset_name, asset_upload_panel, action_space, joint_names],
            queue=False, show_progress="hidden",
        )
        asset_name.change(
            asset_upload_visibility,
            inputs=[asset_name],
            outputs=[asset_upload_panel],
            queue=False,
            preprocess=False,
            show_progress="hidden",
        )
        import_event = asset_import_button.click(
            import_robot_asset,
            inputs=[embodiment, asset_urdf_path, asset_import_name],
            outputs=[asset_name, asset_upload_panel, action_notice],
            show_progress="full",
        )
        import_event.then(
            input_submit_state,
            inputs=submit_inputs,
            outputs=[input_button],
            queue=False,
            preprocess=False,
            show_progress="hidden",
        )
        asset_import_cancel.click(
            lambda body: (gr.update(value=_default_asset(_assets(body))), gr.update(visible=False)),
            inputs=[embodiment],
            outputs=[asset_name, asset_upload_panel],
            queue=False,
            show_progress="hidden",
        )
        input_type.input(
            input_visibility,
            inputs=[input_type, embodiment, action_space],
            outputs=[image_file, trajectory_file, video_file, action_space, fps, joint_names],
            queue=False,
        )
        action_space.input(
            lambda kind, space: gr.update(visible=str(kind) == "1" and space == "joint"),
            inputs=[input_type, action_space], outputs=[joint_names], queue=False,
            show_progress="hidden",
        )
        def new_form():
            if manager.view()["busy"]:
                return (*[gr.skip() for _ in range(REFRESH_OUTPUT_COUNT)],
                        _notice("A task is running or awaiting review. Finish it before creating another task."),
                        gr.skip(), gr.skip(), gr.skip(), gr.skip())
            picker = gr.update(choices=_task_choices(list_tasks(OUTPUT_ROOT)), value=None)
            return (picker, *workspace_values(None, "create", reset_create=True), [], "create",
                    {}, gr.update(selected="input"), gr.update(visible=False), "",
                    gr.update(visible=False))

        new_task.click(new_form, outputs=[*refresh_outputs, action_notice, stage_tabs,
                                        rename_editor, rename_target, history_panel],
                       queue=False, show_progress="hidden")
        def open_history(selected):
            if manager.view()["busy"]:
                return gr.skip(), gr.skip(), gr.skip(), _notice("Finish the active task before loading a historical task.")
            tasks = [task for task in list_tasks(OUTPUT_ROOT) if task.stage_states["blender"] == "completed"]
            paths = {str(task.task_dir) for task in tasks}
            value = selected if selected in paths else str(tasks[0].task_dir) if tasks else None
            return (gr.update(visible=True), gr.update(choices=_history_choices(tasks), value=value),
                    gr.update(interactive=bool(tasks)), {})

        load_history_button.click(
            open_history, inputs=[task_picker],
            outputs=[history_panel, history_picker, history_confirm, action_notice],
            queue=False, show_progress="hidden",
        )
        history_cancel.click(lambda: gr.update(visible=False), outputs=[history_panel],
                             queue=False, show_progress="hidden")
        history_confirm.click(
            load_history, inputs=[history_picker],
            outputs=[*refresh_outputs, action_notice, stage_tabs, history_panel],
            queue=False, show_progress="hidden",
        )
        input_button.click(create_from_form,
                           inputs=[embodiment, asset_name, input_type, task_text, image_file, trajectory_file,
                                   video_file, action_space, fps, joint_names, automatic],
                           outputs=[*refresh_outputs, action_notice], queue=False, show_progress="hidden")
        # 旧值的 change 请求可能晚于新 choices 到达，此处读取原始值。
        # 创建任务仍使用 Gradio 的严格预处理。
        for component in (input_type, asset_name, image_file, trajectory_file, video_file,
                          action_space, fps, joint_names):
            component.change(
                input_submit_state,
                inputs=submit_inputs,
                outputs=[input_button],
                queue=False,
                preprocess=False,
                show_progress="hidden",
            )
        input_readiness_bridge.click(
            input_submit_state,
            inputs=submit_inputs,
            outputs=[input_button],
            queue=False,
            preprocess=False,
            show_progress="hidden",
        )
        for button, operation in ((blender_start, "blender"), (render_continue, "continue"),
                                  (render_evaluate, "evaluate"), (render_confirm, "confirm"),
                                  (render_export, "export")):
            def bind_action(operation):
                return lambda selected: execute_action(selected, operation)
            button.click(bind_action(operation), inputs=[task_picker], outputs=action_outputs,
                         queue=False, show_progress="hidden")
        blender_accept.click(lambda selected, token: execute_action(selected, "accept", token=token),
                             inputs=[task_picker, review_token], outputs=action_outputs, queue=False)
        blender_revise.click(lambda selected, token, text, images: execute_action(
            selected, "revise_blender", text=text, images=images, token=token),
            inputs=[task_picker, review_token, blender_feedback, blender_refs],
            outputs=action_outputs, queue=False)
        render_revise.click(lambda selected, text: execute_action(selected, "revise", text=text),
                            inputs=[task_picker, render_feedback], outputs=action_outputs, queue=False)
        rename_bridge.click(
            open_rename_editor,
            inputs=[task_picker],
            outputs=[rename_editor, rename_name, rename_target, action_notice],
            queue=False,
            show_progress="hidden",
        )
        rename_save.click(
            apply_task_rename,
            inputs=[rename_target, rename_name],
            outputs=[*refresh_outputs, action_notice, rename_editor, rename_name, rename_target],
            queue=False,
            show_progress="hidden",
        )
        rename_cancel.click(
            lambda: (gr.update(visible=False), "", "", {}),
            outputs=[rename_editor, rename_name, rename_target, action_notice],
            queue=False,
            show_progress="hidden",
        )
        action_notice.change(
            fn=None,
            inputs=[action_notice],
            js=ACTION_NOTICE_JS,
            queue=False,
            show_progress="hidden",
        )
        delete_event = delete_confirm_bridge.click(
            confirm_task_delete,
            inputs=[task_picker],
            outputs=[*refresh_outputs, action_notice, delete_result],
            queue=False,
            show_progress="hidden",
        )
        delete_event.then(
            fn=None,
            inputs=[delete_result],
            js=DELETE_RESULT_JS,
            queue=False,
            show_progress="hidden",
        )
    return demo


def main() -> None:
    demo = build_app()
    demo.queue(max_size=16, default_concurrency_limit=2)
    demo.launch(
        server_name="127.0.0.1",
        server_port=7860,
        inbrowser=True,
        share=False,
        show_error=True,
        allowed_paths=[str(OUTPUT_ROOT.resolve())],
        theme=build_theme(),
        css_paths=GUI_ROOT / "styles.css",
        footer_links=[],
    )


if __name__ == "__main__":
    main()

