"""Opt-in peer configuration; no optional dependencies needed to parse it."""
from dataclasses import dataclass
from pathlib import Path
import ssl
from urllib.parse import urlsplit

from .addressing import validate_node_id


@dataclass(frozen=True)
class NetworkConfig:
    enabled: bool = False
    listen_host: str = '127.0.0.1'
    listen_port: int = 18765
    cert_file: Path = None
    key_file: Path = None

    def server_context(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(self.cert_file), str(self.key_file))
        return context


@dataclass(frozen=True)
class PeerConfig:
    node_id: str
    url: str
    ca_file: Path
    token_file: Path

    def token(self):
        token = self.token_file.read_text(encoding='ascii').strip()
        if len(token) != 64 or any(c not in '0123456789abcdef' for c in token):
            raise ValueError('peer token must be 256-bit lowercase hexadecimal')
        return token

    def client_context(self):
        context = ssl.create_default_context(cafile=str(self.ca_file))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context


def load_network(raw, config_path, node_id):
    values = raw.get('network', {})
    peers_raw = raw.get('peers', {})
    if not isinstance(values, dict) or not isinstance(peers_raw, dict):
        raise ValueError('network and peers must be tables')
    enabled = values.get('enabled', False)
    if not isinstance(enabled, bool):
        raise ValueError('network.enabled must be boolean')
    port = values.get('listen_port', 18765)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('network listen port must be between 1 and 65535')
    host = values.get('listen_host', '127.0.0.1')
    if not isinstance(host, str) or not host.strip():
        raise ValueError('network listen_host must be nonempty')
    base = Path(config_path).resolve().parent if config_path else Path.cwd()

    def path(value):
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ValueError('credential paths must be nonempty strings')
        result = Path(value).expanduser()
        return result if result.is_absolute() else base / result

    network = NetworkConfig(enabled, host, port, path(values.get('cert_file')), path(values.get('key_file')))
    peers = {}
    for peer_id, peer in peers_raw.items():
        validate_node_id(peer_id)
        if peer_id == node_id or not isinstance(peer, dict):
            raise ValueError('peer node IDs must differ from the local node ID')
        url = peer.get('url', '')
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme == 'https' and parsed.hostname and parsed.port != 0
                     and not parsed.username and not parsed.password and not parsed.query
                     and not parsed.fragment and parsed.path in ('', '/'))
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise ValueError('peer URL must be an HTTPS origin without credentials, path, query or fragment')
        peers[peer_id] = PeerConfig(peer_id, url.rstrip('/'), path(peer.get('ca_file')), path(peer.get('token_file')))
    if enabled:
        if not network.cert_file or not network.key_file:
            raise ValueError('enabled network requires cert_file and key_file')
        network.server_context()
        for peer in peers.values():
            if not peer.ca_file or not peer.token_file:
                raise ValueError('enabled peer requires ca_file and token_file')
            peer.token()
            peer.client_context()
    return network, peers
