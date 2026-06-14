# Codex Task Loop

A Codex SDK harness for specifying, executing, validating, and reviewing
bounded Codex task series through a manifest-only runner.

## Repository Map

- Task specification: [`skills/task-specifier/SKILL.md`](skills/task-specifier/SKILL.md)
  and [`skills/task-specifier/templates/packet_authoring_prompt.md`](skills/task-specifier/templates/packet_authoring_prompt.md)
- Manifest execution: [`skills/task-loop/SKILL.md`](skills/task-loop/SKILL.md)
  and [`skills/task-loop/scripts/task_loop.py`](skills/task-loop/scripts/task_loop.py)
- Manifest contract: [`skills/task-loop/schemas/task_series_manifest.schema.json`](skills/task-loop/schemas/task_series_manifest.schema.json)
- Generated state contract: [`skills/task-loop/schemas/task_series_state.schema.json`](skills/task-loop/schemas/task_series_state.schema.json)
- Task packet contract: [`skills/task-loop/schemas/task_packet.schema.json`](skills/task-loop/schemas/task_packet.schema.json)
- Diagnostic packet validator: [`skills/task-loop/scripts/validate_task_packet.py`](skills/task-loop/scripts/validate_task_packet.py)
- Deterministic evidence gate: [`skills/eval-gate/SKILL.md`](skills/eval-gate/SKILL.md)
- Isolated evidence review: [`skills/evidence-review/SKILL.md`](skills/evidence-review/SKILL.md)
- Bundle inventory: [`MANIFEST.md`](MANIFEST.md)
- Agent rules: [`AGENTS.md`](AGENTS.md)

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
.venv/bin/pip install -r requirements.txt
```

## Basic Workflow

1. Specify broad work into one manifest with one or more bounded task packets.
   A one-task run still uses a manifest with one packet.
2. Run the manifest:

   ```bash
   .venv/bin/python skills/task-loop/scripts/task_loop.py --manifest examples/docs_manifest.json
   ```

3. Inspect generated series state:

   ```text
   codex_task_loop_series/<series_id>/state.json
   ```

4. Inspect packet run artifacts under `.codex_task_loop/runs/`.

Run artifact pointers:

- `.codex_task_loop/runs/<run_id>/task.json`: task packet copied into the run.
- `.codex_task_loop/runs/<run_id>/run_events.jsonl`: append-only lifecycle
  event stream.
- `.codex_task_loop/runs/<run_id>/RUN_SUMMARY.md`: operator-readable packet
  status, Git lifecycle, changed files, and per-iteration artifact links.
- `.codex_task_loop/runs/<run_id>/final.json`: machine-readable packet final
  status.
- `.codex_task_loop/runs/<run_id>/iteration_XX/`: `composed_prompt.md`,
  `codex_execution.md`, `evidence.json`, `workspace.diff`, `decision.json`.

See CLI surface:

```bash
.venv/bin/python skills/task-loop/scripts/task_loop.py --help
```

## What Git Operations Will This Perform?

The manifest runner owns the Git lifecycle for the series:

- Fetches `origin` during preflight.
- Requires the current branch to be `main`.
- Requires a clean worktree before the loop begins.
- Requires local `main` to match `origin/main` before the loop begins.
- Creates or switches to the manifest `series_branch` from verified `main`.
- Commits accepted, diff-audited packet changes to `series_branch`.
- Writes and commits `codex_task_loop_series/<series_id>/state.json`.
- Pushes only the series branch.
- Never advances `main`, pushes `main`, rebases `main`, or merges the series
  branch back to `main`.
- Cleans incomplete packet work before evaluating the next runnable packet.
  Ignored run artifacts under `.codex_task_loop/runs/` remain local artifacts
  referenced by durable series state.

The runner requires push access to `origin` for the manifest `series_branch`.

## Contracts

The canonical series manifest contract is
[`skills/task-loop/schemas/task_series_manifest.schema.json`](skills/task-loop/schemas/task_series_manifest.schema.json).
Every manifest packet has `packet_id`, `task`, and `depends_on`.
`depends_on: null` marks an independent packet. A dependency array means the
packet can run only after every referenced packet is `completed` with
`outcome=accepted`.

The generated progress state contract is
[`skills/task-loop/schemas/task_series_state.schema.json`](skills/task-loop/schemas/task_series_state.schema.json).
Progress state is tracked under `codex_task_loop_series/<series_id>/state.json`
and committed to the manifest `series_branch`.

The canonical task packet contract is
[`skills/task-loop/schemas/task_packet.schema.json`](skills/task-loop/schemas/task_packet.schema.json).
The task specification standard and split rules live in
[`skills/task-specifier/SKILL.md`](skills/task-specifier/SKILL.md).

Examples:

Manifest examples:

- [`examples/docs_manifest.json`](examples/docs_manifest.json)
- [`examples/pytest_manifest.json`](examples/pytest_manifest.json)
- [`examples/promptfoo_eval_manifest.json`](examples/promptfoo_eval_manifest.json)
- [`examples/dependent_series_manifest.json`](examples/dependent_series_manifest.json)

Task packet examples:

- [`examples/docs_task.json`](examples/docs_task.json)
- [`examples/pytest_task.json`](examples/pytest_task.json)
- [`examples/promptfoo_eval_task.json`](examples/promptfoo_eval_task.json)
- [`examples/dependent_docs_plan_task.json`](examples/dependent_docs_plan_task.json)
- [`examples/dependent_docs_usage_task.json`](examples/dependent_docs_usage_task.json)

## Series State Example

The excerpt below is illustrative of scheduler progress and packet artifacts.
Real timestamps, run IDs, and commit hashes differ.

```json
{
  "series_id": "dependent-docs-series",
  "series_branch": "codex/dependent-docs-series",
  "workspace_root": ".",
  "git_policy": "clean-main-series-branch-push",
  "started_at": "2026-06-14T12:00:00Z",
  "updated_at": "2026-06-14T12:05:00Z",
  "start_main_commit": "1111111111111111111111111111111111111111",
  "origin_main_commit": "1111111111111111111111111111111111111111",
  "last_state_commit": "1234567890abcdef1234567890abcdef12345678",
  "packets": [
    {
      "packet_id": "docs-plan",
      "task": "examples/dependent_docs_plan_task.json",
      "depends_on": [],
      "state": "completed",
      "outcome": "accepted",
      "dependency_status": {
        "accepted": [],
        "pending": [],
        "blocked": []
      },
      "run_dir": ".codex_task_loop/runs/20260614T120000Z_dependent-docs-series_docs-plan",
      "artifacts": {
        "final": ".codex_task_loop/runs/20260614T120000Z_dependent-docs-series_docs-plan/final.json",
        "run_events": ".codex_task_loop/runs/20260614T120000Z_dependent-docs-series_docs-plan/run_events.jsonl",
        "run_summary": ".codex_task_loop/runs/20260614T120000Z_dependent-docs-series_docs-plan/RUN_SUMMARY.md",
        "latest_evidence": ".codex_task_loop/runs/20260614T120000Z_dependent-docs-series_docs-plan/iteration_01/evidence.json",
        "latest_decision": ".codex_task_loop/runs/20260614T120000Z_dependent-docs-series_docs-plan/iteration_01/decision.json",
        "latest_diff": ".codex_task_loop/runs/20260614T120000Z_dependent-docs-series_docs-plan/iteration_01/workspace.diff"
      },
      "accepted_commit": "abcdef1234567890abcdef1234567890abcdef12",
      "state_commit": "1234567890abcdef1234567890abcdef12345678"
    },
    {
      "packet_id": "docs-usage",
      "task": "examples/dependent_docs_usage_task.json",
      "depends_on": ["docs-plan"],
      "state": "pending",
      "outcome": null,
      "dependency_status": {
        "accepted": ["docs-plan"],
        "pending": [],
        "blocked": []
      },
      "run_dir": null,
      "artifacts": {},
      "accepted_commit": null,
      "state_commit": null
    }
  ]
}
```

## Runtime Notes

- The runner accepts only a manifest. Single-packet work is represented as a
  one-packet manifest.
- Full manifest, dependency, task-packet, workspace, and Git preflight
  validation happens before the series branch is created and before any packet
  receives an outcome.
- The runner starts from clean local `main` matching `origin/main`, runs on the
  manifest `series_branch`, pushes that branch, and never advances `main`.
- Accepted packet changes and generated series progress state are committed to
  the series branch.
- Incomplete packet work is cleaned before the scheduler evaluates the next
  runnable packet.
- `task-loop` invokes `eval-gate` and `evidence-review` through JSON file
  handoff: `task.json` -> `evidence.json` -> `decision.json`.
- Evidence failures remain in packet artifacts. Public series state records
  scheduler state and task outcome separately.
