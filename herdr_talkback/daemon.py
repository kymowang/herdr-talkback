from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional

from .transport import LocalServer, open_local_connection, start_local_server
from .addressing import EndpointAddress, parse_endpoint_address
from .core_types import CoreMessage, CoreMessageState
from .messages import MessageParseError, parse_message
from .notifier import NullNotifier, notify_alert
from .types import MessageState


DaemonHandler = Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


async def submit_daemon_request(socket_path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    reader, writer = await open_local_connection(socket_path)
    try:
        writer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
        await writer.drain()
        line = await reader.readline()
        if not line:
            return {"ok": False, "error": "daemon closed connection without a response"}
        response = json.loads(line.decode("utf-8"))
        if not isinstance(response, dict):
            return {"ok": False, "error": "daemon returned a non-object response"}
        return response
    finally:
        writer.close()
        await writer.wait_closed()


class DaemonInboxServer:
    def __init__(self, socket_path: Path, handler: DaemonHandler) -> None:
        self.socket_path = Path(socket_path)
        self.handler = handler
        self._server: Optional[LocalServer] = None

    async def serve_forever(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        self._server = await start_local_server(self._handle_client, self.socket_path)
        try:
            await self._server.serve_forever()
        finally:
            await self.close()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self.socket_path.exists():
            self.socket_path.unlink()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                line = await reader.readline()
                if not line:
                    # Startup probes connect and immediately close without a request.
                    return
                else:
                    payload = json.loads(line.decode("utf-8"))
                    if not isinstance(payload, dict):
                        response: Mapping[str, Any] = {"ok": False, "error": "daemon request must be a JSON object"}
                    else:
                        response = await self.handler(payload)
            except json.JSONDecodeError as exc:
                response = {"ok": False, "error": f"invalid JSON request: {exc.msg}"}
            except Exception as exc:  # pragma: no cover - defensive daemon boundary
                response = {"ok": False, "error": str(exc)}

            writer.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
            await writer.drain()
        except ConnectionError:
            pass  # A disconnected CLI must not produce an unhandled server task.
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass


class DaemonMessageRouter:
    def __init__(self, config, registry, dispatcher, state=None, notifier=None, peer_adapter=None) -> None:
        self.config = config
        self.registry = registry
        self.dispatcher = dispatcher
        self.state = state
        self.notifier = notifier or NullNotifier()
        self.peer_adapter = peer_adapter
        self._lock = asyncio.Lock()

    async def handle(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if payload.get("type") != "send":
            return {"ok": False, "error": "unsupported daemon request type"}
        raw_message = payload.get("message")
        if not isinstance(raw_message, str):
            return {"ok": False, "error": "message must be a string"}

        async with self._lock:
            try:
                await self.registry.refresh()
                message = parse_message(
                    raw_message,
                    self.config.roles,
                    self.config.aliases,
                    local_node_id=self.config.core_node_id,
                    message_rules=getattr(self.config, "message_rules", ()),
                )
                target = parse_endpoint_address(message.target_endpoint)
                if not isinstance(target, EndpointAddress):
                    raise AssertionError("canonical target endpoint must be qualified")
                if target.node_id != self.config.core_node_id:
                    if self.peer_adapter is None:
                        raise MessageParseError("only local Herdr targets are supported; configure a network peer first")
                    self.peer_adapter.ensure_endpoint(target)
                    from .peer_state import validate_envelope, message_envelope
                    validate_envelope(self.config.core_node_id, target.node_id, message_envelope(message))
                    pane_id = ''
                else:
                    pane_id = self.registry.resolve_pane(target.endpoint_id)
            except MessageParseError as exc:
                await self._notify_send_failed(raw_message, str(exc))
                return {"ok": False, "error": str(exc)}
            except Exception as exc:
                await self._notify_send_failed(raw_message, str(exc))
                return {"ok": False, "error": str(exc)}

            if bool(payload.get("dry_run")):
                return {
                    "ok": True,
                    "dry_run": True,
                    "source": message.source,
                    "target": message.target,
                    "resolved_target": message.resolved_target,
                    "pane_id": pane_id,
                    "source_endpoint": message.source_endpoint,
                    "target_endpoint": message.target_endpoint,
                }

            core_message = CoreMessage(
                message_id=message.message_id,
                source_endpoint=message.source_endpoint,
                target_endpoint=message.target_endpoint,
                body=message.body,
                raw_text=message.raw_text,
                state=CoreMessageState.QUEUED,
                conversation_id=message.conversation_id,
                reply_to=message.reply_to,
                metadata=message.metadata,
            )
            created = self.state.enqueue_message(core_message, target_pane_id=pane_id)
            if not created:
                return {
                    "ok": True,
                    "queued": False,
                    "duplicate": True,
                    "message_id": core_message.message_id,
                    "source_endpoint": message.source_endpoint,
                    "target_endpoint": message.target_endpoint,
                }
            self.dispatcher.wake(core_message.target_endpoint)
            return {
                "ok": True,
                "queued": True,
                "message_id": message.message_id,
                "source": message.source,
                "target": message.target,
                "resolved_target": message.resolved_target,
                "pane_id": pane_id,
                "source_endpoint": message.source_endpoint,
                "target_endpoint": message.target_endpoint,
            }

    async def drain(self) -> None:
        await self.dispatcher.drain()

    async def close(self) -> None:
        await self.dispatcher.close()

    async def _notify_send_failed(self, raw_message: str, reason: str) -> None:
        if self.state is not None:
            self.state.record_event("send_failed", {"raw_message": raw_message, "reason": reason})
        await notify_alert(
            self.notifier,
            "Herdr Talkback send failed",
            f"{raw_message}\n{reason}",
            "send_failed",
            {"raw_message": raw_message, "reason": reason},
        )
