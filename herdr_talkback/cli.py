from __future__ import annotations

import argparse
import asyncio
import json
import time
import unicodedata
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
    parser.add_argument("--config", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)

    peer = sub.add_parser('peer', help='Provision peer credentials')
    peer_sub = peer.add_subparsers(dest='peer_command', required=True)
    tls = peer_sub.add_parser('init-tls')
    tls.add_argument('--directory', type=Path, required=True)
    tls.add_argument('--host', action='append', required=True)
    tls.add_argument('--force', action='store_true')
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

    send = sub.add_parser("send")
    send.add_argument("message", nargs="+", action=JoinMessageAction)
    send.add_argument("--dry-run", action="store_true")
    send.add_argument("--direct", action="store_true")

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
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        return 130


async def _main_async(args) -> int:
    if args.command == 'peer':
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
            {"type": "send", "message": old_message["raw_text"], "dry_run": False},
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
            response = await submit_daemon_request(
                daemon_socket_path,
                {"type": "send", "message": args.message, "dry_run": args.dry_run},
            )
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
            message = parse_message(
                args.message,
                cfg.roles,
                cfg.aliases,
                local_node_id=local_node_id,
                message_rules=getattr(cfg, "message_rules", ()),
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
