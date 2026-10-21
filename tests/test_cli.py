import asyncio
import sqlite3
import sys
import json
from pathlib import Path
from types import SimpleNamespace
from types import ModuleType

import herdr_talkback.cli as cli
import herdr_talkback.service as service_module
import pytest
from herdr_talkback.cli import build_parser
from herdr_talkback.core_types import CoreMessage, CoreMessageState
from herdr_talkback.state import TalkbackState
from herdr_talkback.types import MessageState


def test_parser_accepts_panes_command():
    args = build_parser().parse_args(["panes"])
    assert args.command == "panes"
    assert args.config is None


def test_parser_accepts_send_command():
    args = build_parser().parse_args(["send", "[lead→dev-back] ping", "--dry-run"])
    assert args.command == "send"
    assert args.message == "[lead→dev-back] ping"
    assert args.dry_run is True


def test_parser_accepts_unquoted_send_message_parts():
    args = build_parser().parse_args(["send", "[lead→dev-back]", "ping", "--dry-run"])
    assert args.command == "send"
    assert args.message == "[lead→dev-back] ping"
    assert args.dry_run is True


def test_parser_accepts_send_direct_command():
    args = build_parser().parse_args(["send", "[lead→dev-back] ping", "--direct"])
    assert args.command == "send"
    assert args.message == "[lead→dev-back] ping"
    assert args.direct is True


def test_parser_accepts_input_command():
    args = build_parser().parse_args(["input", "assistant", "--expected", "[lead→assistant] ping", "--lines", "40"])
    assert args.command == "input"
    assert args.target == "assistant"
    assert args.expected == "[lead→assistant] ping"
    assert args.lines == 40


def test_parser_accepts_status_command():
    args = build_parser().parse_args(["status", "assistant", "--json"])
    assert args.command == "status"
    assert args.target == "assistant"
    assert args.json is True


def test_parser_accepts_exec_command():
    args = build_parser().parse_args(["exec", "lead,dev-back", "/status"])
    assert args.command == "exec"
    assert args.targets == "lead,dev-back"
    assert args.exec_command == "/status"


def test_main_returns_130_on_keyboard_interrupt(monkeypatch):
    async def raise_keyboard_interrupt(args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_main_async", raise_keyboard_interrupt)

    assert cli.main(["tail"]) == 130


@pytest.mark.parametrize(
    "command",
    (["queue"], ["tail", "--limit", "1"]),
    ids=("queue", "tail"),
)
def test_state_read_command_uses_configured_node_for_first_migration(monkeypatch, tmp_path, command):
    asyncio.run(_state_read_command_uses_configured_node_for_first_migration(monkeypatch, tmp_path, command))


async def _state_read_command_uses_configured_node_for_first_migration(monkeypatch, tmp_path, command):
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    state_path = tmp_path / "legacy.db"
    with sqlite3.connect(state_path) as db:
        db.executescript(
            """
            create table messages (
                message_id text primary key,
                source text not null,
                target text not null,
                target_pane_id text not null,
                raw_text text not null,
                state text not null,
                reason text not null default '',
                attempts integer not null default 0,
                created_at real not null,
                updated_at real not null
            );
            insert into messages values
                ('legacy', 'lead', 'runner', 'p1', '[lead->runner] ping', 'queued', '', 0, 10, 11);
            """
        )
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        'state_path = "{0}"\n\n[core]\nnode_id = "workstation"\n'.format(state_path),
        encoding="utf-8",
    )

    args = build_parser().parse_args(["--config", str(config_file)] + command)
    assert await cli._main_async(args) == 0

    with sqlite3.connect(state_path) as db:
        row = db.execute(
            "select source_endpoint, target_endpoint from messages where message_id = 'legacy'"
        ).fetchone()
        version = db.execute("select value from schema_meta where key = 'version'").fetchone()[0]
    assert row == ("workstation/lead", "workstation/runner")
    assert version == "5"


@pytest.mark.parametrize(
    "command",
    (["queue"], ["tail", "--limit", "1"]),
    ids=("queue", "tail"),
)
def test_state_read_command_rejects_invalid_node_before_migration(tmp_path, command):
    asyncio.run(_state_read_command_rejects_invalid_node_before_migration(tmp_path, command))


async def _state_read_command_rejects_invalid_node_before_migration(tmp_path, command):
    state_path = tmp_path / "legacy.db"
    with sqlite3.connect(state_path) as db:
        db.execute(
            """
            create table messages (
                message_id text primary key,
                source text not null,
                target text not null,
                target_pane_id text not null,
                raw_text text not null,
                state text not null,
                reason text not null default '',
                attempts integer not null default 0,
                created_at real not null,
                updated_at real not null
            )
            """
        )
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        'state_path = "{0}"\n\n[core]\nnode_id = "remote/node"\n'.format(state_path),
        encoding="utf-8",
    )

    args = build_parser().parse_args(["--config", str(config_file)] + command)
    with pytest.raises(ValueError, match="invalid node ID"):
        await cli._main_async(args)

    with sqlite3.connect(state_path) as db:
        columns = {row[1] for row in db.execute("pragma table_info(messages)")}
    assert "source_endpoint" not in columns


def test_tail_reads_state_without_herdr_socket(monkeypatch, tmp_path, capsys):
    asyncio.run(_tail_reads_state_without_herdr_socket(monkeypatch, tmp_path, capsys))


async def _tail_reads_state_without_herdr_socket(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    state_path = tmp_path / "state.db"
    state = TalkbackState(state_path)
    state.init()
    state.record_event("delivery", {"message_id": "msg_1"})
    config_file = tmp_path / "herdr-talkback.toml"
    config_file.write_text(f'state_path = "{state_path}"\n', encoding="utf-8")

    def fail_herdr_client(socket_path):
        raise AssertionError("tail must not construct HerdrClient")

    class FailingRoleRegistry:
        def __init__(self, client, roles, aliases):
            raise AssertionError("tail must not construct RoleRegistry")

    monkeypatch.setattr(cli, "HerdrClient", fail_herdr_client)
    monkeypatch.setattr(cli, "RoleRegistry", FailingRoleRegistry)

    args = build_parser().parse_args(["--config", str(config_file), "tail", "--limit", "1"])
    result = await cli._main_async(args)

    assert result == 0
    output = capsys.readouterr().out
    assert '"kind": "delivery"' in output
    assert '"message_id": "msg_1"' in output


def test_queue_reads_active_messages_without_herdr_socket(monkeypatch, tmp_path, capsys):
    asyncio.run(_queue_reads_active_messages_without_herdr_socket(monkeypatch, tmp_path, capsys))


async def _queue_reads_active_messages_without_herdr_socket(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    state_path = tmp_path / "state.db"
    state = TalkbackState(state_path)
    state.init()
    state.create_message("msg_1", "lead", "dev-back", "p-back", "[lead→dev-back] retry")
    state.update_message_state("msg_1", MessageState.RETRYING, "target input occupied")
    state.record_attempt("msg_1", 1, "checking_input", "retrying", "target input occupied")
    state.create_message("msg_2", "lead", "runner", "p-runner", "[lead→runner] done")
    state.update_message_state("msg_2", MessageState.DELIVERED, "input cleared")
    for message_id, message_state in (
        ("msg_waiting", CoreMessageState.WAITING_ENDPOINT),
        ("msg_unknown", CoreMessageState.DELIVERY_UNKNOWN),
        ("msg_failed", CoreMessageState.FAILED),
    ):
        state.enqueue_message(
            CoreMessage(
                message_id=message_id,
                source_endpoint="linux-main/lead",
                target_endpoint="linux-main/dev-back",
                body="core message",
                raw_text="[lead→dev-back] core message",
                state=message_state,
                conversation_id=message_id,
            )
        )
    config_file = tmp_path / "herdr-talkback.toml"
    config_file.write_text(f'state_path = "{state_path}"\n', encoding="utf-8")

    def fail_herdr_client(socket_path):
        raise AssertionError("queue must not construct HerdrClient")

    monkeypatch.setattr(cli, "HerdrClient", fail_herdr_client)

    args = build_parser().parse_args(["--config", str(config_file), "queue"])
    result = await cli._main_async(args)

    assert result == 0
    output = capsys.readouterr().out
    assert '"message_id": "msg_1"' in output
    assert '"state": "retrying"' in output
    assert '"message_id": "msg_waiting"' in output
    assert '"state": "waiting_endpoint"' in output
    assert '"message_id": "msg_unknown"' in output
    assert '"state": "delivery_unknown"' in output
    assert "msg_2" not in output
    assert "msg_failed" not in output


def test_alerts_reads_open_alerts_without_herdr_socket(monkeypatch, tmp_path, capsys):
    asyncio.run(_alerts_reads_open_alerts_without_herdr_socket(monkeypatch, tmp_path, capsys))


async def _alerts_reads_open_alerts_without_herdr_socket(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    state_path = tmp_path / "state.db"
    state = TalkbackState(state_path)
    state.init()
    state.record_alert("delivery_failed", "Herdr Talkback delivery blocked", "lead -> runner failed", {"role": "runner"})
    cleared_id = state.record_alert("status_unknown", "Herdr Talkback status unknown", "runner unknown", {})
    state.clear_alerts([cleared_id])
    config_file = tmp_path / "herdr-talkback.toml"
    config_file.write_text(f'state_path = "{state_path}"\n', encoding="utf-8")

    def fail_herdr_client(socket_path):
        raise AssertionError("alerts must not construct HerdrClient")

    monkeypatch.setattr(cli, "HerdrClient", fail_herdr_client)

    args = build_parser().parse_args(["--config", str(config_file), "alerts"])
    result = await cli._main_async(args)

    assert result == 0
    output = capsys.readouterr().out
    assert "#1 delivery_failed Herdr Talkback delivery blocked" in output
    assert "lead -> runner failed" in output
    assert json.dumps({"role": "runner"}, ensure_ascii=False) in output
    assert "status_unknown" not in output


def test_alerts_clear_marks_open_alerts_without_herdr_socket(monkeypatch, tmp_path, capsys):
    asyncio.run(_alerts_clear_marks_open_alerts_without_herdr_socket(monkeypatch, tmp_path, capsys))


async def _alerts_clear_marks_open_alerts_without_herdr_socket(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    state_path = tmp_path / "state.db"
    state = TalkbackState(state_path)
    state.init()
    state.record_alert("delivery_failed", "Herdr Talkback delivery blocked", "lead -> runner failed", {})
    config_file = tmp_path / "herdr-talkback.toml"
    config_file.write_text(f'state_path = "{state_path}"\n', encoding="utf-8")

    args = build_parser().parse_args(["--config", str(config_file), "alerts", "clear"])
    result = await cli._main_async(args)

    assert result == 0
    assert capsys.readouterr().out == "cleared 1 alerts\n"
    assert state.list_open_alerts() == []


def test_alerts_lists_latest_twenty_by_default(monkeypatch, tmp_path, capsys):
    asyncio.run(_alerts_lists_latest_twenty_by_default(monkeypatch, tmp_path, capsys))


async def _alerts_lists_latest_twenty_by_default(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    state_path = tmp_path / "state.db"
    state = TalkbackState(state_path)
    state.init()
    for index in range(25):
        state.record_alert("test", f"title {index}", f"body {index}", {})
    config_file = tmp_path / "herdr-talkback.toml"
    config_file.write_text(f'state_path = "{state_path}"\n', encoding="utf-8")

    args = build_parser().parse_args(["--config", str(config_file), "alerts"])
    result = await cli._main_async(args)

    assert result == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 20
    assert "#6 test title 5" in lines[0]
    assert "#25 test title 24" in lines[-1]


def test_alerts_can_list_open_alerts_after_id(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    first = state.record_alert("test", "first", "body 1", {})
    second = state.record_alert("test", "second", "body 2", {})
    cleared = state.record_alert("test", "cleared", "body 3", {})
    state.clear_alerts([cleared])

    alerts = state.list_open_alerts_after(first)

    assert [alert["id"] for alert in alerts] == [second]
    assert alerts[0]["title"] == "second"


def test_parser_accepts_alerts_follow():
    args = build_parser().parse_args(["alerts", "--follow", "--limit", "5"])
    assert args.command == "alerts"
    assert args.follow is True
    assert args.limit == 5


def test_parser_accepts_alerts_json():
    args = build_parser().parse_args(["alerts", "--json"])
    assert args.command == "alerts"
    assert args.json is True


def test_format_alert_includes_local_time():
    line = cli._format_alert(
        {
            "id": 7,
            "kind": "delivery_failed",
            "title": "Herdr Talkback delivery blocked",
            "body": "lead -> runner failed",
            "payload": {"role": "runner"},
            "created_at": 1782825480.0,
        }
    )

    assert line.startswith("[")
    assert "] #7 delivery_failed Herdr Talkback delivery blocked" in line
    assert "lead -> runner failed" in line
    assert json.dumps({"role": "runner"}, ensure_ascii=False) in line


def test_send_dry_run_submits_to_daemon_without_herdr_socket(monkeypatch, tmp_path, capsys):
    asyncio.run(_send_dry_run_submits_to_daemon_without_herdr_socket(monkeypatch, tmp_path, capsys))


async def _send_dry_run_submits_to_daemon_without_herdr_socket(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    daemon_socket = tmp_path / "herdr-talkback.sock"
    config_file = tmp_path / "herdr-talkback.toml"
    config_file.write_text(f'daemon_socket_path = "{daemon_socket}"\n', encoding="utf-8")
    requests = []

    async def fake_submit(socket_path, payload):
        requests.append((socket_path, payload))
        return {
            "ok": True,
            "dry_run": True,
            "source": "lead",
            "target": "dev-back",
            "resolved_target": "dev-back",
            "pane_id": "p-back",
        }

    def fail_herdr_client(socket_path):
        raise AssertionError("daemon send must not construct HerdrClient")

    monkeypatch.setattr(cli, "submit_daemon_request", fake_submit)
    monkeypatch.setattr(cli, "HerdrClient", fail_herdr_client)

    args = build_parser().parse_args(["--config", str(config_file), "send", "[lead→dev-back] ping", "--dry-run"])
    result = await cli._main_async(args)

    assert result == 0
    assert requests == [
        (
            daemon_socket,
            {"type": "send", "message": "[lead→dev-back] ping", "dry_run": True},
        )
    ]
    assert capsys.readouterr().out == "dry-run: lead -> dev-back resolved=dev-back pane=p-back\n"


def test_retry_submits_stored_message_to_daemon_and_marks_old_message(monkeypatch, tmp_path, capsys):
    asyncio.run(_retry_submits_stored_message_to_daemon_and_marks_old_message(monkeypatch, tmp_path, capsys))


async def _retry_submits_stored_message_to_daemon_and_marks_old_message(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    daemon_socket = tmp_path / "herdr-talkback.sock"
    state_path = tmp_path / "state.db"
    state = TalkbackState(state_path)
    state.init()
    state.create_message("msg_1", "lead", "assistant", "p-assistant", "[lead→assistant] retry me")
    state.update_message_state("msg_1", MessageState.RETRYING, "target input occupied")
    config_file = tmp_path / "herdr-talkback.toml"
    config_file.write_text(
        f'state_path = "{state_path}"\ndaemon_socket_path = "{daemon_socket}"\n',
        encoding="utf-8",
    )
    requests = []

    async def fake_submit(socket_path, payload):
        requests.append((socket_path, payload))
        return {"ok": True, "queued": True, "message_id": "msg_2"}

    monkeypatch.setattr(cli, "submit_daemon_request", fake_submit)

    args = build_parser().parse_args(["--config", str(config_file), "retry", "msg_1"])
    result = await cli._main_async(args)

    assert result == 0
    assert requests == [
        (
            daemon_socket,
            {"type": "send", "message": "[lead→assistant] retry me", "dry_run": False},
        )
    ]
    assert state.get_message("msg_1")["state"] == "failed"
    assert state.get_message("msg_1")["reason"] == "superseded by retry msg_2"
    assert '"message_id": "msg_2"' in capsys.readouterr().out


def test_send_direct_dry_run_resolves_pane_without_delivering(monkeypatch, tmp_path, capsys):
    asyncio.run(_send_direct_dry_run_resolves_pane_without_delivering(monkeypatch, tmp_path, capsys))


async def _send_direct_dry_run_resolves_pane_without_delivering(monkeypatch, tmp_path, capsys):
    cfg = SimpleNamespace(
        core_node_id="linux-main",
        socket_path=Path("/tmp/herdr.sock"),
        state_path=tmp_path / "missing-state" / "state.db",
        daemon_socket_path=tmp_path / "herdr-talkback.sock",
        roles=("lead", "dev-back", "system-bus"),
        aliases={"system-bus": "lead"},
        delivery_max_attempts=5,
        delivery_retry_interval_seconds=30,
        delivery_verify_timeout_ms=5000,
        notify_enabled=True,
        notify_position="bottom-right",
        notify_sound="none",
        alerts_pane_role=None,
    )
    fake_client = FakeHerdrClient()

    class FailingDeliveryService:
        def __init__(self, client, registry, state, max_attempts=5):
            raise AssertionError("dry-run must not construct DeliveryService")

    monkeypatch.setattr(cli, "load_config", lambda path: cfg)
    monkeypatch.setattr(cli, "HerdrClient", lambda socket_path: fake_client)
    monkeypatch.setattr(cli, "DeliveryService", FailingDeliveryService)

    args = build_parser().parse_args(["send", "[lead→dev-back] ping", "--dry-run", "--direct"])
    result = await cli._main_async(args)

    assert result == 0
    assert capsys.readouterr().out == "dry-run: lead -> dev-back resolved=dev-back pane=p-back\n"
    assert fake_client.requests == [("pane.list", {})]
    assert not cfg.state_path.parent.exists()


def test_send_direct_dry_run_does_not_migrate_existing_schema(monkeypatch, tmp_path, capsys):
    asyncio.run(_send_direct_dry_run_does_not_migrate_existing_schema(monkeypatch, tmp_path, capsys))


async def _send_direct_dry_run_does_not_migrate_existing_schema(monkeypatch, tmp_path, capsys):
    state_path = tmp_path / "state.db"
    TalkbackState(state_path).init()
    with sqlite3.connect(state_path) as db:
        db.execute("update schema_meta set value = '3' where key = 'version'")
    original_bytes = state_path.read_bytes()
    cfg = SimpleNamespace(
        core_node_id="linux-main",
        socket_path=Path("/tmp/herdr.sock"),
        state_path=state_path,
        daemon_socket_path=tmp_path / "herdr-talkback.sock",
        roles=("lead", "dev-back", "system-bus"),
        aliases={"system-bus": "lead"},
        delivery_max_attempts=5,
        delivery_retry_interval_seconds=30,
        notify_enabled=True,
        notify_position="bottom-right",
        notify_sound="none",
    )

    monkeypatch.setattr(cli, "load_config", lambda path: cfg)
    monkeypatch.setattr(cli, "HerdrClient", lambda socket_path: FakeHerdrClient())

    args = build_parser().parse_args(["send", "[lead→dev-back] ping", "--dry-run", "--direct"])
    assert await cli._main_async(args) == 0

    assert state_path.read_bytes() == original_bytes
    with sqlite3.connect(state_path) as db:
        assert db.execute("select value from schema_meta where key = 'version'").fetchone() == ("3",)
    assert capsys.readouterr().out == "dry-run: lead -> dev-back resolved=dev-back pane=p-back\n"


def test_send_direct_reports_resolve_failure_without_traceback(monkeypatch, tmp_path, capsys):
    asyncio.run(_send_direct_reports_resolve_failure_without_traceback(monkeypatch, tmp_path, capsys))


async def _send_direct_reports_resolve_failure_without_traceback(monkeypatch, tmp_path, capsys):
    cfg = SimpleNamespace(
        socket_path=Path("/tmp/herdr.sock"),
        state_path=tmp_path / "state.db",
        daemon_socket_path=tmp_path / "herdr-talkback.sock",
        roles=(),
        aliases={},
        delivery_max_attempts=5,
        delivery_retry_interval_seconds=30,
        delivery_verify_timeout_ms=5000,
        notify_enabled=True,
        notify_position="bottom-right",
        notify_sound="none",
        alerts_pane_role=None,
    )
    monkeypatch.setattr(cli, "load_config", lambda path: cfg)
    monkeypatch.setattr(cli, "HerdrClient", lambda socket_path: FakeHerdrClient())

    args = build_parser().parse_args(["send", "[lead→missing] ping", "--direct"])
    result = await cli._main_async(args)

    assert result == 1
    output = capsys.readouterr().out
    assert '"ok": false' in output
    assert "role has no active pane: missing" in output


def test_input_command_prints_parser_state_and_raw_content(monkeypatch, tmp_path, capsys):
    asyncio.run(_input_command_prints_parser_state_and_raw_content(monkeypatch, tmp_path, capsys))


async def _input_command_prints_parser_state_and_raw_content(monkeypatch, tmp_path, capsys):
    cfg = SimpleNamespace(
        socket_path=Path("/tmp/herdr.sock"),
        state_path=tmp_path / "state.db",
        daemon_socket_path=tmp_path / "herdr-talkback.sock",
        roles=(),
        aliases={},
        delivery_max_attempts=5,
        delivery_retry_interval_seconds=30,
        delivery_verify_timeout_ms=5000,
        notify_enabled=True,
        notify_position="bottom-right",
        notify_sound="none",
        alerts_pane_role=None,
    )
    client = FakeHerdrClient(
        pane_read_text="❯\u00a0\x1b[0m\x1b[2mping 一下 dev-back 看护栏修复进度\x1b[0m"
    )
    monkeypatch.setattr(cli, "load_config", lambda path: cfg)
    monkeypatch.setattr(cli, "HerdrClient", lambda socket_path: client)

    args = build_parser().parse_args(["input", "assistant", "--expected", "[lead→assistant] ping"])
    result = await cli._main_async(args)

    assert result == 0
    output = capsys.readouterr().out
    assert "target: assistant" in output
    assert "pane_id: p-assistant" in output
    assert "kind: empty" in output
    assert "text_after_prompt: ping 一下 dev-back 看护栏修复进度" in output
    assert "raw_ansi:" in output
    assert "\\x1b[2m" in output


def test_status_command_prints_role_status(monkeypatch, tmp_path, capsys):
    asyncio.run(_status_command_prints_role_status(monkeypatch, tmp_path, capsys))


async def _status_command_prints_role_status(monkeypatch, tmp_path, capsys):
    cfg = SimpleNamespace(
        socket_path=Path("/tmp/herdr.sock"),
        state_path=tmp_path / "state.db",
        daemon_socket_path=tmp_path / "herdr-talkback.sock",
        roles=(),
        aliases={},
        delivery_max_attempts=5,
        delivery_retry_interval_seconds=30,
        delivery_verify_timeout_ms=5000,
        notify_enabled=True,
        notify_position="bottom-right",
        notify_sound="none",
        alerts_pane_role=None,
    )
    client = FakeHerdrClient()
    monkeypatch.setattr(cli, "load_config", lambda path: cfg)
    monkeypatch.setattr(cli, "HerdrClient", lambda socket_path: client)

    args = build_parser().parse_args(["status", "dev-back"])
    result = await cli._main_async(args)

    assert result == 0
    assert capsys.readouterr().out == (
        "target: dev-back\n"
        "role: dev-back\n"
        "pane_id: p-back\n"
        "agent: codex\n"
        "agent_status: working\n"
        "workspace_id: w1\n"
        "tab_id: t2\n"
        "cwd: /repo/backend\n"
    )
    assert client.requests == [("pane.list", {})]


def test_status_command_accepts_pane_id(monkeypatch, tmp_path, capsys):
    asyncio.run(_status_command_accepts_pane_id(monkeypatch, tmp_path, capsys))


async def _status_command_accepts_pane_id(monkeypatch, tmp_path, capsys):
    cfg = SimpleNamespace(
        socket_path=Path("/tmp/herdr.sock"),
        state_path=tmp_path / "state.db",
        daemon_socket_path=tmp_path / "herdr-talkback.sock",
        roles=(),
        aliases={},
        delivery_max_attempts=5,
        delivery_retry_interval_seconds=30,
        delivery_verify_timeout_ms=5000,
        notify_enabled=True,
        notify_position="bottom-right",
        notify_sound="none",
        alerts_pane_role=None,
    )
    client = FakeHerdrClient()
    monkeypatch.setattr(cli, "load_config", lambda path: cfg)
    monkeypatch.setattr(cli, "HerdrClient", lambda socket_path: client)

    args = build_parser().parse_args(["status", "p-unlabeled"])
    result = await cli._main_async(args)

    assert result == 0
    output = capsys.readouterr().out
    assert "target: p-unlabeled\n" in output
    assert "role: \n" in output
    assert "pane_id: p-unlabeled\n" in output
    assert "agent_status: blocked\n" in output


def test_status_command_prints_json(monkeypatch, tmp_path, capsys):
    asyncio.run(_status_command_prints_json(monkeypatch, tmp_path, capsys))


async def _status_command_prints_json(monkeypatch, tmp_path, capsys):
    cfg = SimpleNamespace(
        socket_path=Path("/tmp/herdr.sock"),
        state_path=tmp_path / "state.db",
        daemon_socket_path=tmp_path / "herdr-talkback.sock",
        roles=(),
        aliases={},
        delivery_max_attempts=5,
        delivery_retry_interval_seconds=30,
        delivery_verify_timeout_ms=5000,
        notify_enabled=True,
        notify_position="bottom-right",
        notify_sound="none",
        alerts_pane_role=None,
    )
    monkeypatch.setattr(cli, "load_config", lambda path: cfg)
    monkeypatch.setattr(cli, "HerdrClient", lambda socket_path: FakeHerdrClient())

    args = build_parser().parse_args(["status", "assistant", "--json"])
    result = await cli._main_async(args)

    assert result == 0
    assert json.loads(capsys.readouterr().out) == {
        "target": "assistant",
        "role": "assistant",
        "pane_id": "p-assistant",
        "agent": "claude",
        "agent_status": "idle",
        "workspace_id": "w1",
        "tab_id": "t3",
        "cwd": None,
    }


def test_status_command_reports_missing_target_without_traceback(monkeypatch, tmp_path, capsys):
    asyncio.run(_status_command_reports_missing_target_without_traceback(monkeypatch, tmp_path, capsys))


async def _status_command_reports_missing_target_without_traceback(monkeypatch, tmp_path, capsys):
    cfg = SimpleNamespace(
        socket_path=Path("/tmp/herdr.sock"),
        state_path=tmp_path / "state.db",
        daemon_socket_path=tmp_path / "herdr-talkback.sock",
        roles=(),
        aliases={},
        delivery_max_attempts=5,
        delivery_retry_interval_seconds=30,
        delivery_verify_timeout_ms=5000,
        notify_enabled=True,
        notify_position="bottom-right",
        notify_sound="none",
        alerts_pane_role=None,
    )
    monkeypatch.setattr(cli, "load_config", lambda path: cfg)
    monkeypatch.setattr(cli, "HerdrClient", lambda socket_path: FakeHerdrClient())

    args = build_parser().parse_args(["status", "missing"])
    result = await cli._main_async(args)

    assert result == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "error": "pane not found: missing",
    }


def test_exec_command_sends_control_to_multiple_targets(monkeypatch, tmp_path, capsys):
    asyncio.run(_exec_command_sends_control_to_multiple_targets(monkeypatch, tmp_path, capsys))


async def _exec_command_sends_control_to_multiple_targets(monkeypatch, tmp_path, capsys):
    cfg = SimpleNamespace(
        socket_path=Path("/tmp/herdr.sock"),
        state_path=tmp_path / "state.db",
        daemon_socket_path=tmp_path / "herdr-talkback.sock",
        roles=(),
        aliases={},
        delivery_max_attempts=5,
        delivery_retry_interval_seconds=30,
        delivery_verify_timeout_ms=5000,
        notify_enabled=True,
        notify_position="bottom-right",
        notify_sound="none",
        alerts_pane_role=None,
    )
    client = FakeHerdrClient(pane_read_text="❯\u00a0")
    monkeypatch.setattr(cli, "load_config", lambda path: cfg)
    monkeypatch.setattr(cli, "HerdrClient", lambda socket_path: client)

    args = build_parser().parse_args(["exec", "lead,dev-back", "/status"])
    result = await cli._main_async(args)

    assert result == 0
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == [
        {
            "target": "lead",
            "pane_id": "p-lead",
            "delivered": True,
            "reason": "control sent",
            "attempts": 1,
        },
        {
            "target": "dev-back",
            "pane_id": "p-back",
            "delivered": True,
            "reason": "control sent",
            "attempts": 1,
        },
    ]
    assert ("pane.send_input", {"pane_id": "p-lead", "text": "/status", "keys": ["enter"]}) in client.requests
    assert ("pane.send_input", {"pane_id": "p-back", "text": "/status", "keys": ["enter"]}) in client.requests


def test_exec_command_reports_missing_target_and_continues(monkeypatch, tmp_path, capsys):
    asyncio.run(_exec_command_reports_missing_target_and_continues(monkeypatch, tmp_path, capsys))


async def _exec_command_reports_missing_target_and_continues(monkeypatch, tmp_path, capsys):
    cfg = SimpleNamespace(
        socket_path=Path("/tmp/herdr.sock"),
        state_path=tmp_path / "state.db",
        daemon_socket_path=tmp_path / "herdr-talkback.sock",
        roles=(),
        aliases={},
        delivery_max_attempts=5,
        delivery_retry_interval_seconds=30,
        delivery_verify_timeout_ms=5000,
        notify_enabled=True,
        notify_position="bottom-right",
        notify_sound="none",
        alerts_pane_role=None,
    )
    client = FakeHerdrClient(pane_read_text="❯\u00a0")
    monkeypatch.setattr(cli, "load_config", lambda path: cfg)
    monkeypatch.setattr(cli, "HerdrClient", lambda socket_path: client)

    args = build_parser().parse_args(["exec", "lead,missing", "continue"])
    result = await cli._main_async(args)

    assert result == 1
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == [
        {
            "target": "lead",
            "pane_id": "p-lead",
            "delivered": True,
            "reason": "control sent",
            "attempts": 1,
        },
        {
            "target": "missing",
            "pane_id": None,
            "delivered": False,
            "reason": "pane not found: missing",
            "attempts": 0,
        },
    ]


def test_send_direct_persists_configured_local_node(monkeypatch, tmp_path, capsys):
    asyncio.run(_send_direct_persists_configured_local_node(monkeypatch, tmp_path, capsys))


async def _send_direct_persists_configured_local_node(monkeypatch, tmp_path, capsys):
    cfg = SimpleNamespace(
        core_node_id="workstation",
        socket_path=Path("/tmp/herdr.sock"),
        state_path=tmp_path / "state.db",
        daemon_socket_path=tmp_path / "herdr-talkback.sock",
        roles=("lead", "dev-back"),
        aliases={},
        delivery_max_attempts=1,
        delivery_retry_interval_seconds=0,
        notify_enabled=False,
        notify_position="bottom-right",
        notify_sound="none",
    )

    class DeliveringHerdrClient(FakeHerdrClient):
        async def request(self, method, params):
            if method == "pane.list":
                return await super().request(method, params)
            self.requests.append((method, params))
            if method == "pane.read":
                return {"read": {"text": "❯ \x1b[38;2;136;136;136mType a message\x1b[0m"}}
            if method == "pane.send_input":
                return {"type": "input_sent"}
            raise AssertionError(method)

    monkeypatch.setattr(cli, "load_config", lambda path: cfg)
    monkeypatch.setattr(cli, "HerdrClient", lambda socket_path: DeliveringHerdrClient())

    args = build_parser().parse_args(["send", "[lead→dev-back] ping", "--direct"])
    assert await cli._main_async(args) == 0

    with sqlite3.connect(cfg.state_path) as db:
        row = db.execute("select source_endpoint, target_endpoint from messages").fetchone()
    assert row == ("workstation/lead", "workstation/dev-back")
    assert '"delivered": true' in capsys.readouterr().out


def test_send_reports_when_daemon_is_unavailable(monkeypatch, tmp_path, capsys):
    asyncio.run(_send_reports_when_daemon_is_unavailable(monkeypatch, tmp_path, capsys))


async def _send_reports_when_daemon_is_unavailable(monkeypatch, tmp_path, capsys):
    daemon_socket = tmp_path / "herdr-talkback.sock"
    config_file = tmp_path / "herdr-talkback.toml"
    config_file.write_text(f'daemon_socket_path = "{daemon_socket}"\n', encoding="utf-8")

    async def fail_submit(socket_path, payload):
        raise FileNotFoundError(str(socket_path))

    monkeypatch.setattr(cli, "submit_daemon_request", fail_submit)

    args = build_parser().parse_args(["--config", str(config_file), "send", "[lead→dev-back] ping"])
    result = await cli._main_async(args)

    assert result == 1
    output = capsys.readouterr().out
    assert '"ok": false' in output
    assert "herdr-talkback daemon is not reachable" in output
    assert str(daemon_socket) in output


def test_serve_dry_run_uses_existing_one_shot_service_interface(monkeypatch, tmp_path):
    asyncio.run(_serve_dry_run_uses_existing_one_shot_service_interface(monkeypatch, tmp_path))


async def _serve_dry_run_uses_existing_one_shot_service_interface(monkeypatch, tmp_path):
    cfg = SimpleNamespace(
        socket_path=Path("/tmp/herdr.sock"),
        state_path=tmp_path / "state.db",
        daemon_socket_path=tmp_path / "herdr-talkback.sock",
    )
    calls = []

    async def fake_run_service(config, dry_run=False):
        calls.append((config, dry_run))
        return 0

    def fail_dependency(*args, **kwargs):
        raise AssertionError("serve assembly belongs to herdr_talkback.service")

    monkeypatch.setattr(cli, "load_config", lambda path: cfg)
    monkeypatch.setattr(service_module, "run_service", fake_run_service, raising=False)
    monkeypatch.setattr(cli, "TalkbackState", fail_dependency)
    monkeypatch.setattr(cli, "HerdrClient", fail_dependency)

    args = build_parser().parse_args(["serve", "--dry-run"])
    result = await cli._main_async(args)

    assert result == 0
    assert calls == [(cfg, True)]


class FakeHerdrClient:
    def __init__(self, pane_read_text=None):
        self.requests = []
        self.pane_read_text = pane_read_text

    async def request(self, method, params):
        self.requests.append((method, params))
        if method == "pane.list":
            return {
                "panes": [
                    {
                        "label": "lead",
                        "agent": "codex",
                        "agent_status": "idle",
                        "pane_id": "p-lead",
                        "workspace_id": "w1",
                        "tab_id": "t1",
                    },
                    {
                        "label": "dev-back",
                        "agent": "codex",
                        "agent_status": "working",
                        "pane_id": "p-back",
                        "workspace_id": "w1",
                        "tab_id": "t2",
                        "cwd": "/repo/backend",
                    },
                    {
                        "label": "assistant",
                        "agent": "claude",
                        "agent_status": "idle",
                        "pane_id": "p-assistant",
                        "workspace_id": "w1",
                        "tab_id": "t3",
                    },
                    {
                        "agent": "codex",
                        "agent_status": "blocked",
                        "pane_id": "p-unlabeled",
                        "workspace_id": "w1",
                        "tab_id": "t4",
                    },
                ]
            }
        if method == "pane.read":
            return {"read": {"text": self.pane_read_text or "", "pane_id": params["pane_id"]}}
        if method == "pane.send_input":
            return {"ok": True}
        raise AssertionError(method)


def test_node_command_is_removed():
    with pytest.raises(SystemExit):
        build_parser().parse_args(['node', '--config', 'node.toml'])
