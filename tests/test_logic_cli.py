import json
from types import SimpleNamespace

from herdr_talkback import cli
from herdr_talkback.logic_state import LogicStore
from herdr_talkback.state import TalkbackState


def test_monitor_status_without_socket_is_read_only(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv('HERDR_SOCKET_PATH', raising=False)
    state = TalkbackState(tmp_path / 'state.db'); state.init()
    store = LogicStore(state)
    store.observe('p1', 'idle', 'next step', 100)
    cfg = tmp_path / 'config.toml'
    cfg.write_text('state_path="' + state.path.as_posix() + '"\n', encoding='utf-8')
    before = state.path.read_bytes()
    assert cli.main(['--config', str(cfg), 'monitor', 'status', '--json']) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]['herdr_status'] == 'idle' and rows[0]['logic_status'] == 'todo'
    assert 'content' not in rows[0]  # Status output does not dump transcripts.
    assert state.path.read_bytes() == before
    assert cli.main(['--config', str(cfg), 'monitor', 'status']) == 0
    assert 'p1' in capsys.readouterr().out


def test_monitor_status_missing_database_does_not_create_it(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv('HERDR_SOCKET_PATH', raising=False)
    cfg = tmp_path / 'config.toml'
    db = tmp_path / 'missing' / 'state.db'
    cfg.write_text('state_path="' + db.as_posix() + '"\n', encoding='utf-8')
    assert cli.main(['--config', str(cfg), 'monitor', 'status', '--json']) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert not db.parent.exists()


def test_monitor_disable_reports_offline_without_changing_setting(tmp_path, monkeypatch, capsys):
    from herdr_talkback.operator_controls import OperatorStore
    monkeypatch.setenv('HERDR_SOCKET_PATH', str(tmp_path / 'herdr.sock'))
    cfg = tmp_path / 'config.toml'
    cfg.write_text('state_path="' + (tmp_path / 'state.db').as_posix() + '"\n'
                   'daemon_socket_path="' + (tmp_path / 'daemon.sock').as_posix() + '"\n', encoding='utf-8')
    assert cli.main(['--config', str(cfg), 'monitor', 'disable']) == 1
    output = json.loads(capsys.readouterr().out)
    assert output['ok'] is False
    assert OperatorStore(TalkbackState(tmp_path / 'state.db')).monitor_enabled(True) is True
    assert cli.main(['--config', str(cfg), 'monitor', 'state']) == 0
    status = json.loads(capsys.readouterr().out)
    assert status['enabled'] is False and status['service_running'] is False


def test_register_list_remove_named_pane(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('HERDR_SOCKET_PATH', str(tmp_path / 'herdr.sock'))
    class Client:
        def __init__(self, path): pass
        async def request(self, method, params): return {'panes': []}
    monkeypatch.setattr(cli, 'HerdrClient', Client)
    cfg = tmp_path / 'config.toml'
    cfg.write_text('state_path="' + (tmp_path / 'state.db').as_posix() + '"\n'
                   '[core]\nnode_id="windows"\n', encoding='utf-8')
    assert cli.main(['--config', str(cfg), 'pane', 'register', 'worker', 'windows/ast-dev1']) == 0
    capsys.readouterr()
    assert cli.main(['--config', str(cfg), 'pane', 'registered', '--json']) == 0
    values = json.loads(capsys.readouterr().out)
    assert values[0]['name'] == 'worker' and values[0]['address'] == 'windows/ast-dev1'
    assert values[0]['location'] == 'local'
    assert cli.main(['--config', str(cfg), 'pane', 'unregister', 'worker']) == 0


def test_register_rejects_existing_local_pane_name(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('HERDR_SOCKET_PATH', str(tmp_path / 'herdr.sock'))
    class Client:
        def __init__(self, path): pass
        async def request(self, method, params): return {'panes': [{'label': 'worker'}]}
    monkeypatch.setattr(cli, 'HerdrClient', Client)
    cfg = tmp_path / 'config.toml'
    cfg.write_text('state_path="' + (tmp_path / 'state.db').as_posix() + '"\n', encoding='utf-8')
    assert cli.main(['--config', str(cfg), 'pane', 'register', 'worker', 'linux-main/other']) == 1
    assert 'conflict' in capsys.readouterr().out


def test_monitor_state_does_not_create_database(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / 'config.toml'
    db = tmp_path / 'missing' / 'state.db'
    cfg.write_text('state_path="' + db.as_posix() + '"\n', encoding='utf-8')
    assert cli.main(['--config', str(cfg), 'monitor', 'state']) == 0
    assert json.loads(capsys.readouterr().out)['service_running'] is False
    assert not db.parent.exists()


def test_monitor_enable_uses_live_daemon_and_validates_configuration(tmp_path, monkeypatch, capsys):
    from herdr_talkback import transport
    monkeypatch.setenv('HERDR_SOCKET_PATH', str(tmp_path / 'herdr.sock'))
    monkeypatch.setattr(transport, 'local_endpoint_running', lambda path: True)
    calls = []
    async def request(path, payload):
        calls.append(payload)
        return {'ok': True, 'enabled': payload['enabled'], 'applied': True}
    monkeypatch.setattr(cli, 'submit_daemon_request', request)
    cfg = tmp_path / 'config.toml'
    cfg.write_text('state_path="' + (tmp_path / 'state.db').as_posix() + '"\n'
                   '[logic_monitor]\nmonitor_pane="judge"\ntargets=["worker"]\n', encoding='utf-8')
    assert cli.main(['--config', str(cfg), 'monitor', 'enable']) == 0
    assert json.loads(capsys.readouterr().out)['applied'] is True
    assert calls == [{'type': 'monitor_control', 'enabled': True,
                      'state_path': str((tmp_path / 'state.db').resolve())}]


def test_monitor_without_config_targets_discovered_plugin_daemon(tmp_path, monkeypatch, capsys):
    from herdr_talkback import transport
    plugin_dir = tmp_path / 'plugin'; plugin_dir.mkdir()
    state_path = tmp_path / 'plugin-state.db'
    (plugin_dir / 'config.toml').write_text('state_path="' + state_path.as_posix() + '"\n'
        'daemon_socket_path="' + (tmp_path / 'plugin.sock').as_posix() + '"\n', encoding='utf-8')
    monkeypatch.setenv('HERDR_SOCKET_PATH', str(tmp_path / 'herdr.sock'))
    monkeypatch.setenv('HERDR_BIN_PATH', 'fake-herdr')
    monkeypatch.delenv('HERDR_PLUGIN_CONFIG_DIR', raising=False)
    monkeypatch.setattr(cli.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=str(plugin_dir)+'\n'))
    monkeypatch.setattr(transport, 'local_endpoint_running', lambda path: True)
    calls = []
    async def request(path, payload):
        calls.append((path, payload))
        return {'ok': True, 'enabled': False, 'applied': True}
    monkeypatch.setattr(cli, 'submit_daemon_request', request)
    assert cli.main(['monitor', 'disable']) == 0
    assert calls[0][0] == tmp_path / 'plugin.sock'
    assert calls[0][1]['state_path'] == str(state_path.resolve())


def test_monitor_disable_flag_uses_same_control_command(tmp_path, monkeypatch, capsys):
    from herdr_talkback import transport
    monkeypatch.setenv('HERDR_SOCKET_PATH', str(tmp_path / 'herdr.sock'))
    monkeypatch.setattr(transport, 'local_endpoint_running', lambda path: True)
    cfg = tmp_path / 'config.toml'
    cfg.write_text('state_path="' + (tmp_path / 'state.db').as_posix() + '"\n', encoding='utf-8')
    calls = []
    async def request(path, payload):
        calls.append(payload)
        return {'ok': True, 'enabled': False, 'applied': True}
    monkeypatch.setattr(cli, 'submit_daemon_request', request)
    assert cli.main(['--config', str(cfg), 'monitor', '--disable']) == 0
    assert calls[0]['enabled'] is False


def test_explicit_config_is_never_replaced_by_discovery(tmp_path, monkeypatch, capsys):
    from herdr_talkback import transport
    monkeypatch.setenv('HERDR_SOCKET_PATH', str(tmp_path / 'herdr.sock'))
    monkeypatch.setattr(transport, 'local_endpoint_running', lambda path: True)
    monkeypatch.setattr(cli, '_discover_plugin_config', lambda: (_ for _ in ()).throw(AssertionError('must not discover')))
    cfg = tmp_path / 'standalone.toml'
    cfg.write_text('state_path="' + (tmp_path / 'standalone.db').as_posix() + '"\n', encoding='utf-8')
    calls = []
    async def request(path, payload):
        calls.append(payload)
        return {'ok': True, 'enabled': False, 'applied': True}
    monkeypatch.setattr(cli, 'submit_daemon_request', request)
    assert cli.main(['--config', str(cfg), 'monitor', 'disable']) == 0
    assert calls[0]['state_path'] == str((tmp_path / 'standalone.db').resolve())


def test_monitor_rejects_subcommand_and_conflicting_flag():
    import pytest
    with pytest.raises(SystemExit):
        cli.main(['monitor', '--disable', 'enable'])


def test_monitor_old_daemon_error_explains_config_and_restart(tmp_path, monkeypatch, capsys):
    from herdr_talkback import transport
    monkeypatch.setenv('HERDR_SOCKET_PATH', str(tmp_path / 'herdr.sock'))
    monkeypatch.setattr(transport, 'local_endpoint_running', lambda path: True)
    cfg = tmp_path / 'config.toml'
    cfg.write_text('state_path="' + (tmp_path / 'state.db').as_posix() + '"\n', encoding='utf-8')
    async def request(path, payload):
        return {'ok': False, 'error': 'unsupported daemon request type'}
    monkeypatch.setattr(cli, 'submit_daemon_request', request)
    assert cli.main(['--config', str(cfg), 'monitor', 'disable']) == 1
    error = json.loads(capsys.readouterr().out)['error']
    assert '--config' in error and 'restart' in error
