from __future__ import annotations

from typing import Sequence

from ..addressing import EndpointAddress, parse_endpoint_address
from ..core_types import (
    AdapterDeliveryResult,
    AdapterDeliveryState,
    CoreMessage,
    DeliveryLease,
    EndpointDescriptor,
)
from ..types import RoutedMessage


class HerdrAdapter:
    name = "herdr"

    def __init__(self, node_id: str, registry, delivery) -> None:
        self.node_id = node_id
        self.registry = registry
        self.delivery = delivery

    async def refresh(self) -> Sequence[EndpointDescriptor]:
        return await self.list_endpoints()

    async def list_endpoints(self) -> Sequence[EndpointDescriptor]:
        await self.registry.refresh()
        return tuple(
            EndpointDescriptor(
                address=EndpointAddress(self.node_id, role),
                adapter=self.name,
                label=agent.name or role,
                status=agent.agent_status,
                capabilities=("message",),
            )
            for role, agent in sorted(self.registry.snapshot().items())
        )

    async def deliver(
        self,
        message: CoreMessage,
        lease: DeliveryLease,
    ) -> AdapterDeliveryResult:
        target = parse_endpoint_address(message.target_endpoint)
        if not isinstance(target, EndpointAddress) or target.node_id != self.node_id:
            return AdapterDeliveryResult(
                AdapterDeliveryState.FAILED,
                "target endpoint is not local to this adapter",
                0,
            )

        routed = RoutedMessage(
            message_id=message.message_id,
            source=message.source_endpoint.rsplit("/", 1)[-1],
            target=target.endpoint_id,
            resolved_target=target.endpoint_id,
            body=message.body,
            raw_text=message.raw_text,
            source_endpoint=message.source_endpoint,
            target_endpoint=message.target_endpoint,
            conversation_id=message.conversation_id,
            reply_to=message.reply_to,
            metadata=message.metadata,
        )
        if not lease.accept("accepted for Herdr delivery"):
            return AdapterDeliveryResult(
                AdapterDeliveryState.DELIVERY_UNKNOWN,
                "delivery lease acceptance failed",
                0,
            )

        result = await self.delivery.deliver(routed)
        state = AdapterDeliveryState.DELIVERED if result.delivered else self._failure_state(result.reason)
        return AdapterDeliveryResult(state, result.reason, result.attempts)

    @staticmethod
    def _failure_state(reason: str) -> AdapterDeliveryState:
        unknown_reasons = ("ambiguous", "residue not cleared", "submission state unknown")
        if any(value in reason.lower() for value in unknown_reasons):
            return AdapterDeliveryState.DELIVERY_UNKNOWN
        return AdapterDeliveryState.FAILED
