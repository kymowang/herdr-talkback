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
            assert info['version'] and info['build'].startswith('sha256:')
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


def test_peer_can_inspect_visible_local_pane_input(tmp_path):
    from herdr_talkback.peer_http import PeerServer, PeerClient
    async def scenario():
        cfg, store = make_node(tmp_path, 'receiver', 'sender')
        cfg.peers['sender'] = replace(cfg.peers['sender'], allow_remote_control=True)
        requests = []
        class Client:
            async def request(self, method, params):
                requests.append((method, params))
                return {'read': {'text': '\u203a waiting for instructions'}}
        class Registry:
            client = Client()
            async def refresh(self): pass
            def resolve_pane(self, name):
                assert name == 'worker'
                return 'pane-1'
        async with PeerServer(cfg, store, SimpleNamespace(wake=lambda e: None), Registry()) as server:
            peer = replace(cfg.peers['sender'], node_id='receiver', url=f'https://127.0.0.1:{server.bound_port}')
            result = await PeerClient('sender', peer).request('GET', '/v1/panes/worker/input')
            assert result['input']['kind'] == 'occupied'
            assert result['input']['text'] == 'waiting for instructions'
            assert [entry for entry in requests if entry[0] == 'pane.read'] == [
                ('pane.read', {'pane_id': 'pane-1', 'source': 'visible', 'format': 'ansi'})]
            assert requests[-1] == ('pane.get', {'pane_id': 'pane-1'})
    asyncio.run(scenario())


def test_peer_latest_scrolls_to_live_input_only_when_safe(tmp_path):
    from herdr_talkback.peer_http import PeerServer, PeerClient, PeerHTTPError
    async def scenario():
        cfg, store = make_node(tmp_path, 'receiver', 'sender')
        cfg.peers['sender'] = replace(cfg.peers['sender'], allow_remote_control=True)
        class Client:
            status = 'done'
            screen = '\x1b[2m\u203a earlier message\x1b[0m'
            sends = []
            async def request(self, method, params):
                if method == 'pane.read': return {'read': {'text': self.screen}}
                if method == 'pane.get':
                    return {'pane': {'pane_id': 'pane-1', 'agent_status': self.status,
                                     'scroll': {'offset_from_bottom': 0}}}
                if method == 'pane.send_input':
                    self.sends.append(params)
                    self.screen = '\u203a \x1b[2mAsk Codex to do anything\x1b[0m'
                    return {'type': 'ok'}
                raise AssertionError(method)
        class Registry:
            client = Client()
            async def refresh(self): pass
            def resolve_pane(self, name): return 'pane-1'
        registry = Registry()
        async with PeerServer(cfg, store, SimpleNamespace(wake=lambda e: None), registry) as server:
            peer = replace(cfg.peers['sender'], node_id='receiver', url=f'https://127.0.0.1:{server.bound_port}')
            client = PeerClient('sender', peer)
            result = await client.request('POST', '/v1/panes/worker/latest', {})
            assert result['recovered'] is True
            assert result['key'] == 'esc'
            assert result['input']['kind'] == 'empty'
            assert registry.client.sends == [{'pane_id': 'pane-1', 'text': '', 'keys': ['esc']}]

            registry.client.status = 'working'
            registry.client.screen = '\u203a user draft'
            with pytest.raises(PeerHTTPError) as exc:
                await client.request('POST', '/v1/panes/worker/latest', {})
            assert exc.value.status == 409 and len(registry.client.sends) == 1

            registry.client.status = 'done'
            with pytest.raises(PeerHTTPError) as exc:
                await client.request('POST', '/v1/panes/worker/latest', {})
            assert exc.value.status == 409 and len(registry.client.sends) == 1

            result = await client.request('POST', '/v1/panes/worker/latest', {'key': 'end'})
            assert result['key'] == 'end'
            assert registry.client.sends[-1] == {'pane_id': 'pane-1', 'text': '', 'keys': ['end']}

            with pytest.raises(PeerHTTPError) as exc:
                await client.request('POST', '/v1/panes/worker/latest', {'key': 'tab'})
            assert exc.value.status == 400
    asyncio.run(scenario())


def test_peer_input_reports_unknown_when_viewport_is_scrolled(tmp_path):
    from herdr_talkback.peer_http import PeerServer, PeerClient
    async def scenario():
        cfg, store = make_node(tmp_path, 'receiver', 'sender')
        cfg.peers['sender'] = replace(cfg.peers['sender'], allow_remote_control=True)
        class Client:
            async def request(self, method, params):
                if method == 'pane.read':
                    return {'read': {'text': '\x1b[2m\u203a previous submitted message\x1b[0m'}}
                if method == 'pane.get':
                    return {'pane': {'pane_id': 'pane-1', 'agent_status': 'working', 'scroll': {
                        'offset_from_bottom': 12, 'max_offset_from_bottom': 240,
                        'viewport_rows': 30}}}
                raise AssertionError(method)
        class Registry:
            client = Client()
            async def refresh(self): pass
            def resolve_pane(self, name): return 'pane-1'
        async with PeerServer(cfg, store, SimpleNamespace(wake=lambda e: None), Registry()) as server:
            peer = replace(cfg.peers['sender'], node_id='receiver', url=f'https://127.0.0.1:{server.bound_port}')
            result = await PeerClient('sender', peer).request('GET', '/v1/panes/worker/input')
            assert result['input']['kind'] == 'unknown'
            assert result['input']['text'] == ''
            assert result['input']['scroll']['offset_from_bottom'] == 12
            assert result['input']['agent_status'] == 'working'
    asyncio.run(scenario())


def test_peer_submit_requires_matching_snapshot_and_is_idempotent(tmp_path):
    from herdr_talkback.peer_http import PeerServer, PeerClient, PeerHTTPError
    async def scenario():
        cfg, store = make_node(tmp_path, 'receiver', 'sender')
        cfg.peers['sender'] = replace(cfg.peers['sender'], allow_remote_control=True)
        class Client:
            screen = '\u203a user draft'
            sends = 0
            async def request(self, method, params):
                if method == 'pane.read': return {'read': {'text': self.screen}}
                assert method == 'pane.send_input' and params == {'pane_id':'pane-1', 'text':'', 'keys':['enter']}
                self.sends += 1
                return {'type':'ok'}
        client = Client()
        class Registry:
            async def refresh(self): pass
            def resolve_pane(self, name): return 'pane-1'
        registry = Registry(); registry.client = client
        async with PeerServer(cfg, store, SimpleNamespace(wake=lambda e: None), registry) as server:
            peer = replace(cfg.peers['sender'], node_id='receiver', url=f'https://127.0.0.1:{server.bound_port}')
            remote = PeerClient('sender', peer)
            snap = await remote.request('GET', '/v1/panes/worker/input')
            args = {'pane_id': snap['input']['pane_id'], 'screen_sha256': snap['input']['screen_sha256'], 'request_id':'req_123'}
            client.screen = '\u203a changed draft'
            with pytest.raises(PeerHTTPError) as exc:
                await remote.request('POST', '/v1/panes/worker/submit', args)
            assert exc.value.status == 409 and client.sends == 0
            client.screen = '\u203a user draft'
            result = await remote.request('POST', '/v1/panes/worker/submit', args)
            assert result['state'] == 'submitted' and client.sends == 1
            registry.resolve_pane = lambda name: (_ for _ in ()).throw(LookupError('pane closed'))
            again = await remote.request('POST', '/v1/panes/worker/submit', args)
            assert again['state'] == 'submitted' and client.sends == 1
    asyncio.run(scenario())


def test_peer_input_control_requires_explicit_permission(tmp_path):
    from herdr_talkback.peer_http import PeerServer, PeerClient, PeerHTTPError
    async def scenario():
        cfg, store = make_node(tmp_path, 'receiver', 'sender')
        async with PeerServer(cfg, store, SimpleNamespace(wake=lambda e: None), SimpleNamespace()) as server:
            peer = replace(cfg.peers['sender'], node_id='receiver', url=f'https://127.0.0.1:{server.bound_port}')
            with pytest.raises(PeerHTTPError) as exc:
                await PeerClient('sender', peer).request('GET', '/v1/panes/worker/input')
            assert exc.value.status == 403
    asyncio.run(scenario())


def test_peer_update_is_authorized_and_repeated_request_does_not_start_twice(tmp_path):
    from herdr_talkback.peer_http import PeerServer, PeerClient
    async def scenario():
        cfg, store = make_node(tmp_path, 'receiver', 'sender')
        cfg.peers['sender'] = replace(cfg.peers['sender'], allow_remote_control=True)
        launches = []
        def launch(peer, request_id): launches.append((peer, request_id))
        async with PeerServer(cfg, store, SimpleNamespace(wake=lambda e: None), update_launcher=launch) as server:
            peer = replace(cfg.peers['sender'], node_id='receiver', url=f'https://127.0.0.1:{server.bound_port}')
            client = PeerClient('sender', peer)
            first = await client.request('POST', '/v1/install/update', {'request_id':'update_123'})
            second = await client.request('POST', '/v1/install/update', {'request_id':'update_123'})
            status = await client.request('GET', '/v1/install/updates/update_123')
            assert first['state'] == second['state'] == status['state'] == 'queued'
            assert launches == [('sender','update_123')]
    asyncio.run(scenario())


def test_peer_submit_folded_input_allowed_by_default_and_can_be_rejected(tmp_path):
    from herdr_talkback.peer_http import PeerServer, PeerClient, PeerHTTPError
    async def scenario():
        cfg, store = make_node(tmp_path, 'receiver', 'sender')
        cfg.peers['sender'] = replace(cfg.peers['sender'], allow_remote_control=True)
        class Client:
            sends = 0
            async def request(self, method, params):
                if method == 'pane.read': return {'read': {'text': '\u203a [Pasted Content 1099 chars]'}}
                if method == 'pane.get': return {'pane': {}}
                self.sends += 1; return {'type':'ok'}
        class Registry:
            client = Client()
            async def refresh(self): pass
            def resolve_pane(self, name): return 'pane-1'
        registry = Registry()
        async with PeerServer(cfg, store, SimpleNamespace(wake=lambda e: None), registry) as server:
            peer = replace(cfg.peers['sender'], node_id='receiver', url=f'https://127.0.0.1:{server.bound_port}')
            client = PeerClient('sender', peer)
            snapshot = (await client.request('GET','/v1/panes/worker/input'))['input']
            assert snapshot['folded']
            payload = {'pane_id':'pane-1','screen_sha256':snapshot['screen_sha256'],'request_id':'folded_1','allow_folded':False}
            with pytest.raises(PeerHTTPError) as exc:
                await client.request('POST','/v1/panes/worker/submit',payload)
            assert exc.value.status == 409 and registry.client.sends == 0
            del payload['allow_folded']
            assert (await client.request('POST','/v1/panes/worker/submit',payload))['state'] == 'submitted'
            assert registry.client.sends == 1
    asyncio.run(scenario())


def test_peer_submit_rejects_dim_codex_transcript_as_unknown(tmp_path):
    from herdr_talkback.peer_http import PeerServer, PeerClient, PeerHTTPError
    async def scenario():
        cfg, store = make_node(tmp_path,'receiver','sender')
        cfg.peers['sender'] = replace(cfg.peers['sender'],allow_remote_control=True)
        class Client:
            sends=0
            async def request(self,method,params):
                if method == 'pane.read': return {'read':{'text':'\x1b[2m\u203a already submitted\x1b[0m'}}
                if method == 'pane.get': return {'pane': {}}
                self.sends+=1; return {'type':'ok'}
        class Registry:
            client=Client()
            async def refresh(self): pass
            def resolve_pane(self,name): return 'pane-1'
        registry=Registry()
        async with PeerServer(cfg,store,SimpleNamespace(wake=lambda e:None),registry) as server:
            peer=replace(cfg.peers['sender'],node_id='receiver',url=f'https://127.0.0.1:{server.bound_port}')
            client=PeerClient('sender',peer)
            snapshot=(await client.request('GET','/v1/panes/worker/input'))['input']
            assert snapshot['kind']=='unknown'
            with pytest.raises(PeerHTTPError) as exc:
                await client.request('POST','/v1/panes/worker/submit',{'pane_id':'pane-1','screen_sha256':snapshot['screen_sha256'],'request_id':'dim_1'})
            assert exc.value.status==409 and registry.client.sends==0
    asyncio.run(scenario())


def test_peer_message_admission_pauses_during_update_and_recovers(tmp_path):
    from herdr_talkback.peer_http import PeerServer, PeerClient, PeerHTTPError
    from herdr_talkback.admission import AdmissionGate
    async def scenario():
        cfg, store = make_node(tmp_path,'receiver','sender')
        gate = AdmissionGate(paused=True); wakes=[]
        async with PeerServer(cfg,store,SimpleNamespace(wake=wakes.append),admission_gate=gate) as server:
            peer=replace(cfg.peers['sender'],node_id='receiver',url=f'https://127.0.0.1:{server.bound_port}')
            client=PeerClient('sender',peer)
            with pytest.raises(PeerHTTPError) as exc:
                await client.request('POST','/v1/messages',envelope('msg_paused'))
            assert exc.value.status == 503 and not store.state.list_messages()
            gate.paused=False
            assert (await client.request('POST','/v1/messages',envelope('msg_paused')))['state']=='queued'
    asyncio.run(scenario())
