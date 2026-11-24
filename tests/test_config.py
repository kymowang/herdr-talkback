import os
from pathlib import Path

import pytest

from herdr_talkback import config
from herdr_talkback.config import load_config


def test_default_config_uses_herdr_socket_env(monkeypatch, tmp_path):
    socket_path = tmp_path / "herdr.sock"
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(socket_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))

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
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    if os.name == "nt":
        config_dir = (tmp_path / "AppData" / "Roaming" / "herdr" / "plugins"
                      / "config" / "herdr-talkback")
    else:
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
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))

    assert load_config(None).core_node_id == "linux-main"


def test_default_config_path_prefers_plugin_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", str(tmp_path / "plugin"))

    assert config.default_config_path() == tmp_path / "plugin" / "config.toml"


def test_config_root_uses_appdata_on_windows():
    assert config.config_root("nt", Path("/home/user"), r"C:\Users\user\AppData\Roaming") == (
        Path(r"C:\Users\user\AppData\Roaming") / "herdr" / "plugins" / "config" / "herdr-talkback"
    )


def test_config_root_falls_back_to_roaming_appdata_on_windows():
    assert config.config_root("nt", Path("/home/user"), None) == (
        Path("/home/user") / "AppData" / "Roaming" / "herdr" / "plugins" / "config" / "herdr-talkback"
    )


def test_config_root_uses_plugin_config_on_posix():
    assert config.config_root("posix", Path("/home/user"), None) == (
        Path("/home/user") / ".config" / "herdr" / "plugins" / "config" / "herdr-talkback"
    )


def test_config_root_prefers_legacy_only_when_it_exists(tmp_path):
    home = tmp_path
    legacy = home / ".config" / "herdr-talkback"
    legacy.mkdir(parents=True)
    assert config.config_root("posix", home, None) == legacy


def test_default_config_path_delegates_to_platform_root(monkeypatch, tmp_path):
    monkeypatch.delenv("HERDR_PLUGIN_CONFIG_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    calls = []
    monkeypatch.setattr(
        config, "config_root",
        lambda platform_name, home, appdata: calls.append((platform_name, appdata)) or tmp_path / "root",
    )

    assert config.default_config_path() == tmp_path / "root" / "config.toml"
    assert calls == [(config.os.name, str(tmp_path / "Roaming"))]


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


def test_stale_input_timeout_is_configurable_and_can_be_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv('HERDR_SOCKET_PATH', '/tmp/herdr.sock')
    path = tmp_path / 'config.toml'
    path.write_text('[delivery]\nstale_input_seconds = 0\n', encoding='utf-8')
    assert load_config(path).delivery_stale_input_seconds == 0
    path.write_text('[delivery]\nstale_input_seconds = 600\n', encoding='utf-8')
    assert load_config(path).delivery_stale_input_seconds == 600
    path.write_text('[delivery]\nstale_input_seconds = -1\n', encoding='utf-8')
    with pytest.raises(ValueError, match='stale_input_seconds'):
        load_config(path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX plugin-config layout")
def test_plugin_config_dir_wins_over_legacy_path(monkeypatch, tmp_path):
    monkeypatch.delenv("HERDR_PLUGIN_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/default.sock")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    legacy = tmp_path / ".config" / "herdr-talkback"
    legacy.mkdir(parents=True)
    (legacy / "config.toml").write_text('[delivery]\nmax_attempts = 2\n', encoding="utf-8")
    plugin = tmp_path / ".config" / "herdr" / "plugins" / "config" / "herdr-talkback"
    plugin.mkdir(parents=True)
    (plugin / "config.toml").write_text('[delivery]\nmax_attempts = 7\n', encoding="utf-8")

    assert load_config(None).delivery_max_attempts == 7


@pytest.mark.skipif(os.name == "nt", reason="POSIX legacy config path")
def test_legacy_config_is_used_when_plugin_config_is_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("HERDR_PLUGIN_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/default.sock")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    legacy = tmp_path / ".config" / "herdr-talkback"
    legacy.mkdir(parents=True)
    (legacy / "config.toml").write_text('[delivery]\nmax_attempts = 3\n', encoding="utf-8")

    assert load_config(None).delivery_max_attempts == 3


def test_delivery_key_delay_is_configurable(tmp_path):
    path = tmp_path / 'talkback.toml'
    path.write_text('[delivery]\nkey_delay_seconds = 0\n', encoding='utf-8')
    assert load_config(path).delivery_key_delay_seconds == 0
    path.write_text('[delivery]\nkey_delay_seconds = 1.5\n', encoding='utf-8')
    assert load_config(path).delivery_key_delay_seconds == 1.5
    path.write_text('[delivery]\nkey_delay_seconds = -1\n', encoding='utf-8')
    with pytest.raises(ValueError, match='key_delay_seconds'):
        load_config(path)
