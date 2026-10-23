import json
from pathlib import Path
import pytest

from herdr_talkback import plugin
from herdr_talkback.config import load_config


def test_manifest_and_commands():
    from herdr_talkback.config import tomllib
    manifest = tomllib.loads(Path('herdr-plugin.toml').read_text(encoding='utf-8'))
    assert manifest['id'] == 'herdr-talkback'
    assert set(manifest['platforms']) == {'linux', 'macos', 'windows'}
    assert manifest['startup'][0]['command'] == ['herdr-talkback-plugin', 'start']
    assert {a['id'] for a in manifest['actions']} >= {'start', 'check', 'queue'}
    assert manifest['panes'][0]['id'] == 'service'


def test_initialize_preserves_user_config_and_scopes_state(tmp_path, monkeypatch):
    monkeypatch.setenv('HERDR_PLUGIN_CONFIG_DIR', str(tmp_path / 'config'))
    monkeypatch.setenv('HERDR_PLUGIN_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('HERDR_SOCKET_PATH', '/tmp/session-a.sock')
    p = plugin.initialize()
    cfg = load_config(p)
    assert cfg.recovery_rules
    assert cfg.message_rules[0].agents == ('lead', 'lead2', 'scheduler')
    assert cfg.state_path.is_relative_to(tmp_path / 'state')
    old = cfg.state_path
    monkeypatch.setenv('HERDR_SOCKET_PATH', '/tmp/session-b.sock')
    assert load_config(p).state_path != old
    p.write_text('custom = true\n')
    assert plugin.initialize().read_text() == 'custom = true\n'


def test_plugin_start_uses_herdr_owned_pane(tmp_path, monkeypatch):
    monkeypatch.setenv('HERDR_PLUGIN_CONFIG_DIR', str(tmp_path / 'config'))
    monkeypatch.setenv('HERDR_PLUGIN_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('HERDR_SOCKET_PATH', '/tmp/test-herdr.sock')
    monkeypatch.setattr(plugin, 'daemon_running', lambda path: False)
    monkeypatch.setattr(plugin, 'legacy_running', lambda: False)
    called = []
    monkeypatch.setattr(plugin.subprocess, 'run', lambda args, **kwargs: called.append(args))
    assert plugin.main(['start']) == 0
    assert called[0][1:] == ['plugin', 'pane', 'open', '--plugin', 'herdr-talkback', '--entrypoint', 'service', '--placement', 'tab', '--no-focus']


def test_plugin_refuses_duplicate_monitor(tmp_path, monkeypatch):
    monkeypatch.setenv('HERDR_PLUGIN_CONFIG_DIR', str(tmp_path / 'config'))
    monkeypatch.setenv('HERDR_PLUGIN_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('HERDR_SOCKET_PATH', '/tmp/test-herdr.sock')
    monkeypatch.setattr(plugin, 'daemon_running', lambda path: False)
    monkeypatch.setattr(plugin, 'legacy_running', lambda: True)
    assert plugin.main(['start']) == 1
