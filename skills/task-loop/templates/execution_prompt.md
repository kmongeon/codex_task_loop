You are executing a bounded Codex task in a local git repository.

Task packet:
{task_json}

Rules:
- Work only inside allowed_paths.
- Do not touch blocked_paths.
- Make the smallest correct change for the stated objective.
- Use the acceptance criteria as the definition of done.
- Do not declare final completion; the outer task loop owns completion.
- Do not stage, commit, branch, checkout, merge, rebase, push, reset, or otherwise manage Git; the outer task loop owns Git.
- Summarize changed files and expected validation after the work turn.
