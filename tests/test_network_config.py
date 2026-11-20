import ssl
from pathlib import Path

import pytest

from herdr_talkback.config import load_config


def test_network_is_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv('HERDR_SOCKET_PATH', 'test.sock')
    monkeypatch.setenv('APPDATA', str(tmp_path / 'AppData' / 'Roaming'))
    # Default discovery must not read the machine's real plugin configuration.
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    monkeypatch.delenv('HERDR_PLUGIN_CONFIG_DIR', raising=False)
    assert not load_config(None).network.enabled


def test_provision_and_load_relative_network_paths(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    from herdr_talkback.certificates import provision

    monkeypatch.setenv('HERDR_SOCKET_PATH', 'test.sock')
    provision(tmp_path / 'tls', ['127.0.0.1', 'localhost'])
    with pytest.raises(FileExistsError):
        provision(tmp_path / 'tls', ['127.0.0.1'])
    (tmp_path / 'config.toml').write_text('''[core]
node_id="local"
[network]
enabled=true
listen_port=18765
cert_file="tls/node.crt"
key_file="tls/node.key"
[peers.remote]
url="https://127.0.0.1:18766"
ca_file="tls/node.crt"
token_file="tls/pair.token"
''', encoding='utf-8')
    cfg = load_config(tmp_path / 'config.toml')
    assert cfg.network.listen_port == 18765
    assert cfg.peers['remote'].token_file == tmp_path / 'tls/pair.token'
    assert len(cfg.peers['remote'].token_file.read_text().strip()) == 64
    ssl.create_default_context(cafile=str(cfg.peers['remote'].ca_file))


@pytest.mark.parametrize('setting', ['listen_port=0', 'listen_port=65536', 'listen_port=true'])
def test_invalid_network_ports(tmp_path, monkeypatch, setting):
    monkeypatch.setenv('HERDR_SOCKET_PATH', 'test.sock')
    path = tmp_path / 'config.toml'
    path.write_text('[network]\n' + setting, encoding='utf-8')
    with pytest.raises(ValueError, match='port'):
        load_config(path)


@pytest.mark.parametrize('url', ['http://host', 'https://u:p@host', 'https://host/?x=1', 'https://host/#x'])
def test_invalid_peer_urls(tmp_path, monkeypatch, url):
    monkeypatch.setenv('HERDR_SOCKET_PATH', 'test.sock')
    path = tmp_path / 'config.toml'
    path.write_text('[peers.remote]\nurl="' + url + '"', encoding='utf-8')
    with pytest.raises(ValueError, match='HTTPS'):
        load_config(path)


def test_remote_control_permission_requires_explicit_boolean(tmp_path, monkeypatch):
    monkeypatch.setenv('HERDR_SOCKET_PATH','test.sock')
    path = tmp_path/'config.toml'
    path.write_text('[peers.remote]\nurl="https://example.test:18765"\n',encoding='utf-8')
    assert not load_config(path).peers['remote'].allow_remote_control
    path.write_text('[peers.remote]\nurl="https://example.test:18765"\nallow_remote_control=true\n',encoding='utf-8')
    assert load_config(path).peers['remote'].allow_remote_control
    path.write_text('[peers.remote]\nurl="https://example.test:18765"\nallow_remote_control="true"\n',encoding='utf-8')
    with pytest.raises(ValueError,match='allow_remote_control'):
        load_config(path)
