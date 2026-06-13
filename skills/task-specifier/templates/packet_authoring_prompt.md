Goal: Convert the user objective into a bounded Codex task-loop manifest and referenced task packet file(s).

Context:
- Work in the current repository only unless the user explicitly names another workspace.
- Treat `skills/task-loop/schemas/task_series_manifest.schema.json` as the canonical manifest contract.
- Treat `skills/task-loop/schemas/task_packet.schema.json` as the canonical task packet contract.
- A one-task objective still produces a manifest with one packet.
- A task packet must be small enough to fit in one explicit packet, constrained to known paths, with concrete deliverables, objective acceptance criteria, and validation evidence that can justify an accept decision without relying on trust, memory, or broad interpretation.

Constraints:
- Do not run the task loop.
- Do not modify implementation files for the target task.
- Do not invent paths, validation commands, fixtures, schemas, dependencies, or deliverables.
- Do not create broad packets to avoid splitting.
- Do not rely on vague acceptance criteria such as "works correctly" or "is improved."
- Do not assume memory, trust, or broad reviewer interpretation can justify acceptance.

Operator steps:
1. Inspect the repository for real paths, existing tests, scripts, schemas, fixtures, generated artifacts, and documentation surfaces relevant to the objective.
2. Determine whether the objective fits one explicit packet. If it does not, split it into a manifest packet series.
3. For each packet, define `task_id`, `task_type`, `objective`, `allowed_paths`, `blocked_paths`, `deliverables`, `acceptance_criteria`, `validation_commands`, `artifact_checks` when file deliverables must exist or contain specific text, and `max_iterations`.
4. Write a manifest with `series_id`, `series_branch`, `workspace_root`, and `packets`.
5. For each manifest packet, define `packet_id`, workspace-relative `task`, and explicit `depends_on`: `null` for independent packets or a non-empty list of packet IDs.
6. Stop and report a split/escalation note when required paths, fixtures, runtime, validation commands, or acceptance evidence cannot be identified from the repository.

Done when the response contains exactly one of:
- One manifest plus one referenced schema-valid task packet.
- One manifest plus an ordered dependency series of referenced schema-valid task packets.
- A split/escalation note naming the missing fact, why it prevents a trustworthy accept decision, and the concrete input needed to continue.
