from pathlib import Path

import pytest

from herdr_talkback.config import load_config


def test_default_config_uses_herdr_socket_env(monkeypatch, tmp_path):
    socket_path = tmp_path / "herdr.sock"
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(socket_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    cfg = load_config(None)

    assert cfg.socket_path == socket_path
    assert cfg.state_path == tmp_path / ".local" / "state" / "herdr-talkback" / "state.db"
    assert cfg.daemon_socket_path == tmp_path / ".local" / "state" / "herdr-talkback" / "herdr-talkback.sock"
    assert cfg.roles == (
        "lead",
        "assistant",
        "dev-back",
        "dev-front",
        "ui",
        "runner",
        "codex1",
        "codex2",
        "system-bus",
    )
    assert cfg.aliases == {"system-bus": "lead"}
    assert cfg.delivery_max_attempts == 5
    assert cfg.delivery_retry_interval_seconds == 30
    assert cfg.notify_enabled is True
    assert cfg.notify_position == "bottom-right"
    assert cfg.notify_sound == "none"
    assert cfg.alerts_pane_role is None
    assert cfg.monitor_continue_cooldown_seconds == 120


def test_config_file_overrides_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/default.sock")
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
socket_path = "/tmp/custom.sock"
state_path = "/tmp/herdr-talkback.db"
daemon_socket_path = "/tmp/herdr-talkback.sock"
roles = ["lead", "runner"]

[aliases]
system-bus = "runner"

[delivery]
max_attempts = 3
retry_interval_seconds = 12
verify_timeout_ms = 7000

[monitor]
unknown_threshold_seconds = 45
continue_cooldown_seconds = 180
max_continues_per_30m = 2

[notify]
enabled = false
position = "top-right"
sound = "request"

[alerts]
pane_role = "runner"
""".strip(),
        encoding="utf-8",
    )

    cfg = load_config(config_file)

    assert cfg.socket_path == Path("/tmp/custom.sock")
    assert cfg.state_path == Path("/tmp/herdr-talkback.db")
    assert cfg.daemon_socket_path == Path("/tmp/herdr-talkback.sock")
    assert cfg.roles == ("lead", "runner")
    assert cfg.aliases == {"system-bus": "runner"}
    assert cfg.delivery_max_attempts == 3
    assert cfg.delivery_retry_interval_seconds == 12
    assert cfg.delivery_verify_timeout_ms == 7000
    assert cfg.notify_enabled is False
    assert cfg.notify_position == "top-right"
    assert cfg.notify_sound == "request"
    assert cfg.alerts_pane_role == "runner"
    assert cfg.monitor_unknown_threshold_seconds == 45
    assert cfg.monitor_continue_cooldown_seconds == 180
    assert cfg.monitor_max_continues_per_30m == 2


def test_default_config_file_is_loaded(monkeypatch, tmp_path):
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/default.sock")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    config_dir = tmp_path / ".config" / "herdr-talkback"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        """
socket_path = "/tmp/from-default-config.sock"

[delivery]
max_attempts = 9
retry_interval_seconds = 3
""".strip(),
        encoding="utf-8",
    )

    cfg = load_config(None)

    assert cfg.socket_path == Path("/tmp/from-default-config.sock")
    assert cfg.delivery_max_attempts == 9
    assert cfg.delivery_retry_interval_seconds == 3


def test_default_core_node_id(monkeypatch, tmp_path):
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(tmp_path / "herdr.sock"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert load_config(None).core_node_id == "linux-main"


def test_core_node_id_from_toml(monkeypatch, tmp_path):
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(tmp_path / "herdr.sock"))
    config_file = tmp_path / "config.toml"
    config_file.write_text('[core]\nnode_id = "workstation"\n', encoding="utf-8")

    assert load_config(config_file).core_node_id == "workstation"


@pytest.mark.parametrize("node_id", ["", " ", "linux/main", "linux main", "-linux"])
def test_rejects_malformed_core_node_id_before_state_construction(
    monkeypatch, tmp_path, node_id
):
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(tmp_path / "herdr.sock"))
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[core]\nnode_id = "{0}"\n'.format(node_id),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid node ID"):
        load_config(config_file)


def test_message_rules_config(tmp_path, monkeypatch):
    monkeypatch.setenv('HERDR_SOCKET_PATH', '/tmp/herdr.sock')
    path = tmp_path / 'config.toml'
    path.write_text('[[message_rules]]\nid="receipt"\nagents=["lead", "lead2", "scheduler"]\nappend="回执"\n', encoding='utf-8')
    assert load_config(path).message_rules[0].agents == ('lead', 'lead2', 'scheduler')
    path.write_text('message_rules=[]\n')
    assert load_config(path).message_rules == ()


@pytest.mark.parametrize('text', ['[node]\nnode_id="windows-dev"\n', '[core]\nlisten="0.0.0.0:8765"\n', '[core]\npublic_url="ws://host/node"\n'])
def test_rejects_removed_remote_configuration(tmp_path, monkeypatch, text):
    monkeypatch.setenv('HERDR_SOCKET_PATH', '/tmp/herdr.sock')
    path = tmp_path / 'config.toml'
    path.write_text(text)
    with pytest.raises(ValueError, match='local Herdr'):
        load_config(path)
