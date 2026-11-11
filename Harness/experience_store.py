#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from utils.prompts import load_prompt
from typing import Any, Dict, Iterable, List, Tuple

from config import (HARNESS_MODEL, HARNESS_OPENAI_BASE_URL, HARNESS_OPENAI_AUTH_FILE,
                    HARNESS_REASONING_EFFORT, HARNESS_CONNECT_TIMEOUT_SECONDS,
                    HARNESS_MAX_OUTPUT_TOKENS, HARNESS_MAX_RETRIES,
                    HARNESS_RETRY_BASE_DELAY, HARNESS_OFFLINE)

TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)
CRUD_ACTIONS = {"create", "update", "delete", "noop"}
SCOPE_ALIASES = {
    "robot": "robot", "arm": "robot", "机械臂": "robot", "机器人": "robot",
    "gripper": "gripper", "夹爪": "gripper",
    "task": "task_type", "task_type": "task_type", "任务": "task_type",
    "scene": "scene", "场景": "scene",
    "asset": "asset", "asset_name": "asset", "资产": "asset",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value: Any) -> str:
    return str(value).strip().lower() if value is not None else ""


def normalize_scope(scope: Any) -> Dict[str, str]:
    if not isinstance(scope, dict):
        return {}
    result: Dict[str, str] = {}
    for raw_key, raw_value in scope.items():
        key = SCOPE_ALIASES.get(_text(raw_key), _text(raw_key))
        value = _text(raw_value)
        if key and value and value not in {"*", "any", "all"}:
            result[key] = value
    return result


def tokens(value: Any) -> set[str]:
    return set(TOKEN_RE.findall(_text(value)))


def normalize_tags(value: Any) -> List[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = []
    return sorted({_text(tag) for tag in values if _text(tag)})


def normalize_relevance(value: Any) -> float:
    try:
        relevance = float(value)
        if math.isfinite(relevance):
            return max(0.0, min(1.0, relevance))
    except (TypeError, ValueError):
        pass
    return 0.5


def evidence_count(value: Any) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    return [] if value is None else [value]


def canonical_scope(scope: Dict[str, str]) -> str:
    return json.dumps(normalize_scope(scope), ensure_ascii=False, sort_keys=True)


def stable_id(kind: str, rule: str, scope: Dict[str, str]) -> str:
    raw = f"{kind}\0{_text(rule)}\0{canonical_scope(scope)}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


def _evidence_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def decision_key(source_run: str, target_id: str, evidence: Any) -> str:
    if not source_run:
        return ""
    raw = f"{source_run}\0{target_id}\0{_evidence_key(evidence)}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


@contextmanager
def file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class ExperienceStore:

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()

    def _path(self, kind: str) -> Path:
        if kind not in {"general", "specific"}:
            raise ValueError(f"unknown experience kind: {kind}")
        return self.root / f"{kind}.jsonl"

    def _read(self, kind: str) -> List[Dict[str, Any]]:
        path = self._path(kind)
        if not path.exists():
            return []
        rows: List[Dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
                if isinstance(value, dict) and value.get("rule"):
                    value.pop("confidence", None)
                    rows.append(value)
            except json.JSONDecodeError:
                continue
        return rows

    def _write(self, kind: str, rows: List[Dict[str, Any]]) -> None:
        path = self._path(kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        tmp.replace(path)

    @staticmethod
    def _new_row(
        kind: str,
        rule: str,
        scope: Dict[str, str],
        experience: Dict[str, Any],
        source_run: str = "",
        reason: str | None = None,
        observation_key: str = "",
    ) -> Dict[str, Any]:
        timestamp = now()
        evidence = experience.get("evidence")
        row = {
            "id": stable_id(kind, rule, scope),
            "kind": kind,
            "rule": rule,
            "scope": scope,
            "evidence_count": 1,
            "evidence": [evidence] if evidence not in (None, "") else [],
            "tags": normalize_tags(experience.get("tags")),
            "source_runs": [source_run] if source_run else [],
            "created_at": timestamp,
            "updated_at": timestamp,
            "status": "active",
        }
        if reason is not None:
            row.update({
                "decision_runs": [source_run] if source_run else [],
                "decision_keys": [observation_key] if observation_key else [],
                "last_decision_reason": reason,
            })
        return row

    @staticmethod
    def _merge_row(
        row: Dict[str, Any],
        experience: Dict[str, Any],
        source_run: str = "",
        reason: str | None = None,
        observation_key: str = "",
    ) -> bool:
        prior_evidence = as_list(row.get("evidence"))
        incoming_evidence = experience.get("evidence")
        same_evidence = incoming_evidence in (None, "") or any(
            _evidence_key(value) == _evidence_key(incoming_evidence)
            for value in prior_evidence
        )
        source_runs = [str(value) for value in as_list(row.get("source_runs"))]
        decision_keys = [str(value) for value in as_list(row.get("decision_keys"))]
        if observation_key:
            if observation_key in decision_keys:
                return False
        elif source_run and source_run in source_runs and same_evidence:
            return False

        count = evidence_count(row.get("evidence_count"))
        row["evidence_count"] = count + 1
        row["tags"] = sorted(
            set(normalize_tags(row.get("tags"))) | set(normalize_tags(experience.get("tags")))
        )
        if incoming_evidence not in (None, "") and not same_evidence:
            prior_evidence.append(incoming_evidence)
        row["evidence"] = prior_evidence
        if source_run:
            source_runs.append(source_run)
        row["source_runs"] = sorted(set(source_runs))
        row["updated_at"] = now()

        if reason is not None:
            decision_runs = [str(value) for value in as_list(row.get("decision_runs"))]
            if source_run:
                decision_runs.append(source_run)
            if observation_key:
                decision_keys.append(observation_key)
            row.update({
                "decision_runs": sorted(set(decision_runs)),
                "decision_keys": sorted(set(decision_keys)),
                "last_decision_reason": reason,
                "status": "active",
            })
            row.pop("deleted_at", None)
        return True

    def upsert(self, kind: str, items: Iterable[Dict[str, Any]], source_run: str = "") -> int:
        candidates = list(items)
        with file_lock(self.root / f".{kind}.lock"):
            rows = self._read(kind)
            index = {(row.get("id") or stable_id(kind, row.get("rule", ""), normalize_scope(row.get("scope")))): i
                     for i, row in enumerate(rows)}
            changed = 0
            for candidate in candidates:
                if not isinstance(candidate, dict) or not candidate.get("rule"):
                    continue
                scope = normalize_scope(candidate.get("scope"))
                rule = str(candidate["rule"]).strip()
                rid = stable_id(kind, rule, scope)
                if rid in index:
                    row = rows[index[rid]]
                    if not self._merge_row(row, candidate, source_run=source_run):
                        continue
                else:
                    row = self._new_row(kind, rule, scope, candidate, source_run=source_run)
                    rows.append(row)
                    index[rid] = len(rows) - 1
                changed += 1
            if changed:
                self._write(kind, rows)
            return changed

    def apply_actions(self, actions: Iterable[Dict[str, Any]], source_run: str = "") -> Dict[str, Any]:
        stats: Dict[str, Any] = {
            "created": 0, "updated": 0, "deleted": 0, "noop": 0, "rejected": 0,
            "changed_by_kind": {"general": 0, "specific": 0},
        }
        with file_lock(self.root / ".general.lock"), file_lock(self.root / ".specific.lock"):
            rows = {kind: self._read(kind) for kind in ("general", "specific")}
            index = {
                row.get("id"): (kind, row)
                for kind, collection in rows.items()
                for row in collection
                if row.get("id")
            }
            changed_kinds = set()
            decided_targets = set()

            def locate(target_id: str) -> Tuple[str, Dict[str, Any]] | None:
                return index.get(target_id)

            for decision in actions:
                if not isinstance(decision, dict) or decision.get("action") not in CRUD_ACTIONS:
                    stats["rejected"] += 1
                    continue
                action = decision["action"]
                if action == "noop":
                    stats["noop"] += 1
                    continue
                reason = str(decision.get("reason") or "").strip()
                target_id = str(decision.get("target_id") or "").strip()
                experience = decision.get("experience") if isinstance(decision.get("experience"), dict) else {}
                if action in {"update", "delete"}:
                    if target_id in decided_targets:
                        stats["rejected"] += 1
                        continue
                    decided_targets.add(target_id)

                if action == "create":
                    kind = experience.get("kind")
                    rule = str(experience.get("rule") or "").strip()
                    if kind not in rows or not rule:
                        stats["rejected"] += 1
                        continue
                    scope = normalize_scope(experience.get("scope"))
                    rid = stable_id(kind, rule, scope)
                    if locate(rid):
                        stats["noop"] += 1
                        continue
                    evidence = experience.get("evidence")
                    observation_key = decision_key(source_run, rid, evidence)
                    row = self._new_row(
                        kind, rule, scope, experience, source_run=source_run,
                        reason=reason, observation_key=observation_key,
                    )
                    rows[kind].append(row)
                    index[rid] = (kind, row)
                    changed_kinds.add(kind)
                    stats["created"] += 1
                    stats["changed_by_kind"][kind] += 1
                    continue

                located = locate(target_id)
                if not located:
                    stats["rejected"] += 1
                    continue
                kind, row = located
                if action == "delete":
                    if row.get("status", "active") != "active":
                        stats["noop"] += 1
                        continue
                    row.update({
                        "status": "inactive", "deleted_at": now(), "updated_at": now(),
                        "last_decision_reason": reason,
                    })
                    changed_kinds.add(kind)
                    stats["deleted"] += 1
                    stats["changed_by_kind"][kind] += 1
                    continue

                incoming_evidence = experience.get("evidence")
                observation_key = decision_key(source_run, target_id, incoming_evidence)
                if not self._merge_row(
                    row, experience, source_run=source_run,
                    reason=reason, observation_key=observation_key,
                ):
                    stats["noop"] += 1
                    continue
                changed_kinds.add(kind)
                stats["updated"] += 1
                stats["changed_by_kind"][kind] += 1

            for kind in changed_kinds:
                self._write(kind, rows[kind])
        return stats

    def all(self) -> List[Dict[str, Any]]:
        return self._read("general") + self._read("specific")

    @staticmethod
    def _compatible(scope: Dict[str, str], context: Dict[str, str]) -> Tuple[bool, int, int]:
        if not scope:
            return True, 0, 0
        matched = 0
        for key, value in scope.items():
            if key in context:
                if context[key] == value:
                    matched += 1
                else:
                    return False, matched, len(scope)
        return True, matched, len(scope)

    def compatible(self, context: Dict[str, Any]) -> List[Dict[str, Any]]:
        context_scope = normalize_scope(context.get("scope") if isinstance(context, dict) else {})
        context_scope.update(normalize_scope(context))
        candidates = []
        for row in self.all():
            if row.get("status", "active") != "active":
                continue
            compatible, matched, total = self._compatible(normalize_scope(row.get("scope")), context_scope)
            if compatible:
                result = dict(row)
                result["match"] = {"scope_matches": matched, "scope_fields": total}
                candidates.append(result)
        return candidates

    def retrieve(self, context: Dict[str, Any], limit: int = 8) -> List[Dict[str, Any]]:
        context_tags = set(normalize_tags(context.get("tags") if isinstance(context, dict) else []))
        query_text = " ".join(str(context.get(k, "")) for k in ("text", "description", "task_type", "scene")) if isinstance(context, dict) else ""
        query_tokens = tokens(query_text)
        ranked: List[Dict[str, Any]] = []
        for row in self.compatible(context):
            scope = normalize_scope(row.get("scope"))
            matched = row["match"]["scope_matches"]
            total = row["match"]["scope_fields"]
            row_tags = set(normalize_tags(row.get("tags")))
            tag_score = len(context_tags & row_tags) / max(1, len(row_tags)) if row_tags else 0.0
            text_score = len(query_tokens & tokens(row.get("rule", ""))) / max(1, len(query_tokens)) if query_tokens else 0.0
            scope_score = matched / total if total else 0.0
            specificity = min(1.0, len(scope) / 4.0)
            quality = min(1.0, evidence_count(row.get("evidence_count")) / 5.0)
            score = 0.36 * scope_score + 0.20 * specificity + 0.18 * tag_score + 0.12 * text_score + 0.14 * quality
            if row.get("kind") == "general":
                score *= 0.92
            result = dict(row)
            result["retrieval_score"] = round(score, 5)
            result["match"] = {"scope_matches": matched, "scope_fields": total, "tag_overlap": sorted(context_tags & row_tags)}
            ranked.append(result)
        ranked.sort(key=lambda x: (x["retrieval_score"], evidence_count(x.get("evidence_count"))), reverse=True)
        return ranked[: max(0, int(limit))]


class GPTExperienceManager:
    CRUD_SCHEMA = {
        "type": "object", "additionalProperties": False, "required": ["summary", "actions"],
        "properties": {
            "summary": {
                "type": "object", "additionalProperties": False,
                "required": ["overall", "stage_summaries", "experience_candidates"],
                "properties": {
                    "overall": {"type": "string"},
                    "stage_summaries": {
                        "type": "array", "items": {
                            "type": "object", "additionalProperties": False,
                            "required": ["stage", "summary"],
                            "properties": {
                                "stage": {"type": "string"},
                                "summary": {"type": "string"},
                            },
                        },
                    },
                    "experience_candidates": {
                        "type": "array", "items": {
                            "type": "object", "additionalProperties": False,
                            "required": ["kind", "rule", "scope", "evidence", "tags"],
                            "properties": {
                                "kind": {"type": "string", "enum": ["general", "specific"]},
                                "rule": {"type": "string"},
                                "scope": {
                                    "type": "array", "items": {
                                        "type": "object", "additionalProperties": False,
                                        "required": ["key", "value"],
                                        "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
                                    },
                                },
                                "evidence": {"type": "string"},
                                "tags": {"type": "array", "items": {"type": "string"}},
                            },
                        },
                    },
                },
            },
            "actions": {
                "type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["action", "target_id", "kind", "rule", "scope", "evidence", "tags", "reason"],
                    "properties": {
                        "action": {"type": "string", "enum": ["create", "update", "delete", "noop"]},
                        "target_id": {"type": "string"},
                        "kind": {"type": "string", "enum": ["general", "specific"]},
                        "rule": {"type": "string"},
                        "scope": {
                            "type": "array", "items": {
                                "type": "object", "additionalProperties": False,
                                "required": ["key", "value"],
                                "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
                            },
                        },
                        "evidence": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "reason": {"type": "string"},
                    },
                },
            },
        },
    }
    RETRIEVAL_SCHEMA = {
        "type": "object", "additionalProperties": False, "required": ["selections"],
        "properties": {
            "selections": {
                "type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["id", "relevance", "reason"],
                    "properties": {
                        "id": {"type": "string"},
                        "relevance": {"type": "number", "minimum": 0, "maximum": 1},
                        "reason": {"type": "string"},
                    },
                },
            },
        },
    }

    def __init__(self, *, model: str = HARNESS_MODEL,
                 base_url: str = HARNESS_OPENAI_BASE_URL,
                 auth_file: str | Path = HARNESS_OPENAI_AUTH_FILE,
                 reasoning_effort: str = HARNESS_REASONING_EFFORT,
                 offline: bool = HARNESS_OFFLINE, transport=None):
        self.request_options = {"model": model, "base_url": base_url, "auth_file": auth_file,
                                "reasoning_effort": reasoning_effort}
        self.offline = offline
        self.transport = transport

    def _request_json(self, instructions: str, value: Dict[str, Any], schema: Dict[str, Any], name: str) -> Dict[str, Any]:
        from utils.gpt_client import request_gpt

        result = asyncio.run(request_gpt(
            **self.request_options, instructions=instructions,
            content=[{"type": "input_text", "text": json.dumps(value, ensure_ascii=False, allow_nan=False)}],
            response_schema=schema, schema_name=name,
            connect_timeout_seconds=HARNESS_CONNECT_TIMEOUT_SECONDS,
            max_output_tokens=HARNESS_MAX_OUTPUT_TOKENS, max_retries=HARNESS_MAX_RETRIES,
            retry_base_delay=HARNESS_RETRY_BASE_DELAY, transport=self.transport))
        return result.data

    def decide(
        self,
        candidates: List[Dict[str, Any]],
        existing: List[Dict[str, Any]],
        context: Dict[str, Any],
        documents: List[Dict[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        documents = documents or []
        if not candidates and not documents:
            return {"mode": "offline" if self.offline else "gpt", "actions": [], "summary": self._offline_summary(documents), "error": None}
        if self.offline:
            return {
                "mode": "offline", "actions": self._offline_actions(candidates, existing, context),
                "summary": self._offline_summary(documents), "error": None,
            }
        instructions = load_prompt("harness/experience_update.md")
        try:
            result = self._request_json(
                instructions, {"context": context, "documents": documents, "candidates": candidates, "existing": existing},
                self.CRUD_SCHEMA, "experience_crud_decisions",
            )
            actions = self._normalize_actions(result.get("actions"), context)
            allowed_ids = {row.get("id") for row in existing if isinstance(row, dict) and row.get("id")}
            actions = [
                action for action in actions
                if action["action"] not in {"update", "delete"} or action["target_id"] in allowed_ids
            ]
            return {
                "mode": "gpt", "actions": actions,
                "summary": result.get("summary") if isinstance(result.get("summary"), dict) else self._offline_summary(documents),
                "error": None,
            }
        except Exception as exc:
            return {
                "mode": "offline", "actions": self._offline_actions(candidates, existing, context),
                "summary": self._offline_summary(documents),
                "error": f"{type(exc).__name__}: {exc}",
            }

    @staticmethod
    def _offline_summary(documents: List[Dict[str, Any]]) -> Dict[str, Any]:
        stage_summaries = []
        for document in documents:
            if not isinstance(document, dict):
                continue
            stage = str(document.get("stage") or document.get("name") or "unknown")
            body = document.get("document")
            summary = body.get("stage_summary") if isinstance(body, dict) else None
            if not isinstance(summary, str):
                summary = body.get("summary") if isinstance(body, dict) else ""
            stage_summaries.append({"stage": stage, "summary": summary or "No GPT summary available."})
        return {"overall": "GPT summary unavailable; retained source stage documents.", "stage_summaries": stage_summaries, "experience_candidates": []}

    def select(self, candidates: List[Dict[str, Any]], context: Dict[str, Any], limit: int) -> Dict[str, Any]:
        if self.offline or not candidates or limit <= 0:
            return {"mode": "offline", "selections": [], "error": None}
        instructions = load_prompt("harness/experience_select.md")
        try:
            result = self._request_json(
                instructions, {"context": context, "limit": limit, "candidates": candidates},
                self.RETRIEVAL_SCHEMA, "experience_retrieval_decisions",
            )
            valid_ids = {row.get("id") for row in candidates}
            selections = []
            seen = set()
            for item in as_list(result.get("selections")):
                rid = str(item.get("id") or "") if isinstance(item, dict) else ""
                if rid not in valid_ids or rid in seen:
                    continue
                seen.add(rid)
                selections.append({
                    "id": rid, "relevance": normalize_relevance(item.get("relevance")),
                    "reason": str(item.get("reason") or ""),
                })
                if len(selections) >= limit:
                    break
            return {"mode": "gpt", "selections": selections, "error": None}
        except Exception as exc:
            return {"mode": "offline", "selections": [], "error": f"{type(exc).__name__}: {exc}"}

    def curate(self, candidates: List[Dict[str, Any]], context: Dict[str, Any]) -> List[Dict[str, Any]]:
        decision = self.decide(candidates, [], context)
        return [
            action["experience"] for action in decision["actions"]
            if action["action"] in {"create", "update"} and action.get("experience", {}).get("rule")
        ]

    @classmethod
    def _normalize_actions(cls, actions: Any, context: Dict[str, Any]) -> List[Dict[str, Any]]:
        normalized = []
        for item in as_list(actions):
            if not isinstance(item, dict) or item.get("action") not in CRUD_ACTIONS:
                continue
            scope = normalize_scope({
                pair.get("key"): pair.get("value") for pair in as_list(item.get("scope"))
                if isinstance(pair, dict) and pair.get("key")
            })
            experience = cls._offline({
                "kind": item.get("kind"), "rule": item.get("rule"), "scope": scope,
                "evidence": item.get("evidence"),
                "tags": item.get("tags"),
            }, context)
            action = item["action"]
            target_id = str(item.get("target_id") or "").strip()
            if action == "create" and not experience.get("rule"):
                continue
            if action in {"update", "delete"} and not target_id:
                continue
            normalized.append({
                "action": action, "target_id": target_id, "experience": experience,
                "reason": str(item.get("reason") or "").strip(),
            })
        return normalized

    @classmethod
    def _offline_actions(cls, candidates: List[Dict[str, Any]], existing: List[Dict[str, Any]], context: Dict[str, Any]) -> List[Dict[str, Any]]:
        index = {row.get("id"): row for row in existing if isinstance(row, dict) and row.get("id")}
        actions = []
        for candidate in candidates:
            if not isinstance(candidate, dict) or not candidate.get("rule"):
                continue
            experience = cls._offline(candidate, context)
            rid = stable_id(experience["kind"], experience["rule"], experience["scope"])
            existing_row = index.get(rid)
            actions.append({
                "action": "update" if existing_row else "create",
                "target_id": rid if existing_row else "",
                "experience": experience,
                "reason": "deterministic offline fallback",
            })
        return actions

    @staticmethod
    def _offline(item: Dict[str, Any], context: Dict[str, Any] | None = None) -> Dict[str, Any]:
        result = dict(item)
        result.pop("confidence", None)
        requested = result.pop("_requested_kind", None)
        scope = normalize_scope(result.get("scope"))
        result["kind"] = requested or result.get("kind") or ("specific" if scope else "general")
        if result["kind"] not in {"general", "specific"}:
            result["kind"] = "specific" if scope else "general"
        # 未指定 scope 的 specific 经验继承任务范围，general 经验保持全局。
        if result["kind"] == "specific" and not scope and isinstance(context, dict):
            scope = normalize_scope(context.get("scope"))
        result["scope"] = scope
        result["tags"] = normalize_tags(result.get("tags"))
        return result


GPTExperienceCurator = GPTExperienceManager
