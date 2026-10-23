import asyncio

from herdr_talkback.transport import start_local_server
import json

import pytest

from herdr_talkback.herdr_client import HerdrClient, HerdrError, PANE_READ_TEXT_LIMIT_BYTES


def test_request_round_trip(tmp_path):
    asyncio.run(_request_round_trip(tmp_path))


async def _request_round_trip(tmp_path):
    socket_path = tmp_path / "herdr.sock"
    seen = []

    async def handle(reader, writer):
        line = await reader.readline()
        request = json.loads(line.decode("utf-8"))
        seen.append(request)
        writer.write(json.dumps({"id": request["id"], "result": {"type": "pong"}}).encode("utf-8") + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await start_local_server(handle, socket_path)
    async with server:
        client = HerdrClient(socket_path)
        result = await client.request("ping", {})

    assert result == {"type": "pong"}
    assert seen[0]["method"] == "ping"
    assert seen[0]["params"] == {}
    assert seen[0]["id"].startswith("herdr-talkback-")


def test_request_error_raises(tmp_path):
    asyncio.run(_request_error_raises(tmp_path))


async def _request_error_raises(tmp_path):
    socket_path = tmp_path / "herdr.sock"

    async def handle(reader, writer):
        line = await reader.readline()
        request = json.loads(line.decode("utf-8"))
        writer.write(
            json.dumps({"id": request["id"], "error": {"code": "not_found", "message": "pane not found"}}).encode("utf-8")
            + b"\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await start_local_server(handle, socket_path)
    async with server:
        client = HerdrClient(socket_path)
        with pytest.raises(HerdrError) as exc:
            await client.request("pane.get", {"pane_id": "missing"})

    assert exc.value.code == "not_found"
    assert exc.value.message == "pane not found"
    assert str(exc.value) == "not_found: pane not found"
    assert "pane not found" in str(exc.value)


def test_request_truncates_large_pane_read_to_tail(tmp_path):
    asyncio.run(_request_truncates_large_pane_read_to_tail(tmp_path))


async def _request_truncates_large_pane_read_to_tail(tmp_path):
    socket_path = tmp_path / "herdr.sock"
    tail = "❯ current prompt"
    oversized_text = "x" * (PANE_READ_TEXT_LIMIT_BYTES + 1024) + tail

    async def handle(reader, writer):
        request = json.loads((await reader.readline()).decode("utf-8"))
        response = {"id": request["id"], "result": {"read": {"text": oversized_text}}}
        writer.write(json.dumps(response).encode("utf-8") + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await start_local_server(handle, socket_path)
    async with server:
        result = await HerdrClient(socket_path).request("pane.read", {"pane_id": "p1"})

    text = result["read"]["text"]
    assert len(text.encode("utf-8")) <= PANE_READ_TEXT_LIMIT_BYTES
    assert text.endswith(tail)


def test_subscribe_yields_events_after_ack(tmp_path):
    asyncio.run(_subscribe_yields_events_after_ack(tmp_path))


async def _subscribe_yields_events_after_ack(tmp_path):
    socket_path = tmp_path / "herdr.sock"
    seen = []

    async def handle(reader, writer):
        line = await reader.readline()
        request = json.loads(line.decode("utf-8"))
        seen.append(request)
        writer.write(json.dumps({"id": request["id"], "result": {"type": "subscription_started"}}).encode("utf-8") + b"\n")
        writer.write(json.dumps({"event": "pane.agent_status_changed", "data": {"pane_id": "p1", "agent_status": "blocked"}}).encode("utf-8") + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await start_local_server(handle, socket_path)
    async with server:
        client = HerdrClient(socket_path)
        events = []
        async for event in client.subscribe([{"type": "pane.agent_status_changed", "pane_id": "p1"}]):
            events.append(event)
            break

    assert seen[0]["method"] == "events.subscribe"
    assert seen[0]["params"] == {"subscriptions": [{"type": "pane.agent_status_changed", "pane_id": "p1"}]}
    assert events == [{"event": "pane.agent_status_changed", "data": {"pane_id": "p1", "agent_status": "blocked"}}]


def test_check_subscription_returns_after_ack_without_waiting_for_event(tmp_path):
    asyncio.run(_check_subscription_returns_after_ack_without_waiting_for_event(tmp_path))


async def _check_subscription_returns_after_ack_without_waiting_for_event(tmp_path):
    socket_path = tmp_path / "herdr.sock"

    async def handle(reader, writer):
        line = await reader.readline()
        request = json.loads(line.decode("utf-8"))
        writer.write(
            json.dumps({"id": request["id"], "result": {"type": "subscription_started"}}).encode("utf-8")
            + b"\n"
        )
        await writer.drain()
        await asyncio.sleep(1)
        writer.close()
        await writer.wait_closed()

    server = await start_local_server(handle, socket_path)
    async with server:
        client = HerdrClient(socket_path, timeout=0.1)
        await asyncio.wait_for(
            client.check_subscription([{"type": "pane.created"}]),
            timeout=0.2,
        )


def test_check_subscription_times_out_when_ack_stalls(tmp_path):
    asyncio.run(_check_subscription_times_out_when_ack_stalls(tmp_path))


async def _check_subscription_times_out_when_ack_stalls(tmp_path):
    socket_path = tmp_path / "herdr.sock"

    async def handle(reader, writer):
        await reader.readline()
        await asyncio.sleep(1)
        writer.close()
        await writer.wait_closed()

    server = await start_local_server(handle, socket_path)
    async with server:
        client = HerdrClient(socket_path, timeout=0.01)
        with pytest.raises(asyncio.TimeoutError):
            await client.check_subscription([{"type": "pane.created"}])


def test_check_subscription_rejects_missing_ack(tmp_path):
    asyncio.run(_check_subscription_rejects_missing_ack(tmp_path))


async def _check_subscription_rejects_missing_ack(tmp_path):
    socket_path = tmp_path / "herdr.sock"

    async def handle(reader, writer):
        await reader.readline()
        writer.close()
        await writer.wait_closed()

    server = await start_local_server(handle, socket_path)
    async with server:
        client = HerdrClient(socket_path)
        with pytest.raises(HerdrError, match="closed before subscription ack"):
            await client.check_subscription([{"type": "pane.created"}])


def test_check_subscription_propagates_ack_error(tmp_path):
    asyncio.run(_check_subscription_propagates_ack_error(tmp_path))


async def _check_subscription_propagates_ack_error(tmp_path):
    socket_path = tmp_path / "herdr.sock"

    async def handle(reader, writer):
        line = await reader.readline()
        request = json.loads(line.decode("utf-8"))
        writer.write(
            json.dumps(
                {
                    "id": request["id"],
                    "error": {"code": "subscription_denied", "message": "not available"},
                }
            ).encode("utf-8")
            + b"\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await start_local_server(handle, socket_path)
    async with server:
        client = HerdrClient(socket_path)
        with pytest.raises(HerdrError, match="subscription_denied: not available"):
            await client.check_subscription([{"type": "pane.created"}])
