from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Tuple

from .addressing import EndpointAddress


class CoreMessageState(str, Enum):
    QUEUED = "queued"
    WAITING_ENDPOINT = "waiting_endpoint"
    OFFERED = "offered"
    ACCEPTED = "accepted"
    DELIVERED = "delivered"
    FAILED = "failed"
    DELIVERY_UNKNOWN = "delivery_unknown"


@dataclass(frozen=True)
class EndpointDescriptor:
    address: EndpointAddress
    adapter: str
    label: str
    status: str
    capabilities: Tuple[str, ...]


@dataclass(frozen=True)
class CoreMessage:
    message_id: str
    source_endpoint: str
    target_endpoint: str
    body: str
    raw_text: str
    state: CoreMessageState
    conversation_id: str
    reply_to: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class AdapterDeliveryState(str, Enum):
    DEFERRED = "deferred"
    DELIVERED = "delivered"
    FAILED = "failed"
    DELIVERY_UNKNOWN = "delivery_unknown"


@dataclass
class DeliveryLease:
    message_id: str
    lease_id: str
    adapter: str
    accept_callback: Callable[[str], bool]
    accepted: bool = False
    expires_at: Optional[float] = None

    def accept(self, reason: str) -> bool:
        if self.accepted:
            return True
        self.accepted = self.accept_callback(reason)
        return self.accepted


@dataclass(frozen=True)
class AdapterDeliveryResult:
    state: AdapterDeliveryState
    reason: str
    attempts: int
