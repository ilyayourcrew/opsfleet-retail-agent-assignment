"""Tool registry - the extension point for new capabilities.

A capability is a plain Python function with type hints + a docstring (ADK turns it into a
function declaration for Gemini). To add one (e-mail a report, web trends, ...):

    @tool(group="reporting")
    def email_report(report_id: int, to: str, tool_context: ToolContext) -> dict: ...

then add the group to `enabled_groups` (config) - no change to the agent loop, plugins or CLI.
Every tool automatically inherits tracing, budgets, PII scanning of its output and error
containment from the ADK plugins.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

ToolFactory = Callable[..., Callable]


@dataclass
class ToolSpec:
    name: str
    group: str
    factory: ToolFactory  # (services) -> function
    description: str = ""


_REGISTRY: dict[str, ToolSpec] = {}


def register(name: str, group: str, description: str = "") -> Callable[[ToolFactory], ToolFactory]:
    def deco(factory: ToolFactory) -> ToolFactory:
        _REGISTRY[name] = ToolSpec(name, group, factory, description)
        return factory

    return deco


def build_tools(services, enabled_groups: set[str] | None = None) -> list[Callable]:
    from retail_agent.tools import builtin  # noqa: F401 - registers the built-in tools

    specs = [s for s in _REGISTRY.values() if enabled_groups is None or s.group in enabled_groups]
    return [s.factory(services) for s in specs]


def registered() -> list[ToolSpec]:
    from retail_agent.tools import builtin  # noqa: F401

    return list(_REGISTRY.values())
