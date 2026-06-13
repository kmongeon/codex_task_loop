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

- [`examples/docs_manifest.json`](examples/docs_manifest.json)
- [`examples/docs_task.json`](examples/docs_task.json)
- [`examples/pytest_task.json`](examples/pytest_task.json)
- [`examples/promptfoo_eval_task.json`](examples/promptfoo_eval_task.json)

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
