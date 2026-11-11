#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

from Harness.experience_store import ExperienceStore, GPTExperienceManager
from config import (HARNESS_MODEL, HARNESS_OPENAI_BASE_URL, HARNESS_OPENAI_AUTH_FILE,
                    HARNESS_REASONING_EFFORT, HARNESS_OFFLINE)

SCHEMA = 1
STAGE_SUMMARY_SCHEMA = 1
SAFE_COMPONENT_RE = re.compile(r'^[^<>:"/\\|?*\x00-\x1f]+$')


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    tmp.replace(path)


def safe_component(value: Any, label: str) -> str:
    component = str(value).strip()
    if component in {"", ".", ".."} or not SAFE_COMPONENT_RE.fullmatch(component):
        raise SystemExit(f"{label} must be a single safe path component: {value!r}")
    return component


def load_manifest(run_dir: Path) -> Dict[str, Any]:
    path = run_dir / "manifest.json"
    data = read_json(path)
    if not isinstance(data, dict):
        raise SystemExit(f"manifest not found or invalid: {path}")
    return data


def save_manifest(run_dir: Path, manifest: Dict[str, Any]) -> None:
    manifest["updated_at"] = now()
    write_json(run_dir / "manifest.json", manifest)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def iter_paths(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for x in value:
            yield from iter_paths(x)
    elif isinstance(value, dict):
        for key in ("path", "file", "filepath", "artifact"):
            if isinstance(value.get(key), str):
                yield value[key]


def stage_summary_paths(stage: Dict[str, Any]) -> Iterable[str]:
    for key in ("inputs", "outputs", "artifacts"):
        yield from iter_paths(stage.get(key, []))


def load_stage_documents(run_dir: Path, manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    documents: List[Dict[str, Any]] = []
    for stage in manifest.get("stages", []):
        if not isinstance(stage, dict):
            continue
        name = str(stage.get("stage") or stage.get("name") or "unknown")
        summary_file = stage.get("summary_file")
        document: Any = None
        path = None
        if isinstance(summary_file, str):
            path = source_file(run_dir, manifest, summary_file)
            document = read_json(path)
        if not isinstance(document, dict):
            document = dict(stage)
        documents.append({"stage": name, "path": str(path) if path else None, "document": document})
    return documents


def stage_retrieved_experiences(documents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    seen = set()
    for item in documents:
        document = item.get("document") if isinstance(item, dict) else None
        values = document.get("retrieved_experiences", []) if isinstance(document, dict) else []
        for row in values if isinstance(values, list) else []:
            if not isinstance(row, dict) or not row.get("id") or row["id"] in seen:
                continue
            seen.add(row["id"])
            rows.append(dict(row))
    return rows


def normalize_stage_summary(stage: Dict[str, Any], source_path: Path | None = None) -> Dict[str, Any]:
    name = stage.get("stage") or stage.get("name")
    if not name:
        raise ValueError("stage JSON requires 'stage' or 'name'")
    safe_component(name, "stage name")
    summary = dict(stage)
    summary["stage"] = str(name)
    summary.setdefault("summary_schema_version", STAGE_SUMMARY_SCHEMA)
    summary.setdefault("status", "completed")
    summary.setdefault("revision", 1)
    try:
        summary["revision"] = max(1, int(summary["revision"]))
    except (TypeError, ValueError):
        raise ValueError("stage summary revision must be a positive integer") from None
    for key in ("inputs", "outputs", "artifacts", "checks", "changes", "lessons_candidates", "user_feedback", "retrieved_experiences"):
        if key in summary and not isinstance(summary[key], list):
            raise ValueError(f"stage summary '{key}' must be an array")
    if source_path:
        summary["summary_file"] = str(source_path.resolve())
    return summary


def source_file(run_dir: Path, manifest: Dict[str, Any], raw: str) -> Path:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = Path(manifest.get("source_dir") or run_dir).expanduser() / p
    return p.resolve()


def cmd_init(args: argparse.Namespace) -> None:
    task_id = safe_component(args.task_id, "task id")
    run_dir = Path(args.output_dir).expanduser().resolve() / task_id
    if (run_dir / "manifest.json").exists() and not args.force:
        raise SystemExit(f"run already exists: {run_dir} (use --force to replace manifest)")
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": SCHEMA,
        "run_id": f"{task_id}-{int(time.time())}",
        "task_id": task_id,
        "created_at": now(),
        "updated_at": now(),
        "source_dir": str(Path(args.source_dir).expanduser().resolve()) if args.source_dir else str(run_dir),
        "task": {"robot": args.robot, "task_type": args.task_type, "scene": args.scene},
        "inputs": {"files": [], "description": args.description},
        "stages": [],
        "evaluation": None,
        "feedback": None,
        "quality": None,
        "artifacts": [],
        "experience_refs": [],
    }
    save_manifest(run_dir, manifest)
    print(run_dir / "manifest.json")


def archive_run(run_dir: Path) -> Dict[str, int]:
    run_dir = Path(run_dir).expanduser().resolve()
    manifest = load_manifest(run_dir)
    entries: List[Dict[str, Any]] = []
    raw_paths: List[tuple[str, str]] = []
    for p in iter_paths(manifest.get("inputs", {}).get("files", [])):
        raw_paths.append(("inputs", p))
    for stage in manifest.get("stages", []):
        if not isinstance(stage, dict):
            continue
        name = safe_component(stage.get("stage") or stage.get("name") or "unknown", "stage name")
        if isinstance(stage.get("summary_file"), str):
            raw_paths.append((name, stage["summary_file"]))
        for p in stage_summary_paths(stage):
            raw_paths.append((name, p))
    evaluation = manifest.get("evaluation")
    if isinstance(evaluation, dict) and isinstance(evaluation.get("candidate"), str):
        raw_paths.append(("high_fidelity", evaluation["candidate"]))
    seen = set()
    missing = 0
    duplicates = 0
    for stage, raw in raw_paths:
        src = source_file(run_dir, manifest, raw)
        key = (stage, str(src))
        if key in seen:
            duplicates += 1
            continue
        if not src.is_file():
            missing += 1
            continue
        seen.add(key)
        digest = sha256(src)
        target = run_dir / "artifacts" / safe_component(stage, "stage name") / src.name
        if target.exists() and sha256(target) != digest:
            target = target.with_name(f"{target.stem}-{digest[:10]}{target.suffix}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(src, target)
        entries.append({"stage": stage, "source": str(src), "path": str(target.relative_to(run_dir)),
                        "size": src.stat().st_size, "mtime": datetime.fromtimestamp(src.stat().st_mtime, timezone.utc).isoformat(),
                        "sha256": digest})
    manifest["artifacts"] = entries
    save_manifest(run_dir, manifest)
    return {"archived": len(entries), "skipped_missing": missing, "skipped_duplicates": duplicates}


def cmd_archive(args: argparse.Namespace) -> None:
    print(json.dumps(archive_run(Path(args.run_dir)), ensure_ascii=False))


def cmd_record_stage(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).expanduser().resolve()
    manifest = load_manifest(run_dir)
    stage_path = Path(args.stage_json).expanduser().resolve()
    stage = read_json(stage_path)
    if not isinstance(stage, dict):
        raise SystemExit("stage JSON must contain an object")
    if args.stage:
        stage["stage"] = args.stage
    try:
        stage = normalize_stage_summary(stage, stage_path)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if args.status:
        stage["status"] = args.status
    stage["recorded_at"] = now()
    stages = [x for x in manifest.get("stages", []) if isinstance(x, dict) and (x.get("stage") or x.get("name")) != (stage.get("stage") or stage.get("name"))]
    stages.append(stage)
    manifest["stages"] = stages
    history = [x for x in manifest.get("stage_history", []) if isinstance(x, dict)]
    history.append(dict(stage))
    manifest["stage_history"] = history
    if isinstance(stage.get("evaluation"), dict):
        manifest["evaluation"] = stage["evaluation"]
        manifest["evaluation_source"] = stage.get("summary_file")
    save_manifest(run_dir, manifest)
    print(json.dumps(stage, ensure_ascii=False, indent=2))


def evaluate(evaluation: Dict[str, Any]) -> Dict[str, Any]:
    metrics = evaluation.get("metrics") or []
    issues = evaluation.get("issues") or []
    if not isinstance(metrics, list):
        raise ValueError("evaluation 'metrics' must be an array")
    if not isinstance(issues, list):
        raise ValueError("evaluation 'issues' must be an array")
    results = []
    errors = []
    warnings = []
    for index, item in enumerate(metrics):
        if not isinstance(item, dict):
            raise ValueError(f"metrics[{index}] must be an object")
        try:
            score = float(item["score"])
            threshold = float(item.get("threshold", 0.8))
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"metrics[{index}] requires numeric score and threshold") from None
        if not all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in (score, threshold)):
            raise ValueError(f"metrics[{index}] score and threshold must be finite values between 0 and 1")
        passed = score >= threshold
        severity = str(item.get("severity", "error" if not passed else "info"))
        if severity not in {"error", "warning", "info"}:
            raise ValueError(f"metrics[{index}] has invalid severity: {severity!r}")
        row = {"name": item.get("name", "unnamed"), "score": score, "threshold": threshold,
               "passed": passed, "severity": severity, "notes": item.get("notes")}
        results.append(row)
        if not passed:
            (errors if severity == "error" else warnings).append(row)
    for index, issue in enumerate(issues):
        if not isinstance(issue, dict):
            raise ValueError(f"issues[{index}] must be an object")
        severity = issue.get("severity", "warning")
        if severity not in {"error", "warning"}:
            raise ValueError(f"issues[{index}] has invalid severity: {severity!r}")
    issue_errors = [x for x in issues if x.get("severity", "warning") == "error"]
    issue_warnings = [x for x in issues if x.get("severity", "warning") == "warning"]
    errors.extend(issue_errors)
    warnings.extend(issue_warnings)
    status = "failed" if errors else ("needs_review" if warnings else "passed")
    return {"status": status, "candidate": evaluation.get("candidate"), "metrics": results,
            "errors": errors, "warnings": warnings, "vlm": evaluation.get("vlm"),
            "evaluated_at": now()}


def assess_run(run_dir: Path, evaluation_path: str | Path | None = None) -> Dict[str, Any]:
    run_dir = Path(run_dir).expanduser().resolve()
    manifest = load_manifest(run_dir)
    evaluation = read_json(Path(evaluation_path).expanduser().resolve()) if evaluation_path else manifest.get("evaluation")
    if not isinstance(evaluation, dict):
        raise SystemExit("an evaluation JSON is required")
    try:
        quality = evaluate(evaluation)
    except ValueError as exc:
        raise SystemExit(f"invalid evaluation: {exc}") from exc
    manifest["evaluation"] = evaluation
    manifest["quality"] = quality
    save_manifest(run_dir, manifest)
    write_json(run_dir / "reports" / "quality.json", quality)
    return quality


def cmd_assess(args: argparse.Namespace) -> None:
    quality = assess_run(args.run_dir, args.evaluation)
    print(json.dumps(quality, ensure_ascii=False, indent=2))


def markdown_cell(value: Any, default: str = "") -> str:
    text = str(value) if value not in (None, "") else default
    return text.replace("|", "\\|").replace("\r\n", "<br>").replace("\r", "<br>").replace("\n", "<br>")


def markdown_report(manifest: Dict[str, Any]) -> str:
    q = manifest.get("quality") or {}
    lines = [f"# Task report: {manifest.get('task_id', 'unknown')}", "", f"- Run: `{manifest.get('run_id')}`",
             f"- Created: {manifest.get('created_at')}", f"- Quality: **{q.get('status', 'not_assessed')}**", ""]
    task = manifest.get("task", {})
    if task:
        lines += ["## Task", "", f"- Robot: {task.get('robot') or '—'}", f"- Type: {task.get('task_type') or '—'}", f"- Scene: {task.get('scene') or '—'}", ""]
    lines += ["## Stages", "", "| Stage | Status | Notes |", "|---|---|---|"]
    for stage in manifest.get("stages", []):
        if isinstance(stage, dict):
            name = markdown_cell(stage.get("stage", stage.get("name")), "—")
            status = markdown_cell(stage.get("status"), "—")
            notes = markdown_cell(stage.get("notes"))
            lines.append(f"| {name} | {status} | {notes} |")
    lines += ["", "## Evaluation", "", "| Metric | Score | Threshold | Result |", "|---|---:|---:|---|"]
    for m in q.get("metrics", []):
        lines.append(f"| {markdown_cell(m['name'])} | {m['score']:.3f} | {m['threshold']:.3f} | {'pass' if m['passed'] else 'fail'} |")
    if q.get("errors") or q.get("warnings"):
        lines += ["", "## Issues", ""]
        for issue in q.get("errors", []) + q.get("warnings", []):
            lines.append(f"- **{issue.get('severity', 'warning')}** {issue.get('message') or issue.get('name')}: {issue.get('recommendation') or issue.get('notes') or ''}")
    lines += ["", f"Archived artifacts: **{len(manifest.get('artifacts', []))}**", ""]
    return "\n".join(lines)


def report_run(run_dir: Path) -> Path:
    run_dir = Path(run_dir).expanduser().resolve()
    manifest = load_manifest(run_dir)
    report = {"manifest": manifest, "generated_at": now()}
    write_json(run_dir / "reports" / "task_report.json", report)
    path = run_dir / "reports" / "task_report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown_report(manifest), encoding="utf-8")
    return path


def cmd_report(args: argparse.Namespace) -> None:
    print(report_run(args.run_dir))


def append_experiences(run_dir: Path, kind: str, rows: List[Dict[str, Any]]) -> int:
    path = run_dir / "experiences" / f"{kind}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
                existing.add(json.dumps([item.get("rule"), item.get("scope")], sort_keys=True, ensure_ascii=False))
            except json.JSONDecodeError:
                pass
    count = 0
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            if not isinstance(row, dict) or not row.get("rule"):
                continue
            item = dict(row)
            item.pop("confidence", None)
            item.setdefault("created_at", now())
            key = json.dumps([item.get("rule"), item.get("scope")], sort_keys=True, ensure_ascii=False)
            if key in existing:
                continue
            f.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
            existing.add(key)
            count += 1
    return count


def learn_run(
    run_dir: Path,
    feedback_path: str | Path | None = None,
    experience_store: str | Path | None = None,
    *,
    model: str = HARNESS_MODEL,
    base_url: str = HARNESS_OPENAI_BASE_URL,
    auth_file: str | Path = HARNESS_OPENAI_AUTH_FILE,
    reasoning_effort: str = HARNESS_REASONING_EFFORT,
    offline: bool = HARNESS_OFFLINE,
    transport=None,
) -> Dict[str, Any]:
    run_dir = Path(run_dir).expanduser().resolve()
    manifest = load_manifest(run_dir)
    feedback = read_json(Path(feedback_path).expanduser().resolve()) if feedback_path else manifest.get("feedback") or {}
    general = [dict(row, _requested_kind="general") if isinstance(row, dict) else row for row in (feedback.get("general", []) if isinstance(feedback, dict) else [])]
    specific = [dict(row, _requested_kind="specific") if isinstance(row, dict) else row for row in (feedback.get("specific", []) if isinstance(feedback, dict) else [])]
    for stage in manifest.get("stages", []):
        if not isinstance(stage, dict):
            continue
        for lesson in stage.get("lessons_candidates", []):
            if isinstance(lesson, dict):
                lesson = dict(lesson)
                lesson.setdefault("_requested_kind", lesson.get("kind") or
                                  ("specific" if lesson.get("scope") else "general"))
                (specific if lesson.get("_requested_kind") == "specific" else general).append(lesson)
        stage_evaluation = stage.get("evaluation")
        for issue in stage_evaluation.get("issues", []) if isinstance(stage_evaluation, dict) else []:
            if isinstance(issue, dict) and issue.get("recommendation"):
                recommendation = {
                    "rule": issue["recommendation"], "evidence": issue.get("id"),
                    "tags": [issue.get("category", stage.get("stage", "stage"))],
                    "_requested_kind": "specific",
                }
                specific.append(recommendation)
    for issue in (manifest.get("quality") or {}).get("errors", []):
        if isinstance(issue, dict) and issue.get("recommendation"):
            recommendation = {"rule": issue["recommendation"], "evidence": issue.get("id") or issue.get("name"), "tags": [issue.get("category", "quality")], "_requested_kind": "specific" if manifest.get("task") else "general"}
            (specific if manifest.get("task") else general).append(recommendation)
    g = append_experiences(run_dir, "general", general)
    s = append_experiences(run_dir, "specific", specific)
    # 本轮 JSONL 保留原始候选，复用库由模型决策更新。
    global_changed = {"general": 0, "specific": 0}
    global_actions = {"created": 0, "updated": 0, "deleted": 0, "noop": 0, "rejected": 0}
    decision_mode = "disabled"
    decision_error = None
    if experience_store:
        task = manifest.get("task", {}) if isinstance(manifest.get("task"), dict) else {}
        context = {"scope": {"robot": task.get("robot"), "task_type": task.get("task_type"), "scene": task.get("scene")}, "task_id": manifest.get("task_id")}
        candidates = general + specific
        stage_documents = load_stage_documents(run_dir, manifest)
        store = ExperienceStore(Path(experience_store))
        # 使用上游实际检索过的经验，收尾阶段不再检索或重排。
        retrieved_existing = stage_retrieved_experiences(stage_documents)
        manager = GPTExperienceManager(
            model=model, base_url=base_url, auth_file=auth_file,
            reasoning_effort=reasoning_effort, offline=offline, transport=transport,
        )
        decision = manager.decide(candidates, retrieved_existing, context, documents=stage_documents)
        stats = store.apply_actions(decision["actions"], source_run=manifest.get("run_id", ""))
        global_changed = stats.pop("changed_by_kind")
        global_actions = stats
        decision_mode = decision["mode"]
        decision_error = decision["error"]
        write_json(run_dir / "experiences" / "gpt_crud_decisions.json", {
            "mode": decision_mode, "error": decision_error, "actions": decision["actions"],
            "summary": decision.get("summary", {}),
            "stage_documents": [{"stage": item["stage"], "path": item.get("path")} for item in stage_documents],
            "retrieved_experience_ids": [row.get("id") for row in retrieved_existing],
            "stats": global_actions, "decided_at": now(),
        })
        if decision_error:
            print(f"warning: GPT CRUD decision failed; used offline fallback ({decision_error})", file=sys.stderr)
        manifest["global_experience_store"] = str(Path(experience_store).expanduser().resolve())
    manifest["feedback"] = feedback
    manifest["experience_refs"] = [
        {"kind": "general", "count": g, "global_changed": global_changed["general"]},
        {"kind": "specific", "count": s, "global_changed": global_changed["specific"]},
    ]
    manifest["experience_decision"] = {"mode": decision_mode, "error": decision_error, "stats": global_actions}
    save_manifest(run_dir, manifest)
    return {
        "general_added": g, "specific_added": s, "decision_mode": decision_mode,
        "global_changed": global_changed, "global_actions": global_actions,
    }


def cmd_learn(args: argparse.Namespace) -> None:
    result = learn_run(
        args.run_dir, args.feedback, args.experience_store,
        model=args.model, base_url=args.base_url, auth_file=args.auth_file,
        offline=args.offline,
    )
    print(json.dumps(result, ensure_ascii=False))


def query_experiences(
    experience_store: str | Path,
    context: Dict[str, Any],
    limit: int = 8,
    *,
    model: str = HARNESS_MODEL,
    base_url: str = HARNESS_OPENAI_BASE_URL,
    auth_file: str | Path = HARNESS_OPENAI_AUTH_FILE,
    reasoning_effort: str = HARNESS_REASONING_EFFORT,
    offline: bool = HARNESS_OFFLINE,
    transport=None,
) -> Dict[str, Any]:
    if not isinstance(context, dict):
        raise ValueError("context must be an object")
    limit = max(0, int(limit))
    store = ExperienceStore(Path(experience_store))
    manager = GPTExperienceManager(
        model=model, base_url=base_url, auth_file=auth_file,
        reasoning_effort=reasoning_effort, offline=offline, transport=transport,
    )
    candidates = store.compatible(context)
    decision = manager.select(candidates, context, limit)
    if decision["mode"] == "gpt":
        by_id = {row.get("id"): row for row in candidates}
        results = []
        for selection in decision["selections"]:
            row = dict(by_id[selection["id"]])
            row["retrieval_score"] = selection["relevance"]
            row["retrieval_reason"] = selection["reason"]
            row["retrieval_mode"] = "gpt"
            results.append(row)
    else:
        results = store.retrieve(context, limit=limit)
        for row in results:
            row["retrieval_mode"] = "offline"
    return {
        "experiences": results,
        "mode": decision["mode"],
        "candidate_count": len(candidates),
        "returned_count": len(results),
        "limit": limit,
        "error": decision["error"],
    }


def cmd_query_experiences(args: argparse.Namespace) -> None:
    context = read_json(Path(args.context).expanduser().resolve()) if args.context else {}
    try:
        response = query_experiences(
            args.experience_store, context, limit=args.limit,
            model=args.model, base_url=args.base_url, auth_file=args.auth_file,
            offline=args.offline,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if response["error"]:
        print(f"warning: GPT retrieval failed; used offline fallback ({response['error']})", file=sys.stderr)
    print(json.dumps(response, ensure_ascii=False, indent=2))


def cmd_finalize(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).expanduser().resolve()
    manifest = load_manifest(run_dir)
    if args.evaluation:
        assess_run(run_dir, args.evaluation)
    elif isinstance(manifest.get("evaluation"), dict) and not manifest.get("quality"):
        assess_run(run_dir)
    archive_run(run_dir)
    learn_run(
        run_dir, args.feedback, args.experience_store,
        model=args.model, base_url=args.base_url, auth_file=args.auth_file,
        offline=args.offline,
    )
    report_path = report_run(run_dir)
    manifest = load_manifest(run_dir)
    print(json.dumps({
        "run_dir": str(run_dir),
        "status": (manifest.get("quality") or {}).get("status", "not_assessed"),
        "report": str(report_path),
    }, ensure_ascii=False))


def add_api_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", default=HARNESS_MODEL)
    parser.add_argument("--base-url", default=HARNESS_OPENAI_BASE_URL)
    parser.add_argument("--auth-file", type=Path, default=HARNESS_OPENAI_AUTH_FILE)
    parser.add_argument("--offline", action="store_true", default=HARNESS_OFFLINE)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    i = sub.add_parser("init", help="create a run manifest")
    i.add_argument("--task-id", required=True)
    i.add_argument("--output-dir", default="runs")
    i.add_argument("--source-dir")
    i.add_argument("--robot", default="")
    i.add_argument("--task-type", default="")
    i.add_argument("--scene", default="")
    i.add_argument("--description", default="")
    i.add_argument("--force", action="store_true")
    i.set_defaults(func=cmd_init)
    for name, func in (("archive", cmd_archive), ("assess", cmd_assess), ("report", cmd_report), ("learn", cmd_learn), ("finalize", cmd_finalize)):
        x = sub.add_parser(name)
        x.add_argument("--run-dir", required=True)
        if name in {"assess", "finalize"}:
            x.add_argument("--evaluation")
        if name in {"learn", "finalize"}:
            x.add_argument("--feedback")
            x.add_argument("--experience-store")
            add_api_arguments(x)
        x.set_defaults(func=func)
    s = sub.add_parser("record-stage", help="append or replace a stage result JSON")
    s.add_argument("--run-dir", required=True)
    s.add_argument("--stage-json", required=True)
    s.add_argument("--stage")
    s.add_argument("--status")
    s.set_defaults(func=cmd_record_stage)
    r = sub.add_parser("query-experiences", help="query the local experience store and return an envelope")
    r.add_argument("--experience-store", required=True)
    r.add_argument("--context", required=True)
    r.add_argument("--limit", type=int, default=8)
    add_api_arguments(r)
    r.set_defaults(func=cmd_query_experiences)
    return p


if __name__ == "__main__":
    try:
        cli_args = parser().parse_args()
        cli_args.func(cli_args)
    except KeyboardInterrupt:
        sys.exit(130)
