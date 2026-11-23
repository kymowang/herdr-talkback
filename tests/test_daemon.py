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

    def check_source(self, name):
        if name != "lead":
            raise RoleResolutionError(f"source pane not found: {name}")
        return name


class SourceRegistry:
    labels = {"w1:p9": "monitor", "w1:p2": "codex2"}

    async def refresh(self):
        pass

    def snapshot(self):
        return {name: SimpleNamespace(name=name, pane_id=pane_id)
                for pane_id, name in self.labels.items()}

    def get_pane(self, pane_id):
        name = self.labels.get(pane_id)
        if name is None:
            return None
        return SimpleNamespace(name=name, pane_id=pane_id)

    def resolve_pane(self, role):
        for pane_id, name in self.labels.items():
            if name == role or pane_id == role:
                return pane_id
        raise RoleResolutionError(f"role has no active pane: {role}")

    def check_source(self, name):
        if name in self.labels or name in self.labels.values():
            return name
        raise RoleResolutionError(f"source pane not found: {name}")


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
        cfg = SimpleNamespace(core_node_id='linux-main', roles=('lead', 'dev-back'), aliases={}, message_rules=__import__('herdr_talkback.rules', fromlist=['parse_message_rules']).parse_message_rules([dict(id='receipt', agents=['lead'], append='上述内容如果是任务而非通知，完成后请通过talkback发送回执给我')]))
        router = DaemonMessageRouter(cfg, FakeRegistry(), FakeDispatcher(), state=state)
        response = await router.handle({'type': 'send', 'message': '[lead->dev-back] one'})
        assert response['ok']
        stored = state.get_message(response['message_id'])
        assert stored['body'] == 'one\n上述内容如果是任务而非通知，完成后请通过talkback发送回执给我'
        assert stored['raw_text'] == '[lead->dev-back] ' + stored['body']
    asyncio.run(check())


def test_daemon_rejects_node_id_and_unknown_sources(tmp_path):
    async def check():
        state = TalkbackState(tmp_path / 'state.db')
        state.init()
        cfg = SimpleNamespace(core_node_id='openclaw', roles=(), aliases={})
        dispatcher = FakeDispatcher()
        router = DaemonMessageRouter(cfg, SourceRegistry(), dispatcher, state=state)

        node_id = await router.handle({
            'type': 'send', 'message': '[openclaw->codex2] task', 'pane_id': 'w1:p9',
        })
        assert node_id['ok'] is False
        assert 'source is the node id, not a sender: openclaw' in node_id['error']
        assert "this pane is 'monitor', pane id w1:p9" in node_id['error']

        unknown = await router.handle({'type': 'send', 'message': '[ghost->codex2] task'})
        assert unknown['ok'] is False
        assert 'unknown source role: ghost' in unknown['error']

        assert dispatcher.wake_calls == []
    asyncio.run(check())


def test_daemon_router_accepts_live_pane_label_as_source(tmp_path):
    async def check():
        state = TalkbackState(tmp_path / 'state.db')
        state.init()
        cfg = SimpleNamespace(core_node_id='openclaw', roles=(), aliases={})
        router = DaemonMessageRouter(cfg, SourceRegistry(), FakeDispatcher(), state=state)

        response = await router.handle({'type': 'send', 'message': '[monitor->codex2] task'})

        assert response['ok'] is True
        assert response['source'] == 'monitor'
        assert response['source_endpoint'] == 'openclaw/monitor'
    asyncio.run(check())


def test_daemon_rejects_ambiguous_source_and_target(tmp_path):
    async def check():
        state = TalkbackState(tmp_path / 'state.db')
        state.init()
        cfg = SimpleNamespace(core_node_id='openclaw', roles=(), aliases={})

        class AmbiguousRegistry:
            async def refresh(self): pass
            def snapshot(self):
                return {'unique': SimpleNamespace(name='unique', pane_id='w1:p1')}
            def addressable_names(self): return {'unique', 'cmd'}
            def resolve_pane(self, name):
                if name == 'cmd':
                    raise RoleResolutionError(
                        'pane name is ambiguous: cmd (w1:p2, w1:p3); use a pane ID')
                if name in ('unique', 'w1:p1'):
                    return 'w1:p1'
                raise RoleResolutionError(f'role has no active pane: {name}')
            def check_source(self, name):
                if name == 'cmd':
                    raise RoleResolutionError(
                        'source pane is ambiguous: cmd (w1:p2, w1:p3); use a pane ID')
                if name in ('unique', 'w1:p1'):
                    return name
                raise RoleResolutionError(f'source pane not found: {name}')

        router = DaemonMessageRouter(cfg, AmbiguousRegistry(), FakeDispatcher(), state=state)
        bad_source = await router.handle({'type': 'send', 'message': '[cmd->unique] task'})
        assert bad_source['ok'] is False
        assert 'source pane is ambiguous' in bad_source['error']
        bad_target = await router.handle({'type': 'send', 'message': '[unique->cmd] task'})
        assert bad_target['ok'] is False
        assert 'ambiguous' in bad_target['error']

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
