from concurrent.futures import ThreadPoolExecutor

import pytest

from herdr_talkback.state import TalkbackState
from herdr_talkback.core_types import CoreMessageState


def envelope(message_id='msg_test'):
    return dict(protocol_version=1, message_id=message_id, source_endpoint='sender/agent',
                target_endpoint='receiver/agent', body='hello', conversation_id=message_id,
                reply_to=None, metadata={})


def store_at(path):
    from herdr_talkback.peer_state import PeerStore
    state = TalkbackState(path, local_node_id='receiver')
    state.init()
    return PeerStore(state)


def test_concurrent_duplicates_and_archival_keep_receipt(tmp_path):
    store = store_at(tmp_path / 'state.db')
    with ThreadPoolExecutor(max_workers=4) as pool:
        created = list(pool.map(lambda _: store.receive('sender', envelope())[0], range(4)))
    assert sum(created) == 1
    store.state.update_message_state('msg_test', CoreMessageState.DELIVERED, 'confirmed')
    assert store.receipt('sender', 'msg_test')['state'] == 'delivered'
    with store.state._connect() as db:
        db.execute('delete from messages where message_id=?', ('msg_test',))
    assert store.receive('sender', envelope())[0] is False
    assert store.receipt('sender', 'msg_test')['state'] == 'delivered'
    with pytest.raises(KeyError):
        store.receipt('stranger', 'msg_test')


def test_identity_conflict_and_capacity_are_atomic(tmp_path):
    from herdr_talkback.peer_state import PeerConflict, PeerCapacity
    store = store_at(tmp_path / 'state.db')
    store.receive('sender', envelope(), capacity=1)
    changed = envelope()
    changed['body'] = 'changed'
    with pytest.raises(PeerConflict):
        store.receive('sender', changed)
    with pytest.raises(PeerCapacity):
        store.receive('sender', envelope('msg_next'), capacity=1)


def test_instance_pin_survives_restart(tmp_path):
    from herdr_talkback.peer_state import PeerInstanceChanged
    store = store_at(tmp_path / 'state.db')
    identity = store.instance_id
    store.pin_instance('sender', 'first')
    other = store_at(tmp_path / 'state.db')
    assert other.instance_id == identity
    other.pin_instance('sender', 'first')
    with pytest.raises(PeerInstanceChanged):
        other.pin_instance('sender', 'reset')


def test_archived_local_message_identity_cannot_be_reused_by_peer(tmp_path):
    from herdr_talkback.peer_state import PeerConflict
    store = store_at(tmp_path / 'state.db')
    with store.state._connect() as db:
        db.execute('insert into message_archive(message_id,message_json,archived_at) values (?,?,?)',
                   ('msg_test', '{}', 0))
    with pytest.raises(PeerConflict):
        store.receive('sender', envelope())
