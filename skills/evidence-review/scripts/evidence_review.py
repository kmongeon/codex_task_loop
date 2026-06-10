#!/usr/bin/env python3
"""
Independent task-loop reviewer.

Runs a single read-only, ephemeral Codex turn whose only inputs are the task
packet, the eval-gate evidence, and the decision schema. The reviewer never
shares a thread with the execution agent, so the maker/checker split holds by
construction. Writes a schema-validated decision.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Sequence


SKILL_ROOT = Path(__file__).resolve().parents[1]
DECISION_SCHEMA_PATH = SKILL_ROOT / "schemas" / "decision.schema.json"
REVIEW_TEMPLATE_PATH = SKILL_ROOT / "templates" / "review_prompt.md"


# --- file helpers -----------------------------------------------------------


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def render_template(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", value)
    return rendered


def validate_json(schema: dict[str, Any], obj: dict[str, Any], label: str) -> None:
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(obj), key=lambda error: list(error.path))
    if errors:
        message = "\n".join(f"{label}: {list(error.path)}: {error.message}" for error in errors)
        raise ValueError(message)


# --- decision extraction ----------------------------------------------------


def fenced_code_blocks(text: str) -> Iterator[str]:
    collecting = False
    buffer: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            marker = stripped[3:].strip().lower()
            if collecting:
                yield "\n".join(buffer)
                collecting = False
                buffer = []
                continue
            if marker in {"", "json"}:
                collecting = True
                buffer = []
                continue
        if collecting:
            buffer.append(line)


def require_json_object(obj: Any) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ValueError("Decoded JSON is not an object.")
    return obj


def extract_json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()

    for block in fenced_code_blocks(text):
        try:
            return require_json_object(json.loads(block))
        except (json.JSONDecodeError, ValueError):
            continue

    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[index:])
            return require_json_object(obj)
        except (json.JSONDecodeError, ValueError):
            continue

    raise ValueError("No JSON object found in Codex review output.")


def invalid_review_decision(error: Exception, task: dict[str, Any]) -> dict[str, Any]:
    return {
        "decision": "repair",
        "reason": f"Codex review did not return valid decision JSON: {error}",
        "next_prompt": "Repair the task using the evidence from the latest iteration. Return a concise summary of changes.",
        "completed_criteria": [],
        "unresolved_criteria": task["acceptance_criteria"],
        "validation_required": task.get("validation_commands", []),
        "risks": ["review_json_invalid"],
        "new_task_packets": [],
    }


def parse_review_decision(
    review_text: str,
    decision_schema: dict[str, Any],
    task: dict[str, Any],
) -> dict[str, Any]:
    try:
        decision = extract_json_object(review_text)
        validate_json(decision_schema, decision, "decision")
        return decision
    except Exception as exc:
        return invalid_review_decision(exc, task)


# --- isolated review turn ---------------------------------------------------


def run_review_turn(prompt: str, model: str, effort: str | None, cwd: str, decision_schema: dict[str, Any]) -> str:
    from openai_codex import ApprovalMode, Codex, Sandbox

    turn_kwargs: dict[str, Any] = {"sandbox": Sandbox("read-only"), "output_schema": decision_schema}
    if effort is not None:
        turn_kwargs["effort"] = effort

    with Codex() as codex:
        thread = codex.thread_start(
            approval_mode=ApprovalMode("deny_all"),
            cwd=cwd,
            ephemeral=True,
            model=model,
            sandbox=Sandbox("read-only"),
        )
        result = thread.run(prompt, **turn_kwargs)
    return getattr(result, "final_response", str(result))


# --- entrypoint --------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an isolated read-only review of task-loop evidence.")
    parser.add_argument("--task", required=True, help="Path to task packet JSON.")
    parser.add_argument("--evidence", required=True, help="Path to eval-gate evidence JSON.")
    parser.add_argument("--output", required=True, help="Path to write decision JSON.")
    parser.add_argument("--workspace-root", required=True, help="Repository state the reviewer may read.")
    parser.add_argument("--model", default="gpt-5.4", help="Codex model for the review turn.")
    parser.add_argument("--effort", help="Reasoning effort for the review turn.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    task = read_json(Path(args.task).resolve())
    evidence = read_json(Path(args.evidence).resolve())
    decision_schema = read_json(DECISION_SCHEMA_PATH)

    prompt = render_template(
        REVIEW_TEMPLATE_PATH.read_text(encoding="utf-8"),
        {
            "task_json": json.dumps(task, indent=2, ensure_ascii=False),
            "evidence_json": json.dumps(evidence, indent=2, ensure_ascii=False),
            "decision_schema_json": json.dumps(decision_schema, indent=2, ensure_ascii=False),
        },
    )

    review_text = run_review_turn(
        prompt,
        args.model,
        args.effort,
        str(Path(args.workspace_root).resolve()),
        decision_schema,
    )
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    (output_path.parent / "codex_review_raw.md").write_text(review_text, encoding="utf-8")

    decision = parse_review_decision(review_text, decision_schema, task)
    write_json(output_path, decision)

    return 0


if __name__ == "__main__":
    sys.exit(main())
