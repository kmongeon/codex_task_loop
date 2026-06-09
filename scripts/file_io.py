"""File, JSON, template, and text extraction helpers for the task loop."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def render_template(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", value)
    return rendered


def fenced_code_blocks(text: str) -> Iterator[str]:
    collecting = False
    buffer: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            marker = stripped[3:].strip().lower()
            if collecting:
                yield "\n".join(buffer)
                collecting = False
                buffer = []
                continue
            if marker in {"", "json"}:
                collecting = True
                buffer = []
                continue
        if collecting:
            buffer.append(line)


def require_json_object(obj: Any) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ValueError("Decoded JSON is not an object.")
    return obj


def extract_json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()

    for block in fenced_code_blocks(text):
        try:
            return require_json_object(json.loads(block))
        except (json.JSONDecodeError, ValueError):
            continue

    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[index:])
            return require_json_object(obj)
        except (json.JSONDecodeError, ValueError):
            continue

    raise ValueError("No JSON object found in Codex review output.")


def tail(text: str, limit: int = 4000) -> str:
    return text[-limit:]
