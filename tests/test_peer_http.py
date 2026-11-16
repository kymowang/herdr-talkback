import pytest
pytest.importorskip("aiohttp")
pytest.importorskip("cryptography")

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from herdr_talkback.certificates import provision
from herdr_talkback.network_config import NetworkConfig, PeerConfig
from herdr_talkback.peer_state import PeerStore
from herdr_talkback.state import TalkbackState
from test_peer_state import envelope


def make_node(tmp_path, node_id, peer_id):
    directory = tmp_path / node_id
    provision(directory, ['127.0.0.1'])
    (directory / 'pair.token').write_text('a' * 64, encoding='ascii')
    state = TalkbackState(directory / 'state.db', local_node_id=node_id)
    state.init()
    network = NetworkConfig(True, '127.0.0.1', 0, directory / 'node.crt', directory / 'node.key')
    peer = PeerConfig(peer_id, 'https://127.0.0.1:1', directory / 'node.crt', directory / 'pair.token')
    return SimpleNamespace(core_node_id=node_id, network=network, peers={peer_id: peer}, roles=()), PeerStore(state)


def test_https_admission_duplicate_and_receipt(tmp_path):
    from herdr_talkback.peer_http import PeerServer, PeerClient, PeerHTTPError

    async def scenario():
        config, store = make_node(tmp_path, 'receiver', 'sender')
        wakes = []
        async with PeerServer(config, store, SimpleNamespace(wake=wakes.append)) as server:
            peer = replace(config.peers['sender'], node_id='receiver', url=f'https://127.0.0.1:{server.bound_port}')
            client = PeerClient('sender', peer)
            info = await client.request('GET', '/v1/info')
            assert info['node_id'] == 'receiver' and info['instance_id'] == store.instance_id
            first = await client.request('POST', '/v1/messages', envelope())
            second = await client.request('POST', '/v1/messages', envelope())
            assert first['message_id'] == second['message_id'] == 'msg_test'
            assert len(store.state.list_messages()) == 1
            assert wakes == ['receiver/agent', 'receiver/agent']
            assert (await client.request('GET', '/v1/messages/msg_test'))['state'] == 'queued'
            unicode_message = envelope('msg_unicode')
            unicode_message['body'] = '\u6d4b' * 21845  # 65535 UTF-8 bytes, below the body limit.
            unicode_receipt = await client.request('POST', '/v1/messages', unicode_message)
            assert unicode_receipt['state'] == 'queued'
            bad = envelope()
            bad['source_endpoint'] = 'stranger/agent'
            with pytest.raises(PeerHTTPError) as exc:
                await client.request('POST', '/v1/messages', bad)
            assert exc.value.status == 400
            wrong = replace(peer, token_file=config.network.key_file)
            with pytest.raises(ValueError):
                PeerClient('sender', wrong)
            bad_token = tmp_path / 'wrong.token'
            bad_token.write_text('b' * 64)
            with pytest.raises(PeerHTTPError) as exc:
                await PeerClient('sender', replace(peer, token_file=bad_token)).request('GET', '/v1/info')
            assert exc.value.status == 401
            # A reset between preflight and POST must not accept an old replay.
            store.instance_id = 'replacement'
            with pytest.raises(PeerHTTPError) as exc:
                await client.request('POST', '/v1/messages', envelope('msg_after_reset'))
            assert exc.value.status == 412
            assert len(store.state.list_messages()) == 2

    asyncio.run(scenario())


def test_https_wrong_certificate_and_payload_limit(tmp_path):
    from herdr_talkback.peer_http import PeerServer, PeerClient, PeerHTTPError

    async def scenario():
        cfg, store = make_node(tmp_path, 'receiver', 'sender')
        async with PeerServer(cfg, store, SimpleNamespace(wake=lambda endpoint: None)) as server:
            peer = replace(cfg.peers['sender'], node_id='receiver', url=f'https://127.0.0.1:{server.bound_port}')
            body = envelope()
            body['body'] = 'x' * 65537
            with pytest.raises(PeerHTTPError) as exc:
                await PeerClient('sender', peer).request('POST', '/v1/messages', body)
            assert exc.value.status == 400
            other, _ = make_node(tmp_path, 'other', 'sender')
            import aiohttp
            with pytest.raises(aiohttp.ClientConnectorCertificateError):
                await PeerClient('sender', replace(peer, ca_file=other.network.cert_file)).request('GET', '/v1/info')

    asyncio.run(scenario())


def test_whoami_registration_is_atomic_idempotent_and_authenticated(tmp_path):
    from herdr_talkback.operator_controls import OperatorStore
    cfg, store = make_node(tmp_path, 'receiver', 'sender')
    cfg.aliases = {}
    controls = OperatorStore(store.state)
    request = envelope('msg_identity')
    request['metadata'] = {'talkback_whoami': 1}
    created, receipt = store.receive('sender', request, registration_config=cfg, local_names=set())
    assert created and controls.pane('agent') == 'sender/agent'
    assert store.receive('sender', request, registration_config=cfg, local_names=set())[0] is False
    another = envelope('msg_conflict')
    another['source_endpoint'] = 'sender/lead'
    another['metadata'] = {'talkback_whoami': 1}
    controls.register_pane('lead', 'sender/other', cfg)
    with pytest.raises(ValueError):
        store.receive('sender', another, registration_config=cfg, local_names=set())
    with pytest.raises(KeyError):
        store.state.get_message('msg_conflict')


def test_https_whoami_admission_registers_sender_and_enqueues_message(tmp_path):
    from herdr_talkback.peer_http import PeerServer, PeerClient
    from herdr_talkback.operator_controls import OperatorStore
    async def scenario():
        cfg, store = make_node(tmp_path, 'receiver', 'sender')
        cfg.aliases = {}
        controls = OperatorStore(store.state)
        class Registry:
            async def refresh(self): pass
            def snapshot(self): return {'agent': object()}
        # Use a different source name from the local recipient.
        payload = envelope('msg_intro')
        payload['source_endpoint'] = 'sender/monitor'
        payload['metadata'] = {'talkback_whoami': 1}
        wakes = []
        async with PeerServer(cfg, store, SimpleNamespace(wake=wakes.append), Registry()) as server:
            peer = replace(cfg.peers['sender'], node_id='receiver', url=f'https://127.0.0.1:{server.bound_port}')
            client = PeerClient('sender', peer)
            assert 'whoami_registration' in (await client.request('GET', '/v1/info'))['capabilities']
            result = await client.request('POST', '/v1/messages', payload)
            assert result['state'] == 'queued'
            assert controls.pane('monitor') == 'sender/monitor'
            assert wakes == ['receiver/agent']
    asyncio.run(scenario())
