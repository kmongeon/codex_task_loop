---
name: task-loop
description: Orchestrate bounded Codex tasks with per-iteration prompt composition, fresh execution threads, deterministic gates, and isolated review decisions.
---

Use this skill when a Codex task should be handled through a repeatable, bounded lifecycle loop.

This skill is self-contained. It owns the loop entrypoint, the task packet contract, and the execution prompt template:

- `scripts/task_loop.py`: loop entrypoint.
- `scripts/codex_session.py`: execution-only Codex SDK adapter; one fresh thread per iteration.
- `scripts/validate_task_packet.py`: standalone task packet validator.
- `schemas/task_packet.schema.json`: task packet contract.
- `templates/execution_prompt.md`: fixed execution contract rendered into every composed prompt.
- `templates/ordered_packet_series_prompt.md`: operator prompt for supervising an ordered series of task packets through repeated single-packet runs.

Run:

```bash
python skills/task-loop/scripts/validate_task_packet.py --task <task_packet>.json [--workspace-root <dir>]
python skills/task-loop/scripts/task_loop.py --task <task_packet>.json [--workspace-root <dir>] [--model <model>] [--review-model <model>]
```
For an ordered packet series, use `templates/ordered_packet_series_prompt.md`
as an operator prompt.

A well-specified task is small enough to fit in one explicit packet,
constrained to known paths, with concrete deliverables, objective acceptance
criteria, and validation evidence that can justify an accept decision without
relying on trust, memory, or broad interpretation.

Loop per iteration:

1. Compose the prompt: fixed task contract + iteration counter + unresolved criteria + failure evidence + reviewer direction. The contract never mutates; dynamic state is rebuilt from the latest `evidence.json` and `decision.json`.
2. Run one execution turn on a fresh thread (no carried-over context).
3. Invoke the `eval-gate` skill as a subprocess to produce `evidence.json`.
4. Invoke the `evidence-review` skill as a subprocess to produce `decision.json`.
5. Dispatch: `accept` with the outer gate and diff audit passed creates one accepted task commit and fast-forwards `main`; `escalate`, `reject`, or `split` stops; otherwise recompose without creating Git commits.

Git lifecycle:

1. The runner starts only from a clean local `main` that matches `origin/main`.
2. The runner creates `codex/<task_id>` from verified `main` and runs all task work on that branch.
3. Execution turns must not stage, commit, branch, checkout, merge, rebase, push, reset, or otherwise manage Git.
4. The runner commits only accepted changes that passed the diff audit.
5. The runner never commits `continue`, `repair`, `narrow`, `reject`, `split`, `escalate`, or max-iteration states.
6. After an accepted commit, the runner switches to `main`, verifies that `main` still matches `origin/main`, fast-forwards `main` to the task branch, reruns final validation, and verifies clean `main`.
7. The runner never pushes, rebases, creates merge commits, resolves conflicts, or deletes branches.
8. Failed or stopped runs discard unaccepted task-branch changes before returning to clean `main`; run evidence remains under ignored `.codex_task_loop/`.

Rules:

1. Read the task packet; identify objective, allowed paths, blocked paths, deliverables, and acceptance criteria.
2. Make only the smallest scoped change per iteration.
3. The external loop owns validation and acceptance; never self-declare completion.
4. Exit codes: 0 accepted, 1 max iterations reached, 2 stopped by reviewer decision.
