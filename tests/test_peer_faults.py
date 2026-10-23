import pytest
pytest.importorskip("aiohttp")
pytest.importorskip("cryptography")

import asyncio
from dataclasses import replace
from types import SimpleNamespace

from herdr_talkback.addressing import EndpointAddress
from herdr_talkback.core_dispatcher import CoreDispatcher
from herdr_talkback.core_types import CoreMessage, CoreMessageState, DeliveryLease
from herdr_talkback.endpoints import EndpointRegistry
from herdr_talkback.adapters.peer import PeerAdapter
from herdr_talkback.peer_http import PeerServer
from test_peer_http import make_node


def test_both_directions_and_lost_acceptance_response(tmp_path):
    async def scenario():
        a_cfg, a = make_node(tmp_path, 'a', 'b')
        b_cfg, b = make_node(tmp_path, 'b', 'a')
        delivered = []

        def wake(store, message_id):
            def receive(endpoint):
                row = store.state.get_message(message_id)
                if row['state'] != 'delivered':
                    delivered.append(endpoint)
                    store.state.update_message_state(message_id, CoreMessageState.DELIVERED, 'confirmed')
            return SimpleNamespace(wake=receive)

        async with PeerServer(a_cfg, a, wake(a, 'msg_ba')) as a_server, PeerServer(b_cfg, b, wake(b, 'msg_ab')) as b_server:
            a_cfg.peers['b'] = replace(a_cfg.peers['b'], url=f'https://127.0.0.1:{b_server.bound_port}', ca_file=b_cfg.network.cert_file)
            b_cfg.peers['a'] = replace(b_cfg.peers['a'], url=f'https://127.0.0.1:{a_server.bound_port}', ca_file=a_cfg.network.cert_file)
            ab = PeerAdapter(a_cfg, a, EndpointRegistry(), retry_delay=lambda count: 0)
            ba = PeerAdapter(b_cfg, b, EndpointRegistry(), retry_delay=lambda count: 0)
            real = ab.clients['b']

            class LoseAck:
                lost = False

                async def request(self, method, path, envelope=None, **kwargs):
                    receipt = await real.request(method, path, envelope, **kwargs)
                    if method == 'POST' and not self.lost:
                        self.lost = True
                        raise ConnectionResetError('ack lost after commit')
                    return receipt

            ab.clients['b'] = LoseAck()
            for adapter, origin, target, mid in ((ab, 'a', 'b', 'msg_ab'), (ba, 'b', 'a', 'msg_ba')):
                message = CoreMessage(mid, origin + '/agent', target + '/agent', 'hello', 'hello', CoreMessageState.OFFERED, mid)
                result = await asyncio.wait_for(adapter.deliver(message, DeliveryLease(mid, 'lease', 'peer', lambda reason: True)), 3)
                assert result.state.value == 'delivered'
            assert delivered == ['b/agent', 'a/agent']
            assert len(b.state.list_messages()) == 1

    asyncio.run(scenario())


def test_sender_restart_queries_receipt_without_another_remote_enqueue(tmp_path):
    async def scenario():
        a_cfg, a = make_node(tmp_path, 'a', 'b')
        b_cfg, b = make_node(tmp_path, 'b', 'a')
        wakes = []
        async with PeerServer(b_cfg, b, SimpleNamespace(wake=wakes.append)) as server:
            a_cfg.peers['b'] = replace(a_cfg.peers['b'], url=f'https://127.0.0.1:{server.bound_port}', ca_file=b_cfg.network.cert_file)
            registry = EndpointRegistry()
            adapter = PeerAdapter(a_cfg, a, registry, retry_delay=lambda count: 0.1)
            adapter.ensure_endpoint(EndpointAddress('b', 'agent'))
            message = CoreMessage('msg_restart', 'a/agent', 'b/agent', 'hello', 'hello', CoreMessageState.QUEUED, 'msg_restart')
            a.state.enqueue_message(message)
            dispatcher = CoreDispatcher(a.state, registry, {'peer': adapter})
            dispatcher.wake('b/agent')
            for _ in range(100):
                progress = a.progress('msg_restart')
                if progress and progress['remote_received_at']:
                    break
                await asyncio.sleep(0.01)
            assert progress['remote_received_at']
            await dispatcher.close()
            assert a.state.get_message('msg_restart')['state'] == 'queued'
            b.state.update_message_state('msg_restart', CoreMessageState.DELIVERED, 'confirmed while sender offline')
            from herdr_talkback.peer_state import PeerStore
            restarted = PeerAdapter(a_cfg, PeerStore(a.state), registry, retry_delay=lambda count: 0)
            dispatcher = CoreDispatcher(a.state, registry, {'peer': restarted})
            await dispatcher.recover()
            await asyncio.wait_for(dispatcher.drain(), 3)
            assert a.state.get_message('msg_restart')['state'] == 'delivered'
            assert wakes == ['b/agent']
            await dispatcher.close()

    asyncio.run(scenario())
