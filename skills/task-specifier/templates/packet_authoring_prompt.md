Goal: Convert the user objective into bounded Codex task-loop packet(s) that can be executed one packet at a time.

Context:
- Work in the current repository only unless the user explicitly names another workspace.
- Treat `skills/task-loop/schemas/task_packet.schema.json` as the canonical task packet contract.
- Treat `skills/task-loop/scripts/validate_task_packet.py` as the packet validator.
- A task must be small enough to fit in one explicit packet, constrained to known paths, with concrete deliverables, objective acceptance criteria, and validation evidence that can justify an accept decision without relying on trust, memory, or broad interpretation.

Constraints:
- Do not run the task loop.
- Do not modify implementation files for the target task.
- Do not invent paths, validation commands, fixtures, schemas, or deliverables.
- Do not create broad packets to avoid splitting.
- Do not rely on vague acceptance criteria such as "works correctly" or "is improved."
- Do not assume memory, trust, or broad reviewer interpretation can justify acceptance.

Operator steps:
1. Inspect the repository for real paths, existing tests, scripts, schemas, fixtures, generated artifacts, and documentation surfaces relevant to the objective.
2. Determine whether the objective fits one explicit packet. If it does not, split it into an ordered packet series.
3. For each packet, define `task_id`, `task_type`, `objective`, `allowed_paths`, `blocked_paths`, `deliverables`, `acceptance_criteria`, `validation_commands`, `artifact_checks` when file deliverables must exist or contain specific text, and `max_iterations`.
4. Validate each packet with:
   `python skills/task-loop/scripts/validate_task_packet.py --task <task_packet>.json [--workspace-root <dir>]`
5. Stop and report a split/escalation note when required paths, fixtures, runtime, validation commands, or acceptance evidence cannot be identified from the repository.

Done when the response contains exactly one of:
- One schema-valid task packet plus the validator command used.
- An ordered packet series with one schema-valid packet per step, the packet order, and the validator command used for each packet.
- A split/escalation note naming the missing fact, why it prevents a trustworthy accept decision, and the concrete input needed to continue.
