# Codex Task Loop

A Codex SDK harness for applying the loop approach to all Codex tasks. The
plugin is organized as three self-contained skills that communicate only
through JSON files (`task.json` -> `evidence.json` -> `decision.json`):

- `skills/task-loop`: orchestrator. Per-iteration prompt composition, fresh
  execution thread per iteration, git checkpointing, run bookkeeping.
- `skills/eval-gate`: deterministic verifier. Validation commands, artifact
  checks, allowed/blocked path diff audit, evidence assembly. No LLM calls.
- `skills/evidence-review`: independent reviewer. Ephemeral read-only Codex
  turn that returns a schema-validated decision.

Each skill owns its scripts, schemas, and templates and is independently
runnable. The orchestrator invokes the other two skills as subprocess CLIs;
there are no cross-skill Python imports.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

Run from the root of the git repository you want Codex to edit:

```bash
python skills/task-loop/scripts/task_loop.py --task examples/docs_task.json
```

For nested projects inside a larger git repository, pass the project root
explicitly:

```bash
python skills/task-loop/scripts/task_loop.py \
  --task examples/docs_task.json \
  --workspace-root /path/to/nested/project
```

Both execution and review turns default to `gpt-5.5`. The review model
follows the execution model unless overridden. Use different models for
execution and review:

```bash
python skills/task-loop/scripts/task_loop.py \
  --task examples/pytest_task.json \
  --model gpt-5.5 \
  --review-model gpt-5.5 \
  --review-effort high
```

## Task packet fields

Defined in `skills/task-loop/schemas/task_packet.schema.json`.

| Field | Required | Meaning |
| --- | --- | --- |
| `task_id` | yes | Run identifier; used in run directory and checkpoint commit messages. |
| `task_type` | yes | One of `feature`, `bugfix`, `refactor`, `test`, `docs`, `eval`, `schema`, `pipeline`, `cleanup`, `analysis`. |
| `objective` | yes | What the execution agent is asked to accomplish. |
| `allowed_paths` | yes | Path prefixes the agent may change; anything else fails the diff audit. |
| `acceptance_criteria` | yes | Definition of done; the reviewer tracks these as completed/unresolved. |
| `validation_commands` | yes (may be `[]`) | Shell commands that must all exit 0. |
| `max_iterations` | yes | Loop budget, 1-25. |
| `blocked_paths` | no | Paths that must not change even if under an allowed prefix. |
| `regression_commands` | no | Additional must-pass commands (run identically to validation). |
| `artifact_checks` | no | Deliverable checks: `exists`, `nonempty`, or `contains` (with `text`). |
| `deliverables` | no | Listed outputs, shown to the agent and reviewer. |
| `workspace_root` | no | Nested project root inside a larger git repository. |
| `git_checkpoint` | no (default true) | Commit audited changed files after each iteration. |
| `command_timeout_seconds` | no (default 120) | Per-command timeout; timeout counts as failure. |
| `human_review_required` | no | Flag carried in the packet for downstream policy. |

## How an iteration works

1. Compose the execution prompt: fixed task contract + iteration counter +
   unresolved acceptance criteria + failure evidence + reviewer direction.
   The contract never mutates; dynamic state is rebuilt every pass.
2. Run one execution turn on a fresh thread (clean context each iteration).
3. `eval-gate` runs validation commands, artifact checks, and the diff audit,
   writing schema-validated `evidence.json` plus a `workspace.diff` snapshot.
4. `evidence-review` judges the evidence on an isolated read-only thread and
   writes `decision.json`.
5. Dispatch on the decision; unless stopped, checkpoint the audited changed
   files to git and recompose.

## Output

Each run writes evidence to:

```text
.codex_task_loop/runs/<timestamp>_<task_id>/
  task.json
  iteration_NN/
    composed_prompt.md
    codex_execution.md
    command_NN.json
    evidence.json
    workspace.diff
    codex_review_raw.md
    decision.json
  final.json
```

The loop accepts only when:

- validation and regression commands pass,
- artifact checks pass,
- changed files stay within allowed paths and blocked paths are untouched,
- the isolated review returns `decision: "accept"`.

Exit codes: 0 accepted, 1 max iterations reached, 2 stopped by reviewer
decision (`escalate`, `reject`, or `split`).

## Git checkpointing

With `"git_checkpoint": true` in the task packet (the default), the loop
commits the audited changed files after each iteration:

```text
task-loop(<task_id>): iteration <N> <decision>
task-loop(<task_id>): accepted at iteration <N>
```

Only the files reported by the diff audit are staged, by explicit path. Set
`"git_checkpoint": false` to disable.

## Large tasks

Do not raise `max_iterations` to fit a large objective into one packet. Split
the work into small packets, each with its own scope and validation, and run
them sequentially. The reviewer enforces this: when a task is too large it
returns `decision: "split"` with proposed `new_task_packets` in
`decision.json`, and the run exits with code 2. Save those packets, refine
them, and run each one as its own loop. Git checkpoints make every packet a
durable, reviewable unit of progress.

## Task decisions

The reviewer must return one of:

```text
accept | continue | repair | narrow | escalate | reject | split
```

Repair is only one branch. This is a general Codex task lifecycle loop.
