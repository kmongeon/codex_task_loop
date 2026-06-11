#!/usr/bin/env python3
"""
Bounded Codex task lifecycle orchestrator.

Each iteration composes a fresh prompt from the fixed task contract plus the
latest evidence and reviewer direction, runs an execution turn on a fresh
thread, then delegates verification to the eval-gate skill and judgment to the
evidence-review skill via subprocess CLIs. Skills communicate only through
JSON files: task.json -> evidence.json -> decision.json.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from codex_session import CodexLaunchOptions, ThreadOptions, TurnOptions, run_execution_turn


SKILL_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = SKILL_ROOT.parent
EVAL_GATE_SCRIPT = SKILLS_ROOT / "eval-gate" / "scripts" / "eval_gate.py"
EVIDENCE_REVIEW_SCRIPT = SKILLS_ROOT / "evidence-review" / "scripts" / "evidence_review.py"
TASK_SCHEMA_PATH = SKILL_ROOT / "schemas" / "task_packet.schema.json"
EXECUTION_TEMPLATE_PATH = SKILL_ROOT / "templates" / "execution_prompt.md"

RUNS_DIR = ".codex_task_loop/runs"
STOP_DECISIONS = {"escalate", "reject", "split"}
DEFAULT_REPAIR_PROMPT = (
    "Continue the bounded task. Use the latest evidence log, repair unresolved criteria, "
    "stay within allowed paths, and summarize changed files."
)


# --- file helpers -----------------------------------------------------------


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def relative_artifact_path(workspace: Path, path: Path) -> str:
    return path.relative_to(workspace).as_posix()


def append_run_event(run_dir: Path, event: dict[str, Any]) -> None:
    record = {
        "timestamp": dt.datetime.now(dt.UTC).isoformat(),
        **event,
    }
    with (run_dir / "run_events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def print_artifact(label: str, workspace: Path, path: Path) -> None:
    print(f"{label}: {relative_artifact_path(workspace, path)}")


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
        raise SystemExit(message)


def git_root(cwd: Path) -> Path:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(cwd),
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise SystemExit("Run from inside a git repository.")
    return Path(proc.stdout.strip()).resolve()


# --- CLI ---------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a bounded Codex task lifecycle loop.")
    parser.add_argument("--task", required=True, help="Path to task packet JSON.")
    parser.add_argument("--model", default="gpt-5.5", help="Codex model for execution turns.")
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

    execution = parser.add_argument_group("Execution turn options")
    execution.add_argument(
        "--approval-mode",
        choices=("auto_review", "deny_all"),
        default="auto_review",
        help="Approval mode for execution threads.",
    )
    execution.add_argument("--base-instructions", help="Base instructions for execution threads.")
    execution.add_argument(
        "--thread-config-json",
        help="JSON object passed as thread_start(config=...).",
    )
    execution.add_argument("--developer-instructions", help="Developer instructions for execution threads.")
    execution.add_argument("--model-provider", help="Model provider passed to thread_start.")
    execution.add_argument("--personality", help="Personality passed to thread_start and turns.")
    execution.add_argument(
        "--execution-sandbox",
        choices=("read-only", "read_only", "workspace-write", "workspace_write", "full-access", "full_access"),
        default="workspace-write",
        help="Sandbox used for execution threads and turns.",
    )
    execution.add_argument("--service-name", help="Service name passed to thread_start.")
    execution.add_argument("--service-tier", help="Service tier passed to thread_start and turns.")
    execution.add_argument("--effort", help="Reasoning effort passed to execution turns.")
    execution.add_argument("--summary", help="Reasoning summary setting passed to execution turns.")

    review = parser.add_argument_group("Review turn options")
    review.add_argument("--review-model", help="Model override for the isolated review turn.")
    review.add_argument("--review-effort", help="Reasoning effort for the isolated review turn.")


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


def build_thread_options(args: argparse.Namespace, workspace: Path) -> ThreadOptions:
    return ThreadOptions(
        approval_mode=args.approval_mode,
        base_instructions=args.base_instructions,
        config=parse_json_object_argument(args.thread_config_json, "--thread-config-json"),
        cwd=str(workspace),
        developer_instructions=args.developer_instructions,
        model=args.model,
        model_provider=args.model_provider,
        personality=args.personality,
        sandbox=args.execution_sandbox,
        service_name=args.service_name,
        service_tier=args.service_tier,
    )


def build_turn_options(args: argparse.Namespace, workspace: Path) -> TurnOptions:
    return TurnOptions(
        cwd=str(workspace),
        effort=args.effort,
        model=args.model,
        personality=args.personality,
        sandbox=args.execution_sandbox,
        service_tier=args.service_tier,
        summary=args.summary,
    )


# --- per-iteration prompt composition ----------------------------------------


def evidence_failure_summary(evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "failing_commands": [
            command for command in evidence["commands"] if not command["passed"]
        ],
        "failing_artifact_checks": [
            check for check in evidence["artifact_checks"] if not check["passed"]
        ],
        "unexpected_files": evidence["diff_audit"]["unexpected_files"],
        "blocked_files_changed": evidence["diff_audit"]["blocked_files_changed"],
    }


def compose_prompt(
    execution_template: str,
    task_json: str,
    iteration: int,
    max_iterations: int,
    decision: dict[str, Any] | None,
    evidence: dict[str, Any] | None,
) -> str:
    """Recompose the execution prompt: fixed task contract plus latest loop state.

    The task contract never mutates. Dynamic state (unresolved criteria, failure
    evidence, reviewer direction) is rebuilt from the latest artifacts each pass,
    so fresh execution threads carry full context without shared memory.
    """
    prompt = render_template(execution_template, {"task_json": task_json})
    prompt += f"\n\nIteration {iteration} of {max_iterations}."
    if decision is None:
        return prompt

    direction = decision["next_prompt"].strip() or DEFAULT_REPAIR_PROMPT
    prompt += (
        f"\n\nUnresolved acceptance criteria:\n"
        f"{json.dumps(decision['unresolved_criteria'], indent=2, ensure_ascii=False)}"
        f"\n\nFailure evidence from iteration {evidence['iteration']}:\n"
        f"{json.dumps(evidence_failure_summary(evidence), indent=2, ensure_ascii=False)}"
        f"\n\nReviewer direction:\n{direction}"
    )
    return prompt


# --- sibling skill invocation -------------------------------------------------


def run_eval_gate(task_path: Path, workspace: Path, iteration: int, iteration_dir: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [
            sys.executable,
            str(EVAL_GATE_SCRIPT),
            "--task", str(task_path),
            "--workspace-root", str(workspace),
            "--iteration", str(iteration),
            "--iteration-dir", str(iteration_dir),
        ],
        text=True,
        capture_output=True,
    )
    if proc.returncode not in (0, 1):
        raise SystemExit(f"eval_gate.py failed:\n{proc.stdout}\n{proc.stderr}")
    return read_json(iteration_dir / "evidence.json")


def run_evidence_review(
    task_path: Path,
    workspace: Path,
    iteration_dir: Path,
    model: str,
    effort: str | None,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(EVIDENCE_REVIEW_SCRIPT),
        "--task", str(task_path),
        "--evidence", str(iteration_dir / "evidence.json"),
        "--output", str(iteration_dir / "decision.json"),
        "--workspace-root", str(workspace),
        "--model", model,
    ]
    if effort is not None:
        command += ["--effort", effort]
    proc = subprocess.run(command, text=True, capture_output=True)
    if proc.returncode != 0:
        raise SystemExit(f"evidence_review.py failed:\n{proc.stdout}\n{proc.stderr}")
    return read_json(iteration_dir / "decision.json")


# --- git checkpointing ---------------------------------------------------------


def skipped_checkpoint(files: list[str], reason: str) -> dict[str, Any]:
    return {
        "checkpointed": False,
        "commit": None,
        "files": files,
        "reason": reason,
    }


def git_checkpoint(repo: Path, workspace: Path, files: list[str], message: str) -> dict[str, Any]:
    """Commit only the audited changed files as a durable per-iteration checkpoint."""
    if not files:
        return skipped_checkpoint([], "no_changed_files")
    paths = [str((workspace / file).relative_to(repo)) for file in files]
    subprocess.run(["git", "add", "--", *paths], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-m", message, "--", *paths], cwd=str(repo), check=True)
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        text=True,
        capture_output=True,
        check=True,
    )
    return {
        "checkpointed": True,
        "commit": proc.stdout.strip(),
        "files": files,
    }


# --- run bookkeeping ----------------------------------------------------------


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


def markdown_list(values: list[str]) -> str:
    if not values:
        return "- none"
    return "\n".join(f"- `{value}`" for value in values)


def write_run_summary(
    run_dir: Path,
    workspace: Path,
    task: dict[str, Any],
    final: dict[str, Any],
    iteration_records: list[dict[str, Any]],
) -> None:
    lines = [
        f"# Task Loop Run Summary: {task['task_id']}",
        "",
        "## Final Status",
        "",
        f"- Complete: `{final['complete']}`",
        f"- Iterations: `{final['iterations']}`",
        f"- Run directory: `{final['run_dir']}`",
        f"- Final result: `{relative_artifact_path(workspace, run_dir / 'final.json')}`",
        f"- Run events: `{final['run_events_file']}`",
    ]
    if "reason" in final:
        lines.append(f"- Reason: `{final['reason']}`")
    decision = final.get("decision")
    if decision is not None:
        lines.append(f"- Decision: `{decision['decision']}`")
        lines.append(f"- Decision reason: {decision['reason']}")

    checkpoint_results = final.get("checkpoint_results", [])
    lines += [
        "",
        "## Changed Files",
        "",
        markdown_list(final.get("changed_files", [])),
        "",
        "## Checkpoints",
        "",
    ]
    if checkpoint_results:
        for checkpoint in checkpoint_results:
            commit = checkpoint["commit"] or "none"
            status = "created" if checkpoint["checkpointed"] else "skipped"
            reason = checkpoint.get("reason")
            suffix = f" ({reason})" if reason else ""
            lines.append(f"- {status}: `{commit}`{suffix}; files: {len(checkpoint['files'])}")
    else:
        lines.append("- none")

    lines += [
        "",
        "## Iterations",
        "",
    ]
    for record in iteration_records:
        artifacts = record["artifacts"]
        evidence = record.get("evidence", {})
        decision_record = record.get("decision", {})
        lines += [
            f"### Iteration {record['iteration']}",
            "",
            f"- Prompt: `{artifacts['prompt']}`",
            f"- Execution: `{artifacts['execution']}`",
            f"- Evidence: `{artifacts['evidence']}`",
            f"- Diff: `{artifacts['diff']}`",
            f"- Decision: `{artifacts['decision']}`",
            f"- Outer gate passed: `{evidence.get('outer_gate_passed')}`",
            f"- Validation passed: `{evidence.get('validation_passed')}`",
            f"- Artifact checks passed: `{evidence.get('artifact_checks_passed')}`",
            f"- Diff audit passed: `{evidence.get('diff_audit_passed')}`",
            f"- Reviewer decision: `{decision_record.get('decision')}`",
            "",
        ]

    summary_path = run_dir / "RUN_SUMMARY.md"
    summary_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def changed_files_from_checkpoints(checkpoint_results: list[dict[str, Any]]) -> list[str]:
    return sorted({file for checkpoint in checkpoint_results for file in checkpoint["files"]})


def write_final_result(
    run_dir: Path,
    workspace: Path,
    task: dict[str, Any],
    complete: bool,
    iterations: int,
    changed: list[str],
    iteration_records: list[dict[str, Any]],
    checkpoint_results: list[dict[str, Any]],
    decision: dict[str, Any] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    final: dict[str, Any] = {
        "complete": complete,
        "iterations": iterations,
        "run_dir": str(run_dir.relative_to(workspace)),
        "run_events_file": relative_artifact_path(workspace, run_dir / "run_events.jsonl"),
        "run_summary_file": relative_artifact_path(workspace, run_dir / "RUN_SUMMARY.md"),
        "checkpoint_results": checkpoint_results,
    }
    if decision is not None:
        final["decision"] = decision
    if reason is not None:
        final["reason"] = reason
    if changed:
        final["changed_files"] = changed
    write_json(run_dir / "final.json", final)
    write_run_summary(run_dir, workspace, task, final, iteration_records)
    return final


# --- loop ---------------------------------------------------------------------


def run_task_loop(args: argparse.Namespace) -> int:
    repo = git_root(Path.cwd())
    task = read_json(Path(args.task).resolve())
    validate_json(read_json(TASK_SCHEMA_PATH), task, "task packet")
    workspace = resolve_workspace_root(repo, args.workspace_root or task.get("workspace_root"))

    codex_launch = build_codex_launch_options(args)
    thread_options = build_thread_options(args, workspace)
    turn_options = build_turn_options(args, workspace)
    review_model = args.review_model or args.model

    max_iterations = args.max_iterations or int(task["max_iterations"])
    run_dir = create_run_directory(workspace, task)
    task_path = run_dir / "task.json"
    execution_template = EXECUTION_TEMPLATE_PATH.read_text(encoding="utf-8")
    task_json = json.dumps(task, indent=2, ensure_ascii=False)

    print(f"Git root: {repo}")
    print(f"Workspace root: {workspace}")
    print(f"Run directory: {run_dir.relative_to(workspace)}")
    print(f"Task: {task['task_id']}")
    print(f"Execution model: {args.model}")
    print(f"Review model: {review_model}")
    print_artifact("Run events", workspace, run_dir / "run_events.jsonl")

    append_run_event(
        run_dir,
        {
            "event": "run_started",
            "task_id": task["task_id"],
            "git_root": str(repo),
            "workspace_root": str(workspace),
            "run_dir": relative_artifact_path(workspace, run_dir),
            "task_file": relative_artifact_path(workspace, task_path),
            "execution_model": args.model,
            "review_model": review_model,
        },
    )

    decision: dict[str, Any] | None = None
    evidence: dict[str, Any] | None = None
    iteration_records: list[dict[str, Any]] = []
    checkpoint_results: list[dict[str, Any]] = []

    for iteration in range(1, max_iterations + 1):
        iter_dir = run_dir / f"iteration_{iteration:02d}"
        iter_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = iter_dir / "composed_prompt.md"
        execution_path = iter_dir / "codex_execution.md"
        evidence_path = iter_dir / "evidence.json"
        diff_path = iter_dir / "workspace.diff"
        decision_path = iter_dir / "decision.json"
        iteration_record: dict[str, Any] = {
            "iteration": iteration,
            "artifacts": {
                "prompt": relative_artifact_path(workspace, prompt_path),
                "execution": relative_artifact_path(workspace, execution_path),
                "evidence": relative_artifact_path(workspace, evidence_path),
                "diff": relative_artifact_path(workspace, diff_path),
                "decision": relative_artifact_path(workspace, decision_path),
            },
        }
        iteration_records.append(iteration_record)

        print(f"\n--- iteration {iteration} ---")
        append_run_event(
            run_dir,
            {
                "event": "iteration_started",
                "iteration": iteration,
                "iteration_dir": relative_artifact_path(workspace, iter_dir),
            },
        )
        prompt = compose_prompt(execution_template, task_json, iteration, max_iterations, decision, evidence)
        prompt_path.write_text(prompt, encoding="utf-8")
        print_artifact("Composed prompt", workspace, prompt_path)
        append_run_event(
            run_dir,
            {
                "event": "prompt_written",
                "iteration": iteration,
                "path": relative_artifact_path(workspace, prompt_path),
            },
        )

        print("Codex execution turn (fresh thread)")
        work_text = run_execution_turn(codex_launch, thread_options, turn_options, prompt)
        execution_path.write_text(work_text, encoding="utf-8")
        print_artifact("Execution transcript", workspace, execution_path)
        append_run_event(
            run_dir,
            {
                "event": "execution_completed",
                "iteration": iteration,
                "path": relative_artifact_path(workspace, execution_path),
            },
        )

        print("Eval gate")
        evidence = run_eval_gate(task_path, workspace, iteration, iter_dir)
        iteration_record["evidence"] = {
            "outer_gate_passed": evidence["outer_gate_passed"],
            "validation_passed": evidence["validation_passed"],
            "artifact_checks_passed": evidence["artifact_checks_passed"],
            "diff_audit_passed": evidence["diff_audit_passed"],
        }
        print(f"outer_gate_passed={evidence['outer_gate_passed']}")
        print(f"validation_passed={evidence['validation_passed']}")
        print(f"artifact_checks_passed={evidence['artifact_checks_passed']}")
        print(f"diff_audit_passed={evidence['diff_audit_passed']}")
        print_artifact("Evidence", workspace, evidence_path)
        print_artifact("Workspace diff", workspace, diff_path)
        append_run_event(
            run_dir,
            {
                "event": "eval_gate_completed",
                "iteration": iteration,
                "outer_gate_passed": evidence["outer_gate_passed"],
                "validation_passed": evidence["validation_passed"],
                "artifact_checks_passed": evidence["artifact_checks_passed"],
                "diff_audit_passed": evidence["diff_audit_passed"],
                "evidence_file": relative_artifact_path(workspace, evidence_path),
                "workspace_diff_file": relative_artifact_path(workspace, diff_path),
            },
        )

        print("Isolated evidence review")
        decision = run_evidence_review(task_path, workspace, iter_dir, review_model, args.review_effort)
        iteration_record["decision"] = {
            "decision": decision["decision"],
            "reason": decision["reason"],
        }
        print(f"decision={decision['decision']}")
        print_artifact("Decision", workspace, decision_path)
        append_run_event(
            run_dir,
            {
                "event": "review_completed",
                "iteration": iteration,
                "decision": decision["decision"],
                "decision_file": relative_artifact_path(workspace, decision_path),
            },
        )

        changed = evidence["diff_audit"]["changed_files"]
        checkpoint_enabled = task.get("git_checkpoint", True)
        accepted = decision["decision"] == "accept" and evidence["outer_gate_passed"]
        if accepted:
            checkpoint = (
                git_checkpoint(repo, workspace, changed, f"task-loop({task['task_id']}): accepted at iteration {iteration}")
                if checkpoint_enabled
                else skipped_checkpoint(changed, "git_checkpoint_disabled")
            )
            checkpoint_results.append(checkpoint)
            iteration_record["checkpoint"] = checkpoint
            append_run_event(
                run_dir,
                {
                    "event": "checkpoint_created" if checkpoint["checkpointed"] else "checkpoint_skipped",
                    "iteration": iteration,
                    "checkpoint": checkpoint,
                },
            )
            final = write_final_result(
                run_dir,
                workspace,
                task,
                True,
                iteration,
                changed_files_from_checkpoints(checkpoint_results),
                iteration_records,
                checkpoint_results,
                decision=decision,
            )
            append_run_event(
                run_dir,
                {
                    "event": "run_finished",
                    "complete": True,
                    "iterations": iteration,
                    "final_file": relative_artifact_path(workspace, run_dir / "final.json"),
                    "run_summary_file": final["run_summary_file"],
                },
            )
            print_artifact("Final result", workspace, run_dir / "final.json")
            print_artifact("Run summary", workspace, run_dir / "RUN_SUMMARY.md")
            print_artifact("Run events", workspace, run_dir / "run_events.jsonl")
            print("Accepted")
            return 0

        checkpoint = (
            git_checkpoint(repo, workspace, changed, f"task-loop({task['task_id']}): iteration {iteration} {decision['decision']}")
            if checkpoint_enabled
            else skipped_checkpoint(changed, "git_checkpoint_disabled")
        )
        checkpoint_results.append(checkpoint)
        iteration_record["checkpoint"] = checkpoint
        append_run_event(
            run_dir,
            {
                "event": "checkpoint_created" if checkpoint["checkpointed"] else "checkpoint_skipped",
                "iteration": iteration,
                "checkpoint": checkpoint,
            },
        )

        if decision["decision"] in STOP_DECISIONS:
            final = write_final_result(
                run_dir,
                workspace,
                task,
                False,
                iteration,
                changed_files_from_checkpoints(checkpoint_results),
                iteration_records,
                checkpoint_results,
                decision=decision,
            )
            append_run_event(
                run_dir,
                {
                    "event": "run_stopped",
                    "complete": False,
                    "iterations": iteration,
                    "decision": decision["decision"],
                    "final_file": relative_artifact_path(workspace, run_dir / "final.json"),
                    "run_summary_file": final["run_summary_file"],
                },
            )
            append_run_event(
                run_dir,
                {
                    "event": "run_finished",
                    "complete": False,
                    "iterations": iteration,
                    "final_file": relative_artifact_path(workspace, run_dir / "final.json"),
                    "run_summary_file": final["run_summary_file"],
                },
            )
            print_artifact("Final result", workspace, run_dir / "final.json")
            print_artifact("Run summary", workspace, run_dir / "RUN_SUMMARY.md")
            print_artifact("Run events", workspace, run_dir / "run_events.jsonl")
            print(f"Stopped: {decision['decision']}")
            return 2

    final = write_final_result(
        run_dir,
        workspace,
        task,
        False,
        max_iterations,
        changed_files_from_checkpoints(checkpoint_results),
        iteration_records,
        checkpoint_results,
        reason="Reached max_iterations",
    )
    append_run_event(
        run_dir,
        {
            "event": "run_stopped",
            "complete": False,
            "iterations": max_iterations,
            "reason": "Reached max_iterations",
            "final_file": relative_artifact_path(workspace, run_dir / "final.json"),
            "run_summary_file": final["run_summary_file"],
        },
    )
    append_run_event(
        run_dir,
        {
            "event": "run_finished",
            "complete": False,
            "iterations": max_iterations,
            "final_file": relative_artifact_path(workspace, run_dir / "final.json"),
            "run_summary_file": final["run_summary_file"],
        },
    )
    print_artifact("Final result", workspace, run_dir / "final.json")
    print_artifact("Run summary", workspace, run_dir / "RUN_SUMMARY.md")
    print_artifact("Run events", workspace, run_dir / "run_events.jsonl")
    print("Reached max_iterations")
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    return run_task_loop(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
