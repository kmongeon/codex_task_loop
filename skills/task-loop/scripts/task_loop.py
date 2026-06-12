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
MAIN_BRANCH = "main"
ORIGIN_REMOTE = "origin"
ORIGIN_MAIN = "origin/main"
GIT_POLICY = "clean-main-task-branch-fast-forward"
DEFAULT_REPAIR_PROMPT = (
    "Continue the bounded task. Use the latest evidence log, repair unresolved criteria, "
    "stay within allowed paths, and summarize changed files."
)


class GitPolicyError(RuntimeError):
    """Raised when the runner cannot satisfy the mandatory Git lifecycle."""


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


def run_final_validation(task_path: Path, workspace: Path, iteration: int, run_dir: Path) -> tuple[dict[str, Any], Path]:
    final_validation_dir = run_dir / "final_validation"
    evidence = run_eval_gate(task_path, workspace, iteration, final_validation_dir)
    return evidence, final_validation_dir


# --- git lifecycle ------------------------------------------------------------


def git_command(repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["git", *args], cwd=str(repo), text=True, capture_output=True)
    if proc.returncode != 0:
        command = "git " + " ".join(args)
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise GitPolicyError(f"{command} failed: {detail}")
    return proc


def git_stdout(repo: Path, args: list[str]) -> str:
    return git_command(repo, args).stdout.strip()


def git_head(repo: Path) -> str:
    return git_stdout(repo, ["rev-parse", "HEAD"])


def git_ref(repo: Path, ref: str) -> str:
    return git_stdout(repo, ["rev-parse", "--verify", ref])


def current_branch(repo: Path) -> str:
    branch = git_stdout(repo, ["branch", "--show-current"])
    if not branch:
        raise GitPolicyError("task-loop must run on a named branch, not detached HEAD.")
    return branch


def clean_status(repo: Path) -> str:
    return git_stdout(repo, ["status", "--porcelain", "--untracked-files=all"])


def require_clean_worktree(repo: Path, label: str) -> None:
    status = clean_status(repo)
    if status:
        raise GitPolicyError(f"{label} requires a clean worktree:\n{status}")


def ensure_origin(repo: Path) -> None:
    git_stdout(repo, ["remote", "get-url", ORIGIN_REMOTE])


def fetch_origin(repo: Path) -> None:
    git_command(repo, ["fetch", ORIGIN_REMOTE])


def ensure_main_matches_origin(repo: Path) -> tuple[str, str]:
    main_commit = git_ref(repo, f"refs/heads/{MAIN_BRANCH}")
    origin_commit = git_ref(repo, f"refs/remotes/{ORIGIN_MAIN}")
    if main_commit != origin_commit:
        raise GitPolicyError(
            f"{MAIN_BRANCH} must match {ORIGIN_MAIN}: "
            f"{MAIN_BRANCH}={main_commit}, {ORIGIN_MAIN}={origin_commit}"
        )
    return main_commit, origin_commit


def git_preflight(repo: Path) -> dict[str, str]:
    branch = current_branch(repo)
    if branch != MAIN_BRANCH:
        raise GitPolicyError(f"task-loop must start on {MAIN_BRANCH}; current branch is {branch}.")
    ensure_origin(repo)
    fetch_origin(repo)
    main_commit, origin_commit = ensure_main_matches_origin(repo)
    require_clean_worktree(repo, f"{MAIN_BRANCH} preflight")
    return {
        "start_main_commit": main_commit,
        "origin_main_commit": origin_commit,
    }


def task_branch_name(task: dict[str, Any]) -> str:
    return f"codex/{task['task_id']}"


def validate_branch_name(repo: Path, branch: str) -> None:
    git_stdout(repo, ["check-ref-format", "--branch", branch])


def branch_exists(repo: Path, branch: str) -> bool:
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=str(repo),
        text=True,
        capture_output=True,
    )
    return proc.returncode == 0


def create_task_branch(repo: Path, branch: str, start_main_commit: str) -> None:
    validate_branch_name(repo, branch)
    if branch_exists(repo, branch):
        raise GitPolicyError(f"task branch already exists: {branch}")
    git_command(repo, ["switch", "-c", branch, start_main_commit])
    branch_commit = git_head(repo)
    if branch_commit != start_main_commit:
        raise GitPolicyError(
            f"task branch {branch} must start at {start_main_commit}; got {branch_commit}."
        )


def repo_paths_from_workspace_files(repo: Path, workspace: Path, files: list[str]) -> list[str]:
    return sorted(str((workspace / file).relative_to(repo)) for file in files)


def commit_accepted_changes(
    repo: Path,
    workspace: Path,
    files: list[str],
    task_id: str,
    iteration: int,
) -> str:
    if not files:
        raise GitPolicyError("accepted task must change at least one diff-audited file.")
    paths = repo_paths_from_workspace_files(repo, workspace, files)
    git_command(repo, ["add", "--", *paths])
    staged = sorted(git_stdout(repo, ["diff", "--cached", "--name-only"]).splitlines())
    if staged != paths:
        raise GitPolicyError(f"staged paths must equal diff-audited paths: staged={staged}, audited={paths}")
    git_command(repo, ["commit", "-m", f"task-loop({task_id}): accepted at iteration {iteration}", "--", *paths])
    require_clean_worktree(repo, "accepted task commit")
    return git_head(repo)


def fast_forward_main(repo: Path, task_branch: str) -> str:
    git_command(repo, ["switch", MAIN_BRANCH])
    fetch_origin(repo)
    ensure_main_matches_origin(repo)
    git_command(repo, ["merge", "--ff-only", task_branch])
    final_main_commit = git_head(repo)
    require_clean_worktree(repo, "post fast-forward")
    return final_main_commit


def discard_unaccepted_task_changes(repo: Path) -> None:
    git_command(repo, ["restore", "--staged", "--worktree", "."])
    git_command(repo, ["clean", "-fd"])


def return_to_clean_main(repo: Path) -> str:
    discard_unaccepted_task_changes(repo)
    git_command(repo, ["switch", MAIN_BRANCH])
    require_clean_worktree(repo, "return to main")
    return git_head(repo)


def verify_final_clean_main(repo: Path) -> str:
    branch = current_branch(repo)
    if branch != MAIN_BRANCH:
        raise GitPolicyError(f"final branch must be {MAIN_BRANCH}; current branch is {branch}.")
    require_clean_worktree(repo, "final main")
    return git_head(repo)


def initial_git_metadata(preflight: dict[str, str], task_branch: str) -> dict[str, Any]:
    return {
        "git_policy": GIT_POLICY,
        "start_main_commit": preflight["start_main_commit"],
        "origin_main_commit": preflight["origin_main_commit"],
        "task_branch": task_branch,
        "accepted_commit": None,
        "fast_forwarded_to_main": False,
        "final_main_commit": None,
        "final_validation_passed": False,
        "final_clean_main": False,
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

    lines += [
        "",
        "## Git Lifecycle",
        "",
        f"- Policy: `{final['git_policy']}`",
        f"- Start main commit: `{final['start_main_commit']}`",
        f"- Origin main commit: `{final['origin_main_commit']}`",
        f"- Task branch: `{final['task_branch']}`",
        f"- Accepted commit: `{final.get('accepted_commit') or 'none'}`",
        f"- Fast-forwarded to main: `{final['fast_forwarded_to_main']}`",
        f"- Final main commit: `{final.get('final_main_commit') or 'none'}`",
        f"- Final validation passed: `{final['final_validation_passed']}`",
        f"- Final clean main: `{final['final_clean_main']}`",
    ]
    if "final_validation_file" in final:
        lines.append(f"- Final validation evidence: `{final['final_validation_file']}`")
    if "git_failure_reason" in final:
        lines.append(f"- Git failure reason: `{final['git_failure_reason']}`")

    lines += [
        "",
        "## Changed Files",
        "",
        markdown_list(final.get("changed_files", [])),
    ]

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


def write_final_result(
    run_dir: Path,
    workspace: Path,
    task: dict[str, Any],
    complete: bool,
    iterations: int,
    changed: list[str],
    iteration_records: list[dict[str, Any]],
    git_metadata: dict[str, Any],
    decision: dict[str, Any] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    final: dict[str, Any] = {
        "complete": complete,
        "iterations": iterations,
        "run_dir": str(run_dir.relative_to(workspace)),
        "run_events_file": relative_artifact_path(workspace, run_dir / "run_events.jsonl"),
        "run_summary_file": relative_artifact_path(workspace, run_dir / "RUN_SUMMARY.md"),
        **git_metadata,
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

    try:
        preflight = git_preflight(repo)
        task_branch = task_branch_name(task)
        create_task_branch(repo, task_branch, preflight["start_main_commit"])
    except GitPolicyError as exc:
        raise SystemExit(f"Git policy failed before task execution: {exc}") from exc

    codex_launch = build_codex_launch_options(args)
    thread_options = build_thread_options(args, workspace)
    turn_options = build_turn_options(args, workspace)
    review_model = args.review_model or args.model

    max_iterations = args.max_iterations or int(task["max_iterations"])
    run_dir = create_run_directory(workspace, task)
    task_path = run_dir / "task.json"
    execution_template = EXECUTION_TEMPLATE_PATH.read_text(encoding="utf-8")
    task_json = json.dumps(task, indent=2, ensure_ascii=False)
    git_metadata = initial_git_metadata(preflight, task_branch)

    print(f"Git root: {repo}")
    print(f"Workspace root: {workspace}")
    print(f"Run directory: {run_dir.relative_to(workspace)}")
    print(f"Task: {task['task_id']}")
    print(f"Task branch: {task_branch}")
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
    append_run_event(
        run_dir,
        {
            "event": "git_preflight_started",
            "recorded_after_preflight": True,
            "policy": GIT_POLICY,
        },
    )
    append_run_event(
        run_dir,
        {
            "event": "git_preflight_passed",
            "start_main_commit": preflight["start_main_commit"],
            "origin_main_commit": preflight["origin_main_commit"],
        },
    )
    append_run_event(
        run_dir,
        {
            "event": "task_branch_created",
            "task_branch": task_branch,
            "start_main_commit": preflight["start_main_commit"],
        },
    )

    decision: dict[str, Any] | None = None
    evidence: dict[str, Any] | None = None
    iteration_records: list[dict[str, Any]] = []
    latest_changed: list[str] = []

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
        latest_changed = changed
        accepted = (
            decision["decision"] == "accept"
            and evidence["outer_gate_passed"]
            and evidence["diff_audit_passed"]
        )
        if accepted:
            try:
                accepted_commit = commit_accepted_changes(repo, workspace, changed, task["task_id"], iteration)
                git_metadata["accepted_commit"] = accepted_commit
                append_run_event(
                    run_dir,
                    {
                        "event": "accepted_commit_created",
                        "iteration": iteration,
                        "accepted_commit": accepted_commit,
                        "files": changed,
                    },
                )

                final_main_commit = fast_forward_main(repo, task_branch)
                git_metadata["fast_forwarded_to_main"] = True
                git_metadata["final_main_commit"] = final_main_commit
                append_run_event(
                    run_dir,
                    {
                        "event": "main_fast_forwarded",
                        "task_branch": task_branch,
                        "final_main_commit": final_main_commit,
                    },
                )

                final_validation, final_validation_dir = run_final_validation(task_path, workspace, iteration, run_dir)
                final_validation_file = relative_artifact_path(
                    workspace,
                    final_validation_dir / "evidence.json",
                )
                git_metadata["final_validation_passed"] = final_validation["outer_gate_passed"]
                git_metadata["final_validation_file"] = final_validation_file
                append_run_event(
                    run_dir,
                    {
                        "event": "final_validation_completed",
                        "outer_gate_passed": final_validation["outer_gate_passed"],
                        "evidence_file": final_validation_file,
                    },
                )
                if not final_validation["outer_gate_passed"]:
                    git_metadata["git_failure_reason"] = "final_validation_failed"
                    try:
                        git_metadata["final_main_commit"] = verify_final_clean_main(repo)
                        git_metadata["final_clean_main"] = True
                        append_run_event(
                            run_dir,
                            {
                                "event": "final_clean_main_verified",
                                "final_main_commit": git_metadata["final_main_commit"],
                            },
                        )
                    except GitPolicyError as exc:
                        try:
                            git_metadata["final_main_commit"] = return_to_clean_main(repo)
                            git_metadata["final_clean_main"] = True
                            append_run_event(
                                run_dir,
                                {
                                    "event": "final_clean_main_verified",
                                    "final_main_commit": git_metadata["final_main_commit"],
                                },
                            )
                        except GitPolicyError as cleanup_exc:
                            git_metadata["git_failure_reason"] = (
                                f"final_validation_failed; {exc}; cleanup failed: {cleanup_exc}"
                            )
                    final = write_final_result(
                        run_dir,
                        workspace,
                        task,
                        False,
                        iteration,
                        changed,
                        iteration_records,
                        git_metadata,
                        decision=decision,
                        reason="Final validation failed on main",
                    )
                    append_run_event(
                        run_dir,
                        {
                            "event": "git_policy_failed",
                            "reason": "final_validation_failed",
                            "final_file": relative_artifact_path(workspace, run_dir / "final.json"),
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
                    print(f"Stopped: {final['reason']}")
                    return 1

                git_metadata["final_main_commit"] = verify_final_clean_main(repo)
                git_metadata["final_clean_main"] = True
                append_run_event(
                    run_dir,
                    {
                        "event": "final_clean_main_verified",
                        "final_main_commit": git_metadata["final_main_commit"],
                    },
                )
            except GitPolicyError as exc:
                git_metadata["git_failure_reason"] = str(exc)
                try:
                    git_metadata["final_main_commit"] = return_to_clean_main(repo)
                    git_metadata["final_clean_main"] = True
                    append_run_event(
                        run_dir,
                        {
                            "event": "final_clean_main_verified",
                            "final_main_commit": git_metadata["final_main_commit"],
                        },
                    )
                except GitPolicyError as cleanup_exc:
                    git_metadata["git_failure_reason"] = f"{exc}; cleanup failed: {cleanup_exc}"
                final = write_final_result(
                    run_dir,
                    workspace,
                    task,
                    False,
                    iteration,
                    changed,
                    iteration_records,
                    git_metadata,
                    decision=decision,
                    reason="Git policy failed after acceptance",
                )
                append_run_event(
                    run_dir,
                    {
                        "event": "git_policy_failed",
                        "reason": str(exc),
                        "final_file": relative_artifact_path(workspace, run_dir / "final.json"),
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
                print(f"Stopped: {final['reason']}")
                return 2

            final = write_final_result(
                run_dir,
                workspace,
                task,
                True,
                iteration,
                changed,
                iteration_records,
                git_metadata,
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

        if decision["decision"] in STOP_DECISIONS:
            try:
                git_metadata["final_main_commit"] = return_to_clean_main(repo)
                git_metadata["final_clean_main"] = True
                append_run_event(
                    run_dir,
                    {
                        "event": "final_clean_main_verified",
                        "final_main_commit": git_metadata["final_main_commit"],
                    },
                )
            except GitPolicyError as exc:
                git_metadata["git_failure_reason"] = str(exc)
                append_run_event(
                    run_dir,
                    {
                        "event": "git_policy_failed",
                        "reason": str(exc),
                    },
                )
            final = write_final_result(
                run_dir,
                workspace,
                task,
                False,
                iteration,
                changed,
                iteration_records,
                git_metadata,
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

    try:
        git_metadata["final_main_commit"] = return_to_clean_main(repo)
        git_metadata["final_clean_main"] = True
        append_run_event(
            run_dir,
            {
                "event": "final_clean_main_verified",
                "final_main_commit": git_metadata["final_main_commit"],
            },
        )
    except GitPolicyError as exc:
        git_metadata["git_failure_reason"] = str(exc)
        append_run_event(
            run_dir,
            {
                "event": "git_policy_failed",
                "reason": str(exc),
            },
        )
    final = write_final_result(
        run_dir,
        workspace,
        task,
        False,
        max_iterations,
        latest_changed,
        iteration_records,
        git_metadata,
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
