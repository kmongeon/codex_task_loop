# Codex Task Loop

A clean, single-entrypoint Codex SDK workflow for applying the loop approach to
all Codex tasks, not only repair tasks.

The entrypoint is `scripts/codex_task_loop.py`. It delegates SDK-specific setup
to `scripts/codex_session.py`, so the CLI can stay focused on task-loop
orchestration.

Supporting scripts are named by responsibility:

- `scripts/codex_session.py`: Codex SDK launch, thread, and turn configuration.
- `scripts/evidence.py`: validation command execution, artifact checks, and
  evidence assembly.
- `scripts/file_io.py`: JSON, text, template, and review-output extraction.
- `scripts/git_scope.py`: Git root lookup, changed-file discovery, and
  allowed/blocked path auditing.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

Run from the root of the git repository you want Codex to edit:

```bash
python scripts/codex_task_loop.py --task examples/docs_task.json
```

For nested projects inside a larger git repository, pass the project root
explicitly. Validation commands, artifact checks, run logs, and allowed/blocked
path audits are then evaluated relative to that workspace:

```bash
python scripts/codex_task_loop.py \
  --task examples/docs_task.json \
  --workspace-root /path/to/nested/project
```

Use another model if desired:

```bash
python scripts/codex_task_loop.py --task examples/pytest_task.json --model gpt-5.4
```

Specify Codex SDK launch, thread, and turn settings with explicit flags:

```bash
python scripts/codex_task_loop.py \
  --task examples/pytest_task.json \
  --codex-bin /path/to/codex \
  --codex-config-override model_reasoning_effort=high \
  --model gpt-5.4 \
  --model-provider openai \
  --approval-mode auto_review \
  --thread-sandbox workspace-write \
  --execution-sandbox workspace-write \
  --review-sandbox read-only
```

## Output

Each run writes evidence to:

```text
.codex_task_loop/runs/<timestamp>_<task_id>/
```

The loop stops only when:

- validation commands pass,
- artifact checks pass,
- changed files under the workspace root stay within allowed paths,
- blocked paths are untouched,
- the read-only Codex review returns `decision: "accept"`.

## Task decisions

The reviewer must return one of:

```text
accept | continue | repair | narrow | escalate | reject | split
```

Repair is only one branch. This is a general Codex task lifecycle loop.
