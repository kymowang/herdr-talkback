"""Versioned, authenticated short HTTPS requests for peer messages."""
import asyncio
import hmac
import json
import re
import hashlib
import os

import aiohttp
from aiohttp import web

from .peer_state import PeerCapacity, PeerConflict, REQUEST_LIMIT, PROTOCOL_VERSION, validate_envelope
from . import __version__, __build__


def _has_folded_input(raw, state):
    from .input_parser import CODEX_PASTE_BLOCK_RE, ANSI_RE, _segment_after_last_prompt
    segment = _segment_after_last_prompt(raw)
    text = ANSI_RE.sub('', segment) if segment is not None else state.text_after_prompt
    return bool(CODEX_PASTE_BLOCK_RE.search(text) or '[Pasted text #' in text)


def _inspect_remote_input(raw):
    from .input_parser import inspect_input, _last_codex_prompt_is_dim, InputState
    state = inspect_input(raw, '')
    if state.kind == 'occupied' and _last_codex_prompt_is_dim(raw):
        return InputState(kind='unknown', text_after_prompt=state.text_after_prompt)
    return state


class PeerHTTPError(RuntimeError):
    def __init__(self, status, retry_after=0):
        self.status = status
        self.retry_after = retry_after
        self.retryable = status in (408, 429) or status >= 500
        super().__init__(f'peer HTTP status {status}')


class PeerProtocolError(ValueError):
    pass


class PeerServer:
    def __init__(self, config, store, dispatcher, registry=None, update_launcher=None, admission_gate=None):
        self.config, self.store, self.dispatcher = config, store, dispatcher
        self.registry = registry
        self.update_launcher = update_launcher
        self.admission_gate = admission_gate
        self.update_service_pane = os.environ.get('HERDR_PANE_ID', '')
        self.tokens = {node: peer.token() for node, peer in config.peers.items()}
        self.active = 0
        self.runner = None
        self.bound_port = None
        self.closed = asyncio.Event()
        self.control_lock = asyncio.Lock()

    async def _pane_id(self, target):
        await self.registry.refresh()
        if hasattr(self.registry, 'panes_named') and len(self.registry.panes_named(target)) > 1:
            raise ValueError('pane name is ambiguous; use a pane ID')
        try:
            return self.registry.resolve_pane(target)
        except Exception:
            info = self.registry.get_pane(target) if hasattr(self.registry, 'get_pane') else None
            if info is None:
                return None
            return info.pane_id

    async def _pane_screen(self, pane_id):
        result = await self.registry.client.request('pane.read', {
            'pane_id': pane_id, 'source': 'visible', 'format': 'ansi'})
        return result['read']['text']

    async def _pane_info(self, pane_id):
        """Return the pane record when the server exposes it."""
        try:
            result = await self.registry.client.request('pane.get', {'pane_id': pane_id})
        except Exception:  # noqa: BLE001 - older servers may not support pane.get
            return {}
        pane = result.get('pane') if isinstance(result, dict) else None
        return pane if isinstance(pane, dict) else {}

    def _control_response(self, request_id, state):
        return {'protocol_version': 1, 'node_id': self.config.core_node_id,
                'instance_id': self.store.instance_id, 'request_id': request_id, 'state': state}

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
        if request.method == 'GET' and request.path == '/v1/ping':
            # Fixed, credential-free liveness probe for connectivity triage.
            # It never exposes message bodies, tokens or certificates.
            response = web.json_response({
                'ping': True,
                'protocol_version': 1,
                'node_id': self.config.core_node_id,
                'instance_id': self.store.instance_id,
                'version': __version__,
                'build': __build__,
            })
            response.force_close()
            return response
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
        guarded = request.path == '/v1/messages' or bool(re.fullmatch(r'/v1/panes/[A-Za-z0-9._:-]{1,128}/submit', request.path))
        if request.method == 'POST' and guarded and self.admission_gate is not None:
            async with self.admission_gate.lock:
                if self.admission_gate.paused:
                    return web.json_response({'error': 'receiver is updating'}, status=503, headers={'Retry-After': '5'})
                return await self._authorized_unlocked(request, peer)
        return await self._authorized_unlocked(request, peer)

    async def _authorized_unlocked(self, request, peer):
        if request.method == 'GET' and request.path == '/v1/info':
            return web.json_response(dict(protocol_version=1, node_id=self.config.core_node_id,
                                          instance_id=self.store.instance_id,
                                          version=__version__, build=__build__,
                                          capabilities=(['whoami_registration', 'peer_pane_control'] if self.registry is not None else []) + (['peer_install_update'] if self.update_launcher is not None else [])))
        control_status = re.fullmatch(r'/v1/control/requests/([A-Za-z0-9_-]{1,128})', request.path)
        if request.method == 'GET' and control_status:
            if not getattr(self.config.peers[peer], 'allow_remote_control', False):
                return web.json_response({'error': 'remote control not enabled for peer'}, status=403)
            control = self.store.control_state(peer, control_status.group(1))
            if control is None:
                return web.json_response({'error': 'control request not found'}, status=404)
            return web.json_response(self._control_response(control_status.group(1), control['state']))
        update_status = re.fullmatch(r'/v1/install/updates/([A-Za-z0-9_-]{1,128})', request.path)
        if request.method == 'GET' and update_status:
            if not getattr(self.config.peers[peer], 'allow_remote_control', False):
                return web.json_response({'error': 'remote control not enabled for peer'}, status=403)
            job = self.store.update_job(peer, update_status.group(1))
            if not job:
                return web.json_response({'error': 'update request not found'}, status=404)
            return web.json_response({**self._control_response(job['request_id'], job['state']), 'detail': job['detail']})
        if request.method == 'POST' and request.path == '/v1/install/update':
            if not getattr(self.config.peers[peer], 'allow_remote_control', False):
                return web.json_response({'error': 'remote control not enabled for peer'}, status=403)
            if self.update_launcher is None:
                return web.json_response({'error': 'installation update unavailable'}, status=503)
            if request.headers.get('X-Talkback-Instance') != self.store.instance_id:
                return web.json_response({'error': 'receiver storage instance changed'}, status=412)
            payload = await request.json(loads=lambda text: json.loads(text, parse_constant=_invalid_constant))
            if (not isinstance(payload, dict) or set(payload) != {'request_id'}
                or not isinstance(payload['request_id'], str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', payload['request_id'])):
                raise ValueError('invalid update request')
            async with self.control_lock:
                request_id = payload['request_id']
                try:
                    created = self.store.start_update(peer, request_id, self.update_service_pane)
                except PeerConflict:
                    return web.json_response({'error': 'another update is in progress'}, status=409)
                if created:
                    self.store.state.record_event('peer_update_requested', {'peer': peer, 'request_id': request_id})
                    try:
                        await asyncio.to_thread(self.update_launcher, peer, request_id)
                    except Exception:
                        self.store.fail_queued_update(peer, request_id, 'could not start update worker')
                job = self.store.update_job(peer, request_id)
                return web.json_response({**self._control_response(request_id, job['state']), 'detail': job['detail']})
        pane_input = re.fullmatch(r'/v1/panes/([A-Za-z0-9._:-]{1,128})/input', request.path)
        if request.method == 'GET' and pane_input and self.registry is not None:
            from .input_parser import ANSI_RE, _segment_after_last_prompt
            if not getattr(self.config.peers[peer], 'allow_remote_control', False):
                return web.json_response({'error': 'remote control not enabled for peer'}, status=403)
            target = pane_input.group(1)
            pane_id = await self._pane_id(target)
            if pane_id is None:
                return web.json_response({'error': 'pane not found'}, status=404)
            raw = await self._pane_screen(pane_id)
            state = _inspect_remote_input(raw)
            pane = await self._pane_info(pane_id)
            scroll = pane.get('scroll')
            if not isinstance(scroll, dict):
                scroll = None
            agent_status = pane.get('agent_status')
            if not isinstance(agent_status, str):
                agent_status = None
            if scroll and scroll.get('offset_from_bottom'):
                # The viewport is scrolled away from the live composer, so the
                # visible screen cannot tell us what the input box holds. A dim
                # transcript echo there must not be reported as an empty input.
                from .input_parser import InputState
                state = InputState(kind='unknown', text_after_prompt='')
            segment = _segment_after_last_prompt(raw)
            excerpt = ANSI_RE.sub('', segment).replace('\r', '') if segment is not None else state.text_after_prompt
            return web.json_response({'protocol_version': 1, 'node_id': self.config.core_node_id,
                'instance_id': self.store.instance_id,
                'input': {'pane_id': pane_id, 'kind': state.kind, 'text': state.text_after_prompt[:4000],
                          'visible_excerpt': excerpt[:4000], 'excerpt_truncated': len(excerpt) > 4000,
                          'folded': _has_folded_input(raw, state), 'scroll': scroll,
                          'agent_status': agent_status,
                          'screen_sha256': hashlib.sha256(raw.encode('utf-8')).hexdigest()}})
        submit = re.fullmatch(r'/v1/panes/([A-Za-z0-9._:-]{1,128})/submit', request.path)
        if request.method == 'POST' and submit and self.registry is not None:
            if not getattr(self.config.peers[peer], 'allow_remote_control', False):
                return web.json_response({'error': 'remote control not enabled for peer'}, status=403)
            if request.headers.get('X-Talkback-Instance') != self.store.instance_id:
                return web.json_response({'error': 'receiver storage instance changed'}, status=412)
            payload = await request.json(loads=lambda text: json.loads(text, parse_constant=_invalid_constant))
            if (not isinstance(payload, dict) or not {'pane_id', 'screen_sha256', 'request_id'} <= set(payload)
                or set(payload) - {'pane_id', 'screen_sha256', 'request_id', 'allow_folded'}
                or ('allow_folded' in payload and type(payload['allow_folded']) is not bool)
                or not isinstance(payload['pane_id'], str) or not payload['pane_id']
                or not isinstance(payload['screen_sha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', payload['screen_sha256'])
                or not isinstance(payload['request_id'], str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', payload['request_id'])):
                raise ValueError('invalid submit request')
            async with self.control_lock:
                old = self.store.control_state(peer, payload['request_id'])
                if old:
                    if old['pane_id'] != payload['pane_id'] or old['screen_sha256'] != payload['screen_sha256']:
                        return web.json_response({'error': 'request identity conflict'}, status=409)
                    return web.json_response(self._control_response(payload['request_id'], old['state']))
                pane_id = await self._pane_id(submit.group(1))
                if pane_id is None:
                    return web.json_response({'error': 'pane not found'}, status=404)
                if pane_id != payload['pane_id']:
                    return web.json_response({'error': 'pane identity changed'}, status=409)
                screen = await self._pane_screen(pane_id)
                input_state = _inspect_remote_input(screen)
                if (hashlib.sha256(screen.encode('utf-8')).hexdigest() != payload['screen_sha256']
                    or input_state.kind != 'occupied'):
                    return web.json_response({'error': 'screen changed or input not occupied'}, status=409)
                if _has_folded_input(screen, input_state) and not payload.get('allow_folded', True):
                    return web.json_response({'error': 'folded input cannot be fully verified'}, status=409)
                self.store.start_control(peer, payload['request_id'], pane_id, payload['screen_sha256'])
                await self.registry.client.request('pane.send_input', {'pane_id': pane_id, 'text': '', 'keys': ['enter']})
                self.store.finish_control(peer, payload['request_id'])
                self.store.state.record_event('peer_input_submitted', {'peer': peer, 'pane_id': pane_id, 'request_id': payload['request_id']})
                return web.json_response(self._control_response(payload['request_id'], 'submitted'))
        if request.method == 'POST' and request.path == '/v1/messages':
            if request.headers.get('X-Talkback-Instance') != self.store.instance_id:
                return web.json_response({'error': 'receiver storage instance changed'}, status=412)
            payload = await request.json(loads=lambda text: json.loads(text, parse_constant=_invalid_constant))
            _, target = validate_envelope(peer, self.config.core_node_id, payload)
            if self.config.roles and target.endpoint_id not in self.config.roles:
                raise ValueError('target role is not configured')
            try:
                if payload['metadata'].get('talkback_whoami') == 1:
                    if self.registry is None:
                        raise ValueError('whoami registry unavailable')
                    await self.registry.refresh()
                    created, receipt = self.store.receive(peer, payload, registration_config=self.config,
                                                          local_names=set(self.registry.snapshot()))
                else:
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
