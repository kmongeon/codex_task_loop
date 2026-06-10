---
name: eval-gate
description: Deterministic validation gate. Treat tests, schema checks, lint, build checks, artifact checks, and diff audits as authoritative pass/fail evidence.
---

Use this skill when task completion depends on validation commands, artifact checks, or scope audits.

This skill is self-contained and makes no LLM calls:

- `scripts/eval_gate.py`: gate entrypoint.
- `schemas/evidence.schema.json`: evidence contract; output is self-validated before writing.

Run:

```bash
python skills/eval-gate/scripts/eval_gate.py \
  --task <task_packet>.json \
  --workspace-root <dir> \
  --iteration <N> \
  --iteration-dir <dir>
```

Outputs written to the iteration directory:

- `evidence.json`: schema-validated gate result (`outer_gate_passed`, command results, artifact checks, diff audit).
- `command_NN.json`: full stdout/stderr per validation command.
- `workspace.diff`: snapshot of working-tree changes plus untracked files for the audit trail.

Exit code 0 when the outer gate passed; 1 otherwise.

Rules:

- Passing validation is required for acceptance unless the task packet has no validation command.
- Failing validation requires repair or escalation.
- Regression failures prevent acceptance.
- Changes outside `allowed_paths` or touching `blocked_paths` fail the diff audit.
