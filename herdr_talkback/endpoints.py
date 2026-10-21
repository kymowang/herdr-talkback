from __future__ import annotations

from dataclasses import replace
from typing import Dict, Sequence, Set

from .addressing import EndpointAddress, UnqualifiedEndpoint, parse_endpoint_address
from .core_types import EndpointDescriptor


class EndpointNotFound(LookupError):
    pass


class AmbiguousEndpoint(LookupError):
    pass


class EndpointRegistry:
    def __init__(self) -> None:
        self._by_qualified: Dict[str, EndpointDescriptor] = {}
        self._by_alias: Dict[str, Set[str]] = {}

    def register(self, node_id: str, endpoints: Sequence[EndpointDescriptor]) -> None:
        self._replace_snapshot(node_id, endpoints, omitted_status="offline")

    def register_remote(
        self, node_id: str, endpoints: Sequence[EndpointDescriptor]
    ) -> None:
        self._replace_snapshot(node_id, endpoints, omitted_status="retired")

    def list_node(self, node_id: str) -> tuple[EndpointDescriptor, ...]:
        return tuple(
            descriptor
            for qualified, descriptor in sorted(self._by_qualified.items())
            if descriptor.address.node_id == node_id
        )

    def _replace_snapshot(
        self,
        node_id: str,
        endpoints: Sequence[EndpointDescriptor],
        *,
        omitted_status: str,
    ) -> None:
        for descriptor in endpoints:
            if descriptor.address.node_id != node_id:
                raise ValueError(
                    "descriptor node {0} does not match {1}".format(
                        descriptor.address.node_id, node_id
                    )
                )

        incoming = {descriptor.address.qualified: descriptor for descriptor in endpoints}
        retained: Dict[str, EndpointDescriptor] = {}
        for qualified, descriptor in self._by_qualified.items():
            if descriptor.address.node_id != node_id or descriptor.status == "retired":
                retained[qualified] = descriptor
            elif qualified not in incoming:
                retained[qualified] = replace(descriptor, status=omitted_status)

        for qualified, descriptor in incoming.items():
            if qualified not in retained:
                retained[qualified] = descriptor
        self._by_qualified = retained
        self._rebuild_aliases()

    def mark_node_offline(self, node_id: str) -> None:
        self._by_qualified = {
            qualified: (
                replace(descriptor, status="offline")
                if descriptor.address.node_id == node_id and descriptor.status != "retired"
                else descriptor
            )
            for qualified, descriptor in self._by_qualified.items()
        }
        self._rebuild_aliases()

    def resolve(self, value: str, sender_node_id: str) -> EndpointDescriptor:
        address = parse_endpoint_address(value)
        if isinstance(address, EndpointAddress):
            return self._resolve_qualified(address.qualified)

        return self._resolve_unqualified(address, sender_node_id)

    def _resolve_qualified(self, qualified: str) -> EndpointDescriptor:
        descriptor = self._by_qualified.get(qualified)
        if descriptor is None:
            raise EndpointNotFound("endpoint not found: {0}".format(qualified))
        return descriptor

    def _resolve_unqualified(
        self, address: UnqualifiedEndpoint, sender_node_id: str
    ) -> EndpointDescriptor:
        qualified_addresses = self._by_alias.get(address.endpoint_id, set())
        if not qualified_addresses:
            raise EndpointNotFound("endpoint not found: {0}".format(address.endpoint_id))

        if len(qualified_addresses) == 1:
            return self._by_qualified[next(iter(qualified_addresses))]

        local_qualified = EndpointAddress(sender_node_id, address.endpoint_id).qualified
        if local_qualified in qualified_addresses:
            return self._by_qualified[local_qualified]

        conflicts = ", ".join(sorted(qualified_addresses))
        raise AmbiguousEndpoint(
            "ambiguous endpoint {0}: {1}".format(address.endpoint_id, conflicts)
        )

    def _rebuild_aliases(self) -> None:
        aliases: Dict[str, Set[str]] = {}
        for qualified, descriptor in self._by_qualified.items():
            aliases.setdefault(descriptor.address.endpoint_id, set()).add(qualified)
        self._by_alias = aliases
