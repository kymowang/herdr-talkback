from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Sequence

PANE_READ_TEXT_LIMIT_BYTES = 64 * 1024
SOCKET_RESPONSE_LIMIT_BYTES = 4 * 1024 * 1024


class HerdrError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class HerdrClient:
    def __init__(self, socket_path: Path, timeout: float = 10.0) -> None:
        self.socket_path = Path(socket_path)
        self.timeout = timeout
        self._counter = 0

    async def request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        request_id = self._next_id()
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(self.socket_path), limit=SOCKET_RESPONSE_LIMIT_BYTES),
            timeout=self.timeout,
        )
        try:
            payload = {"id": request_id, "method": method, "params": dict(params)}
            writer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
            await writer.drain()

            line = await asyncio.wait_for(reader.readline(), timeout=self.timeout)
            if not line:
                raise HerdrError("connection_closed", "Herdr socket closed before response")

            response = json.loads(line.decode("utf-8"))
            if "error" in response:
                error = response["error"]
                raise HerdrError(str(error.get("code", "error")), str(error.get("message", "")))
            return _truncate_pane_read_response(method, response["result"])
        finally:
            writer.close()
            await writer.wait_closed()

    async def subscribe(self, subscriptions: Sequence[Mapping[str, Any]]) -> AsyncIterator[Mapping[str, Any]]:
        reader, writer = await self._open_subscription(subscriptions)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                yield json.loads(line.decode("utf-8"))
        finally:
            writer.close()
            await writer.wait_closed()

    async def check_subscription(self, subscriptions: Sequence[Mapping[str, Any]]) -> None:
        _, writer = await self._open_subscription(subscriptions)
        writer.close()
        await writer.wait_closed()

    async def _open_subscription(self, subscriptions: Sequence[Mapping[str, Any]]):
        request_id = self._next_id()
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(self.socket_path), limit=SOCKET_RESPONSE_LIMIT_BYTES),
            timeout=self.timeout,
        )
        try:
            payload = {
                "id": request_id,
                "method": "events.subscribe",
                "params": {"subscriptions": list(subscriptions)},
            }
            writer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
            await writer.drain()

            line = await asyncio.wait_for(reader.readline(), timeout=self.timeout)
            if not line:
                raise HerdrError("connection_closed", "Herdr socket closed before subscription ack")

            ack = json.loads(line.decode("utf-8"))
            if "error" in ack:
                error = ack["error"]
                raise HerdrError(str(error.get("code", "error")), str(error.get("message", "")))
            result = ack.get("result")
            if ack.get("id") != request_id or not isinstance(result, dict):
                raise HerdrError("invalid_subscription_ack", "Herdr returned an invalid subscription ack")
            if result.get("type") != "subscription_started":
                raise HerdrError("invalid_subscription_ack", "Herdr did not confirm subscription startup")
            return reader, writer
        except BaseException:
            writer.close()
            await writer.wait_closed()
            raise

    def _next_id(self) -> str:
        self._counter += 1
        return f"herdr-talkback-{time.time_ns()}-{self._counter}"


def _truncate_pane_read_response(method: str, result: Mapping[str, Any]) -> Mapping[str, Any]:
    if method != "pane.read":
        return result

    read = result.get("read")
    if not isinstance(read, dict) or not isinstance(read.get("text"), str):
        return result

    encoded = read["text"].encode("utf-8")
    if len(encoded) <= PANE_READ_TEXT_LIMIT_BYTES:
        return result

    # The active prompt is at the end of scrollback, so retain the tail for input inspection.
    truncated = encoded[-PANE_READ_TEXT_LIMIT_BYTES:].decode("utf-8", errors="ignore")
    copied_result = dict(result)
    copied_read = dict(read)
    copied_read["text"] = truncated
    copied_result["read"] = copied_read
    return copied_result
