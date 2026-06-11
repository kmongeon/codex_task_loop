---
name: task-specifier
description: Specify bounded Codex task-loop packets or ordered packet series from user objectives, with concrete paths, deliverables, acceptance criteria, and validation evidence.
---

Use this skill when a user objective needs to be converted into one or more task-loop packets before execution.

This skill is self-contained. It owns task specification guidance and delegates packet validation to the task-loop skill:

- `templates/packet_authoring_prompt.md`: operator prompt for producing one valid task packet or an ordered packet series.
- `../task-loop/schemas/task_packet.schema.json`: canonical task packet contract.
- `../task-loop/scripts/validate_task_packet.py`: standalone task packet validator.

Run the validator after writing each packet:

```bash
python skills/task-loop/scripts/validate_task_packet.py --task <task_packet>.json [--workspace-root <dir>]
```

For a broad objective, use `templates/packet_authoring_prompt.md` to specify packets before running `task-loop`.

A well-specified task is small enough to fit in one explicit packet,
constrained to known paths, with concrete deliverables, objective acceptance
criteria, and validation evidence that can justify an accept decision without
relying on trust, memory, or broad interpretation.

Specification workflow:

1. Inspect the repository before writing packets. Identify real paths, scripts, schemas, fixtures, and validation commands.
2. Decide whether the objective fits one packet. If not, split it into an ordered packet series.
3. For each packet, define objective, allowed paths, blocked paths, deliverables, acceptance criteria, validation commands, artifact checks, and iteration budget.
4. Validate each packet against the canonical task packet schema before execution.
5. Report any missing path, fixture, runtime, or validation command as a blocker instead of guessing.

Rules:

1. Do not run `task-loop`; this skill specifies packets only.
2. Do not edit target implementation files while specifying the task.
3. Do not invent validation commands, paths, schemas, fixtures, or deliverables.
4. Do not write broad packets to avoid splitting.
5. Do not use vague acceptance criteria such as "works correctly" or "is improved."
6. Output either one valid packet, an ordered packet series, or a split/escalation note with the concrete missing input.
