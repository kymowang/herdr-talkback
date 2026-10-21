from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Optional

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.9/3.10
    import tomli as tomllib  # type: ignore[import, no-redef]

from .addressing import validate_node_id
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

    sub.add_parser("panes")
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

    if args.command == "serve":
        from .service import run_service

        return await run_service(cfg, dry_run=args.dry_run)

    client = HerdrClient(cfg.socket_path)
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
