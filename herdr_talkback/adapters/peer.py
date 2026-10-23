"""Recoverable peer delivery: durable identity plus short POST/status requests."""
import asyncio
import random
import ssl
import time

import aiohttp

from ..core_types import AdapterDeliveryResult, AdapterDeliveryState, EndpointDescriptor
from ..peer_http import PeerClient, PeerHTTPError, PeerProtocolError
from ..peer_state import PeerInstanceChanged, TERMINAL, message_envelope


def retry_delay(count):
    return (1, 2, 5, 10, 30, 60)[min(max(count - 1, 0), 5)] * random.uniform(0.8, 1.0)


class PeerAdapter:
    name = 'peer'

    def __init__(self, config, store, registry, retry_delay=retry_delay):
        self.config, self.store, self.registry = config, store, registry
        self.clients = {node: PeerClient(config.core_node_id, peer) for node, peer in config.peers.items()}
        self.retry_delay = retry_delay

    def ensure_endpoint(self, address):
        if address.node_id not in self.clients:
            raise ValueError('remote node is not configured')
        existing = [item for item in self.registry.list_node(address.node_id) if item.address != address]
        descriptor = EndpointDescriptor(address, self.name, address.endpoint_id, 'online', ('message',))
        self.registry.register(address.node_id, existing + [descriptor])

    async def deliver(self, message, lease):
        peer_id = message.target_endpoint.partition('/')[0]
        if not lease.accept('peer adapter owns durable network delivery'):
            return AdapterDeliveryResult(AdapterDeliveryState.DELIVERY_UNKNOWN, 'peer lease not accepted', 0)
        client = self.clients[peer_id]
        progress = self.store.progress(message.message_id, peer_id)
        failures = polls = 0
        need_info = True
        envelope = message_envelope(message)
        while True:
            wait = max(0, progress['next_attempt_at'] - time.time())
            if wait:
                await asyncio.sleep(wait)
            attempt = progress['attempts'] + 1
            progress = self.store.progress(message.message_id, attempts=attempt, next_attempt_at=0)
            try:
                if need_info:
                    info = await client.request('GET', '/v1/info')
                    self.store.pin_instance(peer_id, info['instance_id'])
                    expected_instance = info['instance_id']
                    need_info = False
                if progress['remote_received_at']:
                    try:
                        receipt = await client.request('GET', '/v1/messages/' + message.message_id)
                    except PeerHTTPError as exc:
                        if exc.status != 404:
                            raise
                        # Storage identity is pinned before any possible resubmission.
                        info = await client.request('GET', '/v1/info')
                        self.store.pin_instance(peer_id, info['instance_id'])
                        expected_instance = info['instance_id']
                        receipt = await client.request('POST', '/v1/messages', envelope, expected_instance=expected_instance)
                else:
                    receipt = await client.request('POST', '/v1/messages', envelope, expected_instance=expected_instance)
                self.store.pin_instance(peer_id, receipt['instance_id'])
                state = receipt.get('state')
                if (receipt.get('message_id') != message.message_id or not isinstance(state, str)
                        or state not in {'queued', 'waiting_endpoint', 'offered', 'accepted'} | TERMINAL):
                    raise PeerProtocolError('unexpected remote message receipt')
                progress = self.store.progress(message.message_id, remote_state=state,
                    remote_received_at=progress['remote_received_at'] or time.time(), last_error='')
                failures = 0
                if state in TERMINAL:
                    self.store.state.record_event('peer_receipt', {'peer': peer_id,
                        'message_id': message.message_id, 'state': state})
                    return AdapterDeliveryResult(AdapterDeliveryState(state), str(receipt.get('reason', ''))[:1024], attempt)
                polls += 1
                delay = self.retry_delay(polls)
            except PeerInstanceChanged:
                return AdapterDeliveryResult(AdapterDeliveryState.DELIVERY_UNKNOWN,
                    'receiver storage instance changed; automatic replay stopped', attempt)
            except (aiohttp.ClientConnectorCertificateError, aiohttp.ClientConnectorSSLError, ssl.SSLError,
                    PeerProtocolError) as exc:
                self.store.progress(message.message_id, last_error=type(exc).__name__)
                return AdapterDeliveryResult(AdapterDeliveryState.FAILED, 'peer certificate or protocol validation failed', attempt)
            except PeerHTTPError as exc:
                if exc.status == 412:
                    return AdapterDeliveryResult(AdapterDeliveryState.DELIVERY_UNKNOWN,
                        'receiver storage instance changed; automatic replay stopped', attempt)
                if not exc.retryable:
                    self.store.progress(message.message_id, last_error=str(exc))
                    return AdapterDeliveryResult(AdapterDeliveryState.FAILED, str(exc), attempt)
                failures += 1
                delay = max(self.retry_delay(failures), exc.retry_after)
                progress = self.store.progress(message.message_id, last_error=str(exc))
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
                failures += 1
                delay = self.retry_delay(failures)
                need_info = True
                progress = self.store.progress(message.message_id, last_error='peer connection unavailable')
            progress = self.store.progress(message.message_id, next_attempt_at=time.time() + delay)
            self.store.state.record_event('peer_pending', {'peer': peer_id, 'message_id': message.message_id,
                'remote_state': progress['remote_state'], 'next_attempt_at': progress['next_attempt_at'],
                'error': progress['last_error']})
