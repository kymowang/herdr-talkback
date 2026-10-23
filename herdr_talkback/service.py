from __future__ import annotations

import asyncio

from .adapters.herdr import HerdrAdapter
from .core_dispatcher import CoreDispatcher
from .daemon import DaemonInboxServer, DaemonMessageRouter
from .delivery import DeliveryService
from .endpoints import EndpointNotFound, EndpointRegistry
from .herdr_client import HerdrClient
from .monitor import Monitor
from .notifier import HerdrNotifier
from .roles import RoleRegistry
from .state import TalkbackState


LIFECYCLE_EVENTS = {"pane.created", "pane.closed", "pane.moved", "pane.agent_detected", "pane.updated"}


class TalkbackService:
    def __init__(
        self,
        client,
        registry,
        monitor,
        reconnect_delay_seconds: float = 2.0,
        inbox=None,
        endpoint_registry=None,
        adapter=None,
        dispatcher=None,
        local_node_id=None,
        registry_refresh_seconds: float = 5.0,
    ) -> None:
        self.client = client
        self.registry = registry
        self.monitor = monitor
        self.reconnect_delay_seconds = reconnect_delay_seconds
        self.inbox = inbox
        self.endpoint_registry = endpoint_registry
        self.adapter = adapter
        self.dispatcher = dispatcher
        self.local_node_id = local_node_id
        if registry_refresh_seconds <= 0:
            raise ValueError("registry refresh interval must be positive")
        self.registry_refresh_seconds = registry_refresh_seconds
        self._refresh_lock = asyncio.Lock()

    async def run_once(self) -> None:
        await self._run_subscription(stop_after_one=True)

    async def run_until_disconnect(self) -> None:
        await self._run_subscription(stop_after_one=False)

    async def _run_subscription(self, stop_after_one: bool) -> None:
        await self._refresh_registries()
        if not stop_after_one:
            for info in self.registry.snapshot().values():
                await self.monitor.handle_status_event({
                    "event": "pane.agent_status_changed",
                    "data": {"pane_id": info.pane_id, "agent_status": getattr(info, "agent_status", "unknown")},
                })
        async for event in self.client.subscribe(_subscriptions(self.registry)):
            if event.get("event") == "pane.agent_status_changed":
                await self.monitor.handle_status_event(event)
            elif event.get("event") in LIFECYCLE_EVENTS:
                await self._refresh_registries()
                break
            if stop_after_one:
                break

    async def _refresh_registries(self) -> None:
        async with self._refresh_lock:
            await self._refresh_registries_locked()

    async def _refresh_registries_locked(self) -> None:
        if self.adapter is None:
            await self.registry.refresh()
            return

        descriptors = tuple(await self.adapter.refresh())
        newly_online = []
        for descriptor in descriptors:
            try:
                previous = self.endpoint_registry.resolve(
                    descriptor.address.qualified,
                    self.local_node_id,
                )
            except EndpointNotFound:
                previous = None
            if descriptor.status not in {"offline", "retired"} and (
                previous is None or previous.status == "offline"
            ):
                newly_online.append(descriptor.address.qualified)

        self.endpoint_registry.register(self.local_node_id, descriptors)
        for endpoint in newly_online:
            descriptor = self.endpoint_registry.resolve(endpoint, self.local_node_id)
            if descriptor.status not in {"offline", "retired"}:
                self.dispatcher.wake(endpoint)

    async def run_forever(self) -> None:
        tasks = [self._run_monitor_forever(), self._refresh_periodically()]
        if self.inbox is not None:
            tasks.append(self.inbox.serve_forever())
        await asyncio.gather(*tasks)

    async def _refresh_periodically(self) -> None:
        # Some Herdr versions do not emit pane.updated when a label changes.
        # Refresh locally so a waiting delivery can find a newly named pane.
        while True:
            await asyncio.sleep(self.registry_refresh_seconds)
            try:
                await self._refresh_registries()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # Retry after a temporary Herdr disconnect.

    async def _run_monitor_forever(self) -> None:
        while True:
            try:
                await self.run_until_disconnect()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(self.reconnect_delay_seconds)


async def run_service(config, dry_run: bool = False) -> int:
    client = HerdrClient(config.socket_path)
    role_registry = RoleRegistry(client, config.roles, config.aliases)
    network_enabled = getattr(getattr(config, 'network', None), 'enabled', False)
    if network_enabled:
        try:
            from .adapters.peer import PeerAdapter
            from .peer_http import PeerServer
            from .peer_state import PeerStore
        except ImportError as exc:
            raise RuntimeError('network support requires: pip install herdr-talkback[network]') from exc
        config.network.server_context()
    if dry_run:
        await role_registry.refresh()
        await client.check_subscription(_subscriptions(role_registry))
        return 0

    state = TalkbackState(config.state_path, local_node_id=config.core_node_id)
    state.init()
    notifier = HerdrNotifier(
        client,
        enabled=config.notify_enabled,
        position=config.notify_position,
        sound=config.notify_sound,
    )
    delivery = DeliveryService(
        client,
        role_registry,
        state,
        max_attempts=config.delivery_max_attempts,
        retry_interval_seconds=config.delivery_retry_interval_seconds,
        verify_timeout_ms=getattr(config, "delivery_verify_timeout_ms", 0),
        notifier=notifier,
        manage_message_state=False,
    )
    adapter = HerdrAdapter(config.core_node_id, role_registry, delivery)
    endpoint_registry = EndpointRegistry()
    endpoint_registry.register(config.core_node_id, await adapter.refresh())
    adapters = {adapter.name: adapter}
    peer_adapter = None
    peer_server = None
    if network_enabled:
        from .addressing import EndpointAddress, parse_endpoint_address
        peer_store = PeerStore(state)
        peer_adapter = PeerAdapter(config, peer_store, endpoint_registry)
        adapters[peer_adapter.name] = peer_adapter
        for row in state.list_active_messages():
            target = parse_endpoint_address(row['target_endpoint'])
            if isinstance(target, EndpointAddress) and target.node_id in config.peers:
                peer_adapter.ensure_endpoint(target)
    dispatcher = CoreDispatcher(state, endpoint_registry, adapters)
    if network_enabled:
        peer_server = PeerServer(config, peer_store, dispatcher)
    daemon_router = DaemonMessageRouter(
        config,
        role_registry,
        dispatcher,
        state=state,
        notifier=notifier,
        peer_adapter=peer_adapter,
    )
    inbox = DaemonInboxServer(config.daemon_socket_path, daemon_router.handle)
    monitor = Monitor(
        client,
        role_registry,
        delivery,
        state,
        cooldown_seconds=config.monitor_continue_cooldown_seconds,
        unknown_threshold_seconds=config.monitor_unknown_threshold_seconds,
        max_per_window=config.monitor_max_continues_per_30m,
        rules=getattr(config, "recovery_rules", None),
        notifier=notifier,
    )
    service = TalkbackService(
        client,
        role_registry,
        monitor,
        inbox=inbox,
        endpoint_registry=endpoint_registry,
        adapter=adapter,
        dispatcher=dispatcher,
        local_node_id=config.core_node_id,
    )

    try:
        if peer_server is not None:
            async with peer_server:
                await dispatcher.recover()
                await service.run_forever()
        else:
            await dispatcher.recover()
            await service.run_forever()
    finally:
        await dispatcher.close()
    return 0


def _subscriptions(registry):
    subscriptions = [
        {"type": "pane.created"},
        {"type": "pane.closed"},
        {"type": "pane.moved"},
        {"type": "pane.agent_detected"},
        {"type": "pane.updated"},
    ]
    subscriptions.extend(
        {"type": "pane.agent_status_changed", "pane_id": info.pane_id}
        for info in registry.snapshot().values()
    )
    return subscriptions
