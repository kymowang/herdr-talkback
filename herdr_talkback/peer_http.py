"""Versioned, authenticated short HTTPS requests for peer messages."""
import asyncio
import hmac
import json
import re

import aiohttp
from aiohttp import web

from .peer_state import PeerCapacity, PeerConflict, REQUEST_LIMIT, PROTOCOL_VERSION, validate_envelope


class PeerHTTPError(RuntimeError):
    def __init__(self, status, retry_after=0):
        self.status = status
        self.retry_after = retry_after
        self.retryable = status in (408, 429) or status >= 500
        super().__init__(f'peer HTTP status {status}')


class PeerProtocolError(ValueError):
    pass


class PeerServer:
    def __init__(self, config, store, dispatcher):
        self.config, self.store, self.dispatcher = config, store, dispatcher
        self.tokens = {node: peer.token() for node, peer in config.peers.items()}
        self.active = 0
        self.runner = None
        self.bound_port = None
        self.closed = asyncio.Event()

    async def __aenter__(self):
        app = web.Application(client_max_size=REQUEST_LIMIT)
        app.router.add_route('*', '/{tail:.*}', self._handle)
        self.runner = web.AppRunner(app, access_log=None, keepalive_timeout=0,
                                    max_line_size=4096, max_field_size=4096)
        try:
            await self.runner.setup()
            site = web.TCPSite(self.runner, self.config.network.listen_host,
                              self.config.network.listen_port,
                              ssl_context=self.config.network.server_context())
            await site.start()
            self.bound_port = site._server.sockets[0].getsockname()[1]
        except BaseException:
            await self.runner.cleanup()
            raise
        return self

    async def __aexit__(self, *args):
        self.closed.set()
        if self.runner:
            await self.runner.cleanup()

    async def _handle(self, request):
        peer = request.headers.get('X-Talkback-Node', '')
        token = self.tokens.get(peer)
        supplied = request.headers.get('Authorization', '')
        if token is None or not hmac.compare_digest(supplied.encode('utf-8'), ('Bearer ' + token).encode('ascii')):
            response = web.json_response({'error': 'unauthorized'}, status=401)
        elif self.active >= 16:
            response = web.json_response({'error': 'receiver busy'}, status=503, headers={'Retry-After': '1'})
        else:
            self.active += 1
            try:
                response = await asyncio.wait_for(self._authorized(request, peer), timeout=10)
            except asyncio.TimeoutError:
                response = web.json_response({'error': 'request timed out'}, status=408)
            except web.HTTPRequestEntityTooLarge:
                response = web.json_response({'error': 'request too large'}, status=413)
            except (ValueError, UnicodeError, TypeError, RecursionError):
                response = web.json_response({'error': 'invalid message envelope'}, status=400)
            finally:
                self.active -= 1
        response.force_close()
        return response

    async def _authorized(self, request, peer):
        if request.method == 'GET' and request.path == '/v1/info':
            return web.json_response(dict(protocol_version=1, node_id=self.config.core_node_id,
                                          instance_id=self.store.instance_id))
        if request.method == 'POST' and request.path == '/v1/messages':
            if request.headers.get('X-Talkback-Instance') != self.store.instance_id:
                return web.json_response({'error': 'receiver storage instance changed'}, status=412)
            payload = await request.json(loads=lambda text: json.loads(text, parse_constant=_invalid_constant))
            _, target = validate_envelope(peer, self.config.core_node_id, payload)
            if self.config.roles and target.endpoint_id not in self.config.roles:
                raise ValueError('target role is not configured')
            try:
                created, receipt = self.store.receive(peer, payload)
            except PeerConflict:
                return web.json_response({'error': 'message identity conflict'}, status=409)
            except PeerCapacity:
                return web.json_response({'error': 'receiver queue full'}, status=429, headers={'Retry-After': '30'})
            self.dispatcher.wake(target.qualified)
            self.store.state.record_event('peer_received' if created else 'peer_duplicate',
                                          {'peer': peer, 'message_id': payload['message_id']})
            return web.json_response(receipt, status=202 if created else 200)
        match = re.fullmatch(r'/v1/messages/([A-Za-z0-9_-]{1,128})', request.path)
        if request.method == 'GET' and match:
            try:
                return web.json_response(self.store.receipt(peer, match.group(1)))
            except KeyError:
                return web.json_response({'error': 'message not found'}, status=404)
        return web.json_response({'error': 'not found'}, status=404)


def _invalid_constant(value):
    raise ValueError('nonfinite JSON value')


class PeerClient:
    def __init__(self, local_node, peer):
        self.local_node, self.peer = local_node, peer
        self.token = peer.token()
        self.context = peer.client_context()
        self.limit = asyncio.Semaphore(4)
        self.instance_id = None

    async def request(self, method, path, envelope=None, *, expected_instance=None):
        if method == 'POST' and expected_instance is None and self.instance_id is None:
            await self.request('GET', '/v1/info')
        headers = {'X-Talkback-Node': self.local_node, 'Authorization': 'Bearer ' + self.token}
        if method == 'POST':
            headers['X-Talkback-Instance'] = expected_instance or self.instance_id
        async with self.limit:
            connector = aiohttp.TCPConnector(ssl=self.context, force_close=True)
            timeout = aiohttp.ClientTimeout(total=10, connect=3)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout, trust_env=False,
                    json_serialize=lambda value: json.dumps(value, ensure_ascii=False, allow_nan=False)) as session:
                async with session.request(method, self.peer.url + path, json=envelope,
                                           headers=headers, allow_redirects=False) as response:
                    if response.status not in (200, 202):
                        try:
                            retry_after = min(3600, max(0, float(response.headers.get('Retry-After', '0'))))
                        except ValueError:
                            retry_after = 0
                        raise PeerHTTPError(response.status, retry_after)
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(16384):
                        data.extend(chunk)
                        if len(data) > REQUEST_LIMIT:
                            raise PeerProtocolError('oversized peer response')
                    try:
                        result = json.loads(data, parse_constant=_invalid_constant)
                    except (ValueError, UnicodeError, RecursionError) as exc:
                        raise PeerProtocolError('invalid peer response') from exc
                    if (not isinstance(result, dict) or result.get('node_id') != self.peer.node_id
                            or type(result.get('protocol_version')) is not int
                            or result['protocol_version'] != PROTOCOL_VERSION
                            or not isinstance(result.get('instance_id'), str)):
                        raise PeerProtocolError('peer identity or protocol mismatch')
                    if method == 'GET' and path == '/v1/info':
                        self.instance_id = result['instance_id']
                    return result
