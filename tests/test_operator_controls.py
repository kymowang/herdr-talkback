import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from herdr_talkback.operator_controls import OperatorStore, MonitorController, read_registered_pane
from herdr_talkback.state import TalkbackState
from herdr_talkback.daemon import DaemonMessageRouter, DaemonInboxServer, submit_daemon_request


def test_monitor_override_persists_per_session(tmp_path):
    state = TalkbackState(tmp_path / 'one.db'); state.init()
    controls = OperatorStore(state)
    assert controls.monitor_enabled(default=True) is True
    controls.set_monitor_enabled(False)
    assert OperatorStore(state).monitor_enabled(default=True) is False
    controls.set_monitor_enabled(True)
    assert OperatorStore(state).monitor_enabled(default=False) is True
    other = TalkbackState(tmp_path / 'two.db'); other.init()
    assert OperatorStore(other).monitor_enabled(default=False) is False


def test_monitor_disable_cancels_inflight_and_enable_starts_again(tmp_path):
    async def scenario():
        state = TalkbackState(tmp_path / 'state.db'); state.init()
        controls = OperatorStore(state)
        starts, stops = [], []
        class FakeMonitor:
            async def run_forever(self):
                starts.append(1)
                try: await asyncio.Event().wait()
                finally: stops.append(1)
        controller = MonitorController(controls, lambda: FakeMonitor())
        await controller.set_enabled(True)
        await asyncio.sleep(0)
        assert starts == [1]
        await controller.set_enabled(False)
        assert stops == [1] and not controls.monitor_enabled(True)
        await controller.set_enabled(True)
        await asyncio.sleep(0)
        assert starts == [1, 1]
        await controller.close()
    asyncio.run(scenario())


def test_pane_directory_validates_peers_and_retains_unique_aliases(tmp_path):
    state = TalkbackState(tmp_path / 'state.db'); state.init()
    store = OperatorStore(state)
    cfg = SimpleNamespace(core_node_id='windows', peers={'openclaw': object()})
    store.register_pane('backend', 'openclaw/lead', cfg)
    store.register_pane('local_worker', 'windows/ast-dev1', cfg)
    assert store.pane('backend') == 'openclaw/lead'
    assert OperatorStore(state).pane('local_worker') == 'windows/ast-dev1'
    with pytest.raises(ValueError): store.register_pane('backend', 'windows/a', cfg)
    with pytest.raises(ValueError): store.register_pane('unknown', 'missing/a', cfg)
    with pytest.raises(ValueError): store.register_pane('bad/name', 'windows/a', cfg)
    store.remove_pane('backend')
    assert store.pane('backend') is None


def test_read_registered_pane_does_not_create_database(tmp_path):
    path = tmp_path / 'missing' / 'state.db'
    assert read_registered_pane(path, 'worker') is None
    assert not path.parent.exists()


@pytest.mark.parametrize('arrow', ['->', '→'])
def test_router_resolves_registered_remote_alias_without_contacting_peer(tmp_path, arrow):
    async def scenario():
        state = TalkbackState(tmp_path / 'state.db'); state.init()
        store = OperatorStore(state)
        cfg = SimpleNamespace(core_node_id='windows', peers={'openclaw': object()},
                              roles=(), aliases={}, message_rules=())
        store.register_pane('remote_lead', 'openclaw/lead', cfg)
        class Registry:
            async def refresh(self): pass
            def snapshot(self): return {}
        class Peer:
            def ensure_endpoint(self, target): self.target = target
        class Dispatcher:
            def wake(self, target): self.target = target
        peer, dispatcher = Peer(), Dispatcher()
        router = DaemonMessageRouter(cfg, Registry(), dispatcher, state=state,
                                     peer_adapter=peer, operator_store=store)
        result = await router.handle({'type': 'send', 'message': f'[sender{arrow}remote_lead] hello'})
        assert result['ok'] and result['target_endpoint'] == 'openclaw/lead'
        assert peer.target.qualified == 'openclaw/lead'
        assert state.get_message(result['message_id'])['raw_text'] == f'[sender{arrow}openclaw/lead] hello'
    asyncio.run(scenario())


def test_router_rejects_registered_alias_conflicting_with_live_local_pane(tmp_path):
    async def scenario():
        state = TalkbackState(tmp_path / 'state.db'); state.init()
        store = OperatorStore(state)
        cfg = SimpleNamespace(core_node_id='windows', peers={'openclaw': object()},
                              roles=(), aliases={}, message_rules=())
        store.register_pane('lead', 'openclaw/lead', cfg)
        class Registry:
            async def refresh(self): pass
            def snapshot(self): return {'lead': object()}
        router = DaemonMessageRouter(cfg, Registry(), None, state=state, operator_store=store)
        result = await router.handle({'type': 'send', 'message': '[sender->lead] hello', 'dry_run': True})
        assert not result['ok'] and 'conflicts' in result['error']
    asyncio.run(scenario())


def test_monitor_control_rejects_other_state_and_cancels_running_task(tmp_path):
    async def scenario():
        state = TalkbackState(tmp_path / 'state.db'); state.init()
        store = OperatorStore(state)
        stopped = asyncio.Event()
        class FakeMonitor:
            async def run_forever(self):
                try: await asyncio.Event().wait()
                finally: stopped.set()
        controller = MonitorController(store, lambda: FakeMonitor())
        router = DaemonMessageRouter(SimpleNamespace(), None, None, state=state,
                                     operator_store=store, monitor_controller=controller)
        payload = {'type': 'monitor_control', 'enabled': True, 'state_path': str(state.path.resolve())}
        assert (await router.handle(payload))['applied'] is True
        await asyncio.sleep(0)
        wrong = dict(payload, enabled=False, state_path=str(tmp_path / 'other.db'))
        assert not (await router.handle(wrong))['ok']
        assert store.monitor_enabled() is True and not stopped.is_set()
        assert (await router.handle(dict(payload, enabled=False)))['applied'] is True
        assert stopped.is_set() and store.monitor_enabled(True) is False
        await controller.close()
    asyncio.run(scenario())


def test_starting_with_toml_default_does_not_create_override(tmp_path):
    async def scenario():
        state = TalkbackState(tmp_path / 'state.db'); state.init()
        store = OperatorStore(state)
        class FakeMonitor:
            async def run_forever(self): await asyncio.Event().wait()
        controller = MonitorController(store, lambda: FakeMonitor())
        await controller.set_enabled(True, persist=False)
        assert store.monitor_enabled(default=False) is False
        await controller.close()
    asyncio.run(scenario())


def test_monitor_control_round_trip_through_local_daemon(tmp_path):
    async def scenario():
        state = TalkbackState(tmp_path / 'state.db'); state.init()
        store = OperatorStore(state)
        class FakeMonitor:
            async def run_forever(self): await asyncio.Event().wait()
        controller = MonitorController(store, lambda: FakeMonitor())
        router = DaemonMessageRouter(SimpleNamespace(), None, None, state=state,
                                     operator_store=store, monitor_controller=controller)
        socket_path = tmp_path / 'daemon.sock'
        server = DaemonInboxServer(socket_path, router.handle)
        task = asyncio.create_task(server.serve_forever())
        try:
            for _ in range(100):
                if socket_path.exists(): break
                await asyncio.sleep(.01)
            response = await submit_daemon_request(socket_path, {'type': 'monitor_control', 'enabled': True,
                                                                  'state_path': str(state.path.resolve())})
            assert response == {'ok': True, 'enabled': True, 'applied': True}
            response = await submit_daemon_request(socket_path, {'type': 'monitor_control', 'enabled': False,
                                                                  'state_path': str(state.path.resolve())})
            assert response == {'ok': True, 'enabled': False, 'applied': True}
            assert not store.monitor_enabled(True)
        finally:
            await server.close()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await controller.close()
    asyncio.run(scenario())


def test_monitor_cli_controls_real_local_daemon(tmp_path):
    async def scenario():
        state = TalkbackState(tmp_path / 'state.db'); state.init()
        store = OperatorStore(state)
        class FakeMonitor:
            async def run_forever(self): await asyncio.Event().wait()
        controller = MonitorController(store, lambda: FakeMonitor())
        config = SimpleNamespace(logic_monitor=SimpleNamespace(enabled=False))
        router = DaemonMessageRouter(config, None, None, state=state,
                                     operator_store=store, monitor_controller=controller)
        socket_path = tmp_path / 'daemon.sock'
        cfg = tmp_path / 'config.toml'
        cfg.write_text('state_path="' + state.path.as_posix() + '"\n'
                       'daemon_socket_path="' + socket_path.as_posix() + '"\n'
                       '[logic_monitor]\nmonitor_pane="judge"\ntargets=["worker"]\n', encoding='utf-8')
        server = DaemonInboxServer(socket_path, router.handle)
        task = asyncio.create_task(server.serve_forever())
        env = dict(os.environ, HERDR_SOCKET_PATH=str(tmp_path / 'herdr.sock'))
        async def command(verb):
            result = await asyncio.to_thread(subprocess.run,
                [sys.executable, '-m', 'herdr_talkback', '--config', str(cfg), 'monitor', verb],
                env=env, capture_output=True, encoding='utf-8', timeout=10)
            assert result.returncode == 0, result.stderr + result.stdout
            return json.loads(result.stdout)
        try:
            for _ in range(100):
                if socket_path.exists(): break
                await asyncio.sleep(.01)
            assert (await command('enable'))['applied'] is True
            assert (await command('state'))['active'] is True
            assert (await command('disable'))['applied'] is True
            status = await command('state')
            assert status['enabled'] is False and status['active'] is False
        finally:
            await server.close()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await controller.close()
    asyncio.run(scenario())


def test_bulk_local_registration_is_idempotent_and_reports_conflicts(tmp_path):
    from herdr_talkback.operator_controls import register_local_panes
    state = TalkbackState(tmp_path / 'state.db'); state.init()
    store = OperatorStore(state)
    cfg = SimpleNamespace(core_node_id='windows', peers={'openclaw': object()}, aliases={})
    store.register_pane('lead', 'openclaw/lead', cfg)
    panes = [{'pane_id': 'p1', 'label': 'worker'}, {'pane_id': 'p2', 'label': None},
             {'pane_id': 'p3', 'label': 'lead'}]
    first = register_local_panes(store, cfg, panes)
    second = register_local_panes(store, cfg, panes)
    assert store.pane('worker') == 'windows/worker'
    assert [r['status'] for r in first] == ['registered', 'unnamed', 'conflict']
    assert second[0]['status'] == 'existing'


def test_identity_card_validates_peer_and_never_includes_credentials(tmp_path):
    from herdr_talkback.operator_controls import make_identity_card, import_identity_card
    state = TalkbackState(tmp_path / 'state.db'); state.init()
    store = OperatorStore(state)
    sender = SimpleNamespace(core_node_id='windows', network=SimpleNamespace(listen_host='192.168.1.2', listen_port=18765))
    peer = SimpleNamespace(url='https://192.168.1.2:18765', token_file='secret')
    receiver = SimpleNamespace(core_node_id='linux', peers={'windows': peer}, aliases={})
    card = make_identity_card(sender, 'worker')
    assert 'secret' not in json.dumps(card)
    assert import_identity_card(store, receiver, card, set()) == 'registered'
    assert store.pane('worker') == 'windows/worker'
    assert import_identity_card(store, receiver, card, set()) == 'existing'
    with pytest.raises(ValueError):
        import_identity_card(store, receiver, dict(card, url='https://other:18765'), set())


def test_self_registered_local_pane_keeps_local_routing(tmp_path):
    async def scenario():
        state = TalkbackState(tmp_path / 'state.db'); state.init()
        store = OperatorStore(state)
        cfg = SimpleNamespace(core_node_id='windows', peers={}, roles=(), aliases={}, message_rules=())
        store.register_pane('worker', 'windows/worker', cfg)
        class Registry:
            async def refresh(self): pass
            def snapshot(self): return {'worker': object()}
            def resolve_pane(self, role): return 'p1'
        router = DaemonMessageRouter(cfg, Registry(), None, state=state, operator_store=store)
        result = await router.handle({'type': 'send', 'message': '[sender->worker] hello', 'dry_run': True})
        assert result['ok'] and result['target_endpoint'] == 'windows/worker'
    asyncio.run(scenario())
