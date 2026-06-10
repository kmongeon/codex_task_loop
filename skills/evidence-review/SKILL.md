---
name: evidence-review
description: Review task-loop evidence in an isolated read-only Codex turn and return a machine-readable decision.
---

Use this skill for the independent review step of a task loop.

This skill is self-contained. The reviewer runs on its own ephemeral, read-only Codex thread whose only inputs are the task packet, the eval-gate evidence, and the decision schema. It never shares a thread with the execution agent.

- `scripts/evidence_review.py`: review entrypoint.
- `schemas/decision.schema.json`: decision contract; the review output is validated against it before writing.
- `templates/review_prompt.md`: review prompt template.

Run:

```bash
python skills/evidence-review/scripts/evidence_review.py \
  --task <task_packet>.json \
  --evidence <evidence>.json \
  --output <decision>.json \
  --workspace-root <dir> \
  [--model <model>] [--effort <effort>]
```

Outputs written next to `--output`: `decision.json` (schema-validated) and `codex_review_raw.md` (raw review text).

Rules:

- Return one decision: `accept`, `continue`, `repair`, `narrow`, `escalate`, `reject`, or `split`.
- Base the decision only on the task packet, command results, artifact checks, diff audit, and acceptance criteria.
- `next_prompt` is direction for the next composed execution prompt, not a replacement for the task contract.
- A response that fails decision-schema validation is recorded as a `repair` decision with the parse error as the reason.
