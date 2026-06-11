#!/usr/bin/env python3
"""
Validate a Codex task-loop task packet before running the loop.

This script validates the packet against the task packet JSON schema and checks
runtime assumptions that the schema cannot express cleanly: safe relative path
fields, non-empty command strings, contains-check text, and optional workspace
root usability.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Sequence


SKILL_ROOT = Path(__file__).resolve().parents[1]
TASK_SCHEMA_PATH = SKILL_ROOT / "schemas" / "task_packet.schema.json"
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PATH_LIST_FIELDS = ("allowed_paths", "blocked_paths")
COMMAND_LIST_FIELDS = ("validation_commands", "regression_commands")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"File does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc


def schema_errors(schema: dict[str, Any], obj: Any) -> list[str]:
    try:
        from jsonschema import Draft202012Validator
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Missing required dependency: jsonschema. Install this plugin's "
            "requirements or run with the project environment."
        ) from exc

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(obj), key=lambda error: list(error.path))
    return [f"schema {list(error.path)}: {error.message}" for error in errors]


def is_safe_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        value not in ("", ".", "./")
        and not value.startswith(("~", "/"))
        and "\\" not in value
        and ".." not in path.parts
    )


def validate_task_id(task_id: str) -> list[str]:
    if TASK_ID_PATTERN.fullmatch(task_id):
        return []
    return [
        "task_id must start with an ASCII letter or digit and contain only "
        "ASCII letters, digits, dots, underscores, or hyphens."
    ]


def validate_path_list(task: dict[str, Any], field: str) -> list[str]:
    values = task.get(field, [])
    return [
        f"{field}[{index}] must be a safe workspace-relative POSIX path or prefix: {value!r}"
        for index, value in enumerate(values)
        if not is_safe_relative_path(value)
    ]


def validate_command_list(task: dict[str, Any], field: str) -> list[str]:
    values = task.get(field, [])
    return [
        f"{field}[{index}] must be a non-empty shell command string."
        for index, value in enumerate(values)
        if not value.strip()
    ]


def validate_artifact_checks(task: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for index, check in enumerate(task.get("artifact_checks", [])):
        path = check["path"]
        kind = check["kind"]
        text = check.get("text")

        if not is_safe_relative_path(path):
            errors.append(
                f"artifact_checks[{index}].path must be a safe workspace-relative "
                f"POSIX path: {path!r}"
            )
        if kind == "contains" and not isinstance(text, str):
            errors.append(f"artifact_checks[{index}].text is required when kind is 'contains'.")
        if kind == "contains" and isinstance(text, str) and not text:
            errors.append(
                f"artifact_checks[{index}].text must be non-empty when kind is 'contains'."
            )
        if kind != "contains" and text is not None:
            errors.append(f"artifact_checks[{index}].text is only valid when kind is 'contains'.")
    return errors


def git_root(cwd: Path) -> tuple[Path | None, str | None]:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(cwd),
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        return None, proc.stderr.strip() or "not inside a git repository"
    return Path(proc.stdout.strip()).resolve(), None


def resolve_workspace_root(value: str) -> tuple[Path | None, list[str]]:
    workspace = Path(value).expanduser().resolve()
    errors: list[str] = []
    if not workspace.exists():
        errors.append(f"workspace_root does not exist: {workspace}")
        return None, errors
    if not workspace.is_dir():
        errors.append(f"workspace_root is not a directory: {workspace}")
        return None, errors

    repo, git_error = git_root(workspace)
    if git_error is not None:
        errors.append(f"workspace_root must be inside a git repository: {workspace} ({git_error})")
        return workspace, errors
    try:
        workspace.relative_to(repo)
    except ValueError:
        errors.append(f"workspace_root must be inside git root {repo}: {workspace}")
    return workspace, errors


def validate_semantics(task: dict[str, Any]) -> list[str]:
    errors = validate_task_id(task["task_id"])
    for field in PATH_LIST_FIELDS:
        errors.extend(validate_path_list(task, field))
    for field in COMMAND_LIST_FIELDS:
        errors.extend(validate_command_list(task, field))
    errors.extend(validate_artifact_checks(task))
    return errors


def selected_workspace_root(args: argparse.Namespace, task: dict[str, Any]) -> str | None:
    if args.workspace_root is not None:
        return args.workspace_root
    return task.get("workspace_root")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a Codex task-loop task packet.")
    parser.add_argument("--task", required=True, help="Path to task packet JSON.")
    parser.add_argument(
        "--workspace-root",
        help=(
            "Workspace root to validate for this packet. Overrides task.workspace_root "
            "for this validation run."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    task_path = Path(args.task).expanduser().resolve()
    task = read_json(task_path)
    schema = read_json(TASK_SCHEMA_PATH)

    errors = schema_errors(schema, task)
    if isinstance(task, dict) and not errors:
        errors.extend(validate_semantics(task))
        workspace_value = selected_workspace_root(args, task)
        workspace = None
        if workspace_value is not None:
            workspace, workspace_errors = resolve_workspace_root(workspace_value)
            errors.extend(workspace_errors)
    else:
        workspace = None

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    print(f"valid task packet: {task_path}")
    print(f"task_id: {task['task_id']}")
    if workspace is not None:
        print(f"workspace_root: {workspace}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
