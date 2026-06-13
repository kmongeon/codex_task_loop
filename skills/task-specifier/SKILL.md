---
name: task-specifier
description: Specify bounded Codex task-loop manifests and task packets from user objectives, with concrete paths, deliverables, dependencies, acceptance criteria, and validation evidence.
---

Use this skill when a user objective needs to be converted into a task-loop
manifest before execution. A one-task objective still produces a manifest with
one packet.

This skill is self-contained. It owns task specification guidance and delegates
execution-time validation to the task-loop skill:

- `templates/packet_authoring_prompt.md`: operator prompt for producing a valid manifest and referenced task packet files.
- `../task-loop/schemas/task_series_manifest.schema.json`: canonical manifest contract.
- `../task-loop/schemas/task_packet.schema.json`: canonical task packet contract.
- `../task-loop/scripts/task_loop.py`: manifest-only runner and preflight validator.

Run the manifest after writing it:

```bash
python skills/task-loop/scripts/task_loop.py --manifest <manifest>.json
```

A well-specified task is small enough to fit in one explicit packet,
constrained to known paths, with concrete deliverables, objective acceptance
criteria, and validation evidence that can justify an accept decision without
relying on trust, memory, or broad interpretation.

Specification workflow:

1. Inspect the repository before writing manifests or packets. Identify real paths, scripts, schemas, fixtures, and validation commands.
2. Decide whether the objective fits one packet. If not, split it into a manifest packet series with explicit dependencies.
3. For each packet, define objective, allowed paths, blocked paths, deliverables, acceptance criteria, validation commands, artifact checks, and iteration budget.
4. Write a manifest with `series_id`, `series_branch`, `workspace_root`, and one `packets` entry per task packet.
5. Set every manifest packet `depends_on` explicitly: `null` for independent packets or a non-empty dependency list.
6. Report any missing path, fixture, runtime, or validation command as a blocker instead of guessing.

Rules:

1. Do not run `task-loop`; this skill specifies manifests and packets only.
2. Do not edit target implementation files while specifying the task.
3. Do not invent validation commands, paths, schemas, fixtures, dependencies, or deliverables.
4. Do not write broad packets to avoid splitting.
5. Do not use vague acceptance criteria such as "works correctly" or "is improved."
6. Output either a valid manifest with referenced packet files, or a split/escalation note with the concrete missing input.
