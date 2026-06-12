from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "task-loop" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import task_loop  # noqa: E402
import validate_task_packet  # noqa: E402


def run_git(repo: Path, args: list[str]) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        text=True,
        capture_output=True,
        check=True,
    )
    return proc.stdout.strip()


def init_repo(tmp: Path) -> tuple[Path, Path]:
    origin = tmp / "origin.git"
    repo = tmp / "repo"
    repo.mkdir()
    run_git(tmp, ["init", "--bare", "--initial-branch=main", str(origin)])
    run_git(repo, ["init", "--initial-branch=main"])
    run_git(repo, ["config", "user.name", "Task Loop Test"])
    run_git(repo, ["config", "user.email", "task-loop@example.invalid"])
    (repo / "file.txt").write_text("initial\n", encoding="utf-8")
    run_git(repo, ["add", "file.txt"])
    run_git(repo, ["commit", "-m", "initial"])
    run_git(repo, ["remote", "add", "origin", str(origin)])
    run_git(repo, ["push", "-u", "origin", "main"])
    return repo, origin


def origin_head(origin: Path) -> str:
    return run_git(origin, ["rev-parse", "main"])


class GitLifecycleTests(unittest.TestCase):
    def test_preflight_passes_on_clean_main_matching_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, _origin = init_repo(Path(tmpdir))
            metadata = task_loop.git_preflight(repo)

        self.assertEqual(metadata["start_main_commit"], metadata["origin_main_commit"])

    def test_preflight_fails_when_not_on_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, _origin = init_repo(Path(tmpdir))
            run_git(repo, ["switch", "-c", "other"])

            with self.assertRaises(task_loop.GitPolicyError):
                task_loop.git_preflight(repo)

    def test_preflight_fails_when_main_is_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, _origin = init_repo(Path(tmpdir))
            (repo / "file.txt").write_text("dirty\n", encoding="utf-8")

            with self.assertRaises(task_loop.GitPolicyError):
                task_loop.git_preflight(repo)

    def test_preflight_fails_when_main_differs_from_origin_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, _origin = init_repo(Path(tmpdir))
            (repo / "local.txt").write_text("local\n", encoding="utf-8")
            run_git(repo, ["add", "local.txt"])
            run_git(repo, ["commit", "-m", "local main change"])

            with self.assertRaises(task_loop.GitPolicyError):
                task_loop.git_preflight(repo)

    def test_task_branch_starts_from_verified_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, _origin = init_repo(Path(tmpdir))
            preflight = task_loop.git_preflight(repo)
            branch = task_loop.task_branch_name({"task_id": "demo"})
            task_loop.create_task_branch(repo, branch, preflight["start_main_commit"])

            self.assertEqual(task_loop.current_branch(repo), branch)
            self.assertEqual(task_loop.git_head(repo), preflight["start_main_commit"])

    def test_accepted_commit_includes_only_diff_audited_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, _origin = init_repo(Path(tmpdir))
            preflight = task_loop.git_preflight(repo)
            branch = task_loop.task_branch_name({"task_id": "accepted"})
            task_loop.create_task_branch(repo, branch, preflight["start_main_commit"])
            (repo / "file.txt").write_text("accepted\n", encoding="utf-8")

            commit = task_loop.commit_accepted_changes(repo, repo, ["file.txt"], "accepted", 1)
            committed_files = run_git(repo, ["show", "--name-only", "--format=", commit]).splitlines()

            self.assertEqual(committed_files, ["file.txt"])
            self.assertEqual(task_loop.clean_status(repo), "")

    def test_unaccepted_changes_are_discarded_without_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, _origin = init_repo(Path(tmpdir))
            preflight = task_loop.git_preflight(repo)
            branch = task_loop.task_branch_name({"task_id": "rejected"})
            task_loop.create_task_branch(repo, branch, preflight["start_main_commit"])
            before = task_loop.git_head(repo)
            (repo / "file.txt").write_text("rejected\n", encoding="utf-8")

            task_loop.discard_unaccepted_task_changes(repo)

            self.assertEqual(task_loop.git_head(repo), before)
            self.assertEqual(task_loop.clean_status(repo), "")

    def test_fast_forward_main_succeeds_without_pushing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, origin = init_repo(Path(tmpdir))
            preflight = task_loop.git_preflight(repo)
            branch = task_loop.task_branch_name({"task_id": "fast-forward"})
            task_loop.create_task_branch(repo, branch, preflight["start_main_commit"])
            (repo / "file.txt").write_text("accepted\n", encoding="utf-8")
            accepted_commit = task_loop.commit_accepted_changes(repo, repo, ["file.txt"], "fast-forward", 1)
            origin_before = origin_head(origin)

            final_main = task_loop.fast_forward_main(repo, branch)

            self.assertEqual(task_loop.current_branch(repo), "main")
            self.assertEqual(final_main, accepted_commit)
            self.assertEqual(task_loop.verify_final_clean_main(repo), accepted_commit)
            self.assertEqual(origin_head(origin), origin_before)

    def test_fast_forward_failure_preserves_task_branch_and_clean_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, _origin = init_repo(Path(tmpdir))
            preflight = task_loop.git_preflight(repo)
            branch = task_loop.task_branch_name({"task_id": "drift"})
            task_loop.create_task_branch(repo, branch, preflight["start_main_commit"])
            (repo / "file.txt").write_text("accepted\n", encoding="utf-8")
            accepted_commit = task_loop.commit_accepted_changes(repo, repo, ["file.txt"], "drift", 1)
            run_git(repo, ["switch", "main"])
            (repo / "main.txt").write_text("local drift\n", encoding="utf-8")
            run_git(repo, ["add", "main.txt"])
            run_git(repo, ["commit", "-m", "local main drift"])

            with self.assertRaises(task_loop.GitPolicyError):
                task_loop.fast_forward_main(repo, branch)

            self.assertEqual(task_loop.current_branch(repo), "main")
            self.assertEqual(task_loop.clean_status(repo), "")
            self.assertEqual(run_git(repo, ["rev-parse", branch]), accepted_commit)

    def test_final_validation_failure_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, _origin = init_repo(Path(tmpdir))
            run_dir = repo / ".codex_task_loop" / "runs" / "test"
            run_dir.mkdir(parents=True)
            task = {
                "task_id": "validation-fails",
                "task_type": "test",
                "objective": "Prove final validation failure is reported.",
                "allowed_paths": ["file.txt"],
                "acceptance_criteria": ["validation fails"],
                "validation_commands": ["false"],
                "max_iterations": 1,
            }
            task_path = run_dir / "task.json"
            task_path.write_text(json.dumps(task), encoding="utf-8")

            evidence, final_dir = task_loop.run_final_validation(task_path, repo, 1, run_dir)

            self.assertFalse(evidence["outer_gate_passed"])
            self.assertTrue((final_dir / "evidence.json").exists())

    def test_schema_rejects_stale_git_checkpoint_field(self) -> None:
        task = {
            "task_id": "stale-checkpoint",
            "task_type": "docs",
            "objective": "Reject stale field.",
            "allowed_paths": ["README.md"],
            "acceptance_criteria": ["schema rejects stale field"],
            "validation_commands": [],
            "max_iterations": 1,
            "git_checkpoint": True,
        }
        schema = validate_task_packet.read_json(validate_task_packet.TASK_SCHEMA_PATH)
        errors = validate_task_packet.schema_errors(schema, task)

        self.assertTrue(any("git_checkpoint" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
