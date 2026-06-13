# Codex Task Loop

A Codex SDK harness for specifying, executing, validating, and reviewing
bounded Codex tasks.

## Repository Map

- Task specification: [`skills/task-specifier/SKILL.md`](skills/task-specifier/SKILL.md)
  and [`skills/task-specifier/templates/packet_authoring_prompt.md`](skills/task-specifier/templates/packet_authoring_prompt.md)
- Single-packet execution: [`skills/task-loop/SKILL.md`](skills/task-loop/SKILL.md)
  and [`skills/task-loop/scripts/task_loop.py`](skills/task-loop/scripts/task_loop.py)
- Packet validation: [`skills/task-loop/scripts/validate_task_packet.py`](skills/task-loop/scripts/validate_task_packet.py)
  against [`skills/task-loop/schemas/task_packet.schema.json`](skills/task-loop/schemas/task_packet.schema.json)
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

1. Specify broad work into one bounded task packet or an ordered packet series
   with [`task-specifier`](skills/task-specifier/SKILL.md).
2. Validate each packet before execution:

   ```bash
   .venv/bin/python skills/task-loop/scripts/validate_task_packet.py --task examples/docs_task.json
   ```

3. Run one validated packet:

   ```bash
   .venv/bin/python skills/task-loop/scripts/task_loop.py --task examples/docs_task.json
   ```

4. For nested projects inside a larger git repository, pass the workspace root:

   ```bash
   .venv/bin/python skills/task-loop/scripts/task_loop.py \
     --task examples/docs_task.json \
     --workspace-root /path/to/nested/project
   ```

5. Inspect run artifacts under `.codex_task_loop/runs/`.

Run artifact pointers:

- `.codex_task_loop/runs/<run_id>/task.json`: task packet copied into the run.
- `.codex_task_loop/runs/<run_id>/run_events.jsonl`: append-only lifecycle
  event stream.
- `.codex_task_loop/runs/<run_id>/RUN_SUMMARY.md`: operator-readable status,
  Git lifecycle, changed files, and per-iteration artifact links.
- `.codex_task_loop/runs/<run_id>/final.json`: machine-readable final status.
  Key pointer fields: `run_dir`, `run_events_file`, `run_summary_file`.
- `.codex_task_loop/runs/<run_id>/iteration_XX/`: `composed_prompt.md`,
  `codex_execution.md`, `evidence.json`, `workspace.diff`, `decision.json`.
- `.codex_task_loop/runs/<run_id>/final_validation/evidence.json`: written
  only after an accepted commit is fast-forwarded to `main`.

See CLI surface:

```bash
.venv/bin/python skills/task-loop/scripts/validate_task_packet.py --help
.venv/bin/python skills/task-loop/scripts/task_loop.py --help
```

## Task Contract

The canonical packet contract is
[`skills/task-loop/schemas/task_packet.schema.json`](skills/task-loop/schemas/task_packet.schema.json).
The task specification standard and split rules live in
[`skills/task-specifier/SKILL.md`](skills/task-specifier/SKILL.md).

Examples:

- [`examples/docs_task.json`](examples/docs_task.json)
- [`examples/pytest_task.json`](examples/pytest_task.json)
- [`examples/promptfoo_eval_task.json`](examples/promptfoo_eval_task.json)

## Runtime Notes

- The loop executes one task packet per run.
- `task-specifier` handles task decomposition before execution.
- Ordered packet series are supervised through repeated single-packet runs with
  [`skills/task-loop/templates/ordered_packet_series_prompt.md`](skills/task-loop/templates/ordered_packet_series_prompt.md).
- `task-loop` manages the local Git lifecycle for accepted runs; see
  [`skills/task-loop/SKILL.md`](skills/task-loop/SKILL.md).
- `task-loop` invokes `eval-gate` and `evidence-review` through JSON file
  handoff: `task.json` -> `evidence.json` -> `decision.json`.
- The reviewer decision vocabulary and acceptance rules are defined in
  [`skills/evidence-review/SKILL.md`](skills/evidence-review/SKILL.md).
