import asyncio

from herdr_talkback.transport import start_local_server
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import herdr_talkback.service as service_module
from herdr_talkback.addressing import EndpointAddress
from herdr_talkback.core_types import CoreMessage, CoreMessageState, EndpointDescriptor
from herdr_talkback.endpoints import EndpointRegistry
from herdr_talkback.service import TalkbackService
from herdr_talkback.state import TalkbackState


class FakeClient:
    def __init__(self):
        self.subscriptions = None

    async def subscribe(self, subscriptions):
        self.subscriptions = subscriptions
        yield {"event": "pane.agent_status_changed", "data": {"pane_id": "p-runner", "agent_status": "blocked"}}


class FakeRegistry:
    def __init__(self):
        self.refresh_count = 0

    async def refresh(self):
        self.refresh_count += 1
        self.refreshed = True

    def snapshot(self):
        return {"runner": type("Info", (), {"pane_id": "p-runner"})()}


class FakeMonitor:
    def __init__(self):
        self.events = []

    async def handle_status_event(self, event):
        self.events.append(event)


class LifecycleClient(FakeClient):
    def __init__(self, event_name):
        super().__init__()
        self.event_name = event_name

    async def subscribe(self, subscriptions):
        self.subscriptions = subscriptions
        yield {"event": self.event_name, "data": {"pane_id": "p-runner"}}


class SnapshotAdapter:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)

    async def refresh(self):
        return tuple(self.snapshots.pop(0))


class WakeRecordingDispatcher:
    def __init__(self):
        self.wake_calls = []

    def wake(self, endpoint):
        self.wake_calls.append(endpoint)


def _endpoint(name, status="online"):
    return EndpointDescriptor(
        address=EndpointAddress("linux-main", name),
        adapter="herdr",
        label=name,
        status=status,
        capabilities=("message",),
    )


def test_service_subscribes_to_agent_status_events_once():
    asyncio.run(_service_subscribes_to_agent_status_events_once())


async def _service_subscribes_to_agent_status_events_once():
    client = FakeClient()
    registry = FakeRegistry()
    monitor = FakeMonitor()
    service = TalkbackService(client, registry, monitor)

    await service.run_once()

    assert client.subscriptions == [
        {"type": "pane.created"},
        {"type": "pane.closed"},
        {"type": "pane.moved"},
        {"type": "pane.agent_detected"},
        {"type": "pane.updated"},
        {"type": "pane.agent_status_changed", "pane_id": "p-runner"},
    ]
    assert monitor.events == [
        {"event": "pane.agent_status_changed", "data": {"pane_id": "p-runner", "agent_status": "blocked"}}
    ]


def test_service_refreshes_registry_on_lifecycle_event():
    asyncio.run(_service_refreshes_registry_on_lifecycle_event())


async def _service_refreshes_registry_on_lifecycle_event():
    class LifecycleClient(FakeClient):
        async def subscribe(self, subscriptions):
            self.subscriptions = subscriptions
            yield {"event": "pane.closed", "data": {"pane_id": "p-runner"}}

    client = LifecycleClient()
    registry = FakeRegistry()
    monitor = FakeMonitor()
    service = TalkbackService(client, registry, monitor)

    await service.run_once()

    assert {"type": "pane.closed"} in client.subscriptions
    assert registry.refresh_count == 2
    assert monitor.events == []


@pytest.mark.parametrize('event_name', ['pane.created', 'pane.updated'])
def test_service_registers_added_pane_and_wakes_its_waiting_lane(event_name):
    asyncio.run(_service_registers_added_pane_and_wakes_its_waiting_lane(event_name))


async def _service_registers_added_pane_and_wakes_its_waiting_lane(event_name):
    endpoint_registry = EndpointRegistry()
    endpoint_registry.register("linux-main", [_endpoint("lead")])
    adapter = SnapshotAdapter(
        [
            [_endpoint("lead")],
            [_endpoint("lead"), _endpoint("runner")],
        ]
    )
    dispatcher = WakeRecordingDispatcher()
    service = TalkbackService(
        LifecycleClient(event_name),
        FakeRegistry(),
        FakeMonitor(),
        endpoint_registry=endpoint_registry,
        adapter=adapter,
        dispatcher=dispatcher,
        local_node_id="linux-main",
    )

    await service.run_once()

    assert endpoint_registry.resolve("linux-main/runner", "linux-main").status == "online"
    assert dispatcher.wake_calls == ["linux-main/runner"]


def test_service_marks_removed_pane_offline_without_waking_or_retiring_it():
    asyncio.run(_service_marks_removed_pane_offline_without_waking_or_retiring_it())


async def _service_marks_removed_pane_offline_without_waking_or_retiring_it():
    endpoint_registry = EndpointRegistry()
    endpoint_registry.register("linux-main", [_endpoint("lead"), _endpoint("runner")])
    adapter = SnapshotAdapter(
        [
            [_endpoint("lead"), _endpoint("runner")],
            [_endpoint("lead")],
        ]
    )
    dispatcher = WakeRecordingDispatcher()
    service = TalkbackService(
        LifecycleClient("pane.closed"),
        FakeRegistry(),
        FakeMonitor(),
        endpoint_registry=endpoint_registry,
        adapter=adapter,
        dispatcher=dispatcher,
        local_node_id="linux-main",
    )

    await service.run_once()

    assert endpoint_registry.resolve("linux-main/runner", "linux-main").status == "offline"
    assert dispatcher.wake_calls == []


def test_service_consumes_events_until_disconnect():
    asyncio.run(_service_consumes_events_until_disconnect())


async def _service_consumes_events_until_disconnect():
    class MultiEventClient(FakeClient):
        async def subscribe(self, subscriptions):
            self.subscriptions = subscriptions
            yield {"event": "pane.agent_status_changed", "data": {"pane_id": "p-runner", "agent_status": "blocked"}}
            yield {"event": "pane.agent_status_changed", "data": {"pane_id": "p-runner", "agent_status": "idle"}}

    client = MultiEventClient()
    registry = FakeRegistry()
    monitor = FakeMonitor()
    service = TalkbackService(client, registry, monitor)

    await service.run_until_disconnect()

    assert monitor.events == [
        {"event": "pane.agent_status_changed", "data": {"pane_id": "p-runner", "agent_status": "unknown"}},
        {"event": "pane.agent_status_changed", "data": {"pane_id": "p-runner", "agent_status": "blocked"}},
        {"event": "pane.agent_status_changed", "data": {"pane_id": "p-runner", "agent_status": "idle"}},
    ]


def test_service_sleeps_after_clean_disconnect_before_retry(monkeypatch):
    asyncio.run(_service_sleeps_after_clean_disconnect_before_retry(monkeypatch))


async def _service_sleeps_after_clean_disconnect_before_retry(monkeypatch):
    sleep_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)
        raise asyncio.CancelledError

    class DisconnectClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.subscribe_count = 0

        async def subscribe(self, subscriptions):
            self.subscribe_count += 1
            if self.subscribe_count > 1:
                raise asyncio.CancelledError
            if False:
                yield {}

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    client = DisconnectClient()
    registry = FakeRegistry()
    monitor = FakeMonitor()
    service = TalkbackService(client, registry, monitor, reconnect_delay_seconds=0.25)

    try:
        await service.run_forever()
    except asyncio.CancelledError:
        pass

    assert sleep_calls == [0.25]
    assert registry.refresh_count == 1
    assert client.subscribe_count == 1


def test_service_run_forever_starts_daemon_inbox(monkeypatch):
    asyncio.run(_service_run_forever_starts_daemon_inbox(monkeypatch))


async def _service_run_forever_starts_daemon_inbox(monkeypatch):
    class CancellingClient(FakeClient):
        async def subscribe(self, subscriptions):
            raise asyncio.CancelledError
            if False:
                yield {}

    class FakeInbox:
        def __init__(self):
            self.started = False

        async def serve_forever(self):
            self.started = True
            raise asyncio.CancelledError

    inbox = FakeInbox()
    service = TalkbackService(CancellingClient(), FakeRegistry(), FakeMonitor(), inbox=inbox)

    try:
        await service.run_forever()
    except asyncio.CancelledError:
        pass

    assert inbox.started is True


def test_service_resubscribes_after_lifecycle_refreshes_registry():
    asyncio.run(_service_resubscribes_after_lifecycle_refreshes_registry())


async def _service_resubscribes_after_lifecycle_refreshes_registry():
    class GrowingRegistry(FakeRegistry):
        def snapshot(self):
            panes = {"runner": type("Info", (), {"pane_id": "p-runner"})()}
            if self.refresh_count >= 2:
                panes["new"] = type("Info", (), {"pane_id": "p-new"})()
            return panes

    class LifecycleClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.subscriptions = []
            self.continued_after_lifecycle = False

        async def subscribe(self, subscriptions):
            self.subscriptions.append(list(subscriptions))
            if len(self.subscriptions) == 1:
                yield {"event": "pane.created", "data": {"pane_id": "p-new"}}
                self.continued_after_lifecycle = True
                yield {
                    "event": "pane.agent_status_changed",
                    "data": {"pane_id": "p-runner", "agent_status": "idle"},
                }
            else:
                if False:
                    yield {}

    client = LifecycleClient()
    registry = GrowingRegistry()
    monitor = FakeMonitor()
    service = TalkbackService(client, registry, monitor)

    await service.run_until_disconnect()
    await service.run_until_disconnect()

    assert client.continued_after_lifecycle is False
    assert {"type": "pane.agent_status_changed", "pane_id": "p-new"} in client.subscriptions[1]


def _service_config(tmp_path):
    return SimpleNamespace(
        core_node_id="linux-main",
        socket_path=tmp_path / "herdr.sock",
        state_path=tmp_path / "state.db",
        daemon_socket_path=tmp_path / "herdr-talkback.sock",
        roles=("lead",),
        aliases={},
        delivery_max_attempts=1,
        delivery_retry_interval_seconds=0,
        notify_enabled=False,
        notify_position="bottom-right",
        notify_sound="none",
        monitor_continue_cooldown_seconds=120,
        monitor_unknown_threshold_seconds=60,
        monitor_max_continues_per_30m=3,
    )


def _install_dry_run_fakes(monkeypatch):
    class DryRunClient:
        def __init__(self, socket_path):
            self.socket_path = socket_path
            self.subscriptions = None

        async def check_subscription(self, subscriptions):
            self.subscriptions = subscriptions

    class DryRunRoleRegistry:
        def __init__(self, client, roles, aliases):
            self.client = client

        async def refresh(self):
            pass

        def snapshot(self):
            return {}

    clients = []

    def client_factory(socket_path):
        client = DryRunClient(socket_path)
        clients.append(client)
        return client

    monkeypatch.setattr(service_module, "HerdrClient", client_factory)
    monkeypatch.setattr(service_module, "RoleRegistry", DryRunRoleRegistry)
    return clients


def test_run_service_dry_run_does_not_create_state_path(monkeypatch, tmp_path):
    asyncio.run(_run_service_dry_run_does_not_create_state_path(monkeypatch, tmp_path))


async def _run_service_dry_run_does_not_create_state_path(monkeypatch, tmp_path):
    cfg = _service_config(tmp_path)
    cfg.state_path = tmp_path / "missing-state" / "state.db"
    clients = _install_dry_run_fakes(monkeypatch)

    assert await service_module.run_service(cfg, dry_run=True) == 0

    assert not cfg.state_path.parent.exists()
    assert clients[0].subscriptions == [
        {"type": "pane.created"},
        {"type": "pane.closed"},
        {"type": "pane.moved"},
        {"type": "pane.agent_detected"},
        {"type": "pane.updated"},
    ]


def test_run_service_dry_run_does_not_migrate_existing_schema(monkeypatch, tmp_path):
    asyncio.run(_run_service_dry_run_does_not_migrate_existing_schema(monkeypatch, tmp_path))


async def _run_service_dry_run_does_not_migrate_existing_schema(monkeypatch, tmp_path):
    cfg = _service_config(tmp_path)
    TalkbackState(cfg.state_path).init()
    with sqlite3.connect(cfg.state_path) as db:
        db.execute("update schema_meta set value = '3' where key = 'version'")
    original_bytes = cfg.state_path.read_bytes()
    _install_dry_run_fakes(monkeypatch)

    assert await service_module.run_service(cfg, dry_run=True) == 0

    assert cfg.state_path.read_bytes() == original_bytes
    with sqlite3.connect(cfg.state_path) as db:
        assert db.execute("select value from schema_meta where key = 'version'").fetchone() == ("3",)


def _install_minimal_service_fakes(monkeypatch, events):
    class FakeState:
        def __init__(self, path, local_node_id):
            pass

        def init(self):
            pass

    class FakeClient:
        def __init__(self, socket_path):
            pass

    class FakeRoleRegistry:
        def __init__(self, client, roles, aliases):
            pass

    class FakeNotifier:
        def __init__(self, client, enabled, position, sound):
            pass

    class FakeDelivery:
        def __init__(self, *args, **kwargs):
            pass

    class FakeHerdrAdapter:
        name = "herdr"

        def __init__(self, node_id, registry, delivery):
            pass

        async def refresh(self):
            return ()

    class FakeEndpointRegistry:
        def register(self, node_id, endpoints):
            pass

    class FakeDispatcher:
        def __init__(self, state, registry, adapters):
            self.adapters = adapters

        async def recover(self):
            events.append("dispatcher.recover")

        async def close(self):
            events.append("dispatcher.close")

    class FakeRouter:
        def __init__(self, *args, **kwargs):
            pass

        async def handle(self, payload):
            pass

    class FakeInbox:
        def __init__(self, socket_path, handler):
            pass

    class FakeMonitor:
        def __init__(self, *args, **kwargs):
            pass

    class FakeService:
        def __init__(self, *args, **kwargs):
            pass

        async def run_forever(self):
            events.append("service.run_forever")

    replacements = {
        "TalkbackState": FakeState,
        "HerdrClient": FakeClient,
        "RoleRegistry": FakeRoleRegistry,
        "HerdrNotifier": FakeNotifier,
        "DeliveryService": FakeDelivery,
        "HerdrAdapter": FakeHerdrAdapter,
        "EndpointRegistry": FakeEndpointRegistry,
        "CoreDispatcher": FakeDispatcher,
        "DaemonMessageRouter": FakeRouter,
        "DaemonInboxServer": FakeInbox,
        "Monitor": FakeMonitor,
        "TalkbackService": FakeService,
    }
    for name, replacement in replacements.items():
        monkeypatch.setattr(service_module, name, replacement, raising=False)


def test_run_service_dry_run_is_read_only_with_queued_message_and_recoverable_event(
    monkeypatch, tmp_path
):
    asyncio.run(
        _run_service_dry_run_is_read_only_with_queued_message_and_recoverable_event(
            monkeypatch, tmp_path
        )
    )


async def _run_service_dry_run_is_read_only_with_queued_message_and_recoverable_event(
    monkeypatch, tmp_path
):
    state_path = tmp_path / "state.db"
    state = TalkbackState(state_path)
    state.init()
    state.enqueue_message(
        CoreMessage(
            message_id="queued",
            source_endpoint="linux-main/lead",
            target_endpoint="linux-main/runner",
            body="do not send",
            raw_text="[lead→runner] do not send",
            state=CoreMessageState.QUEUED,
            conversation_id="queued",
        ),
        target_pane_id="p-runner",
    )
    socket_path = tmp_path / "herdr.sock"
    methods = []

    async def handle(reader, writer):
        line = await reader.readline()
        request = json.loads(line.decode("utf-8"))
        methods.append(request["method"])
        if request["method"] == "pane.list":
            result = {
                "panes": [
                    {
                        "label": "runner",
                        "agent": "codex",
                        "agent_status": "idle",
                        "pane_id": "p-runner",
                        "workspace_id": "w1",
                        "tab_id": "t1",
                    }
                ]
            }
        elif request["method"] == "events.subscribe":
            result = {"type": "subscription_started"}
        elif request["method"] == "pane.read":
            result = {"read": {"text": "❯ \x1b[38;2;136;136;136mType a message\x1b[0m"}}
        else:
            result = {"type": "input_sent"}
        writer.write(json.dumps({"id": request["id"], "result": result}).encode("utf-8") + b"\n")
        if request["method"] == "events.subscribe":
            writer.write(
                json.dumps(
                    {
                        "event": "pane.agent_status_changed",
                        "data": {"pane_id": "p-runner", "agent_status": "blocked"},
                    }
                ).encode("utf-8")
                + b"\n"
            )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    cfg = SimpleNamespace(
        core_node_id="linux-main",
        socket_path=socket_path,
        state_path=state_path,
        daemon_socket_path=tmp_path / "herdr-talkback.sock",
        roles=("lead", "runner"),
        aliases={},
        delivery_max_attempts=1,
        delivery_retry_interval_seconds=0,
        notify_enabled=False,
        notify_position="bottom-right",
        notify_sound="none",
        monitor_continue_cooldown_seconds=120,
        monitor_unknown_threshold_seconds=60,
        monitor_max_continues_per_30m=3,
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("dry-run must not construct remote runtime resources")

    monkeypatch.setattr(service_module, "RemoteNodeRegistry", forbidden, raising=False)
    monkeypatch.setattr(service_module, "RemoteNodeAdapter", forbidden, raising=False)

    server = await start_local_server(handle, socket_path)
    async with server:
        assert await service_module.run_service(cfg, dry_run=True) == 0

    assert TalkbackState(state_path).get_message("queued")["state"] == "queued"
    assert methods == ["pane.list", "events.subscribe"]
    assert "pane.send_input" not in methods
    assert not cfg.daemon_socket_path.exists()


def test_service_checks_existing_panes_on_monitor_start():
    async def check():
        class IdleRegistry(FakeRegistry):
            def snapshot(self):
                return {'lead2': SimpleNamespace(pane_id='p-lead2', agent_status='idle')}

        class QuietClient:
            async def subscribe(self, subscriptions):
                if False:
                    yield {}

        monitor = FakeMonitor()
        service = TalkbackService(QuietClient(), IdleRegistry(), monitor)
        await service.run_until_disconnect()
        assert monitor.events == [
            {'event': 'pane.agent_status_changed', 'data': {'pane_id': 'p-lead2', 'agent_status': 'idle'}}
        ]
    asyncio.run(check())


def test_run_service_uses_only_herdr_and_closes_dispatcher(monkeypatch, tmp_path):
    async def check():
        events = []
        _install_minimal_service_fakes(monkeypatch, events)
        assert await service_module.run_service(_service_config(tmp_path)) == 0
        assert events == ['dispatcher.recover', 'service.run_forever', 'dispatcher.close']
    asyncio.run(check())
