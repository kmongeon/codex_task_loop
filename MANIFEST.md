# Manifest

This bundle is organized as four self-contained skills. Each skill owns its
scripts, schemas, and templates; runtime skills communicate only through JSON
files.

```text
skills/task-specifier/
  SKILL.md
  templates/packet_authoring_prompt.md

skills/task-loop/
  SKILL.md
  scripts/task_loop.py            # manifest-only series runner
  scripts/codex_session.py        # execution-only SDK adapter, fresh thread per turn
  scripts/validate_task_packet.py # diagnostic task packet validator
  schemas/task_packet.schema.json
  schemas/task_series_manifest.schema.json
  schemas/task_series_state.schema.json
  templates/execution_prompt.md

skills/eval-gate/
  SKILL.md
  scripts/eval_gate.py            # deterministic gate CLI, no LLM
  schemas/evidence.schema.json

skills/evidence-review/
  SKILL.md
  scripts/evidence_review.py      # isolated read-only review CLI
  schemas/decision.schema.json
  templates/review_prompt.md

tests/
  test_git_lifecycle.py           # temp-repo tests for manifest and Git policy helpers
```

Tracked series state:

```text
codex_task_loop_series/<series_id>/
  state.json                      # generated durable series progress state
```

Runtime output:

```text
.codex_task_loop/runs/<run_id>/
  task.json                       # task packet copy used for this packet run
  run_events.jsonl                # append-only lifecycle events
  RUN_SUMMARY.md                  # operator summary and artifact index
  final.json                      # packet final status
  iteration_XX/
    composed_prompt.md
    codex_execution.md
    evidence.json
    workspace.diff
    decision.json
```

Supporting files:

- `requirements.txt`
- `README.md`
- `AGENTS.md`
- `examples/docs_manifest.json`
- `examples/docs_task.json`
- `examples/pytest_task.json`
- `examples/promptfoo_eval_task.json`

There are no root-level `scripts/`, `schemas/`, or `templates/` directories;
those surfaces live inside the skills that own them.
