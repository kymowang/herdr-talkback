from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional


class MessageState(str, Enum):
    QUEUED = "queued"
    CHECKING_INPUT = "checking_input"
    BLOCKED_BY_INPUT = "blocked_by_input"
    SENDING = "sending"
    VERIFYING = "verifying"
    DELIVERED = "delivered"
    RETRYING = "retrying"
    FAILED = "failed"


@dataclass(frozen=True)
class RoutedMessage:
    message_id: str
    source: str
    target: str
    resolved_target: str
    body: str
    raw_text: str
    source_endpoint: str = ""
    target_endpoint: str = ""
    conversation_id: str = ""
    reply_to: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentInfo:
    name: Optional[str]
    agent: Optional[str]
    agent_status: str
    pane_id: str
    workspace_id: str
    tab_id: str
    cwd: Optional[str] = None
