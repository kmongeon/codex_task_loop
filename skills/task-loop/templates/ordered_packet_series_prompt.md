Goal: Run an ordered series of task packets through the existing Codex task-loop, one packet at a time.

Context:
- Work in the current repository only.
- Treat the task-loop as a single-packet executor unless repository inspection proves otherwise.
- Use the repository's existing task-loop entrypoint, task packet schema, validation commands, and run artifact layout.
- The intended approach is prompt-driven sequencing: inspect the packet series, validate one packet, run it through the existing task-loop, inspect its evidence, then move to the next packet only when the evidence supports continuing.

Constraints:
- Do not add a multi-packet runner, queue system, packet-series schema, ledger schema, or new controller infrastructure unless the user explicitly asks for implementation.
- Do not replace the existing single-packet loop.
- Do not assume local absolute paths, project-specific packet names, or domain-specific artifact names.
- Do not silently rewrite stale, invalid, rejected, split, or escalated packets.
- Do not hide failed validation, reviewer stop decisions, hard blockers, or dirty worktree state.
- Preserve the separation between task packet contract, single-packet execution, deterministic evidence, reviewer decision, and operator-level sequencing.

Operator steps:
1. Inspect the repository state and identify the task-loop entrypoint, packet validator, packet schema, and run artifact layout.
2. Identify the ordered packet list from the user-provided plan, index, or packet directory.
3. For the next packet, inspect the packet objective, allowed paths, blocked paths, acceptance criteria, validation commands, artifact checks, and `git_checkpoint` setting.
4. Validate the packet against the task packet schema before running it.
5. Run the existing single-packet task-loop for that packet.
6. Inspect the packet run's `final.json`, latest `evidence.json`, and reviewer decision.
7. Continue to the next packet only when the packet is accepted and the evidence supports the acceptance criteria.
8. Stop on invalid packet schema, failed validation that cannot be repaired in scope, `reject`, `split`, `escalate`, contradictory constraints, missing required fixtures, unavailable required runtime, or any hard blocker.

Done when:
- The response reports whether the current task-loop supports packet series directly or only through repeated single-packet runs.
- The response reports the packet order used, the command used for packet validation, the command used for packet execution, and the evidence inspected after each packet.
- The response reports each packet run directory, `final.json` path, latest `evidence.json` path, final decision, validation result, and stop/continue decision.
- Any blocker is reported with the packet, evidence path if available, attempted command, blocker, and concrete unlock condition.
