import json

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
