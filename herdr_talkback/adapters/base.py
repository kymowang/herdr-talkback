from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence

from ..core_types import AdapterDeliveryResult, CoreMessage, DeliveryLease, EndpointDescriptor


class AgentAdapter(Protocol):
    name: str

    async def list_endpoints(self) -> Sequence[EndpointDescriptor]:
        ...

    async def deliver(self, message: CoreMessage, lease: DeliveryLease) -> AdapterDeliveryResult:
        ...

    async def inspect(self, message_id: str) -> Mapping[str, Any]:
        ...

    async def interrupt(self, turn_id: str) -> bool:
        ...

    async def reconcile(self) -> Sequence[AdapterDeliveryResult]:
        ...
