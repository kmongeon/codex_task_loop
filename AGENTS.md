# Codex Task Loop Instructions

This repository uses a bounded task lifecycle for all Codex work.

Rules:
- Work only inside the paths listed in the task packet.
- Treat validation commands as authoritative.
- Do not declare completion unless evidence supports every acceptance criterion.
- Keep changes minimal and task-scoped.
- If scope is too broad, return a split decision rather than expanding the task.
- Record unresolved ambiguity instead of guessing.
