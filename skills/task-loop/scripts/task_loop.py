#!/usr/bin/env python3
"""
Manifest-only Codex task lifecycle orchestrator.

Every run is driven by a task-series manifest, including one-packet runs. The
runner validates the full manifest and every referenced task packet before it
creates a series branch, writes progress state, or assigns any packet outcome.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import validate_task_packet
from codex_session import CodexLaunchOptions, ThreadOptions, TurnOptions, run_execution_turn


SKILL_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = SKILL_ROOT.parent
EVAL_GATE_SCRIPT = SKILLS_ROOT / "eval-gate" / "scripts" / "eval_gate.py"
EVIDENCE_REVIEW_SCRIPT = SKILLS_ROOT / "evidence-review" / "scripts" / "evidence_review.py"
TASK_SCHEMA_PATH = SKILL_ROOT / "schemas" / "task_packet.schema.json"
MANIFEST_SCHEMA_PATH = SKILL_ROOT / "schemas" / "task_series_manifest.schema.json"
STATE_SCHEMA_PATH = SKILL_ROOT / "schemas" / "task_series_state.schema.json"
EXECUTION_TEMPLATE_PATH = SKILL_ROOT / "templates" / "execution_prompt.md"

RUNS_DIR = ".codex_task_loop/runs"
SERIES_STATE_ROOT = "codex_task_loop_series"
MAIN_BRANCH = "main"
ORIGIN_REMOTE = "origin"
ORIGIN_MAIN = "origin/main"
GIT_POLICY = "clean-main-series-branch-push"
PENDING = "pending"
COMPLETED = "completed"
SKIPPED = "skipped"
STOPPED = "stopped"
ACCEPTED = "accepted"
DEPENDENCY_NOT_COMPLETED = "dependency_not_completed"
MAX_ITERATIONS = "max_iterations"
STOP_DECISION_OUTCOMES = {
    "escalate": "escalated",
    "reject": "rejected",
    "split": "split_required",
}
TERMINAL_STATES = {COMPLETED, SKIPPED, STOPPED}
DEFAULT_REPAIR_PROMPT = (
    "Continue the bounded task. Use the latest evidence log, repair unresolved criteria, "
    "stay within allowed paths, and summarize changed files."
)


class GitPolicyError(RuntimeError):
    """Raised when the runner cannot satisfy the mandatory Git lifecycle."""


@dataclass(frozen=True)
class PacketPlan:
    packet_id: str
    task_path: Path
    task_relpath: str
    task: dict[str, Any]
    depends_on: list[str]


@dataclass(frozen=True)
class ManifestPlan:
    manifest_path: Path
    manifest: dict[str, Any]
    workspace: Path
    packets: list[PacketPlan]


@dataclass(frozen=True)
class ManifestPreflight:
    plan: ManifestPlan
    git_metadata: dict[str, str]


@dataclass(frozen=True)
class PacketRunResult:
    state: str
    outcome: str
    run_dir: str
    artifacts: dict[str, str | None]
    accepted_commit: str | None
    iterations: int
    changed_files: list[str]
    reason: str | None = None


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


def schema_error_messages(schema: dict[str, Any], obj: Any, label: str) -> list[str]:
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(obj), key=lambda error: list(error.path))
    return [f"{label}: {list(error.path)}: {error.message}" for error in errors]


def validate_json(schema: dict[str, Any], obj: Any, label: str) -> None:
    errors = schema_error_messages(schema, obj, label)
    if errors:
        raise SystemExit("\n".join(errors))


def utc_timestamp() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


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
    parser = argparse.ArgumentParser(description="Run a manifest-defined Codex task lifecycle loop.")
    parser.add_argument("--manifest", required=True, help="Path to task-series manifest JSON.")
    parser.add_argument("--model", default="gpt-5.5", help="Codex model for execution turns.")
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


# --- manifest validation ------------------------------------------------------


def resolve_manifest_workspace_root(repo: Path, manifest_path: Path, value: str) -> Path:
    raw_path = Path(value).expanduser()
    workspace = raw_path if raw_path.is_absolute() else manifest_path.parent / raw_path
    workspace = workspace.resolve()
    if not workspace.exists():
        raise SystemExit(f"Manifest workspace_root does not exist: {workspace}")
    if not workspace.is_dir():
        raise SystemExit(f"Manifest workspace_root is not a directory: {workspace}")
    try:
        workspace.relative_to(repo)
    except ValueError as exc:
        raise SystemExit(f"Manifest workspace_root must be inside git root {repo}: {workspace}") from exc
    return workspace


def task_workspace_errors(repo: Path, workspace: Path, task_path: Path, task: dict[str, Any]) -> list[str]:
    value = task.get("workspace_root")
    if value is None:
        return []
    task_workspace = Path(value).expanduser()
    if not task_workspace.is_absolute():
        task_workspace = task_path.parent / task_workspace
    task_workspace = task_workspace.resolve()
    if not task_workspace.exists():
        return [f"{task_path}: task workspace_root does not exist: {task_workspace}"]
    if not task_workspace.is_dir():
        return [f"{task_path}: task workspace_root is not a directory: {task_workspace}"]
    try:
        task_workspace.relative_to(repo)
    except ValueError:
        return [f"{task_path}: task workspace_root must be inside git root {repo}: {task_workspace}"]
    if task_workspace != workspace:
        return [
            f"{task_path}: task workspace_root {task_workspace} must match manifest workspace_root {workspace}"
        ]
    return []


def read_task_packet_for_manifest(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        task = read_json(path)
    except FileNotFoundError:
        return None, [f"Task file does not exist: {path}"]
    except json.JSONDecodeError as exc:
        return None, [f"Invalid JSON in task file {path}: {exc}"]
    if not isinstance(task, dict):
        return None, [f"Task file must contain a JSON object: {path}"]
    return task, []


def dependency_cycle_errors(dependencies: dict[str, list[str]]) -> list[str]:
    visited: set[str] = set()
    visiting: list[str] = []

    def visit(packet_id: str) -> list[str]:
        if packet_id in visited:
            return []
        if packet_id in visiting:
            cycle = visiting[visiting.index(packet_id):] + [packet_id]
            return ["Manifest dependency cycle: " + " -> ".join(cycle)]
        visiting.append(packet_id)
        for dependency in dependencies[packet_id]:
            errors = visit(dependency)
            if errors:
                return errors
        visiting.pop()
        visited.add(packet_id)
        return []

    for packet_id in dependencies:
        errors = visit(packet_id)
        if errors:
            return errors
    return []


def manifest_semantic_errors(
    manifest: dict[str, Any],
    manifest_path: Path,
    repo: Path,
    workspace: Path,
) -> tuple[list[str], list[PacketPlan]]:
    errors: list[str] = []
    packet_ids: list[str] = []
    for index, packet in enumerate(manifest["packets"]):
        packet_id = packet["packet_id"]
        if packet_id in packet_ids:
            errors.append(f"packets[{index}].packet_id duplicates an earlier packet_id: {packet_id}")
        packet_ids.append(packet_id)

    all_ids = set(packet_ids)
    dependencies: dict[str, list[str]] = {}
    packets: list[PacketPlan] = []
    task_schema = read_json(TASK_SCHEMA_PATH)
    for index, packet in enumerate(manifest["packets"]):
        packet_id = packet["packet_id"]
        raw_depends_on = packet["depends_on"]
        depends_on = [] if raw_depends_on is None else list(raw_depends_on)
        duplicate_dependencies = sorted({item for item in depends_on if depends_on.count(item) > 1})
        for dependency in duplicate_dependencies:
            errors.append(f"packets[{index}].depends_on duplicates dependency {dependency!r}.")
        for dependency in depends_on:
            if dependency not in all_ids:
                errors.append(
                    f"packets[{index}].depends_on references unknown packet_id {dependency!r}."
                )
        dependencies[packet_id] = depends_on

        task_relpath = packet["task"]
        if not validate_task_packet.is_safe_relative_path(task_relpath):
            errors.append(
                f"packets[{index}].task must be a safe workspace-relative POSIX path: {task_relpath!r}"
            )
            continue
        task_path = workspace / task_relpath
        task, task_read_errors = read_task_packet_for_manifest(task_path)
        errors.extend(task_read_errors)
        if task is None:
            continue
        task_errors = validate_task_packet.schema_errors(task_schema, task)
        if not task_errors:
            task_errors.extend(validate_task_packet.validate_semantics(task))
            task_errors.extend(task_workspace_errors(repo, workspace, task_path, task))
        errors.extend(f"{task_relpath}: {error}" for error in task_errors)
        packets.append(
            PacketPlan(
                packet_id=packet_id,
                task_path=task_path,
                task_relpath=task_relpath,
                task=task,
                depends_on=depends_on,
            )
        )

    if not errors:
        errors.extend(dependency_cycle_errors(dependencies))
    return errors, packets


def load_manifest_plan(repo: Path, manifest_path: Path) -> ManifestPlan:
    repo = repo.resolve()
    manifest_path = manifest_path.expanduser().resolve()
    manifest = read_json(manifest_path)
    manifest_schema = read_json(MANIFEST_SCHEMA_PATH)
    schema_errors = schema_error_messages(manifest_schema, manifest, "series manifest")
    if schema_errors:
        raise SystemExit("\n".join(schema_errors))
    workspace = resolve_manifest_workspace_root(repo, manifest_path, manifest["workspace_root"])
    semantic_errors, packets = manifest_semantic_errors(manifest, manifest_path, repo, workspace)
    if semantic_errors:
        raise SystemExit("\n".join(semantic_errors))
    return ManifestPlan(
        manifest_path=manifest_path,
        manifest=manifest,
        workspace=workspace,
        packets=packets,
    )


def validate_manifest_preflight(repo: Path, manifest_path: Path) -> ManifestPreflight:
    plan = load_manifest_plan(repo, manifest_path)
    try:
        metadata = git_preflight(repo)
    except GitPolicyError as exc:
        raise SystemExit(f"Git policy failed before series execution: {exc}") from exc
    return ManifestPreflight(plan=plan, git_metadata=metadata)


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
    """Recompose the execution prompt: fixed task contract plus latest loop state."""
    prompt = render_template(execution_template, {"task_json": task_json})
    prompt += f"\n\nIteration {iteration} of {max_iterations}."
    if decision is None:
        return prompt

    if evidence is None:
        raise RuntimeError("Repair prompt composition requires prior evidence.")
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


def prepare_series_branch(repo: Path, branch: str, start_main_commit: str) -> None:
    validate_branch_name(repo, branch)
    if branch_exists(repo, branch):
        branch_commit = git_ref(repo, f"refs/heads/{branch}")
        if branch_commit != start_main_commit:
            raise GitPolicyError(
                f"series branch {branch} already exists at {branch_commit}; "
                f"expected verified main {start_main_commit}."
            )
        git_command(repo, ["switch", branch])
    else:
        git_command(repo, ["switch", "-c", branch, start_main_commit])
    if git_head(repo) != start_main_commit:
        raise GitPolicyError(f"series branch {branch} must start at {start_main_commit}.")
    require_clean_worktree(repo, "series branch preparation")


def push_series_branch(repo: Path, branch: str) -> None:
    git_command(repo, ["push", "-u", ORIGIN_REMOTE, branch])


def repo_paths_from_workspace_files(repo: Path, workspace: Path, files: list[str]) -> list[str]:
    return sorted(str((workspace / file).relative_to(repo)) for file in files)


def commit_accepted_changes(
    repo: Path,
    workspace: Path,
    files: list[str],
    packet_id: str,
    iteration: int,
) -> str:
    if not files:
        raise GitPolicyError("accepted packet must change at least one diff-audited file.")
    paths = repo_paths_from_workspace_files(repo, workspace, files)
    git_command(repo, ["add", "--", *paths])
    staged = sorted(git_stdout(repo, ["diff", "--cached", "--name-only"]).splitlines())
    if staged != paths:
        raise GitPolicyError(f"staged paths must equal diff-audited paths: staged={staged}, audited={paths}")
    git_command(repo, ["commit", "-m", f"task-loop({packet_id}): accept packet at iteration {iteration}", "--", *paths])
    require_clean_worktree(repo, "accepted packet commit")
    return git_head(repo)


def discard_unaccepted_task_changes(repo: Path) -> None:
    git_command(repo, ["restore", "--staged", "--worktree", "."])
    git_command(repo, ["clean", "-fd"])


def commit_tracked_state_file(repo: Path, workspace: Path, state_path: Path, message: str) -> str:
    path = str(state_path.relative_to(repo))
    git_command(repo, ["add", "--", path])
    staged = git_stdout(repo, ["diff", "--cached", "--name-only"]).splitlines()
    if staged != [path]:
        raise GitPolicyError(f"state commit must stage only {path}: staged={staged}")
    git_command(repo, ["commit", "-m", message, "--", path])
    require_clean_worktree(repo, "series state commit")
    return git_head(repo)


# --- run bookkeeping ----------------------------------------------------------


def create_run_directory(workspace: Path, series_id: str, packet_id: str, task: dict[str, Any]) -> Path:
    run_id = f"{dt.datetime.now(dt.UTC).strftime('%Y%m%dT%H%M%SZ')}_{series_id}_{packet_id}"
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
        f"- State: `{final['state']}`",
        f"- Outcome: `{final.get('outcome') or 'none'}`",
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
        f"- Series branch: `{final['series_branch']}`",
        f"- Accepted commit: `{final.get('accepted_commit') or 'none'}`",
        f"- State commit: `{final.get('state_commit') or 'none'}`",
        f"- Main advanced: `{final['main_advanced']}`",
    ]

    lines += [
        "",
        "## Changed Files",
        "",
        markdown_list(final.get("changed_files", [])),
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


def packet_artifacts(workspace: Path, run_dir: Path, iteration_records: list[dict[str, Any]]) -> dict[str, str | None]:
    latest = iteration_records[-1]["artifacts"] if iteration_records else {}
    return {
        "final": relative_artifact_path(workspace, run_dir / "final.json"),
        "run_events": relative_artifact_path(workspace, run_dir / "run_events.jsonl"),
        "run_summary": relative_artifact_path(workspace, run_dir / "RUN_SUMMARY.md"),
        "latest_evidence": latest.get("evidence"),
        "latest_decision": latest.get("decision"),
        "latest_diff": latest.get("diff"),
    }


def write_final_result(
    run_dir: Path,
    workspace: Path,
    task: dict[str, Any],
    state: str,
    outcome: str,
    iterations: int,
    changed: list[str],
    iteration_records: list[dict[str, Any]],
    git_metadata: dict[str, Any],
    decision: dict[str, Any] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    final: dict[str, Any] = {
        "complete": state == COMPLETED and outcome == ACCEPTED,
        "state": state,
        "outcome": outcome,
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


def initial_git_metadata(preflight: dict[str, str], series_branch: str) -> dict[str, Any]:
    return {
        "git_policy": GIT_POLICY,
        "start_main_commit": preflight["start_main_commit"],
        "origin_main_commit": preflight["origin_main_commit"],
        "series_branch": series_branch,
        "accepted_commit": None,
        "state_commit": None,
        "main_advanced": False,
    }


# --- scheduler state ----------------------------------------------------------


def dependency_status(packet: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    accepted: list[str] = []
    pending: list[str] = []
    blocked: list[str] = []
    for dependency in packet["depends_on"]:
        dependency_packet = by_id[dependency]
        if dependency_packet["state"] == COMPLETED and dependency_packet["outcome"] == ACCEPTED:
            accepted.append(dependency)
        elif dependency_packet["state"] == PENDING:
            pending.append(dependency)
        else:
            blocked.append(dependency)
    return {
        "accepted": accepted,
        "pending": pending,
        "blocked": blocked,
    }


def refresh_dependency_statuses(state: dict[str, Any]) -> None:
    by_id = {packet["packet_id"]: packet for packet in state["packets"]}
    for packet in state["packets"]:
        packet["dependency_status"] = dependency_status(packet, by_id)


def initial_series_state(plan: ManifestPlan, git_metadata: dict[str, str]) -> dict[str, Any]:
    state = {
        "series_id": plan.manifest["series_id"],
        "series_branch": plan.manifest["series_branch"],
        "workspace_root": str(plan.workspace),
        "git_policy": GIT_POLICY,
        "started_at": utc_timestamp(),
        "updated_at": utc_timestamp(),
        "start_main_commit": git_metadata["start_main_commit"],
        "origin_main_commit": git_metadata["origin_main_commit"],
        "last_state_commit": None,
        "packets": [
            {
                "packet_id": packet.packet_id,
                "task": packet.task_relpath,
                "depends_on": packet.depends_on,
                "state": PENDING,
                "outcome": None,
                "dependency_status": {
                    "accepted": [],
                    "pending": list(packet.depends_on),
                    "blocked": [],
                },
                "run_dir": None,
                "artifacts": {},
                "accepted_commit": None,
                "state_commit": None,
            }
            for packet in plan.packets
        ],
    }
    refresh_dependency_statuses(state)
    return state


def state_path(workspace: Path, series_id: str) -> Path:
    return workspace / SERIES_STATE_ROOT / series_id / "state.json"


def validate_series_state(state: dict[str, Any]) -> None:
    validate_json(read_json(STATE_SCHEMA_PATH), state, "series state")


def write_and_commit_series_state(
    repo: Path,
    workspace: Path,
    state_file: Path,
    state: dict[str, Any],
    message: str,
) -> str:
    state["updated_at"] = utc_timestamp()
    refresh_dependency_statuses(state)
    validate_series_state(state)
    write_json(state_file, state)
    return commit_tracked_state_file(repo, workspace, state_file, message)


def commit_state_transition(
    repo: Path,
    workspace: Path,
    state_file: Path,
    state: dict[str, Any],
    packet_ids: list[str],
    message: str,
    branch: str,
) -> str:
    transition_commit = write_and_commit_series_state(repo, workspace, state_file, state, message)
    for packet in state["packets"]:
        if packet["packet_id"] in packet_ids:
            packet["state_commit"] = transition_commit
    state["last_state_commit"] = transition_commit
    checkpoint_commit = write_and_commit_series_state(
        repo,
        workspace,
        state_file,
        state,
        f"{message} checkpoint",
    )
    push_series_branch(repo, branch)
    return checkpoint_commit


def packet_by_id(state: dict[str, Any], packet_id: str) -> dict[str, Any]:
    for packet in state["packets"]:
        if packet["packet_id"] == packet_id:
            return packet
    raise KeyError(packet_id)


def runnable_packet_ids(state: dict[str, Any]) -> list[str]:
    refresh_dependency_statuses(state)
    return [
        packet["packet_id"]
        for packet in state["packets"]
        if packet["state"] == PENDING and not packet["dependency_status"]["pending"] and not packet["dependency_status"]["blocked"]
    ]


def mark_dependency_skips(state: dict[str, Any]) -> list[str]:
    changed: list[str] = []
    while True:
        refresh_dependency_statuses(state)
        blocked_packets = [
            packet
            for packet in state["packets"]
            if packet["state"] == PENDING and packet["dependency_status"]["blocked"]
        ]
        if not blocked_packets:
            return changed
        for packet in blocked_packets:
            packet["state"] = SKIPPED
            packet["outcome"] = DEPENDENCY_NOT_COMPLETED
            changed.append(packet["packet_id"])


def series_is_terminal(state: dict[str, Any]) -> bool:
    return all(packet["state"] in TERMINAL_STATES for packet in state["packets"])


def update_packet_from_result(packet: dict[str, Any], result: PacketRunResult) -> None:
    packet["state"] = result.state
    packet["outcome"] = result.outcome
    packet["run_dir"] = result.run_dir
    packet["artifacts"] = result.artifacts
    packet["accepted_commit"] = result.accepted_commit


def exit_code_for_state(state: dict[str, Any]) -> int:
    outcomes = [packet["outcome"] for packet in state["packets"]]
    if all(outcome == ACCEPTED for outcome in outcomes):
        return 0
    if MAX_ITERATIONS in outcomes:
        return 1
    return 2


# --- packet execution ---------------------------------------------------------


def execute_packet(
    args: argparse.Namespace,
    repo: Path,
    workspace: Path,
    series_id: str,
    series_branch: str,
    packet: PacketPlan,
    preflight: dict[str, str],
    codex_launch: CodexLaunchOptions,
    thread_options: ThreadOptions,
    turn_options: TurnOptions,
    review_model: str,
) -> PacketRunResult:
    run_dir = create_run_directory(workspace, series_id, packet.packet_id, packet.task)
    task_path = run_dir / "task.json"
    execution_template = EXECUTION_TEMPLATE_PATH.read_text(encoding="utf-8")
    task_json = json.dumps(packet.task, indent=2, ensure_ascii=False)
    git_metadata = initial_git_metadata(preflight, series_branch)

    print(f"\n=== packet {packet.packet_id} ===")
    print(f"Run directory: {run_dir.relative_to(workspace)}")
    print(f"Task: {packet.task['task_id']}")
    print_artifact("Run events", workspace, run_dir / "run_events.jsonl")

    append_run_event(
        run_dir,
        {
            "event": "run_started",
            "series_id": series_id,
            "packet_id": packet.packet_id,
            "task_id": packet.task["task_id"],
            "git_root": str(repo),
            "workspace_root": str(workspace),
            "series_branch": series_branch,
            "run_dir": relative_artifact_path(workspace, run_dir),
            "task_file": relative_artifact_path(workspace, task_path),
            "execution_model": args.model,
            "review_model": review_model,
        },
    )

    decision: dict[str, Any] | None = None
    evidence: dict[str, Any] | None = None
    iteration_records: list[dict[str, Any]] = []
    latest_changed: list[str] = []
    max_iterations = int(packet.task["max_iterations"])

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

        print(f"--- iteration {iteration} ---")
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
        append_run_event(
            run_dir,
            {
                "event": "prompt_written",
                "iteration": iteration,
                "path": relative_artifact_path(workspace, prompt_path),
            },
        )

        work_text = run_execution_turn(codex_launch, thread_options, turn_options, prompt)
        execution_path.write_text(work_text, encoding="utf-8")
        append_run_event(
            run_dir,
            {
                "event": "execution_completed",
                "iteration": iteration,
                "path": relative_artifact_path(workspace, execution_path),
            },
        )

        evidence = run_eval_gate(task_path, workspace, iteration, iter_dir)
        iteration_record["evidence"] = {
            "outer_gate_passed": evidence["outer_gate_passed"],
            "validation_passed": evidence["validation_passed"],
            "artifact_checks_passed": evidence["artifact_checks_passed"],
            "diff_audit_passed": evidence["diff_audit_passed"],
        }
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

        decision = run_evidence_review(task_path, workspace, iter_dir, review_model, args.review_effort)
        iteration_record["decision"] = {
            "decision": decision["decision"],
            "reason": decision["reason"],
        }
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
            accepted_commit = commit_accepted_changes(repo, workspace, changed, packet.packet_id, iteration)
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
            final = write_final_result(
                run_dir,
                workspace,
                packet.task,
                COMPLETED,
                ACCEPTED,
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
                    "state": COMPLETED,
                    "outcome": ACCEPTED,
                    "iterations": iteration,
                    "final_file": relative_artifact_path(workspace, run_dir / "final.json"),
                    "run_summary_file": final["run_summary_file"],
                },
            )
            return PacketRunResult(
                state=COMPLETED,
                outcome=ACCEPTED,
                run_dir=str(run_dir.relative_to(workspace)),
                artifacts=packet_artifacts(workspace, run_dir, iteration_records),
                accepted_commit=accepted_commit,
                iterations=iteration,
                changed_files=changed,
            )

        if decision["decision"] in STOP_DECISION_OUTCOMES:
            outcome = STOP_DECISION_OUTCOMES[decision["decision"]]
            final = write_final_result(
                run_dir,
                workspace,
                packet.task,
                COMPLETED,
                outcome,
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
                    "state": COMPLETED,
                    "outcome": outcome,
                    "iterations": iteration,
                    "final_file": relative_artifact_path(workspace, run_dir / "final.json"),
                    "run_summary_file": final["run_summary_file"],
                },
            )
            discard_unaccepted_task_changes(repo)
            return PacketRunResult(
                state=COMPLETED,
                outcome=outcome,
                run_dir=str(run_dir.relative_to(workspace)),
                artifacts=packet_artifacts(workspace, run_dir, iteration_records),
                accepted_commit=None,
                iterations=iteration,
                changed_files=changed,
            )

    final = write_final_result(
        run_dir,
        workspace,
        packet.task,
        COMPLETED,
        MAX_ITERATIONS,
        max_iterations,
        latest_changed,
        iteration_records,
        git_metadata,
        reason="Reached max_iterations",
    )
    append_run_event(
        run_dir,
        {
            "event": "run_finished",
            "state": COMPLETED,
            "outcome": MAX_ITERATIONS,
            "iterations": max_iterations,
            "final_file": relative_artifact_path(workspace, run_dir / "final.json"),
            "run_summary_file": final["run_summary_file"],
        },
    )
    discard_unaccepted_task_changes(repo)
    return PacketRunResult(
        state=COMPLETED,
        outcome=MAX_ITERATIONS,
        run_dir=str(run_dir.relative_to(workspace)),
        artifacts=packet_artifacts(workspace, run_dir, iteration_records),
        accepted_commit=None,
        iterations=max_iterations,
        changed_files=latest_changed,
        reason="Reached max_iterations",
    )


# --- manifest loop ------------------------------------------------------------


def run_manifest_loop(args: argparse.Namespace) -> int:
    repo = git_root(Path.cwd())
    preflight = validate_manifest_preflight(repo, Path(args.manifest))
    plan = preflight.plan
    manifest = plan.manifest
    series_id = manifest["series_id"]
    series_branch = manifest["series_branch"]
    workspace = plan.workspace

    try:
        prepare_series_branch(repo, series_branch, preflight.git_metadata["start_main_commit"])
    except GitPolicyError as exc:
        raise SystemExit(f"Git policy failed before series execution: {exc}") from exc

    state_file = state_path(workspace, series_id)
    state = initial_series_state(plan, preflight.git_metadata)
    initial_commit = write_and_commit_series_state(
        repo,
        workspace,
        state_file,
        state,
        f"task-loop({series_id}): initialize series state",
    )
    state["last_state_commit"] = initial_commit
    write_and_commit_series_state(
        repo,
        workspace,
        state_file,
        state,
        f"task-loop({series_id}): record initial state checkpoint",
    )
    push_series_branch(repo, series_branch)

    codex_launch = build_codex_launch_options(args)
    thread_options = build_thread_options(args, workspace)
    turn_options = build_turn_options(args, workspace)
    review_model = args.review_model or args.model
    plans_by_id = {packet.packet_id: packet for packet in plan.packets}

    print(f"Git root: {repo}")
    print(f"Workspace root: {workspace}")
    print(f"Series: {series_id}")
    print(f"Series branch: {series_branch}")
    print(f"Execution model: {args.model}")
    print(f"Review model: {review_model}")
    print_artifact("Series state", workspace, state_file)

    while not series_is_terminal(state):
        skipped = mark_dependency_skips(state)
        if skipped:
            commit_state_transition(
                repo,
                workspace,
                state_file,
                state,
                skipped,
                f"task-loop({series_id}): skip dependency-blocked packets",
                series_branch,
            )

        runnable = runnable_packet_ids(state)
        if not runnable:
            break

        packet_id = runnable[0]
        packet_plan = plans_by_id[packet_id]
        result = execute_packet(
            args,
            repo,
            workspace,
            series_id,
            series_branch,
            packet_plan,
            preflight.git_metadata,
            codex_launch,
            thread_options,
            turn_options,
            review_model,
        )
        update_packet_from_result(packet_by_id(state, packet_id), result)
        commit_state_transition(
            repo,
            workspace,
            state_file,
            state,
            [packet_id],
            f"task-loop({series_id}): record packet {packet_id} {result.outcome}",
            series_branch,
        )

    skipped = mark_dependency_skips(state)
    if skipped:
        commit_state_transition(
            repo,
            workspace,
            state_file,
            state,
            skipped,
            f"task-loop({series_id}): skip dependency-blocked packets",
            series_branch,
        )

    if not series_is_terminal(state):
        raise SystemExit("Series scheduler stopped with pending packets but no runnable packet.")

    print_artifact("Final series state", workspace, state_file)
    return exit_code_for_state(state)


def main(argv: Sequence[str] | None = None) -> int:
    return run_manifest_loop(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
