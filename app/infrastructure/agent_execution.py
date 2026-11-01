# -*- coding: utf-8 -*-
"""Agent execution ownership carried across async tool calls.

This is observability-only metadata.  It must never influence routing,
permissions, tool arguments, or business state.  ContextVar gives each
concurrent dispatch its own value while preserving the parent task context.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
import uuid


@dataclass(frozen=True)
class AgentExecution:
    agent: str = "system"
    dispatch_id: str = ""
    parent_dispatch_id: str = ""

    def correlation(self) -> dict[str, str]:
        values = {
            "agent": self.agent,
            "dispatch_id": self.dispatch_id,
            "parent_dispatch_id": self.parent_dispatch_id,
        }
        return {key: value for key, value in values.items() if value}


_current: ContextVar[AgentExecution] = ContextVar(
    "globex_agent_execution",
    default=AgentExecution(),
)


def current_agent_execution() -> AgentExecution:
    return _current.get()


def set_agent_execution(
    agent: str,
    *,
    dispatch_id: str = "",
    parent_dispatch_id: str = "",
) -> Token:
    if not agent:
        raise ValueError("agent must not be empty")
    return _current.set(AgentExecution(agent, dispatch_id, parent_dispatch_id))


def reset_agent_execution(token: Token) -> None:
    _current.reset(token)


@contextmanager
def agent_execution_scope(
    agent: str,
    *,
    dispatch_id: str = "",
    parent_dispatch_id: str = "",
):
    """Set execution ownership and restore it on success, error or cancel."""
    token = set_agent_execution(
        agent,
        dispatch_id=dispatch_id,
        parent_dispatch_id=parent_dispatch_id,
    )
    try:
        yield current_agent_execution()
    finally:
        reset_agent_execution(token)


def new_dispatch_id() -> str:
    return "dispatch-" + uuid.uuid4().hex
