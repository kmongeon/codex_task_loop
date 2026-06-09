Review the latest repository state and evidence for this bounded Codex task.

Task packet:
{task_json}

Latest evidence:
{evidence_json}

Return only valid JSON matching the decision schema:
{decision_schema_json}

Decision rules:
- decision=accept only when evidence satisfies all acceptance criteria, validation gates, artifact checks, and diff audits.
- decision=repair when validation failed or an artifact/check is missing.
- decision=continue when the task is valid but incomplete.
- decision=narrow when scope drift or unrelated edits occurred.
- decision=split when the task is too large and should become smaller task packets.
- decision=escalate when required information is missing.
- decision=reject when the result is unsupported or inconsistent with the task packet.
