from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Optional

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.9/3.10
    import tomli as tomllib  # type: ignore[import, no-redef]

from .addressing import validate_node_id, parse_endpoint_address, EndpointAddress
from .config import configured_path, default_config_path, default_daemon_socket_path, default_state_path, load_config
from .daemon import submit_daemon_request
from .delivery import DeliveryService
from .herdr_client import HerdrClient
from .input_parser import inspect_input
from .messages import parse_message
from .notifier import AlertingNotifier, HerdrNotifier
from .roles import RoleRegistry
from .state import TalkbackState


class JoinMessageAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, " ".join(values))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="herdr-talkback")
    from . import __version__, __build__
    parser.add_argument('--version', action='version', version=f'herdr-talkback {__version__} {__build__}')
    parser.add_argument("--config", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser('version', help='Compare CLI and running service versions')

    monitor = sub.add_parser('monitor', help='Inspect inferred pane states')
    monitor_flags = monitor.add_mutually_exclusive_group()
    monitor_flags.add_argument('--enable', dest='monitor_flag', action='store_const', const='enable')
    monitor_flags.add_argument('--disable', dest='monitor_flag', action='store_const', const='disable')
    monitor_sub = monitor.add_subparsers(dest='monitor_command')
    monitor_status = monitor_sub.add_parser('status')
    monitor_status.add_argument('--json', action='store_true')
    monitor_sub.add_parser('enable', help='Enable logical monitoring now and after restart')
    monitor_sub.add_parser('disable', help='Disable logical monitoring now and after restart')
    monitor_sub.add_parser('state', help='Show persisted monitor setting and service availability')

    peer = sub.add_parser('peer', help='Provision peer credentials')
    peer_sub = peer.add_subparsers(dest='peer_command', required=True)
    tls = peer_sub.add_parser('init-tls')
    tls.add_argument('--directory', type=Path, required=True)
    tls.add_argument('--host', action='append', required=True)
    tls.add_argument('--force', action='store_true')
    diag = peer_sub.add_parser('diag', help='Diagnose peer connectivity step by step')
    diag.add_argument('node_id')
    diag.add_argument('--json', action='store_true')
    diag.add_argument('--timeout', type=float, default=5.0)
    peer_input = peer_sub.add_parser('input', help='Inspect a peer pane input')
    peer_input.add_argument('node_id')
    peer_input.add_argument('pane')
    peer_latest = peer_sub.add_parser(
        'latest', help='Scroll a peer pane to its live input, then inspect it')
    peer_latest.add_argument('node_id')
    peer_latest.add_argument('pane')
    peer_latest.add_argument('--key', choices=('esc', 'enter'), default='enter',
                             help='Key used to leave the scrolled history view')
    peer_submit = peer_sub.add_parser('submit', help='Submit a previously inspected peer pane input')
    peer_submit.add_argument('node_id')
    peer_submit.add_argument('pane')
    peer_submit.add_argument('--pane-id', required=True)
    peer_submit.add_argument('--screen-sha256', required=True)
    peer_submit.add_argument('--request-id', help='Idempotency ID for checking or repeating a request')
    peer_submit.add_argument('--reject-folded', action='store_true', help='Refuse submit when folded text cannot be verified')
    peer_control_status = peer_sub.add_parser('submit-status', help='Check a peer submit request')
    peer_control_status.add_argument('node_id')
    peer_control_status.add_argument('request_id')
    peer_update = peer_sub.add_parser('update', help='Update a peer Talkback installation from GitHub')
    peer_update.add_argument('node_id')
    peer_update.add_argument('--request-id', help='Idempotency ID for an update request')
    peer_update_status = peer_sub.add_parser('update-status', help='Check a peer update request')
    peer_update_status.add_argument('node_id')
    peer_update_status.add_argument('request_id')
    peers = sub.add_parser('peers', help='Inspect configured network peers')
    peers_sub = peers.add_subparsers(dest='peers_command', required=True)
    peers_list = peers_sub.add_parser('list')
    peers_list.add_argument('--json', action='store_true')
    check_peer = peers_sub.add_parser('check')
    check_peer.add_argument('node_id')
    check_peer.add_argument('--json', action='store_true')
    message = sub.add_parser('message', help='Inspect durable message delivery')
    message_sub = message.add_subparsers(dest='message_command', required=True)
    message_status = message_sub.add_parser('status')
    message_status.add_argument('message_id')
    message_status.add_argument('--json', action='store_true')

    sub.add_parser("panes")
    pane = sub.add_parser("pane", help="Inspect and name Herdr panes")
    pane_sub = pane.add_subparsers(dest="pane_command", required=True)
    pane_sub.add_parser("list", help="List all panes as a table")
    register = pane_sub.add_parser('register', help='Register a name for a local or remote node/pane')
    register.add_argument('name')
    register.add_argument('address')
    pane_sub.add_parser('register-all', help='Register named local panes')
    identity = pane_sub.add_parser('identity', help='Print a portable pane identity card')
    identity.add_argument('pane')
    identity.add_argument('--url', help='Advertised HTTPS origin, including port')
    pane_import = pane_sub.add_parser('import', help='Register a manually forwarded identity card')
    pane_import.add_argument('card', help='Identity JSON')
    registered = pane_sub.add_parser('registered', help='List registered pane names')
    registered.add_argument('--json', action='store_true')
    unregister = pane_sub.add_parser('unregister', help='Remove a registered pane name')
    unregister.add_argument('name')
    name_from_tab = pane_sub.add_parser("name-from-tab", help="Set a pane's name to its Tab name")
    target = name_from_tab.add_mutually_exclusive_group(required=True)
    target.add_argument("pane_id", nargs="?")
    target.add_argument("--all", action="store_true", help="Process every pane in this Herdr session")
    name_from_tab.add_argument("--force", action="store_true", help="Overwrite existing pane names")
    status = sub.add_parser("status")
    status.add_argument("target")
    status.add_argument("--json", action="store_true")

    sub.add_parser("queue")
    alerts = sub.add_parser("alerts")
    alerts.add_argument("--limit", type=int, default=20)
    alerts.add_argument("-f", "--follow", action="store_true")
    alerts.add_argument("--poll-interval", type=float, default=1.0)
    alerts.add_argument("--json", action="store_true")
    alerts_sub = alerts.add_subparsers(dest="alerts_command")
    clear = alerts_sub.add_parser("clear")
    clear.add_argument("ids", nargs="*", type=int)

    send = sub.add_parser(
        "send",
        help="Queue a [source→target] message for a local pane or configured peer",
        description=(
            "Send '[source→target] body'. source MUST be the sender's own Herdr pane "
            "label (see 'herdr-talkback pane list'); the machine node id, $USER and the "
            "host name are not sender identities. Target may be a pane label, a registered "
            "name, an alias or node/pane."
        ),
    )
    send.add_argument(
        "message", nargs="+", action=JoinMessageAction,
        help="[source→target] body, quoted so the arrow is not shell redirection",
    )
    send.add_argument(
        "--dry-run", action="store_true",
        help="Print the resolved source, target and pane id without sending",
    )
    send.add_argument(
        "--direct", action="store_true",
        help="Diagnostics only: bypass the daemon queue (no retries, no recorded state)",
    )
    whoami = sub.add_parser('whoami', help='Register and introduce a pane to a configured peer')
    whoami.add_argument('source')
    whoami.add_argument('host')
    whoami.add_argument('target')

    exec_cmd = sub.add_parser("exec")
    exec_cmd.add_argument("targets")
    exec_cmd.add_argument("exec_command", nargs="+", action=JoinMessageAction)

    serve = sub.add_parser("serve")
    serve.add_argument("--dry-run", action="store_true")

    tail = sub.add_parser("tail")
    tail.add_argument("--limit", type=int, default=50)

    retry = sub.add_parser("retry")
    retry.add_argument("message_id")

    input_cmd = sub.add_parser("input")
    input_cmd.add_argument("target")
    input_cmd.add_argument("--expected", default="")
    input_cmd.add_argument("--lines", type=int, default=100)
    input_cmd.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == 'monitor':
        if args.monitor_flag and args.monitor_command:
            parser.error('use either a monitor subcommand or a monitor flag')
        args.monitor_command = args.monitor_command or args.monitor_flag
        if not args.monitor_command:
            parser.error('monitor requires status, state, enable, or disable')
        if args.config is None:
            args.config = _discover_plugin_config()
    elif args.command == 'version' and args.config is None:
        args.config = _discover_plugin_config()
    elif args.command == 'pane' and args.pane_command == 'identity' and args.config is None:
        args.config = _discover_plugin_config()
    elif args.command == 'peer' and args.peer_command in ('diag', 'input', 'latest', 'submit', 'submit-status', 'update', 'update-status') and args.config is None:
        args.config = _discover_plugin_config()
    try:
        return asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        return 130


def _discover_plugin_config():
    """Prefer the active Herdr plugin's config for monitor commands."""
    root = os.environ.get('HERDR_PLUGIN_CONFIG_DIR')
    if root:
        path = Path(root) / 'config.toml'
        if path.is_file():
            return path
    if not os.environ.get('HERDR_SOCKET_PATH'):
        return None
    herdr = os.environ.get('HERDR_BIN_PATH') or shutil.which('herdr')
    if not herdr:
        return None
    try:
        result = subprocess.run([herdr, 'plugin', 'config-dir', 'herdr-talkback'],
                                capture_output=True, encoding='utf-8', errors='replace', timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            path = Path(result.stdout.strip()) / 'config.toml'
            if path.is_file():
                return path
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


async def _main_async(args) -> int:
    if args.command == 'version':
        from . import __version__, __build__
        result = {'cli': {'version': __version__, 'build': __build__}, 'service': None, 'matches': None}
        try:
            config = load_config(args.config)
            response = await submit_daemon_request(config.daemon_socket_path, {'type': 'runtime_info'})
            if response.get('ok'):
                result['service'] = response
                if response.get('version') and response.get('build'):
                    result['matches'] = response['version'] == __version__ and response['build'] == __build__
                else:
                    result['service_error'] = 'running service does not report a version; update and restart it'
            else:
                result['service_error'] = response.get('error', 'runtime information unavailable')
        except (OSError, RuntimeError, ValueError) as exc:
            result['service_error'] = str(exc)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if args.command == 'monitor':
        if args.monitor_command == 'state':
            from .operator_controls import read_monitor_enabled
            from .transport import local_endpoint_running
            path = _resolve_read_config_path(args.config)
            raw = {}
            if path is not None:
                with path.open('rb') as handle:
                    raw = tomllib.load(handle)
            configured = raw.get('logic_monitor', {}).get('enabled', False)
            state_path = configured_path(raw.get('state_path'), default_state_path())
            socket_path = _load_daemon_socket_path(args.config)
            try:
                if local_endpoint_running(socket_path):
                    response = await submit_daemon_request(socket_path, {'type': 'monitor_state',
                                                                          'state_path': str(state_path.resolve())})
                    response['service_running'] = True
                else:
                    response = {'ok': True, 'enabled': read_monitor_enabled(state_path, bool(configured)),
                                'active': False, 'service_running': False}
            except (OSError, ValueError) as exc:
                response = {'ok': False, 'error': str(exc)}
            print(json.dumps(response, ensure_ascii=False))
            return 0 if response.get('ok') else 1
        if args.monitor_command in ('enable', 'disable'):
            from .transport import local_endpoint_running
            from .logic_config import parse_logic_config
            from dataclasses import asdict
            enabled = args.monitor_command == 'enable'
            try:
                state = _load_read_state(args.config)
                if enabled:
                    cfg = load_config(args.config)
                    values = asdict(cfg.logic_monitor)
                    values['enabled'] = True
                    parse_logic_config(values)
                socket_path = _load_daemon_socket_path(args.config)
                if local_endpoint_running(socket_path):
                    response = await submit_daemon_request(socket_path, {'type': 'monitor_control', 'enabled': enabled,
                                                                        'state_path': str(state.path.resolve())})
                    if response.get('error') == 'unsupported daemon request type':
                        response['error'] += '; verify --config and restart the intended Talkback service with a current version.'
                else:
                    response = {'ok': False, 'error': 'Talkback service is not reachable for this configuration; verify --config and HERDR_SOCKET_PATH.'}
            except (ValueError, OSError, RuntimeError) as exc:
                response = {'ok': False, 'error': str(exc)}
            print(json.dumps(response, ensure_ascii=False))
            return 0 if response.get('ok') else 1
        from .logic_state import read_status
        path = _resolve_read_config_path(args.config)
        raw = {}
        if path is not None:
            with path.open('rb') as handle:
                raw = tomllib.load(handle)
        rows = read_status(configured_path(raw.get('state_path'), default_state_path()))
        if args.json:
            print(json.dumps(rows, ensure_ascii=False))
        elif not rows:
            print('No logical pane observations recorded.')
        else:
            for row in rows:
                print(f"{row['pane_id']}  herdr_status={row['herdr_status']}  "
                      f"logic_status={row['logic_status']}  {row['detail']}")
        return 0
    if args.command == 'pane' and args.pane_command in ('register', 'registered', 'unregister', 'register-all', 'identity', 'import'):
        from .operator_controls import OperatorStore, register_local_panes, make_identity_card, import_identity_card
        try:
            cfg = load_config(args.config)
            state = _load_read_state(args.config)
            store = OperatorStore(state)
            if args.pane_command == 'register-all':
                panes = (await HerdrClient(cfg.socket_path).request('pane.list', {})).get('panes', [])
                values = register_local_panes(store, cfg, panes)
                print(json.dumps({'ok': True, 'panes': values}, ensure_ascii=False))
            elif args.pane_command == 'identity':
                panes = (await HerdrClient(cfg.socket_path).request('pane.list', {})).get('panes', [])
                if sum(p.get('label') == args.pane for p in panes) != 1:
                    raise ValueError('pane name must identify exactly one local pane')
                print(json.dumps(make_identity_card(cfg, args.pane, args.url), ensure_ascii=False))
            elif args.pane_command == 'import':
                panes = (await HerdrClient(cfg.socket_path).request('pane.list', {})).get('panes', [])
                card = json.loads(args.card)
                status = import_identity_card(store, cfg, card, {p.get('label') for p in panes})
                print(json.dumps({'ok': True, 'status': status, 'name': card['pane']}, ensure_ascii=False))
            elif args.pane_command == 'register':
                if args.name in cfg.aliases:
                    raise ValueError('name conflicts with a configured alias')
                panes = (await HerdrClient(cfg.socket_path).request('pane.list', {})).get('panes', [])
                if any(pane.get('label') == args.name for pane in panes) and args.address != cfg.core_node_id + '/' + args.name:
                    raise ValueError('name conflicts with a local pane')
                store.register_pane(args.name, args.address, cfg)
                print(json.dumps({'ok': True, 'name': args.name, 'address': args.address}))
            elif args.pane_command == 'unregister':
                if not store.remove_pane(args.name):
                    raise ValueError('registered pane not found')
                print(json.dumps({'ok': True, 'name': args.name}))
            else:
                values = store.panes()
                for row in values:
                    node = row['address'].split('/', 1)[0]
                    row['location'] = 'local' if node == cfg.core_node_id else 'remote'
                    row['peer_url'] = cfg.peers[node].url if node in cfg.peers else None
                if args.json:
                    print(json.dumps(values, ensure_ascii=False))
                else:
                    for row in values:
                        print(f"{row['name']}  {row['address']}  {row['location']}  {row['peer_url'] or '-'}")
            return 0
        except (ValueError, OSError, RuntimeError) as exc:
            print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
            return 1
    if args.command == 'peer' and args.peer_command == 'diag':
        from .peer_diag import diagnose, summary
        cfg = load_config(args.config)
        peer = cfg.peers.get(args.node_id)
        if peer is None:
            print('peer not configured: ' + args.node_id)
            return 1
        try:
            report = await diagnose(peer, cfg.core_node_id, timeout=args.timeout)
        except Exception as exc:  # noqa: BLE001 - diagnostics must never crash
            print(json.dumps({'peer': args.node_id, 'ok': False,
                              'error': f'{type(exc).__name__}: {exc}'}, ensure_ascii=False))
            return 1
        if args.json:
            print(json.dumps(report, ensure_ascii=False, default=str))
        else:
            print(summary(report))
        return 0 if report['steps'] and all(step['ok'] for step in report['steps']) else 1

    if args.command == 'peer' and args.peer_command == 'init-tls':
        try:
            from .certificates import provision
            paths = provision(args.directory, args.host, args.force)
        except (ImportError, ValueError, OSError) as exc:
            print(f'Credential provisioning failed ({type(exc).__name__}); install the network extra and check existing files.')
            return 1
        print('Created credentials (private contents are not printed):')
        for path in paths:
            print(path)
        return 0

    if args.command == 'peer' and args.peer_command in ('input', 'latest', 'submit', 'submit-status', 'update', 'update-status'):
        from .peer_http import PeerClient
        cfg = load_config(args.config)
        request_id = (args.request_id or 'req_' + uuid.uuid4().hex) if args.peer_command in ('submit', 'update') else None
        if request_id:
            print('request_id: ' + request_id, file=sys.stderr, flush=True)
        try:
            if args.node_id not in cfg.peers:
                raise ValueError('peer not configured: ' + args.node_id)
            if args.peer_command in ('input', 'latest', 'submit') and not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', args.pane):
                raise ValueError('invalid remote pane name or ID')
            client = PeerClient(cfg.core_node_id, cfg.peers[args.node_id])
            if args.peer_command == 'input':
                response = await client.request('GET', f'/v1/panes/{args.pane}/input')
            elif args.peer_command == 'latest':
                response = await client.request('POST', f'/v1/panes/{args.pane}/latest', {'key': args.key})
            elif args.peer_command == 'submit':
                payload = {'pane_id': args.pane_id, 'screen_sha256': args.screen_sha256, 'request_id': request_id}
                if args.reject_folded:
                    payload['allow_folded'] = False
                response = await client.request('POST', f'/v1/panes/{args.pane}/submit', payload)
            elif args.peer_command == 'update':
                response = await client.request('POST', '/v1/install/update', {'request_id': request_id})
            elif args.peer_command == 'submit-status':
                if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', args.request_id):
                    raise ValueError('invalid request ID')
                response = await client.request('GET', f'/v1/control/requests/{args.request_id}')
            else:
                if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', args.request_id):
                    raise ValueError('invalid request ID')
                response = await client.request('GET', f'/v1/install/updates/{args.request_id}')
            print(json.dumps(response, ensure_ascii=False))
            return 0 if response.get('state') not in ('unknown', 'failed') else 1
        except Exception as exc:
            print(json.dumps({'ok': False, 'error': str(exc), 'request_id': request_id}, ensure_ascii=False))
            return 1

    if args.command == 'message':
        from .peer_state import PeerStore
        state = _load_read_state(args.config)
        try:
            row = state.get_message(args.message_id)
        except KeyError:
            print(f'message not found: {args.message_id}')
            return 1
        diagnostic = {key: row[key] for key in ('message_id', 'source_endpoint', 'target_endpoint',
                                               'state', 'reason', 'attempts')}
        diagnostic['peer'] = PeerStore(state).progress(args.message_id)
        if args.json:
            print(json.dumps(diagnostic, ensure_ascii=False))
        else:
            for key, value in diagnostic.items():
                print(f'{key}: {json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value}')
        return 0
    if args.command == "tail":
        state = _load_read_state(args.config)
        for event in state.tail_events(args.limit):
            print(json.dumps(event, ensure_ascii=False))
        return 0

    if args.command == "queue":
        state = _load_read_state(args.config)
        for message in state.list_active_messages():
            print(json.dumps(message, ensure_ascii=False))
        return 0

    if args.command == "retry":
        state = _load_read_state(args.config)
        daemon_socket_path = _load_daemon_socket_path(args.config)
        try:
            old_message = state.get_message(args.message_id)
        except KeyError:
            print(json.dumps({"ok": False, "error": f"message not found: {args.message_id}"}, ensure_ascii=False))
            return 1
        response = await submit_daemon_request(
            daemon_socket_path,
            {"type": "send", "message": old_message["raw_text"], "dry_run": False, "retry": True},
        )
        if response.get("ok") and response.get("message_id"):
            state.mark_message_superseded(args.message_id, str(response["message_id"]))
        print(json.dumps(response, ensure_ascii=False))
        return 0 if response.get("ok") else 1

    if args.command == "alerts":
        state = _load_read_state(args.config)
        if args.alerts_command == "clear":
            cleared = state.clear_alerts(args.ids or None)
            print(f"cleared {cleared} alerts")
            return 0
        alerts = state.list_open_alerts(args.limit)
        for alert in alerts:
            print(_format_alert_for_output(alert, args.json), flush=args.follow)
        if args.follow:
            last_id = max((int(alert["id"]) for alert in alerts), default=0)
            while True:
                await asyncio.sleep(args.poll_interval)
                new_alerts = state.list_open_alerts_after(last_id)
                for alert in new_alerts:
                    print(_format_alert_for_output(alert, args.json), flush=True)
                    last_id = max(last_id, int(alert["id"]))
        return 0

    if args.command == "send" and not args.direct:
        daemon_socket_path = _load_daemon_socket_path(args.config)
        try:
            payload = {"type": "send", "message": args.message, "dry_run": args.dry_run}
            own_pane_id = os.environ.get("HERDR_PANE_ID")
            if own_pane_id:
                payload["pane_id"] = own_pane_id
            response = await submit_daemon_request(daemon_socket_path, payload)
        except OSError as exc:
            response = {
                "ok": False,
                "error": f"herdr-talkback daemon is not reachable at {daemon_socket_path}: {exc}",
            }
        if args.dry_run and response.get("ok"):
            print(
                "dry-run: "
                f"{response['source']} -> {response['target']} "
                f"resolved={response['resolved_target']} pane={response['pane_id']}"
            )
        else:
            print(json.dumps(response, ensure_ascii=False))
        return 0 if response.get("ok") else 1

    cfg = load_config(args.config)

    if args.command == 'whoami':
        from urllib.parse import urlsplit
        from .operator_controls import OperatorStore
        try:
            host = args.host
            if host in cfg.peers:
                matches = [host]
            else:
                requested = urlsplit(host if '://' in host else 'https://' + host)
                matches = [node for node, peer in cfg.peers.items()
                           if urlsplit(peer.url).hostname == requested.hostname
                           and (requested.port is None or (urlsplit(peer.url).port or 443) == requested.port)]
            if len(matches) != 1:
                raise ValueError('host must match exactly one configured peer; include its port if needed')
            node = matches[0]
            panes = (await HerdrClient(cfg.socket_path).request('pane.list', {})).get('panes', [])
            labels = [p.get('label') for p in panes]
            if labels.count(args.source) != 1:
                raise ValueError('source must identify exactly one local pane')
            if args.target in labels or args.target in cfg.aliases:
                raise ValueError('remote name conflicts with local pane or alias')
            store = OperatorStore(_load_read_state(args.config))
            address = node + '/' + args.target
            existing = store.pane(args.target)
            if existing and existing != address:
                raise ValueError('target name is already registered to another pane')
            if not existing:
                store.register_pane(args.target, address, cfg)
            source_location = cfg.core_node_id
            try:
                peer_url = urlsplit(cfg.peers[node].url)
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                    probe.connect((peer_url.hostname, peer_url.port or 443))
                    source_location = '{}:{}'.format(probe.getsockname()[0], cfg.network.listen_port)
            except (OSError, AttributeError):
                pass  # The authenticated node ID remains sufficient for routing.
            raw = '[{}\u2192{}][whoami] \u8fdc\u7a0b pane \u8bf7\u6c42\u767b\u8bb0\uff1a{} {} ({})'.format(
                args.source, address, source_location, args.source, cfg.core_node_id)
            response = await submit_daemon_request(cfg.daemon_socket_path, {'type': 'whoami', 'message': raw})
        except (ValueError, OSError, RuntimeError) as exc:
            response = {'ok': False, 'error': str(exc)}
        print(json.dumps(response, ensure_ascii=False))
        return 0 if response.get('ok') else 1

    if args.command == 'peers':
        if args.peers_command == 'list':
            values = [{'node_id': node, 'url': peer.url} for node, peer in sorted(cfg.peers.items())]
            if args.json:
                print(json.dumps(values, ensure_ascii=False))
            else:
                print('NODE  URL')
                for item in values:
                    print(f"{item['node_id']}  {item['url']}")
            return 0
        if args.node_id not in cfg.peers:
            print(f'peer not configured: {args.node_id}')
            return 1
        try:
            from .peer_http import PeerClient
            info = await PeerClient(cfg.core_node_id, cfg.peers[args.node_id]).request('GET', '/v1/info')
        except Exception as exc:
            print(f'Peer check failed ({type(exc).__name__})')
            return 1
        print(json.dumps(info, ensure_ascii=False))
        return 0

    if args.command == "serve":
        from .service import run_service

        return await run_service(cfg, dry_run=args.dry_run)

    client = HerdrClient(cfg.socket_path)
    if args.command == "pane":
        result = await client.request("pane.list", {})
        panes = result.get("panes", [])
        if args.pane_command == "name-from-tab":
            return await _name_panes_from_tabs(client, panes, args.pane_id, args.force)
        workspace_ids = sorted({pane['workspace_id'] for pane in panes if pane.get('workspace_id')})
        tab_results = await asyncio.gather(*(
            client.request('tab.list', {'workspace_id': workspace_id})
            for workspace_id in workspace_ids
        ))
        tab_labels = {
            tab['tab_id']: tab.get('label')
            for result in tab_results for tab in result.get('tabs', [])
        }
        print(_format_pane_table([
            dict(pane, tab_label=tab_labels.get(pane.get('tab_id')))
            for pane in panes
        ]))
        return 0

    registry = RoleRegistry(client, cfg.roles, cfg.aliases)
    local_node_id = getattr(cfg, "core_node_id", "linux-main")

    await registry.refresh()

    if args.command == "panes":
        for role, info in sorted(registry.snapshot().items()):
            print(f"{role}\t{info.agent_status}\t{info.pane_id}\t{info.agent or ''}")
        return 0

    if args.command == "status":
        diagnostic = _pane_status_diagnostic(registry, args.target)
        if diagnostic is None:
            print(json.dumps({"ok": False, "error": f"pane not found: {args.target}"}, ensure_ascii=False))
            return 1
        if args.json:
            print(json.dumps(diagnostic, ensure_ascii=False))
        else:
            print(_format_pane_status(diagnostic))
        return 0

    if args.command == "input":
        pane_id = _resolve_input_target(registry, args.target)
        result = await client.request(
            "pane.read",
            {"pane_id": pane_id, "source": "recent", "lines": args.lines, "format": "ansi"},
        )
        raw_text = result["read"]["text"]
        input_state = inspect_input(raw_text, args.expected)
        diagnostic = {
            "target": args.target,
            "pane_id": pane_id,
            "kind": input_state.kind,
            "text_after_prompt": input_state.text_after_prompt,
            "contains_expected_message": input_state.contains_expected_message,
            "raw_ansi": raw_text.encode("unicode_escape").decode("ascii"),
        }
        if args.json:
            print(json.dumps(diagnostic, ensure_ascii=False))
        else:
            print(_format_input_diagnostic(diagnostic))
        return 0

    if args.command == "exec":
        state = TalkbackState(cfg.state_path, local_node_id=local_node_id)
        state.init()
        notifier = _build_notifier(cfg, client, registry, state)
        delivery = DeliveryService(
            client,
            registry,
            state,
            max_attempts=cfg.delivery_max_attempts,
            retry_interval_seconds=cfg.delivery_retry_interval_seconds,
            notifier=notifier,
            verify_timeout_ms=getattr(cfg, "delivery_verify_timeout_ms", 0),
        )
        all_delivered = True
        for target in _split_exec_targets(args.targets):
            try:
                pane_id = _resolve_exec_target(registry, target)
            except Exception:
                all_delivered = False
                print(
                    json.dumps(
                        {
                            "target": target,
                            "pane_id": None,
                            "delivered": False,
                            "reason": f"pane not found: {target}",
                            "attempts": 0,
                        },
                        ensure_ascii=False,
                    )
                )
                continue

            result = await delivery.send_control(pane_id, args.exec_command)
            all_delivered = all_delivered and result.delivered
            print(
                json.dumps(
                    {
                        "target": target,
                        "pane_id": pane_id,
                        "delivered": result.delivered,
                        "reason": result.reason,
                        "attempts": result.attempts,
                    },
                    ensure_ascii=False,
                )
            )
        return 0 if all_delivered else 1

    if args.command == "send":
        try:
            from .operator_controls import read_registered_pane
            from .messages import HEADER_RE, MessageParseError
            match = HEADER_RE.match(args.message.strip())
            if match and read_registered_pane(cfg.state_path, match.group(2).strip()):
                raise MessageParseError('registered pane names require daemon delivery; omit --direct')
            known_sources = (
                set(cfg.roles) | set(cfg.aliases) | set(cfg.aliases.values())
                | set(registry.snapshot())
            )
            own_pane = registry.get_pane(os.environ.get("HERDR_PANE_ID", ""))
            source_hint = ""
            if own_pane is not None and own_pane.name:
                source_hint = " (this pane is '{0}', pane id {1})".format(
                    own_pane.name, own_pane.pane_id)
            message = parse_message(
                args.message,
                cfg.roles,
                cfg.aliases,
                local_node_id=local_node_id,
                message_rules=getattr(cfg, "message_rules", ()),
                known_sources=known_sources,
                source_hint=source_hint,
            )
            target_address = parse_endpoint_address(message.target_endpoint)
            if isinstance(target_address, EndpointAddress) and target_address.node_id != local_node_id:
                raise ValueError('remote messages require the durable daemon path; --direct is local-only')
            pane_id = registry.resolve_pane(message.resolved_target)
        except Exception as exc:
            state = TalkbackState(cfg.state_path, local_node_id=local_node_id)
            state.init()
            notifier = _build_notifier(cfg, client, registry, state)
            await notifier.notify(
                "Herdr Talkback send failed",
                f"{args.message}\n{exc}",
                kind="send_failed",
                payload={"raw_message": args.message, "reason": str(exc)},
            )
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
        if args.dry_run:
            print(f"dry-run: {message.source} -> {message.target} resolved={message.resolved_target} pane={pane_id}")
            return 0
        state = TalkbackState(cfg.state_path, local_node_id=local_node_id)
        state.init()
        notifier = _build_notifier(cfg, client, registry, state)
        delivery = DeliveryService(
            client,
            registry,
            state,
            max_attempts=cfg.delivery_max_attempts,
            retry_interval_seconds=cfg.delivery_retry_interval_seconds,
            notifier=notifier,
            verify_timeout_ms=getattr(cfg, "delivery_verify_timeout_ms", 0),
            stale_input_seconds=getattr(cfg, 'delivery_stale_input_seconds', 600),
        )
        result = await delivery.deliver(message)
        print(
            json.dumps(
                {"delivered": result.delivered, "reason": result.reason, "attempts": result.attempts},
                ensure_ascii=False,
            )
        )
        return 0 if result.delivered else 1

    raise AssertionError(args.command)


def _build_notifier(cfg, client, registry, state):
    notifier = HerdrNotifier(
        client,
        enabled=cfg.notify_enabled,
        position=cfg.notify_position,
        sound=cfg.notify_sound,
    )
    return AlertingNotifier(
        notifier,
        state,
        client=client,
        registry=registry,
        pane_role=getattr(cfg, "alerts_pane_role", None),
    )


def _load_read_state(config_path: Optional[Path]) -> TalkbackState:
    resolved_path = _resolve_read_config_path(config_path)
    raw = {}
    if resolved_path is not None:
        with resolved_path.open("rb") as handle:
            raw = tomllib.load(handle)
    core = raw.get("core")
    if core is None:
        core = {}
    if not isinstance(core, dict):
        raise ValueError(f"expected mapping, got {type(core).__name__}")
    state = TalkbackState(
        configured_path(raw.get("state_path"), default_state_path()),
        local_node_id=validate_node_id(core.get("node_id", "linux-main")),
    )
    state.init()
    return state


def _load_daemon_socket_path(config_path: Optional[Path]) -> Path:
    resolved_path = _resolve_read_config_path(config_path)
    if resolved_path is None:
        return default_daemon_socket_path()

    with resolved_path.open("rb") as handle:
        raw = tomllib.load(handle)
    return configured_path(raw.get("daemon_socket_path"), default_daemon_socket_path())


def _format_alert_for_output(alert: dict, as_json: bool) -> str:
    if as_json:
        return json.dumps(alert, ensure_ascii=False)
    return _format_alert(alert)


def _format_alert(alert: dict) -> str:
    created_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(alert["created_at"])))
    payload = alert.get("payload") or {}
    payload_text = ""
    if payload:
        payload_text = f" payload={json.dumps(payload, ensure_ascii=False)}"
    return (
        f"[{created_at}] #{alert['id']} {alert['kind']} {alert['title']} "
        f"- {alert['body']}{payload_text}"
    )


async def _name_panes_from_tabs(client, panes, pane_id: Optional[str], force: bool) -> int:
    selected = [pane for pane in panes if pane_id is None or pane['pane_id'] == pane_id]
    if pane_id is not None and not selected:
        print(f"pane not found: {pane_id}")
        return 1

    candidates = []
    for pane in selected:
        if not force and (pane.get('label') or '').strip():
            print(f"{pane['pane_id']}: skipped (name already set)")
        else:
            candidates.append(pane)

    workspace_ids = sorted({pane['workspace_id'] for pane in candidates})
    results = await asyncio.gather(*(
        client.request('tab.list', {'workspace_id': workspace_id})
        for workspace_id in workspace_ids
    ))
    names = {tab['tab_id']: tab.get('label') for result in results for tab in result.get('tabs', [])}
    failed = False
    for pane in candidates:
        name = names.get(pane['tab_id'])
        if not name or not name.strip():
            print(f"{pane['pane_id']}: Tab has no name: {pane['tab_id']}; pane name unchanged")
            failed = True
            continue
        await client.request('pane.rename', {'pane_id': pane['pane_id'], 'label': name})
        print(f"{pane['pane_id']}: NAME = {name}")
    return 1 if failed else 0


def _display_width(text: str) -> int:
    return sum(
        0 if unicodedata.combining(char) else
        2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        for char in text
    )


def _format_pane_table(panes) -> str:
    if not panes:
        return "No panes found."

    columns = (
        ("PANE", "pane_id"), ("NAME", "label"), ("TITLE", "terminal_title_stripped"),
        ("AGENT", "agent"), ("STATUS", "agent_status"), ("TAB", "tab_id"),
        ("TAB NAME", "tab_label"), ("CWD", "cwd"),
    )

    def cell(value):
        # Keep each pane on one line and prevent terminal control characters
        # in labels or paths from disturbing the table.
        text = str(value) if value else "-"
        text = ''.join(' ' if unicodedata.category(c).startswith('C') else c for c in text)
        return ' '.join(text.split()) or "-"

    rows = [[title for title, _ in columns]]
    for pane in panes:
        values = dict(pane)
        values['terminal_title_stripped'] = pane.get('terminal_title_stripped') or pane.get('terminal_title')
        rows.append([cell(values.get(key)) for _, key in columns])
    widths = [max(_display_width(row[i]) for row in rows) for i in range(len(columns))]

    def render(row):
        return '  '.join(
            value + ' ' * (width - _display_width(value))
            for value, width in zip(row, widths)
        ).rstrip()

    return '\n'.join([
        render(rows[0]), render(['-' * width for width in widths]),
        *(render(row) for row in rows[1:]),
    ])


def _pane_status_diagnostic(registry: RoleRegistry, target: str) -> Optional[dict]:
    try:
        info = registry.get_agent(target)
    except Exception:
        info = registry.get_pane(target)
    if info is None:
        return None
    return {
        "target": target,
        "role": info.name or "",
        "pane_id": info.pane_id,
        "agent": info.agent,
        "agent_status": info.agent_status,
        "workspace_id": info.workspace_id,
        "tab_id": info.tab_id,
        "cwd": info.cwd,
    }


def _format_pane_status(diagnostic: dict) -> str:
    return "\n".join(
        [
            f"target: {diagnostic['target']}",
            f"role: {diagnostic['role']}",
            f"pane_id: {diagnostic['pane_id']}",
            f"agent: {diagnostic['agent'] or ''}",
            f"agent_status: {diagnostic['agent_status']}",
            f"workspace_id: {diagnostic['workspace_id']}",
            f"tab_id: {diagnostic['tab_id']}",
            f"cwd: {diagnostic['cwd'] or ''}",
        ]
    )


def _resolve_input_target(registry: RoleRegistry, target: str) -> str:
    try:
        return registry.resolve_pane(target)
    except Exception:
        return target


def _split_exec_targets(targets: str) -> list[str]:
    return [target.strip() for target in targets.split(",") if target.strip()]


def _resolve_exec_target(registry: RoleRegistry, target: str) -> str:
    try:
        return registry.resolve_pane(target)
    except Exception:
        if registry.get_pane(target) is not None:
            return target
        raise


def _format_input_diagnostic(diagnostic: dict) -> str:
    return "\n".join(
        [
            f"target: {diagnostic['target']}",
            f"pane_id: {diagnostic['pane_id']}",
            f"kind: {diagnostic['kind']}",
            f"text_after_prompt: {diagnostic['text_after_prompt']}",
            f"contains_expected_message: {diagnostic['contains_expected_message']}",
            "raw_ansi:",
            diagnostic["raw_ansi"],
        ]
    )


def _resolve_read_config_path(config_path: Optional[Path]) -> Optional[Path]:
    if config_path is not None:
        return Path(config_path)
    path = default_config_path()
    if path.exists():
        return path
    return None
