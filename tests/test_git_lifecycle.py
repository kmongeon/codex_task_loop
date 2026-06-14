from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch


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


@contextmanager
def chdir(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def init_repo(tmp: Path) -> tuple[Path, Path]:
    origin = tmp / "origin.git"
    repo = tmp / "repo"
    repo.mkdir()
    run_git(tmp, ["init", "--bare", "--initial-branch=main", str(origin)])
    run_git(repo, ["init", "--initial-branch=main"])
    run_git(repo, ["config", "user.name", "Task Loop Test"])
    run_git(repo, ["config", "user.email", "task-loop@example.invalid"])
    (repo / "file.txt").write_text("initial\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".codex_task_loop/\n", encoding="utf-8")
    run_git(repo, ["add", ".gitignore", "file.txt"])
    run_git(repo, ["commit", "-m", "initial"])
    run_git(repo, ["remote", "add", "origin", str(origin)])
    run_git(repo, ["push", "-u", "origin", "main"])
    return repo, origin


def origin_head(origin: Path, ref: str = "main") -> str:
    return run_git(origin, ["rev-parse", ref])


def valid_task() -> dict[str, object]:
    return {
        "task_id": "change-file",
        "task_type": "test",
        "objective": "Change file.txt.",
        "allowed_paths": ["file.txt"],
        "acceptance_criteria": ["file.txt changed"],
        "validation_commands": [],
        "max_iterations": 1,
    }


def valid_manifest(packets: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "series_id": "series",
        "series_branch": "codex/series",
        "workspace_root": ".",
        "packets": packets
        or [
            {
                "packet_id": "first",
                "task": "tasks/first.json",
                "depends_on": None,
            }
        ],
    }


def write_valid_project(repo: Path, manifest: dict[str, object] | None = None) -> Path:
    write_json(repo / "tasks" / "first.json", valid_task())
    manifest_obj = manifest or valid_manifest()
    write_json(repo / "manifest.json", manifest_obj)
    return repo / "manifest.json"


def commit_and_push_main(repo: Path) -> str:
    run_git(repo, ["add", "manifest.json", "tasks"])
    run_git(repo, ["commit", "-m", "add manifest"])
    run_git(repo, ["push", "origin", "main"])
    return run_git(repo, ["rev-parse", "main"])


def accept_decision() -> dict[str, object]:
    return {
        "decision": "accept",
        "reason": "evidence passed",
        "next_prompt": "",
        "completed_criteria": ["file.txt changed"],
        "unresolved_criteria": [],
        "validation_required": [],
        "risks": [],
        "new_task_packets": [],
    }


def reject_decision() -> dict[str, object]:
    return {
        "decision": "reject",
        "reason": "unsupported",
        "next_prompt": "",
        "completed_criteria": [],
        "unresolved_criteria": ["file.txt changed"],
        "validation_required": [],
        "risks": [],
        "new_task_packets": [],
    }


class ManifestValidationTests(unittest.TestCase):
    def test_valid_single_packet_manifest_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, _origin = init_repo(Path(tmpdir))
            manifest_path = write_valid_project(repo)

            plan = task_loop.load_manifest_plan(repo, manifest_path)

        self.assertEqual(plan.manifest["series_id"], "series")
        self.assertEqual([packet.packet_id for packet in plan.packets], ["first"])

    def test_valid_multi_packet_dependency_chain_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, _origin = init_repo(Path(tmpdir))
            write_json(repo / "tasks" / "first.json", valid_task())
            write_json(repo / "tasks" / "second.json", {**valid_task(), "task_id": "second"})
            manifest_path = repo / "manifest.json"
            write_json(
                manifest_path,
                valid_manifest(
                    [
                        {"packet_id": "first", "task": "tasks/first.json", "depends_on": None},
                        {"packet_id": "second", "task": "tasks/second.json", "depends_on": ["first"]},
                    ]
                ),
            )

            plan = task_loop.load_manifest_plan(repo, manifest_path)

        self.assertEqual([packet.packet_id for packet in plan.packets], ["first", "second"])

    def test_duplicate_packet_id_fails_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, _origin = init_repo(Path(tmpdir))
            write_json(repo / "tasks" / "first.json", valid_task())
            manifest_path = repo / "manifest.json"
            write_json(
                manifest_path,
                valid_manifest(
                    [
                        {"packet_id": "first", "task": "tasks/first.json", "depends_on": None},
                        {"packet_id": "first", "task": "tasks/first.json", "depends_on": None},
                    ]
                ),
            )

            with self.assertRaisesRegex(SystemExit, "duplicates"):
                task_loop.load_manifest_plan(repo, manifest_path)

    def test_missing_dependency_reference_fails_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, _origin = init_repo(Path(tmpdir))
            manifest_path = write_valid_project(
                repo,
                valid_manifest(
                    [{"packet_id": "first", "task": "tasks/first.json", "depends_on": ["missing"]}]
                ),
            )

            with self.assertRaisesRegex(SystemExit, "unknown packet_id"):
                task_loop.load_manifest_plan(repo, manifest_path)

    def test_dependency_cycle_fails_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, _origin = init_repo(Path(tmpdir))
            write_json(repo / "tasks" / "first.json", valid_task())
            write_json(repo / "tasks" / "second.json", {**valid_task(), "task_id": "second"})
            manifest_path = repo / "manifest.json"
            write_json(
                manifest_path,
                valid_manifest(
                    [
                        {"packet_id": "first", "task": "tasks/first.json", "depends_on": ["second"]},
                        {"packet_id": "second", "task": "tasks/second.json", "depends_on": ["first"]},
                    ]
                ),
            )

            with self.assertRaisesRegex(SystemExit, "dependency cycle"):
                task_loop.load_manifest_plan(repo, manifest_path)

    def test_missing_task_file_fails_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, _origin = init_repo(Path(tmpdir))
            manifest_path = repo / "manifest.json"
            write_json(repo / "manifest.json", valid_manifest())

            with self.assertRaisesRegex(SystemExit, "Task file does not exist"):
                task_loop.load_manifest_plan(repo, manifest_path)

    def test_invalid_task_packet_fails_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, _origin = init_repo(Path(tmpdir))
            write_json(repo / "tasks" / "first.json", {"task_id": "first"})
            write_json(repo / "manifest.json", valid_manifest())

            with self.assertRaisesRegex(SystemExit, "schema"):
                task_loop.load_manifest_plan(repo, repo / "manifest.json")

    def test_missing_depends_on_fails_without_coercion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, _origin = init_repo(Path(tmpdir))
            write_json(repo / "tasks" / "first.json", valid_task())
            write_json(
                repo / "manifest.json",
                {
                    "series_id": "series",
                    "series_branch": "codex/series",
                    "workspace_root": ".",
                    "packets": [{"packet_id": "first", "task": "tasks/first.json"}],
                },
            )

            with self.assertRaisesRegex(SystemExit, "depends_on"):
                task_loop.load_manifest_plan(repo, repo / "manifest.json")

    def test_preloop_validation_failure_writes_no_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, _origin = init_repo(Path(tmpdir))
            write_json(repo / "manifest.json", {"series_id": "bad"})

            with chdir(repo), self.assertRaises(SystemExit):
                task_loop.run_manifest_loop(task_loop.parse_args(["--manifest", "manifest.json"]))

            self.assertFalse((repo / "codex_task_loop_series").exists())
            self.assertEqual(task_loop.current_branch(repo), "main")


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

    def test_series_branch_starts_from_verified_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, _origin = init_repo(Path(tmpdir))
            preflight = task_loop.git_preflight(repo)

            task_loop.prepare_series_branch(repo, "codex/series", preflight["start_main_commit"])

            self.assertEqual(task_loop.current_branch(repo), "codex/series")
            self.assertEqual(task_loop.git_head(repo), preflight["start_main_commit"])

    def test_accepted_commit_includes_only_diff_audited_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, _origin = init_repo(Path(tmpdir))
            preflight = task_loop.git_preflight(repo)
            task_loop.prepare_series_branch(repo, "codex/series", preflight["start_main_commit"])
            (repo / "file.txt").write_text("accepted\n", encoding="utf-8")

            commit = task_loop.commit_accepted_changes(repo, repo, ["file.txt"], "first", 1)
            committed_files = run_git(repo, ["show", "--name-only", "--format=", commit]).splitlines()

            self.assertEqual(committed_files, ["file.txt"])
            self.assertEqual(task_loop.clean_status(repo), "")

    def test_unaccepted_changes_are_discarded_without_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, _origin = init_repo(Path(tmpdir))
            preflight = task_loop.git_preflight(repo)
            task_loop.prepare_series_branch(repo, "codex/series", preflight["start_main_commit"])
            before = task_loop.git_head(repo)
            run_artifact = repo / ".codex_task_loop" / "runs" / "keep" / "artifact.txt"
            run_artifact.parent.mkdir(parents=True)
            run_artifact.write_text("evidence\n", encoding="utf-8")
            (repo / "file.txt").write_text("rejected\n", encoding="utf-8")

            status = task_loop.discard_unaccepted_task_changes(repo, repo)

            self.assertEqual(task_loop.git_head(repo), before)
            self.assertTrue(status["clean"])
            self.assertEqual(task_loop.clean_status(repo), "")
            self.assertTrue(run_artifact.exists())

    def test_manifest_run_accepts_single_packet_on_series_branch_without_advancing_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, origin = init_repo(Path(tmpdir))
            manifest_path = write_valid_project(repo)
            main_before = commit_and_push_main(repo)

            def fake_execution(*_args: object) -> str:
                (repo / "file.txt").write_text("accepted\n", encoding="utf-8")
                return "changed file.txt"

            with chdir(repo), patch("task_loop.run_execution_turn", fake_execution), patch(
                "task_loop.run_evidence_review",
                return_value=accept_decision(),
            ):
                code = task_loop.run_manifest_loop(
                    task_loop.parse_args(["--manifest", str(manifest_path), "--model", "test"])
                )

            state = json.loads((repo / "codex_task_loop_series" / "series" / "state.json").read_text())
            packet = state["packets"][0]
            self.assertEqual(code, 0)
            self.assertEqual(run_git(repo, ["rev-parse", "main"]), main_before)
            self.assertEqual(origin_head(origin, "main"), main_before)
            self.assertEqual(task_loop.current_branch(repo), "codex/series")
            self.assertEqual(origin_head(origin, "codex/series"), task_loop.git_head(repo))
            self.assertEqual(packet["state"], "completed")
            self.assertEqual(packet["outcome"], "accepted")
            self.assertTrue(packet["accepted_commit"])
            self.assertTrue(packet["state_commit"])
            self.assertTrue(packet["run_dir"])
            self.assertTrue(packet["artifacts"]["final"])
            final = json.loads((repo / packet["artifacts"]["final"]).read_text(encoding="utf-8"))
            self.assertEqual(final["series_branch"], "codex/series")
            self.assertEqual(final["worktree_status"]["label"], "after accepted packet commit")
            self.assertTrue(final["worktree_status"]["clean"])
            self.assertEqual(final["worktree_status"]["status"], [])

    def test_rejected_packet_cleans_work_and_skips_dependent_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, _origin = init_repo(Path(tmpdir))
            write_json(repo / "tasks" / "first.json", valid_task())
            write_json(repo / "tasks" / "second.json", {**valid_task(), "task_id": "second"})
            manifest_path = repo / "manifest.json"
            write_json(
                manifest_path,
                valid_manifest(
                    [
                        {"packet_id": "first", "task": "tasks/first.json", "depends_on": None},
                        {"packet_id": "second", "task": "tasks/second.json", "depends_on": ["first"]},
                    ]
                ),
            )
            commit_and_push_main(repo)

            def fake_execution(*_args: object) -> str:
                (repo / "file.txt").write_text("rejected\n", encoding="utf-8")
                return "changed file.txt"

            with chdir(repo), patch("task_loop.run_execution_turn", fake_execution), patch(
                "task_loop.run_evidence_review",
                return_value=reject_decision(),
            ):
                code = task_loop.run_manifest_loop(
                    task_loop.parse_args(["--manifest", str(manifest_path), "--model", "test"])
                )

            state = json.loads((repo / "codex_task_loop_series" / "series" / "state.json").read_text())
            packets = {packet["packet_id"]: packet for packet in state["packets"]}
            self.assertEqual(code, 2)
            self.assertEqual((repo / "file.txt").read_text(encoding="utf-8"), "initial\n")
            self.assertEqual(packets["first"]["state"], "completed")
            self.assertEqual(packets["first"]["outcome"], "rejected")
            self.assertEqual(packets["second"]["state"], "skipped")
            self.assertEqual(packets["second"]["outcome"], "dependency_not_completed")
            final = json.loads(
                (repo / packets["first"]["artifacts"]["final"]).read_text(encoding="utf-8")
            )
            self.assertFalse(final["cleanup_worktree_status"]["before"]["clean"])
            self.assertTrue(final["cleanup_worktree_status"]["after"]["clean"])
            self.assertTrue(final["worktree_status"]["clean"])


class SchedulerTests(unittest.TestCase):
    def test_runnable_packet_requires_completed_accepted_dependencies(self) -> None:
        state = {
            "packets": [
                {"packet_id": "first", "depends_on": [], "state": "pending", "outcome": None},
                {"packet_id": "second", "depends_on": ["first"], "state": "pending", "outcome": None},
            ]
        }

        self.assertEqual(task_loop.runnable_packet_ids(state), ["first"])
        state["packets"][0]["state"] = "completed"
        state["packets"][0]["outcome"] = "accepted"

        self.assertEqual(task_loop.runnable_packet_ids(state), ["second"])

    def test_dependency_skip_cascades(self) -> None:
        state = {
            "packets": [
                {"packet_id": "first", "depends_on": [], "state": "completed", "outcome": "rejected"},
                {"packet_id": "second", "depends_on": ["first"], "state": "pending", "outcome": None},
                {"packet_id": "third", "depends_on": ["second"], "state": "pending", "outcome": None},
            ]
        }

        changed = task_loop.mark_dependency_skips(state)

        self.assertEqual(changed, ["second", "third"])
        self.assertTrue(task_loop.series_is_terminal(state))
        self.assertEqual(state["packets"][1]["outcome"], "dependency_not_completed")
        self.assertEqual(state["packets"][2]["outcome"], "dependency_not_completed")


class ExampleContractTests(unittest.TestCase):
    def test_all_example_task_packets_validate(self) -> None:
        root = Path(__file__).resolve().parents[1]
        schema = validate_task_packet.read_json(validate_task_packet.TASK_SCHEMA_PATH)
        task_paths = sorted((root / "examples").glob("*_task.json"))

        self.assertGreaterEqual(len(task_paths), 5)
        for path in task_paths:
            task = validate_task_packet.read_json(path)
            errors = validate_task_packet.schema_errors(schema, task)
            if isinstance(task, dict) and not errors:
                errors.extend(validate_task_packet.validate_semantics(task))

            self.assertEqual(errors, [], path.name)

    def test_all_example_manifests_validate(self) -> None:
        root = Path(__file__).resolve().parents[1]
        manifest_paths = sorted((root / "examples").glob("*manifest.json"))

        self.assertGreaterEqual(len(manifest_paths), 4)
        for path in manifest_paths:
            plan = task_loop.load_manifest_plan(root, path)

            self.assertEqual(plan.manifest_path, path.resolve())
            self.assertGreaterEqual(len(plan.packets), 1)

    def test_dependent_series_example_expresses_dependency(self) -> None:
        root = Path(__file__).resolve().parents[1]
        plan = task_loop.load_manifest_plan(
            root,
            root / "examples" / "dependent_series_manifest.json",
        )
        by_id = {packet.packet_id: packet for packet in plan.packets}

        self.assertEqual(by_id["docs-usage"].depends_on, ["docs-plan"])


class CliAndDocsTests(unittest.TestCase):
    def test_task_loop_cli_requires_manifest_not_task(self) -> None:
        args = task_loop.parse_args(["--manifest", "manifest.json"])
        self.assertEqual(args.manifest, "manifest.json")
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            task_loop.parse_args(["--task", "task.json"])

    def test_docs_do_not_reference_removed_ordered_series_prompt(self) -> None:
        root = Path(__file__).resolve().parents[1]
        removed_prompt = "ordered" + "_packet" + "_series" + "_prompt.md"
        self.assertFalse((root / "skills" / "task-loop" / "templates" / removed_prompt).exists())
        checked = [
            root / "README.md",
            root / "MANIFEST.md",
            root / "skills" / "task-loop" / "SKILL.md",
            root / "skills" / "task-specifier" / "SKILL.md",
            root / "skills" / "task-specifier" / "templates" / "packet_authoring_prompt.md",
        ]
        for path in checked:
            self.assertNotIn(removed_prompt.removesuffix(".md"), path.read_text(encoding="utf-8"))

    def test_readme_explains_git_operations_and_state_example(self) -> None:
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        readme_text = " ".join(readme.split())
        expected_text = [
            "What Git Operations Will This Perform?",
            "Series State Example",
            "Pushes only the series branch",
            "never advances",
            "Running a manifest authorizes only the documented runner-owned Git lifecycle",
            "being on a non-main branch is not broad Git authorization",
            "Cleanup of failed or unaccepted work is safe only because",
            "Prefer `codex/<slug>`",
            "codex_task_loop_series/<series_id>/state.json",
            "examples/dependent_series_manifest.json",
        ]

        for text in expected_text:
            self.assertIn(text, readme_text)

        state_block = (
            readme.split("## Series State Example", 1)[1]
            .split("```json", 1)[1]
            .split("```", 1)[0]
            .strip()
        )
        state = json.loads(state_block)
        state_schema = validate_task_packet.read_json(
            root / "skills" / "task-loop" / "schemas" / "task_series_state.schema.json"
        )

        self.assertEqual(
            task_loop.schema_error_messages(state_schema, state, "README series state example"),
            [],
        )

    def test_policy_surfaces_document_runner_owned_git_boundaries(self) -> None:
        root = Path(__file__).resolve().parents[1]
        skill = (root / "skills" / "task-loop" / "SKILL.md").read_text(encoding="utf-8")
        skill_text = " ".join(skill.split())
        prompt = (
            root / "skills" / "task-loop" / "templates" / "execution_prompt.md"
        ).read_text(encoding="utf-8")
        manifest_schema = validate_task_packet.read_json(
            root / "skills" / "task-loop" / "schemas" / "task_series_manifest.schema.json"
        )

        self.assertIn(
            "Running a manifest authorizes only this documented runner-owned Git lifecycle",
            skill_text,
        )
        self.assertIn("Execution turns must not stage, commit, branch", skill_text)
        self.assertIn("It never advances, pushes, rebases, merges into, resets, or rewrites `main`", skill_text)
        self.assertIn("cleanup is limited to runner-produced task changes", skill_text)
        self.assertIn("Do not stage, commit, branch, checkout, merge, rebase, push, reset, clean", prompt)
        self.assertIn("report it in your summary instead of acting", prompt)
        self.assertIn(
            "Prefer codex/<slug>",
            manifest_schema["properties"]["series_branch"]["description"],
        )

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
