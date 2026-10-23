import asyncio
import json
import pytest

from herdr_talkback import cli
from herdr_talkback.state import TalkbackState
from herdr_talkback.core_types import CoreMessage, CoreMessageState


def test_tls_command_works_outside_herdr(tmp_path, monkeypatch, capsys):
    pytest.importorskip("cryptography")
    monkeypatch.delenv('HERDR_SOCKET_PATH', raising=False)
    assert cli.main(['peer', 'init-tls', '--directory', str(tmp_path), '--host', '127.0.0.1']) == 0
    assert (tmp_path / 'node.crt').exists()
    output = capsys.readouterr().out
    assert (tmp_path / 'pair.token').read_text().strip() not in output
    assert cli.main(['peer', 'init-tls', '--directory', str(tmp_path), '--host', '127.0.0.1']) == 1


def test_message_status_shows_local_and_remote_state(tmp_path, monkeypatch, capsys):
    from herdr_talkback.peer_state import PeerStore
    monkeypatch.delenv('HERDR_SOCKET_PATH', raising=False)
    state = TalkbackState(tmp_path / 'state.db')
    state.init()
    state.enqueue_message(CoreMessage('msg_status', 'linux-main/a', 'remote/b', 'hello',
                          '[a->remote/b] hello', CoreMessageState.QUEUED, 'msg_status'))
    PeerStore(state).progress('msg_status', 'remote', remote_state='queued', remote_received_at=123)
    cfg = tmp_path / 'config.toml'
    cfg.write_text('state_path="' + state.path.as_posix() + '"\n', encoding='utf-8')
    assert cli.main(['--config', str(cfg), 'message', 'status', 'msg_status', '--json']) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['state'] == 'queued'
    assert output['peer']['remote_state'] == 'queued'
    assert cli.main(['--config', str(cfg), 'message', 'status', 'unknown']) == 1
