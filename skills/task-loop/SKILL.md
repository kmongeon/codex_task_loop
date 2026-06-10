---
name: task-loop
description: Orchestrate bounded Codex tasks with per-iteration prompt composition, fresh execution threads, deterministic gates, and isolated review decisions.
---

Use this skill when a Codex task should be handled through a repeatable, bounded lifecycle loop.

This skill is self-contained. It owns the loop entrypoint, the task packet contract, and the execution prompt template:

- `scripts/task_loop.py`: loop entrypoint.
- `scripts/codex_session.py`: execution-only Codex SDK adapter; one fresh thread per iteration.
- `schemas/task_packet.schema.json`: task packet contract, including `git_checkpoint` (default true).
- `templates/execution_prompt.md`: fixed execution contract rendered into every composed prompt.

Run:

```bash
python skills/task-loop/scripts/task_loop.py --task <task_packet>.json [--workspace-root <dir>] [--model <model>] [--review-model <model>]
```

Loop per iteration:

1. Compose the prompt: fixed task contract + iteration counter + unresolved criteria + failure evidence + reviewer direction. The contract never mutates; dynamic state is rebuilt from the latest `evidence.json` and `decision.json`.
2. Run one execution turn on a fresh thread (no carried-over context).
3. Invoke the `eval-gate` skill as a subprocess to produce `evidence.json`.
4. Invoke the `evidence-review` skill as a subprocess to produce `decision.json`.
5. Dispatch: `accept` with the outer gate passed completes; `escalate`, `reject`, or `split` stops; otherwise checkpoint changed files to git and recompose.

Rules:

1. Read the task packet; identify objective, allowed paths, blocked paths, deliverables, and acceptance criteria.
2. Make only the smallest scoped change per iteration.
3. The external loop owns validation and acceptance; never self-declare completion.
4. Exit codes: 0 accepted, 1 max iterations reached, 2 stopped by reviewer decision.
