"""Codex SDK session adapter for task-loop execution and review turns."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from dataclasses import field
from typing import Any


JsonObject = dict[str, Any]


@dataclass(frozen=True)
class CodexLaunchOptions:
    codex_bin: str | None = None
    launch_args_override: tuple[str, ...] | None = None
    config_overrides: tuple[str, ...] = ()
    cwd: str | None = None
    env: dict[str, str] | None = None
    client_name: str | None = None
    client_title: str | None = None
    client_version: str | None = None
    experimental_api: bool | None = None


@dataclass(frozen=True)
class ThreadOptions:
    approval_mode: str = "auto_review"
    base_instructions: str | None = None
    config: JsonObject | None = None
    cwd: str | None = None
    developer_instructions: str | None = None
    ephemeral: bool | None = None
    model: str | None = None
    model_provider: str | None = None
    personality: str | None = None
    sandbox: str | None = "workspace-write"
    service_name: str | None = None
    service_tier: str | None = None


@dataclass(frozen=True)
class TurnOptions:
    approval_mode: str | None = None
    cwd: str | None = None
    effort: str | None = None
    model: str | None = None
    output_schema: JsonObject | None = None
    personality: str | None = None
    sandbox: str | None = None
    service_tier: str | None = None
    summary: str | None = None


def final_response_text(result: Any) -> str:
    return getattr(result, "final_response", str(result))


def compact_kwargs(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def resolve_approval_mode(value: str | None, approval_mode_type: Any) -> Any:
    if value is None:
        return None
    return approval_mode_type(value.replace("-", "_"))


def resolve_sandbox(value: str | None, sandbox_type: Any) -> Any:
    if value is None:
        return None
    normalized = value.replace("_", "-")
    return sandbox_type(normalized)


def build_codex_config(options: CodexLaunchOptions, config_type: Any) -> Any:
    kwargs = compact_kwargs(
        {
            "codex_bin": options.codex_bin,
            "launch_args_override": options.launch_args_override,
            "config_overrides": options.config_overrides or None,
            "cwd": options.cwd,
            "env": options.env,
            "client_name": options.client_name,
            "client_title": options.client_title,
            "client_version": options.client_version,
            "experimental_api": options.experimental_api,
        }
    )
    return config_type(**kwargs) if kwargs else None


def build_thread_start_kwargs(options: ThreadOptions, approval_mode_type: Any, sandbox_type: Any) -> dict[str, Any]:
    return compact_kwargs(
        {
            "approval_mode": resolve_approval_mode(options.approval_mode, approval_mode_type),
            "base_instructions": options.base_instructions,
            "config": options.config,
            "cwd": options.cwd,
            "developer_instructions": options.developer_instructions,
            "ephemeral": options.ephemeral,
            "model": options.model,
            "model_provider": options.model_provider,
            "personality": options.personality,
            "sandbox": resolve_sandbox(options.sandbox, sandbox_type),
            "service_name": options.service_name,
            "service_tier": options.service_tier,
        }
    )


def build_turn_run_kwargs(options: TurnOptions, approval_mode_type: Any, sandbox_type: Any) -> dict[str, Any]:
    return compact_kwargs(
        {
            "approval_mode": resolve_approval_mode(options.approval_mode, approval_mode_type),
            "cwd": options.cwd,
            "effort": options.effort,
            "model": options.model,
            "output_schema": options.output_schema,
            "personality": options.personality,
            "sandbox": resolve_sandbox(options.sandbox, sandbox_type),
            "service_tier": options.service_tier,
            "summary": options.summary,
        }
    )


@dataclass
class CodexSession:
    launch_options: CodexLaunchOptions
    thread_options: ThreadOptions
    execution_options: TurnOptions
    review_options: TurnOptions
    _exit_stack: ExitStack = field(init=False, default_factory=ExitStack)
    _approval_mode_type: Any = field(init=False)
    _sandbox_type: Any = field(init=False)
    _thread: Any = field(init=False)

    def __enter__(self) -> "CodexSession":
        from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox

        self._approval_mode_type = ApprovalMode
        self._sandbox_type = Sandbox
        codex_config = build_codex_config(self.launch_options, CodexConfig)
        codex = self._exit_stack.enter_context(Codex(config=codex_config))
        self._thread = codex.thread_start(
            **build_thread_start_kwargs(self.thread_options, ApprovalMode, Sandbox)
        )
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> Any:
        return self._exit_stack.__exit__(exc_type, exc_value, traceback)

    def run_execution(self, prompt: str) -> str:
        result = self._thread.run(
            prompt,
            **build_turn_run_kwargs(self.execution_options, self._approval_mode_type, self._sandbox_type),
        )
        return final_response_text(result)

    def run_review(self, prompt: str) -> str:
        result = self._thread.run(
            prompt,
            **build_turn_run_kwargs(self.review_options, self._approval_mode_type, self._sandbox_type),
        )
        return final_response_text(result)
