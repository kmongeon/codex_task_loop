---
name: eval-gate
description: Treat tests, schema checks, lint, build checks, and Promptfoo evals as authoritative validation gates.
---

Use this skill when task completion depends on validation commands or eval results.

Rules:
- Passing validation is required for acceptance unless the task packet explicitly has no validation command.
- Failing validation requires repair or escalation.
- Regression failures prevent acceptance.
