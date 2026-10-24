import asyncio
import json
from types import SimpleNamespace

import pytest

from herdr_talkback.logic_config import LogicMonitorConfig
from herdr_talkback.logic_monitor import LogicalPaneMonitor, normalize_snapshot, parse_result, resolve_selector
from herdr_talkback.logic_state import LogicStore
from herdr_talkback.pane_actions import PaneActions
from herdr_talkback.state import TalkbackState


class Client:
    def __init__(self):
        self.panes = [dict(pane_id='p1', label='worker', agent_status='idle'),
                      dict(pane_id='m1', label='judge', agent_status='idle')]
        self.text = {'p1': 'Next I can write tests.', 'm1': 'Ready'}
        self.inputs = {'p1': '\u203a ', 'm1': '\u203a '}
        self.sent = []
        self.reads = []
        self.read_hook = None

    async def request(self, method, params):
        if method == 'pane.list':
            return {'panes': [dict(p) for p in self.panes]}
        pane = params['pane_id']
        if method == 'pane.read':
            self.reads.append(dict(params))
            if self.read_hook:
                self.read_hook(pane, params)
            return {'read': {'text': self.inputs[pane] if params.get('format') == 'ansi' else self.text[pane]}}
        assert method == 'pane.send_input'
        self.sent.append((pane, params['text']))
        return {}


class Delivery:
    def __init__(self, client):
        self.client = client
        self.sent = []

    async def send_control(self, pane, text, strict=False):
        assert strict
        if pane == 'm1':
            await self.client.request('pane.send_input', {'pane_id': pane, 'text': text})
        else:
            self.sent.append((pane, text))
        return SimpleNamespace(delivered=True, reason='control sent', attempts=1)


@pytest.fixture
def rig(tmp_path):
    state = TalkbackState(tmp_path / 'state.db'); state.init()
    store, client = LogicStore(state), Client()
    delivery = Delivery(client)
    clock = [0]
    cfg = LogicMonitorConfig(enabled=True, monitor_pane='judge', targets=('worker',))
    engine = LogicalPaneMonitor(cfg, client, store, delivery, PaneActions(store, clock=lambda: clock[0]), clock=lambda: clock[0])
    return engine, store, client, delivery, clock


async def submit(rig):
    engine, store, client, delivery, clock = rig
    await engine.tick()
    clock[0] = 10
    await engine.tick()
    assert len(client.sent) == 1
    return store.active()[0]


def reply(client, req, result):
    client.text['m1'] += '\n' + json.dumps({'request_id': req['request_id'], 'status': result})


def test_normalize_and_resolve():
    assert normalize_snapshot('\x1b[31mA\r\n\r\n') == 'A'
    panes = [dict(pane_id='p1', label='a'), dict(pane_id='p2', label='a')]
    assert resolve_selector(panes, 'p1')['pane_id'] == 'p1'
    with pytest.raises(ValueError):
        resolve_selector(panes, 'a')


def test_response_protocol_rejects_echo_old_ids_conflicts_and_fences():
    valid = '{"request_id":"r1","status":"continue"}'
    assert parse_result(valid, '', 'r1') == 'continue'
    assert parse_result(valid, valid, 'r1') is None
    assert parse_result(valid, '', 'r2') is None
    assert parse_result('Request id: r1\nstatus: continue\ncontinue', '', 'r1') is None
    assert parse_result('```\n' + valid + '\n```', '', 'r1') is None
    assert parse_result(valid + '\n' + valid.replace('continue', 'blocked'), '', 'r1') == 'conflict'
    assert parse_result('{"request_id":"r1","status":"continue","extra":1}', '', 'r1') is None


def test_fence_context_survives_baseline_and_scroll_boundary():
    line = '{"request_id":"r1","status":"continue"}'
    baseline = 'old\n```json\n'
    assert parse_result(baseline + line + '\n```', baseline, 'r1') is None
    baseline = 'old\n```json\ninside\n'
    assert parse_result('inside\n```\n' + line, baseline, 'r1') == 'continue'


def test_continue_once_and_restart_safe(rig):
    async def scenario():
        engine, store, client, delivery, clock = rig
        req = await submit(rig)
        prompt = client.sent[0][1]
        assert '可选' in prompt and req['request_id'] in prompt
        assert parse_result(prompt, '', req['request_id']) is None
        reply(client, req, 'continue'); clock[0] = 20
        await engine.tick()
        assert delivery.sent == [('p1', 'continue')]
        assert store.get('p1')['logic_status'] == 'continue'
        clock[0] = 500
        await engine.tick(); await engine.tick()
        assert len(delivery.sent) == 1 and len(client.sent) == 1
        assert LogicStore(store.state).attempted('p1', req['content_hash'])
        assert client.reads
        assert all(read['source'] == 'visible' and 'lines' not in read for read in client.reads)
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['content', 'working', 'blocked', 'removed', 'monitor_replaced'])
def test_changed_pane_or_monitor_discards_result(rig, change):
    async def scenario():
        engine, store, client, delivery, clock = rig
        req = await submit(rig)
        reply(client, req, 'continue')
        if change == 'content': client.text['p1'] = 'New output'
        elif change in ('working', 'blocked'): client.panes[0]['agent_status'] = change
        elif change == 'removed': client.panes.pop(0)
        else: client.panes[1]['pane_id'] = 'm2'
        clock[0] = 20
        await engine.tick()
        assert delivery.sent == []
        assert store.request(req['request_id'])['state'] == 'invalidated'
    asyncio.run(scenario())


def test_busy_input_waits_and_rechecks_then_continues(rig):
    async def scenario():
        engine, store, client, delivery, clock = rig
        req = await submit(rig)
        client.inputs['p1'] = '\u203a user draft'
        reply(client, req, 'continue'); clock[0] = 20
        await engine.tick()
        assert delivery.sent == [] and not store.attempts()
        client.inputs['p1'] = '\u203a '; clock[0] = 30
        await engine.tick()
        assert delivery.sent == [('p1', 'continue')]
    asyncio.run(scenario())


def test_timeout_late_reply_and_busy_monitor(rig):
    async def scenario():
        engine, store, client, delivery, clock = rig
        client.inputs['m1'] = '\u203a user draft'
        await engine.tick(); clock[0] = 10; await engine.tick()
        assert not client.sent and not store.active()
        client.inputs['m1'] = '\u203a '; clock[0] = 20; await engine.tick()
        req = store.active()[0]
        clock[0] = 140; await engine.tick()
        assert store.get('p1')['logic_status'] == 'timeout'
        reply(client, req, 'continue'); clock[0] = 150; await engine.tick()
        assert not delivery.sent and len(client.sent) == 1
    asyncio.run(scenario())


def test_serial_fair_queue(rig):
    async def scenario():
        engine, store, client, delivery, clock = rig
        engine.config = LogicMonitorConfig(enabled=True, monitor_pane='judge', targets=('worker', 'other'))
        client.panes.append(dict(pane_id='p2', label='other', agent_status='idle'))
        client.text['p2'], client.inputs['p2'] = 'Waiting', '\u203a '
        req = await submit(rig)
        reply(client, req, 'waiting'); clock[0] = 20; await engine.tick()
        assert len(client.sent) == 2
        assert store.active()[0]['pane_id'] == 'p2'
    asyncio.run(scenario())


def test_unknown_submit_never_replayed_immediately(rig):
    async def scenario():
        engine, store, client, delivery, clock = rig
        original = client.request
        async def request(method, params):
            if method == 'pane.send_input': raise ConnectionError('lost response')
            return await original(method, params)
        client.request = request
        await engine.tick(); clock[0] = 10; await engine.tick()
        assert store.get('p1')['logic_status'] == 'unknown'
        assert not store.active()
        clock[0] = 20; await engine.tick()
        assert not delivery.sent
    asyncio.run(scenario())


def test_restart_invalidates_outstanding_request(rig):
    async def scenario():
        engine, store, client, delivery, clock = rig
        req = await submit(rig)
        reply(client, req, 'continue'); clock[0] = 20
        engine = LogicalPaneMonitor(engine.config, client, store, delivery, engine.actions, clock=lambda: clock[0])
        await engine.tick()
        assert not delivery.sent
        assert store.request(req['request_id'])['state'] == 'uncertain'
    asyncio.run(scenario())


def test_recovery_invalidation_and_shared_limit_prevent_continue(rig):
    async def scenario():
        engine, store, client, delivery, clock = rig
        req = await submit(rig)
        engine.actions.invalidate('p1')
        reply(client, req, 'continue'); clock[0] = 20
        await engine.tick()
        assert not delivery.sent
        assert store.request(req['request_id'])['state'] == 'invalidated'
        client.text['p1'] = 'New task'; clock[0] = 30; await engine.tick()
        engine.config = LogicMonitorConfig(enabled=True, monitor_pane='judge', targets=('worker',), timeout_seconds=300)
        clock[0] = 40; await engine.tick()
        req = store.active()[0]
        for t in (1, 2, 3):
            store.state.record_recovery('worker', 'p1', 'idle', 'legacy', 'continue', now=t)
        reply(client, req, 'continue'); clock[0] = 200; await engine.tick()
        assert not delivery.sent
    asyncio.run(scenario())


def test_status_change_during_input_read_prevents_continue(rig):
    async def scenario():
        engine, store, client, delivery, clock = rig
        req = await submit(rig)
        reply(client, req, 'continue'); clock[0] = 20
        def change(pane, params):
            if pane == 'p1' and params.get('format') == 'ansi':
                client.panes[0]['agent_status'] = 'working'
        client.read_hook = change
        await engine.tick()
        assert not delivery.sent and not store.attempts()
    asyncio.run(scenario())


def test_unknown_continue_is_not_retried(rig):
    async def scenario():
        engine, store, client, delivery, clock = rig
        req = await submit(rig)
        async def uncertain(pane, text, strict=False):
            delivery.sent.append((pane, text))
            raise ConnectionError()
        delivery.send_control = uncertain
        reply(client, req, 'continue'); clock[0] = 20
        await engine.tick(); clock[0] = 300; await engine.tick()
        assert len(delivery.sent) == 1
        assert store.attempts()[0]['outcome'] == 'uncertain'
    asyncio.run(scenario())
