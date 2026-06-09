"""Git repository discovery and task-loop diff auditing."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


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


def changed_files(repo: Path, workspace: Path | None = None) -> list[str]:
    tracked = git_lines(repo, ["diff", "--name-only"])
    staged = git_lines(repo, ["diff", "--cached", "--name-only"])
    untracked = git_lines(repo, ["ls-files", "--others", "--exclude-standard"])
    files = sorted(set(tracked + staged + untracked))
    if workspace is None:
        return [path for path in files if not path.startswith(".codex_task_loop/")]

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
