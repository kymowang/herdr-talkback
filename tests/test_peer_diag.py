import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

pytest.importorskip("aiohttp")
pytest.importorskip("cryptography")

from herdr_talkback.peer_diag import diagnose, inspect_cert, inspect_token, summary
from test_peer_http import make_node


def test_ping_is_credential_free_and_leaks_no_secrets(tmp_path):
    from herdr_talkback.peer_http import PeerServer
    import aiohttp

    async def scenario():
        config, store = make_node(tmp_path, "receiver", "sender")
        async with PeerServer(config, store, SimpleNamespace(wake=lambda target: None)) as server:
            peer = replace(config.peers["sender"], url=f"https://127.0.0.1:{server.bound_port}")
            connector = aiohttp.TCPConnector(ssl=peer.client_context(), force_close=True)
            async with aiohttp.ClientSession(connector=connector, trust_env=False) as session:
                async with session.get(peer.url + "/v1/ping") as response:
                    assert response.status == 200
                    payload = await response.json()
                    assert payload["ping"] is True
                    assert payload["node_id"] == "receiver"
                    assert payload["protocol_version"] == 1
                    assert set(payload) == {"ping", "protocol_version", "node_id", "instance_id",
                                            "version", "build"}
                    text = str(payload)
                    assert peer.token() not in text

    asyncio.run(scenario())


def test_diag_reports_each_layer_against_a_live_peer(tmp_path):
    from herdr_talkback.peer_http import PeerServer

    async def scenario():
        config, store = make_node(tmp_path, "receiver", "sender")
        async with PeerServer(config, store, SimpleNamespace(wake=lambda target: None)) as server:
            peer = replace(config.peers["sender"], url=f"https://127.0.0.1:{server.bound_port}")
            report = await diagnose(peer, "sender", timeout=5)
            steps = {step["step"]: step for step in report["steps"]}
            assert steps["tcp"]["ok"] is True
            assert steps["tls_handshake"]["ok"] is True
            assert steps["certificate_matches_url"]["ok"] is True
            assert steps["ping"]["ok"] is True
            assert steps["auth"]["ok"] is True
            assert steps["auth"]["status"] == 200
            assert report["local"]["cert"]["ok"] is True
            assert report["local"]["token"]["ok"] is True
            assert "exchange (manual" in summary(report)

    asyncio.run(scenario())


def test_diag_stops_at_tcp_when_the_peer_is_unreachable(tmp_path):
    pytest.importorskip("cryptography")
    from herdr_talkback.network_config import PeerConfig

    (tmp_path / "token").write_text("a" * 64, encoding="ascii")
    peer = PeerConfig("remote", "https://127.0.0.1:1", tmp_path / "missing.crt", tmp_path / "token")

    report = asyncio.run(diagnose(peer, "local", timeout=1))

    assert [step["step"] for step in report["steps"]] == ["tcp"]
    assert report["steps"][0]["ok"] is False


def test_token_inspection_never_needs_the_secret(tmp_path):
    token = tmp_path / "pair.token"
    token.write_text("b" * 64, encoding="ascii")
    token.chmod(0o600)
    assert inspect_token(token)["ok"] is True
    assert inspect_token(token).get("length") == 64

    token.write_text("not-a-token", encoding="ascii")
    result = inspect_token(token)
    assert result["ok"] is False
    assert "hexadecimal" in result["reason"]


def test_certificate_inspection_flags_missing_san(tmp_path):
    pytest.importorskip("cryptography")
    from herdr_talkback.certificates import provision

    provision(tmp_path / "tls", ["127.0.0.1"])
    cert = tmp_path / "tls" / "node.crt"

    assert inspect_cert(cert, "127.0.0.1")["ok"] is True
    mismatch = inspect_cert(cert, "10.0.0.9")
    assert mismatch["ok"] is False
    assert "no SAN" in mismatch["reason"]
    assert inspect_cert(tmp_path / "missing.crt", "127.0.0.1")["ok"] is False
