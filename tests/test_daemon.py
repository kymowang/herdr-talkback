import asyncio
from pathlib import Path
from types import SimpleNamespace

from herdr_talkback.core_dispatcher import CoreDispatcher
from herdr_talkback.daemon import DaemonInboxServer, DaemonMessageRouter, submit_daemon_request
from herdr_talkback.endpoints import EndpointRegistry
from herdr_talkback.roles import RoleResolutionError
from herdr_talkback.state import TalkbackState
from herdr_talkback.types import MessageState


def test_daemon_socket_round_trip(tmp_path):
    asyncio.run(_daemon_socket_round_trip(tmp_path))


def test_daemon_startup_probe_closes_without_response():
    class Reader:
        async def readline(self):
            return b''

    class Writer:
        closed = False

        def write(self, data):
            raise AssertionError('disconnected probes must not receive a response')

        def close(self):
            self.closed = True

        async def wait_closed(self):
            pass

    writer = Writer()
    server = DaemonInboxServer(Path('unused'), None)
    asyncio.run(server._handle_client(Reader(), writer))
    assert writer.closed


async def _daemon_socket_round_trip(tmp_path):
    socket_path = tmp_path / "herdr-talkback.sock"

    async def handler(payload):
        return {"ok": True, "seen": payload["message"]}

    server = DaemonInboxServer(socket_path, handler)
    task = asyncio.create_task(server.serve_forever())
    try:
        await _wait_for_socket(socket_path)

        response = await submit_daemon_request(socket_path, {"type": "send", "message": "[lead→dev-back] ping"})

        assert response == {"ok": True, "seen": "[lead→dev-back] ping"}
    finally:
        await server.close()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def test_daemon_router_dry_run_resolves_without_delivering(tmp_path):
    asyncio.run(_daemon_router_dry_run_resolves_without_delivering(tmp_path))


async def _daemon_router_dry_run_resolves_without_delivering(tmp_path):
    registry = FakeRegistry()
    dispatcher = FakeDispatcher()
    cfg = SimpleNamespace(core_node_id="linux-main", roles=("lead", "dev-back"), aliases={})
    router = DaemonMessageRouter(cfg, registry, dispatcher)

    response = await router.handle({"type": "send", "message": "[lead→dev-back] ping", "dry_run": True})

    assert response == {
        "ok": True,
        "dry_run": True,
        "source": "lead",
        "target": "dev-back",
        "resolved_target": "dev-back",
        "pane_id": "p-back",
        "source_endpoint": "linux-main/lead",
        "target_endpoint": "linux-main/dev-back",
    }
    assert registry.refresh_count == 1
    assert dispatcher.wake_calls == []


def test_daemon_router_notifies_invalid_message(tmp_path):
    asyncio.run(_daemon_router_notifies_invalid_message(tmp_path))


async def _daemon_router_notifies_invalid_message(tmp_path):
    registry = FakeRegistry()
    dispatcher = FakeDispatcher()
    notifier = FakeNotifier()
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    cfg = SimpleNamespace(core_node_id="linux-main", roles=("lead", "dev-back"), aliases={})
    router = DaemonMessageRouter(cfg, registry, dispatcher, state=state, notifier=notifier)

    response = await router.handle({"type": "send", "message": "[lead→missing] ping"})

    assert response == {"ok": False, "error": "unknown target role: missing"}
    assert notifier.notifications == [
        ("Herdr Talkback send failed", "[lead→missing] ping\nunknown target role: missing")
    ]
    events = list(state.tail_events())
    assert events[-1]["kind"] == "send_failed"
    assert events[-1]["payload"] == {
        "raw_message": "[lead→missing] ping",
        "reason": "unknown target role: missing",
    }
    assert dispatcher.wake_calls == []


def test_daemon_router_persists_before_waking_dispatcher(tmp_path):
    asyncio.run(_daemon_router_persists_before_waking_dispatcher(tmp_path))


async def _daemon_router_persists_before_waking_dispatcher(tmp_path):
    registry = FakeRegistry()
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    dispatcher = PersistInspectingDispatcher(state)
    cfg = SimpleNamespace(core_node_id="linux-main", roles=("lead", "dev-back"), aliases={})
    router = DaemonMessageRouter(cfg, registry, dispatcher, state=state)

    response = await router.handle({"type": "send", "message": "[lead→dev-back] one"})

    assert response == {
        "ok": True,
        "queued": True,
        "message_id": response["message_id"],
        "source": "lead",
        "target": "dev-back",
        "resolved_target": "dev-back",
        "pane_id": "p-back",
        "source_endpoint": "linux-main/lead",
        "target_endpoint": "linux-main/dev-back",
    }
    persisted = state.get_message(response["message_id"])
    assert persisted["state"] == "queued"
    assert persisted["target_endpoint"] == "linux-main/dev-back"
    assert dispatcher.persisted_at_wake == [response["message_id"]]
    assert dispatcher.wake_calls == ["linux-main/dev-back"]


def test_daemon_router_resolves_qualified_local_target(tmp_path):
    asyncio.run(_daemon_router_resolves_qualified_local_target(tmp_path))


async def _daemon_router_resolves_qualified_local_target(tmp_path):
    registry = FakeRegistry()
    state = TalkbackState(tmp_path / "state.db", local_node_id="linux-main")
    state.init()
    dispatcher = PersistInspectingDispatcher(state)
    cfg = SimpleNamespace(
        core_node_id="linux-main",
        roles=("lead", "dev-back"),
        aliases={},
    )
    router = DaemonMessageRouter(cfg, registry, dispatcher, state=state)

    response = await router.handle(
        {
            "type": "send",
            "message": "[lead -> linux-main/dev-back] local work",
        }
    )

    assert response["ok"] is True
    assert response["pane_id"] == "p-back"
    assert response["target_endpoint"] == "linux-main/dev-back"
    persisted = state.get_message(response["message_id"])
    assert persisted["target_pane_id"] == "p-back"
    assert dispatcher.wake_calls == ["linux-main/dev-back"]


def test_daemon_router_duplicate_message_id_does_not_wake_twice(tmp_path, monkeypatch):
    asyncio.run(_daemon_router_duplicate_message_id_does_not_wake_twice(tmp_path, monkeypatch))


async def _daemon_router_duplicate_message_id_does_not_wake_twice(tmp_path, monkeypatch):
    monkeypatch.setattr("herdr_talkback.messages.uuid.uuid4", lambda: SimpleNamespace(hex='a' * 32))
    registry = FakeRegistry()
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    dispatcher = FakeDispatcher()
    cfg = SimpleNamespace(core_node_id="linux-main", roles=("lead", "dev-back"), aliases={})
    router = DaemonMessageRouter(cfg, registry, dispatcher, state=state)
    payload = {"type": "send", "message": "[lead→dev-back] duplicate"}

    first = await router.handle(payload)
    second = await router.handle(payload)

    assert first["queued"] is True
    assert second == {
        "ok": True,
        "queued": False,
        "duplicate": True,
        "message_id": first["message_id"],
        "source_endpoint": "linux-main/lead",
        "target_endpoint": "linux-main/dev-back",
    }
    assert dispatcher.wake_calls == ["linux-main/dev-back"]


def test_daemon_router_delegates_drain_and_close(tmp_path):
    asyncio.run(_daemon_router_delegates_drain_and_close(tmp_path))


async def _daemon_router_delegates_drain_and_close(tmp_path):
    registry = FakeRegistry()
    dispatcher = FakeDispatcher()
    cfg = SimpleNamespace(core_node_id="linux-main", roles=("lead", "dev-back"), aliases={})
    router = DaemonMessageRouter(cfg, registry, dispatcher)

    await router.drain()
    await router.close()

    assert dispatcher.drained is True
    assert dispatcher.closed is True


async def _wait_for_socket(path: Path) -> None:
    for _ in range(50):
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"socket was not created: {path}")


class FakeRegistry:
    def __init__(self):
        self.refresh_count = 0

    async def refresh(self):
        self.refresh_count += 1

    def resolve_pane(self, role):
        if role != "dev-back":
            raise RoleResolutionError(f"role has no active pane: {role}")
        return "p-back"


class FakeDispatcher:
    def __init__(self):
        self.wake_calls = []
        self.drained = False
        self.closed = False

    def wake(self, endpoint):
        self.wake_calls.append(endpoint)

    async def drain(self):
        self.drained = True

    async def close(self):
        self.closed = True


class PersistInspectingDispatcher(FakeDispatcher):
    def __init__(self, state):
        super().__init__()
        self.state = state
        self.persisted_at_wake = []

    def wake(self, endpoint):
        rows = self.state.list_active_messages()
        self.persisted_at_wake.extend(row["message_id"] for row in rows)
        super().wake(endpoint)


class FakeNotifier:
    def __init__(self):
        self.notifications = []

    async def notify(self, title, body):
        self.notifications.append((title, body))


def test_daemon_persists_configured_receipt_reminder(tmp_path):
    async def check():
        state = TalkbackState(tmp_path / 'state.db')
        state.init()
        cfg = SimpleNamespace(core_node_id='linux-main', roles=('lead', 'dev-back'), aliases={}, message_rules=__import__('herdr_talkback.rules', fromlist=['parse_message_rules']).parse_message_rules([dict(id='receipt', agents=['lead'], append='上述内容如果是任务而非通知，完成后请发回执给我')]))
        router = DaemonMessageRouter(cfg, FakeRegistry(), FakeDispatcher(), state=state)
        response = await router.handle({'type': 'send', 'message': '[lead->dev-back] one'})
        assert response['ok']
        stored = state.get_message(response['message_id'])
        assert stored['body'] == 'one\n上述内容如果是任务而非通知，完成后请发回执给我'
        assert stored['raw_text'] == '[lead->dev-back] ' + stored['body']
    asyncio.run(check())


def test_daemon_rejects_nonlocal_target(tmp_path):
    async def check():
        state = TalkbackState(tmp_path / 'state.db')
        state.init()
        cfg = SimpleNamespace(core_node_id='linux-main', roles=('lead',), aliases={})
        dispatcher = FakeDispatcher()
        router = DaemonMessageRouter(cfg, FakeRegistry(), dispatcher, state=state)
        response = await router.handle({'type': 'send', 'message': '[lead->other/runner] task'})
        assert response['ok'] is False
        assert 'local Herdr' in response['error']
        assert dispatcher.wake_calls == []
    asyncio.run(check())
