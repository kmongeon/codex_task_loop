#!/usr/bin/env python3
"""
Run a bounded Codex task lifecycle loop.

This entrypoint owns command-line parsing and lifecycle orchestration. Supporting
scripts own file I/O, Git auditing, and validation so each behavior surface is
small and directly testable.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from codex_session import CodexLaunchOptions, CodexSession, ThreadOptions, TurnOptions
from evidence import (
    build_evidence,
    evaluate_artifact_checks,
    run_validation_commands,
    validate_json,
)
from file_io import extract_json_object, read_json, read_text, render_template, write_json
from git_scope import audit_diff, changed_files, git_root


RUNS_DIR = ".codex_task_loop/runs"
STOP_DECISIONS = {"escalate", "reject", "split"}
DEFAULT_REPAIR_PROMPT = (
    "Continue the bounded task. Use the latest evidence log, repair unresolved criteria, "
    "stay within allowed paths, and summarize changed files."
)


def repository_resources_root() -> Path:
    return Path(__file__).resolve().parents[1]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a bounded Codex task lifecycle loop.")
    parser.add_argument("--task", required=True, help="Path to task packet JSON.")
    parser.add_argument("--model", default="gpt-5.4", help="Codex model name.")
    parser.add_argument("--max-iterations", type=int, default=None, help="Override task max_iterations.")
    parser.add_argument(
        "--workspace-root",
        help=(
            "Project root for validation commands, artifact checks, run logs, "
            "and allowed/blocked path auditing. Defaults to the discovered git root."
        ),
    )
    add_codex_arguments(parser)
    return parser.parse_args(argv)


def add_codex_arguments(parser: argparse.ArgumentParser) -> None:
    codex = parser.add_argument_group("Codex SDK launch options")
    codex.add_argument("--codex-bin", help="Path to the Codex executable for CodexConfig.codex_bin.")
    codex.add_argument(
        "--codex-launch-arg",
        action="append",
        default=[],
        help="Argument for CodexConfig.launch_args_override. Repeat for multiple arguments.",
    )
    codex.add_argument(
        "--codex-config-override",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="CodexConfig config override. Repeat for multiple --config KEY=VALUE entries.",
    )
    codex.add_argument("--codex-cwd", help="Working directory for the Codex runtime process.")
    codex.add_argument(
        "--codex-env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Environment variable for the Codex runtime process. Repeat for multiple variables.",
    )
    codex.add_argument("--client-name", help="CodexConfig client_name.")
    codex.add_argument("--client-title", help="CodexConfig client_title.")
    codex.add_argument("--client-version", help="CodexConfig client_version.")
    codex.add_argument(
        "--no-experimental-api",
        action="store_true",
        help="Set CodexConfig.experimental_api to false.",
    )

    thread = parser.add_argument_group("Codex thread options")
    thread.add_argument(
        "--approval-mode",
        choices=("auto_review", "deny_all"),
        default="auto_review",
        help="Approval mode for thread_start.",
    )
    thread.add_argument("--base-instructions", help="Base instructions for thread_start.")
    thread.add_argument(
        "--thread-config-json",
        help="JSON object passed as thread_start(config=...).",
    )
    thread.add_argument("--thread-cwd", help="cwd passed to thread_start.")
    thread.add_argument("--developer-instructions", help="Developer instructions for thread_start.")
    thread.add_argument("--ephemeral", action="store_true", help="Set thread_start(ephemeral=True).")
    thread.add_argument("--model-provider", help="Model provider passed to thread_start.")
    thread.add_argument("--personality", help="Personality passed to thread_start and turns.")
    thread.add_argument(
        "--thread-sandbox",
        choices=("read-only", "read_only", "workspace-write", "workspace_write", "full-access", "full_access"),
        default="workspace-write",
        help="Sandbox used when starting the thread.",
    )
    thread.add_argument("--service-name", help="Service name passed to thread_start.")
    thread.add_argument("--service-tier", help="Service tier passed to thread_start and turns.")

    turns = parser.add_argument_group("Codex turn options")
    turns.add_argument("--turn-cwd", help="cwd passed to execution and review turns.")
    turns.add_argument("--execution-model", help="Model override for execution turns.")
    turns.add_argument("--review-model", help="Model override for review turns.")
    turns.add_argument(
        "--execution-approval-mode",
        choices=("auto_review", "deny_all"),
        help="Approval mode override for execution turns.",
    )
    turns.add_argument(
        "--review-approval-mode",
        choices=("auto_review", "deny_all"),
        help="Approval mode override for review turns.",
    )
    turns.add_argument(
        "--execution-sandbox",
        choices=("read-only", "read_only", "workspace-write", "workspace_write", "full-access", "full_access"),
        default="workspace-write",
        help="Sandbox used for execution turns.",
    )
    turns.add_argument(
        "--review-sandbox",
        choices=("read-only", "read_only", "workspace-write", "workspace_write", "full-access", "full_access"),
        default="read-only",
        help="Sandbox used for read-only review turns.",
    )
    turns.add_argument("--effort", help="Reasoning effort passed to execution and review turns.")
    turns.add_argument(
        "--output-schema-json",
        help="JSON object passed as output_schema to execution and review turns.",
    )
    turns.add_argument("--summary", help="Reasoning summary setting passed to execution and review turns.")


def parse_key_value_pairs(values: list[str], label: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for value in values:
        key, separator, item_value = value.partition("=")
        if not separator or not key:
            raise SystemExit(f"{label} must use KEY=VALUE format: {value!r}")
        pairs[key] = item_value
    return pairs


def parse_config_overrides(values: list[str]) -> tuple[str, ...]:
    for value in values:
        key, separator, _item_value = value.partition("=")
        if not separator or not key:
            raise SystemExit(f"--codex-config-override must use KEY=VALUE format: {value!r}")
    return tuple(values)


def parse_json_object_argument(value: str | None, label: str) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{label} must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit(f"{label} must be a JSON object.")
    return parsed


def build_codex_launch_options(args: argparse.Namespace) -> CodexLaunchOptions:
    return CodexLaunchOptions(
        codex_bin=args.codex_bin,
        launch_args_override=tuple(args.codex_launch_arg) or None,
        config_overrides=parse_config_overrides(args.codex_config_override),
        cwd=args.codex_cwd,
        env=parse_key_value_pairs(args.codex_env, "--codex-env") or None,
        client_name=args.client_name,
        client_title=args.client_title,
        client_version=args.client_version,
        experimental_api=False if args.no_experimental_api else None,
    )


def build_thread_options(args: argparse.Namespace) -> ThreadOptions:
    return ThreadOptions(
        approval_mode=args.approval_mode,
        base_instructions=args.base_instructions,
        config=parse_json_object_argument(args.thread_config_json, "--thread-config-json"),
        cwd=args.thread_cwd,
        developer_instructions=args.developer_instructions,
        ephemeral=True if args.ephemeral else None,
        model=args.model,
        model_provider=args.model_provider,
        personality=args.personality,
        sandbox=args.thread_sandbox,
        service_name=args.service_name,
        service_tier=args.service_tier,
    )


def build_turn_options(args: argparse.Namespace, *, model: str | None, approval_mode: str | None, sandbox: str) -> TurnOptions:
    return TurnOptions(
        approval_mode=approval_mode,
        cwd=args.turn_cwd,
        effort=args.effort,
        model=model,
        output_schema=parse_json_object_argument(args.output_schema_json, "--output-schema-json"),
        personality=args.personality,
        sandbox=sandbox,
        service_tier=args.service_tier,
        summary=args.summary,
    )


def load_task_context(resources_root: Path, task_path: Path) -> dict[str, Any]:
    task_schema = read_json(resources_root / "schemas" / "task_packet.schema.json")
    decision_schema = read_json(resources_root / "schemas" / "decision.schema.json")
    task = read_json(task_path)
    validate_json(task_schema, task, "task packet")
    return {
        "decision_schema": decision_schema,
        "task": task,
        "execution_template": read_text(resources_root / "templates" / "execution_prompt.md"),
        "review_template": read_text(resources_root / "templates" / "review_prompt.md"),
    }


def resolve_workspace_root(repo: Path, value: str | None) -> Path:
    if value is None:
        return repo
    workspace = Path(value).expanduser().resolve()
    if not workspace.exists():
        raise SystemExit(f"Workspace root does not exist: {workspace}")
    if not workspace.is_dir():
        raise SystemExit(f"Workspace root is not a directory: {workspace}")
    try:
        workspace.relative_to(repo)
    except ValueError as exc:
        raise SystemExit(f"Workspace root must be inside git root {repo}: {workspace}") from exc
    return workspace


def create_run_directory(workspace: Path, task: dict[str, Any]) -> Path:
    run_id = f"{dt.datetime.now(dt.UTC).strftime('%Y%m%dT%H%M%SZ')}_{task['task_id']}"
    run_dir = workspace / RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(run_dir / "task.json", task)
    return run_dir


def build_review_prompt(
    review_template: str,
    task_json: str,
    evidence: dict[str, Any],
    decision_schema_json: str,
) -> str:
    return render_template(
        review_template,
        {
            "task_json": task_json,
            "evidence_json": json.dumps(evidence, indent=2, ensure_ascii=False),
            "decision_schema_json": decision_schema_json,
        },
    )


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


def write_final_result(
    run_dir: Path,
    repo: Path,
    complete: bool,
    iterations: int,
    changed: list[str],
    decision: dict[str, Any] | None = None,
    reason: str | None = None,
) -> None:
    final: dict[str, Any] = {
        "complete": complete,
        "iterations": iterations,
        "run_dir": str(run_dir.relative_to(repo)),
    }
    if decision is not None:
        final["decision"] = decision
    if reason is not None:
        final["reason"] = reason
    if changed:
        final["changed_files"] = changed
    write_json(run_dir / "final.json", final)


def run_task_loop(args: argparse.Namespace) -> int:
    codex_launch = build_codex_launch_options(args)
    thread_options = build_thread_options(args)
    execution_options = build_turn_options(
        args,
        model=args.execution_model,
        approval_mode=args.execution_approval_mode,
        sandbox=args.execution_sandbox,
    )
    review_options = build_turn_options(
        args,
        model=args.review_model,
        approval_mode=args.review_approval_mode,
        sandbox=args.review_sandbox,
    )

    repo = git_root(Path.cwd())
    context = load_task_context(repository_resources_root(), Path(args.task).resolve())
    task = context["task"]
    decision_schema = context["decision_schema"]
    workspace = resolve_workspace_root(repo, args.workspace_root or task.get("workspace_root"))

    max_iterations = args.max_iterations or int(task["max_iterations"])
    timeout_seconds = int(task.get("command_timeout_seconds", 120))
    run_dir = create_run_directory(workspace, task)

    task_json = json.dumps(task, indent=2, ensure_ascii=False)
    decision_schema_json = json.dumps(decision_schema, indent=2, ensure_ascii=False)
    prompt = render_template(context["execution_template"], {"task_json": task_json})

    print(f"Git root: {repo}")
    print(f"Workspace root: {workspace}")
    print(f"Run directory: {run_dir.relative_to(workspace)}")
    print(f"Task: {task['task_id']}")
    print(f"Model: {args.model}")

    with CodexSession(codex_launch, thread_options, execution_options, review_options) as codex_thread:
        for iteration in range(1, max_iterations + 1):
            iter_dir = run_dir / f"iteration_{iteration:02d}"
            iter_dir.mkdir(parents=True, exist_ok=True)

            print(f"\n--- iteration {iteration} ---")
            print("Codex execution turn")
            work_text = codex_thread.run_execution(prompt)
            (iter_dir / "codex_execution.md").write_text(work_text, encoding="utf-8")

            print("Validation")
            commands = task.get("validation_commands", []) + task.get("regression_commands", [])
            command_results = run_validation_commands(commands, workspace, timeout_seconds, iter_dir)
            files = changed_files(repo, workspace)
            diff_result = audit_diff(files, task["allowed_paths"], task.get("blocked_paths", []))
            artifact_results = evaluate_artifact_checks(workspace, task.get("artifact_checks", []))
            evidence = build_evidence(task, iteration, command_results, artifact_results, diff_result, iter_dir, workspace)
            write_json(iter_dir / "evidence.json", evidence)

            print(f"validation_passed={evidence['validation_passed']}")
            print(f"artifact_checks_passed={evidence['artifact_checks_passed']}")
            print(f"diff_audit_passed={evidence['diff_audit_passed']}")

            print("Codex read-only review turn")
            review_prompt = build_review_prompt(
                context["review_template"],
                task_json,
                evidence,
                decision_schema_json,
            )
            review_text = codex_thread.run_review(review_prompt)
            (iter_dir / "codex_review_raw.md").write_text(review_text, encoding="utf-8")

            decision = parse_review_decision(review_text, decision_schema, task)
            write_json(iter_dir / "decision.json", decision)

            accepted = decision["decision"] == "accept" and evidence["outer_gate_passed"]
            if accepted:
                write_final_result(run_dir, workspace, True, iteration, files, decision=decision)
                print("Accepted")
                return 0

            if decision["decision"] in STOP_DECISIONS:
                write_final_result(run_dir, workspace, False, iteration, files, decision=decision)
                print(f"Stopped: {decision['decision']}")
                return 2

            prompt = decision["next_prompt"].strip() or DEFAULT_REPAIR_PROMPT

    write_final_result(run_dir, workspace, False, max_iterations, [], reason="Reached max_iterations")
    print("Reached max_iterations")
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    return run_task_loop(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
