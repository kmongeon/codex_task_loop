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
  scripts/task_loop.py          # loop entrypoint
  scripts/codex_session.py      # execution-only SDK adapter, fresh thread per turn
  scripts/validate_task_packet.py # standalone task packet validator
  schemas/task_packet.schema.json
  templates/execution_prompt.md
  templates/ordered_packet_series_prompt.md

skills/eval-gate/
  SKILL.md
  scripts/eval_gate.py          # deterministic gate CLI, no LLM
  schemas/evidence.schema.json

skills/evidence-review/
  SKILL.md
  scripts/evidence_review.py    # isolated read-only review CLI
  schemas/decision.schema.json
  templates/review_prompt.md

tests/
  test_git_lifecycle.py          # temp-repo tests for Git policy helpers
```

Runtime output:

```text
.codex_task_loop/runs/<run_id>/
  task.json                       # task packet copy used for this run
  run_events.jsonl                # append-only lifecycle events
  RUN_SUMMARY.md                  # operator summary and artifact index
  final.json                      # final status; points to run_dir, run_events_file, run_summary_file
  iteration_XX/
    composed_prompt.md
    codex_execution.md
    evidence.json
    workspace.diff
    decision.json
  final_validation/
    evidence.json                 # only after accepted changes are fast-forwarded to main
```

Supporting files:

- `requirements.txt`
- `README.md`
- `AGENTS.md`
- `examples/docs_task.json`
- `examples/pytest_task.json`
- `examples/promptfoo_eval_task.json`

There are no root-level `scripts/`, `schemas/`, or `templates/` directories;
those surfaces moved into the skills that own them.
