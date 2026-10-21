import asyncio

import pytest

from herdr_talkback.config import load_config
from herdr_talkback.messages import parse_message
from herdr_talkback.monitor import Monitor
from herdr_talkback.rules import parse_recovery_rules, parse_message_rules
from herdr_talkback.state import TalkbackState
from test_monitor import FakeClient, FakeRegistry, FakeDelivery


def test_recovery_agent_scope_and_custom_command(tmp_path):
    async def check():
        rule = parse_recovery_rules([dict(id='resume', agents=['runner'], agent_types=['codex'],
            patterns=['^■ custom disconnect$'], statuses=['idle'], command='resume', cooldown_seconds=0, max_per_30m=0)])[0]
        state = TalkbackState(tmp_path / 'state.db'); state.init()
        class Registry(FakeRegistry):
            def get_pane(self, pane_id):
                return type('Info', (), {'agent': 'codex'})()
        delivery = FakeDelivery()
        monitor = Monitor(FakeClient('› task\n■ custom disconnect\n› '), Registry(), delivery, state, rules=(rule,))
        await monitor.handle_status_event({'data': {'pane_id': 'p-runner', 'agent_status': 'idle'}})
        assert delivery.controls == [('p-runner', 'resume')]
        excluded = parse_recovery_rules([dict(id='excluded', agents=['lead*'], patterns=['disconnect'], statuses=['idle'])])
        monitor.rules = excluded
        await monitor.handle_status_event({'data': {'pane_id': 'p-runner', 'agent_status': 'idle'}})
        assert len(delivery.controls) == 1
    asyncio.run(check())


def test_message_rule_scope_and_idempotence():
    rules = parse_message_rules([dict(id='receipt', agents=['lead*', 'scheduler'], append='请回执')])
    for source, expected in [('lead2', True), ('scheduler', True), ('dev-be', False)]:
        msg = parse_message(f'[{source}->runner] task', (), {}, message_rules=rules)
        assert msg.raw_text.endswith('\n请回执') is expected
        assert parse_message(msg.raw_text, (), {}, message_rules=rules).raw_text == msg.raw_text


@pytest.mark.parametrize('bad', [
    dict(id='bad', patterns=['['], agents=['*']),
    dict(id='bad', patterns=['error'], agents=[]),
    dict(id='bad', patterns=['error'], agents=['*'], statuses=['working']),
    dict(id='bad', patterns=['error'], agents=['*'], cooldown_seconds=-1),
])
def test_rejects_invalid_recovery_rule(bad):
    with pytest.raises(ValueError):
        parse_recovery_rules([bad])


def test_explicit_empty_rules_disable_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv('HERDR_SOCKET_PATH', '/tmp/herdr.sock')
    p = tmp_path / 'config.toml'
    p.write_text('recovery_rules = []\nmessage_rules = []\n')
    cfg = load_config(p)
    assert cfg.recovery_rules == ()
    assert cfg.message_rules == ()


def test_agent_type_and_target_filters():
    rule = parse_recovery_rules([dict(id='codex', agents=['lead*'], agent_types=['codex'], patterns=['error'])])[0]
    assert rule.matches('lead2', 'codex', 'idle', 'error')
    assert not rule.matches('lead2', 'claude', 'idle', 'error')
    assert not rule.matches('runner', 'codex', 'idle', 'error')
    rules = parse_message_rules([dict(id='receipt', agents=['lead*'], targets=['dev-*'], append='回执')])
    assert parse_message('[lead2->dev-be] task', (), {}, message_rules=rules).body == 'task\n回执'
    assert parse_message('[lead2->scheduler] task', (), {}, message_rules=rules).body == 'task'


def test_multiple_message_rules_retry_does_not_duplicate():
    rules = parse_message_rules([dict(id='first', agents=['*'], append='first'), dict(id='second', agents=['*'], append='second')])
    first = parse_message('[lead->runner] task', (), {}, message_rules=rules)
    assert first.body == 'task\nfirst\nsecond'
    assert parse_message(first.raw_text, (), {}, message_rules=rules).raw_text == first.raw_text
