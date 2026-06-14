---
name: task-loop
description: Orchestrate manifest-defined Codex task series with per-packet prompt composition, fresh execution threads, deterministic gates, isolated review decisions, and durable series state.
---

Use this skill when a Codex task or task series should be handled through the
repeatable, bounded lifecycle loop. Every run uses a manifest, including
one-packet runs.

This skill is self-contained. It owns the loop entrypoint, the manifest
contract, the task packet contract, generated series state, and the execution
prompt template:

- `scripts/task_loop.py`: manifest-only loop entrypoint.
- `scripts/codex_session.py`: execution-only Codex SDK adapter; one fresh thread per iteration.
- `scripts/validate_task_packet.py`: diagnostic task packet validator.
- `schemas/task_series_manifest.schema.json`: manifest contract.
- `schemas/task_series_state.schema.json`: generated state contract.
- `schemas/task_packet.schema.json`: task packet contract.
- `templates/execution_prompt.md`: fixed execution contract rendered into every composed prompt.

Run:

```bash
.venv/bin/python skills/task-loop/scripts/task_loop.py --manifest <manifest>.json [--model <model>] [--review-model <model>]
```

Inspect CLI options without starting a task:

```bash
.venv/bin/python skills/task-loop/scripts/task_loop.py --help
```

A well-specified manifest contains one or more small explicit packets,
constrained to known paths, with concrete deliverables, objective acceptance
criteria, and validation evidence that can justify accept decisions without
relying on trust, memory, or broad interpretation.

Manifest rules:

1. Each manifest packet has `packet_id`, `task`, and `depends_on`.
2. `depends_on: null` means independent.
3. `depends_on: [...]` means the packet can run only after every referenced
   packet is `completed` with `outcome=accepted`.
4. Full schema, dependency, task-packet, workspace, and Git validation happens
   before execution starts and before any packet receives an outcome.

Loop per packet:

1. Compose the prompt: fixed task contract + iteration counter + unresolved criteria + failure evidence + reviewer direction. The contract never mutates; dynamic state is rebuilt from the latest `evidence.json` and `decision.json`.
2. Run one execution turn on a fresh thread.
3. Invoke the `eval-gate` skill as a subprocess to produce `evidence.json`.
4. Invoke the `evidence-review` skill as a subprocess to produce `decision.json`.
5. Map accepted or terminal review results into series state/outcome fields and update durable state.

Git lifecycle:

1. Running a manifest authorizes only this documented runner-owned Git
   lifecycle for that manifest.
2. The runner starts only from clean local `main` matching `origin/main`.
3. The runner creates or switches to the manifest `series_branch` from verified
   `main`. Prefer `codex/<slug>` series branches unless the manifest explicitly
   assigns a different branch convention.
4. Execution turns must not stage, commit, branch, checkout, merge, rebase,
   push, reset, clean, or otherwise manage Git. If Git state appears to require
   action, the execution turn reports it instead of acting.
5. The runner commits only accepted packet changes that passed the diff audit.
6. The runner commits generated series state to
   `codex_task_loop_series/<series_id>/state.json`.
7. The runner stages only diff-audited accepted packet paths and the generated
   series state file.
8. The runner pushes only the series branch.
9. The runner treats `main` only as the verified starting base. It never
   advances, pushes, rebases, merges into, resets, or rewrites `main`.
10. Failed or stopped packet work is discarded before evaluating the next
    runnable packet. Cleanup is safe only because the runner starts from a
    clean worktree and cleanup is limited to runner-produced task changes; run
    evidence remains under ignored `.codex_task_loop/`.
11. Durable state records branch and commit evidence. Packet artifacts linked
    from durable state record validation and worktree dirty-state evidence.

Run artifacts:

- Series state: `codex_task_loop_series/<series_id>/state.json`.
- Packet run root: `.codex_task_loop/runs/<run_id>/`.
- `task.json`: copied task packet used for the packet run.
- `run_events.jsonl`: append-only event stream for lifecycle, artifact, review,
  Git, and finish events.
- `RUN_SUMMARY.md`: operator summary with final status, Git lifecycle, changed
  files, and per-iteration artifact links.
- `final.json`: machine-readable packet final status.
- `iteration_XX/`: `composed_prompt.md`, `codex_execution.md`,
  `evidence.json`, `workspace.diff`, `decision.json`.

Rules:

1. Read the manifest and all referenced task packets before execution.
2. Make only the smallest scoped change per packet iteration.
3. The external loop owns validation and acceptance; never self-declare completion.
4. Exit codes: 0 all packets accepted, 1 at least one packet reached max iterations, 2 any other non-accepted series outcome.
