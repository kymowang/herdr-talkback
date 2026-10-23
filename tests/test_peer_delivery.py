import asyncio
from dataclasses import replace
from types import SimpleNamespace

from herdr_talkback.addressing import EndpointAddress
from herdr_talkback.core_types import CoreMessage, CoreMessageState, DeliveryLease
from herdr_talkback.endpoints import EndpointRegistry
from test_peer_http import make_node


def message():
    return CoreMessage('msg_delivery', 'sender/agent', 'receiver/agent', 'hello',
                       '[agent->receiver/agent] hello', CoreMessageState.OFFERED, 'msg_delivery')


def test_peer_adapter_tracks_remote_delivery_without_duplicate_input(tmp_path):
    from herdr_talkback.adapters.peer import PeerAdapter
    from herdr_talkback.peer_http import PeerServer

    async def scenario():
        receiver_cfg, receiver = make_node(tmp_path, 'receiver', 'sender')
        sender_cfg, sender = make_node(tmp_path, 'sender', 'receiver')
        inputs = []

        def wake(endpoint):
            if receiver.state.get_message('msg_delivery')['state'] != 'delivered':
                inputs.append(endpoint)
                receiver.state.update_message_state('msg_delivery', CoreMessageState.DELIVERED, 'Pane confirmed')

        async with PeerServer(receiver_cfg, receiver, SimpleNamespace(wake=wake)) as server:
            peer = replace(sender_cfg.peers['receiver'], url=f'https://127.0.0.1:{server.bound_port}',
                           ca_file=receiver_cfg.network.cert_file)
            sender_cfg.peers['receiver'] = peer
            adapter = PeerAdapter(sender_cfg, sender, EndpointRegistry(), retry_delay=lambda count: 0)
            adapter.ensure_endpoint(EndpointAddress('receiver', 'agent'))
            lease = DeliveryLease('msg_delivery', 'lease', 'peer', lambda reason: True)
            result = await adapter.deliver(message(), lease)
            assert result.state.value == 'delivered'
            assert sender.progress('msg_delivery')['remote_received_at']
            assert sender.progress('msg_delivery')['remote_state'] == 'delivered'
            # Simulate restart/replay after a lost final acknowledgement.
            result = await adapter.deliver(message(), DeliveryLease('msg_delivery', 'new', 'peer', lambda reason: True))
            assert result.state.value == 'delivered'
            assert inputs == ['receiver/agent']

    asyncio.run(scenario())


def test_peer_adapter_does_not_replay_after_receiver_reset(tmp_path):
    from herdr_talkback.adapters.peer import PeerAdapter

    async def scenario():
        cfg, store = make_node(tmp_path, 'sender', 'receiver')
        store.pin_instance('receiver', 'old')
        adapter = PeerAdapter(cfg, store, EndpointRegistry())

        class ResetClient:
            async def request(self, method, path, envelope=None):
                assert method == 'GET' and path == '/v1/info'
                return {'instance_id': 'new'}

        adapter.clients['receiver'] = ResetClient()
        result = await adapter.deliver(message(), DeliveryLease('msg_delivery', 'lease', 'peer', lambda reason: True))
        assert result.state.value == 'delivery_unknown'

    asyncio.run(scenario())


def test_retry_uses_durably_pinned_instance_not_shared_client_cache(tmp_path):
    from herdr_talkback.adapters.peer import PeerAdapter
    from herdr_talkback.peer_http import PeerHTTPError

    async def scenario():
        cfg, store = make_node(tmp_path, 'sender', 'receiver')
        adapter = PeerAdapter(cfg, store, EndpointRegistry(), retry_delay=lambda count: 0)

        class SharedClient:
            instance_id = 'old'
            attempts = 0

            async def request(self, method, path, envelope=None, *, expected_instance=None):
                if method == 'GET':
                    return {'instance_id': 'old'}
                assert expected_instance == 'old'
                self.attempts += 1
                if self.attempts == 1:
                    self.instance_id = 'replacement'  # Another lane observes reset.
                    raise PeerHTTPError(503)
                return {'instance_id': 'old', 'message_id': 'msg_delivery', 'state': 'delivered'}

        adapter.clients['receiver'] = SharedClient()
        result = await adapter.deliver(message(), DeliveryLease('msg_delivery', 'lease', 'peer', lambda reason: True))
        assert result.state.value == 'delivered'

    asyncio.run(scenario())
