import pytest

from herdr_talkback.logic_state import LogicStore, read_status
from herdr_talkback.state import TalkbackState


@pytest.fixture
def store(tmp_path):
    state = TalkbackState(tmp_path / 'state.db')
    state.init()
    return LogicStore(state)


def test_observation_transitions_and_fact_precedence(store):
    assert store.observe('p1', 'idle', 'A', 0)['logic_status'] == 'todo'
    assert not store.ready('p1', 5, 10)
    assert store.ready('p1', 10, 10)
    assert store.observe('p1', 'idle', 'B', 11)['logic_status'] == 'init'
    assert store.observe('p1', 'idle', 'B', 12)['logic_status'] == 'todo'
    assert not store.ready('p1', 12, 10)
    assert store.observe('p1', 'working', 'B', 13)['logic_status'] == 'working'
    assert store.observe('p1', 'blocked', 'B', 14)['logic_status'] == 'blocked'
    assert store.observe('p1', 'idle', 'B', 15)['logic_status'] == 'todo'
    assert store.get('p1')['herdr_status'] == 'idle'


def test_requests_invalidated_and_attempts_survive_restart(store):
    row = store.observe('p1', 'idle', 'A', 0)
    req = store.start_request('p1', 'm1', 'baseline', 10, 120)
    store.observe('p1', 'idle', 'B', 11)
    assert store.request(req['request_id'])['state'] == 'invalidated'
    assert not store.finish(req['request_id'], 'continue', 12, 120)
    assert store.reserve_attempt('p1', row['content_hash'], 13)
    assert not LogicStore(store.state).reserve_attempt('p1', row['content_hash'], 14)


def test_result_cooldown_and_restart_do_not_replay_request(store):
    store.observe('p1', 'idle', 'A', 0)
    req = store.start_request('p1', 'm1', '', 10, 120)
    assert store.finish(req['request_id'], 'unknown', 20, 120)
    assert not store.ready('p1', 139, 10)
    assert store.ready('p1', 140, 10)
    req = store.start_request('p1', 'm1', '', 141, 120)
    store.recover(142, 120)
    assert store.request(req['request_id'])['state'] == 'uncertain'
    assert store.get('p1')['logic_status'] == 'unknown'
    assert not store.ready('p1', 150, 10)


def test_continue_waits_for_execution_and_does_not_reclassify(store):
    store.observe('p1', 'idle', 'A', 0)
    req = store.start_request('p1', 'm1', '', 10, 120)
    store.finish(req['request_id'], 'continue', 20, 120)
    assert not store.ready('p1', 1000, 10)
    assert store.reserve_attempt('p1', store.get('p1')['content_hash'], 21)
    assert 'uncertain' in read_status(store.state.path)[0]['detail']
    store.attempt_outcome('p1', store.get('p1')['content_hash'], 'unknown', 22)
    assert store.attempts()[0]['outcome'] == 'unknown'
