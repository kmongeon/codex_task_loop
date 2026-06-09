"""Validation command execution, artifact checks, and evidence assembly."""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path
from typing import Any, Callable

from file_io import tail, write_json


ArtifactCheck = dict[str, Any]
ArtifactResult = dict[str, Any]


def validate_json(schema: dict[str, Any], obj: dict[str, Any], label: str) -> None:
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(obj), key=lambda error: list(error.path))
    if errors:
        message = "\n".join(f"{label}: {list(error.path)}: {error.message}" for error in errors)
        raise SystemExit(message)


def run_command(command: str, repo: Path, timeout_seconds: int) -> dict[str, Any]:
    started = dt.datetime.now(dt.UTC).isoformat()
    try:
        proc = subprocess.run(
            command,
            cwd=str(repo),
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
    repo: Path,
    timeout_seconds: int,
    iteration_dir: Path,
) -> list[dict[str, Any]]:
    results = [run_command(command, repo, timeout_seconds) for command in commands]
    for index, result in enumerate(results, start=1):
        write_json(iteration_dir / f"command_{index:02d}.json", result)
    return results


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


def evaluate_artifact_check(repo: Path, check: ArtifactCheck) -> ArtifactResult:
    path = repo / check["path"]
    kind = check["kind"]
    handler = ARTIFACT_CHECKS.get(kind)
    if handler is None:
        return {**check, "passed": False, "detail": f"unsupported_check_kind {kind!r}"}
    passed, detail = handler(path, check)
    return {**check, "passed": passed, "detail": detail}


def evaluate_artifact_checks(repo: Path, checks: list[ArtifactCheck]) -> list[ArtifactResult]:
    return [evaluate_artifact_check(repo, check) for check in checks]


def build_evidence(
    task: dict[str, Any],
    iteration: int,
    command_results: list[dict[str, Any]],
    artifact_results: list[ArtifactResult],
    diff_result: dict[str, Any],
    iteration_dir: Path,
    repo: Path,
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
        "codex_execution_summary_file": str((iteration_dir / "codex_execution.md").relative_to(repo)),
    }
