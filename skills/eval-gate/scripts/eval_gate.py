#!/usr/bin/env python3
"""
Deterministic validation gate for the codex task loop.

Runs task-packet validation commands, regression commands, artifact checks, and
the allowed/blocked path diff audit, then writes a schema-validated
evidence.json. No LLM calls. Exit code 0 means the outer gate passed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence


SKILL_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_SCHEMA_PATH = SKILL_ROOT / "schemas" / "evidence.schema.json"
TAIL_LIMIT = 4000


# --- file helpers -----------------------------------------------------------


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def tail(text: str, limit: int = TAIL_LIMIT) -> str:
    return text[-limit:]


def validate_json(schema: dict[str, Any], obj: dict[str, Any], label: str) -> None:
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(obj), key=lambda error: list(error.path))
    if errors:
        message = "\n".join(f"{label}: {list(error.path)}: {error.message}" for error in errors)
        raise SystemExit(message)


# --- git discovery and diff audit -------------------------------------------


def git_root(cwd: Path) -> Path:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(cwd),
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise SystemExit("eval_gate.py must run against a workspace inside a git repository.")
    return Path(proc.stdout.strip()).resolve()


def git_lines(repo: Path, args: list[str]) -> list[str]:
    proc = subprocess.run(["git", *args], cwd=str(repo), text=True, capture_output=True)
    if proc.returncode != 0:
        command = "git " + " ".join(args)
        raise RuntimeError(f"{command} failed: {proc.stderr.strip()}")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def relative_to_workspace(path: str, repo: Path, workspace: Path) -> str | None:
    absolute_path = repo / path
    try:
        return absolute_path.relative_to(workspace).as_posix()
    except ValueError:
        return None


def changed_files(repo: Path, workspace: Path) -> list[str]:
    tracked = git_lines(repo, ["diff", "--name-only"])
    staged = git_lines(repo, ["diff", "--cached", "--name-only"])
    untracked = git_lines(repo, ["ls-files", "--others", "--exclude-standard"])
    files = sorted(set(tracked + staged + untracked))
    workspace_files = [
        relative
        for path in files
        if (relative := relative_to_workspace(path, repo, workspace)) is not None
    ]
    return [path for path in workspace_files if not path.startswith(".codex_task_loop/")]


def path_matches(path: str, patterns: list[str]) -> bool:
    return any(path.startswith(pattern) if pattern.endswith("/") else path == pattern for pattern in patterns)


def audit_diff(files: list[str], allowed_paths: list[str], blocked_paths: list[str]) -> dict[str, Any]:
    unexpected = [path for path in files if not path_matches(path, allowed_paths)]
    blocked = [path for path in files if path_matches(path, blocked_paths)]
    return {
        "changed_files": files,
        "allowed_paths": allowed_paths,
        "blocked_paths": blocked_paths,
        "unexpected_files": unexpected,
        "blocked_files_changed": blocked,
        "passed": not unexpected and not blocked,
    }


def write_diff_snapshot(repo: Path, iteration_dir: Path) -> None:
    diff = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=str(repo),
        text=True,
        capture_output=True,
    ).stdout
    untracked = git_lines(repo, ["ls-files", "--others", "--exclude-standard"])
    untracked_block = "".join(f"# untracked: {path}\n" for path in untracked)
    (iteration_dir / "workspace.diff").write_text(untracked_block + diff, encoding="utf-8")


# --- validation commands ----------------------------------------------------


def run_command(command: str, workspace: Path, timeout_seconds: int) -> dict[str, Any]:
    started = dt.datetime.now(dt.UTC).isoformat()
    try:
        proc = subprocess.run(
            command,
            cwd=str(workspace),
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
        return {
            "command": command,
            "returncode": proc.returncode,
            "passed": proc.returncode == 0,
            "started_at": started,
            "ended_at": dt.datetime.now(dt.UTC).isoformat(),
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "returncode": 124,
            "passed": False,
            "started_at": started,
            "ended_at": dt.datetime.now(dt.UTC).isoformat(),
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or f"Command timed out after {timeout_seconds} seconds.",
        }


def run_validation_commands(
    commands: list[str],
    workspace: Path,
    timeout_seconds: int,
    iteration_dir: Path,
) -> list[dict[str, Any]]:
    results = [run_command(command, workspace, timeout_seconds) for command in commands]
    for index, result in enumerate(results, start=1):
        write_json(iteration_dir / f"command_{index:02d}.json", result)
    return results


# --- artifact checks --------------------------------------------------------


ArtifactCheck = dict[str, Any]


def artifact_exists(path: Path, check: ArtifactCheck) -> tuple[bool, str]:
    exists = path.exists()
    return exists, "exists" if exists else "missing"


def artifact_nonempty(path: Path, check: ArtifactCheck) -> tuple[bool, str]:
    passed = path.exists() and path.is_file() and path.stat().st_size > 0
    return passed, "nonempty" if passed else "missing_or_empty"


def artifact_contains(path: Path, check: ArtifactCheck) -> tuple[bool, str]:
    expected = check.get("text", "")
    content = path.read_text(encoding="utf-8") if path.exists() and path.is_file() else ""
    passed = expected in content
    detail = f"contains {expected!r}" if passed else f"does_not_contain {expected!r}"
    return passed, detail


ARTIFACT_CHECKS: dict[str, Callable[[Path, ArtifactCheck], tuple[bool, str]]] = {
    "exists": artifact_exists,
    "nonempty": artifact_nonempty,
    "contains": artifact_contains,
}


def evaluate_artifact_check(workspace: Path, check: ArtifactCheck) -> dict[str, Any]:
    path = workspace / check["path"]
    kind = check["kind"]
    handler = ARTIFACT_CHECKS.get(kind)
    if handler is None:
        return {**check, "passed": False, "detail": f"unsupported_check_kind {kind!r}"}
    passed, detail = handler(path, check)
    return {**check, "passed": passed, "detail": detail}


# --- evidence assembly ------------------------------------------------------


def build_evidence(
    task: dict[str, Any],
    iteration: int,
    command_results: list[dict[str, Any]],
    artifact_results: list[dict[str, Any]],
    diff_result: dict[str, Any],
    iteration_dir: Path,
    workspace: Path,
) -> dict[str, Any]:
    validation_passed = all(result["passed"] for result in command_results) if command_results else True
    artifacts_passed = all(result["passed"] for result in artifact_results) if artifact_results else True
    outer_gate_passed = validation_passed and artifacts_passed and diff_result["passed"]

    return {
        "task_id": task["task_id"],
        "iteration": iteration,
        "outer_gate_passed": outer_gate_passed,
        "validation_passed": validation_passed,
        "artifact_checks_passed": artifacts_passed,
        "diff_audit_passed": diff_result["passed"],
        "commands": [
            {
                "command": result["command"],
                "returncode": result["returncode"],
                "passed": result["passed"],
                "stdout_tail": tail(result["stdout"]),
                "stderr_tail": tail(result["stderr"]),
            }
            for result in command_results
        ],
        "artifact_checks": artifact_results,
        "diff_audit": diff_result,
        "codex_execution_summary_file": (iteration_dir / "codex_execution.md").relative_to(workspace).as_posix(),
    }


# --- entrypoint --------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the deterministic task-loop validation gate.")
    parser.add_argument("--task", required=True, help="Path to task packet JSON.")
    parser.add_argument("--workspace-root", required=True, help="Workspace root for commands, checks, and audits.")
    parser.add_argument("--iteration", required=True, type=int, help="Current loop iteration number.")
    parser.add_argument("--iteration-dir", required=True, help="Directory for evidence and command logs.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    task = read_json(Path(args.task).resolve())
    workspace = Path(args.workspace_root).resolve()
    iteration_dir = Path(args.iteration_dir).resolve()
    iteration_dir.mkdir(parents=True, exist_ok=True)

    repo = git_root(workspace)
    timeout_seconds = int(task.get("command_timeout_seconds", 120))

    commands = task.get("validation_commands", []) + task.get("regression_commands", [])
    command_results = run_validation_commands(commands, workspace, timeout_seconds, iteration_dir)
    files = changed_files(repo, workspace)
    diff_result = audit_diff(files, task["allowed_paths"], task.get("blocked_paths", []))
    artifact_results = [evaluate_artifact_check(workspace, check) for check in task.get("artifact_checks", [])]
    write_diff_snapshot(repo, iteration_dir)

    evidence = build_evidence(task, args.iteration, command_results, artifact_results, diff_result, iteration_dir, workspace)
    validate_json(read_json(EVIDENCE_SCHEMA_PATH), evidence, "evidence")
    write_json(iteration_dir / "evidence.json", evidence)

    return 0 if evidence["outer_gate_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
