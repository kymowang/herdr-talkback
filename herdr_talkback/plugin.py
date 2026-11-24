"""Native Herdr entrypoints. The long-running service belongs to a Herdr pane."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import socket
import hashlib
import re

from .config import load_config, default_config_path, default_state_path
from .platform_support import ServiceAlreadyRunning, service_lock
from .transport import local_endpoint_running


PLUGIN_ID = 'herdr-talkback'


def initialize():
    path = default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    state_root = Path(os.environ.get('HERDR_PLUGIN_STATE_DIR', default_state_path().parent))
    template = Path(__file__).with_name('default-rules.toml').read_text(encoding='utf-8')
    hostname = socket.gethostname()
    node_id = re.sub(r'[^A-Za-z0-9._-]+', '-', hostname).strip('._-')
    if not node_id:
        node_id = 'host-' + hashlib.sha256(hostname.encode('utf-8')).hexdigest()[:12]
    # The Herdr socket identifies a session, so multiple sessions never share queues/locks.
    prefix = ('# Generated once; changes are never overwritten.\nroles = []\n'
              'state_path = ' + json.dumps(str(state_root / '{session}' / 'state.db')) + '\n'
              'daemon_socket_path = ' + json.dumps(str(state_root / '{session}' / 'talkback.sock')) + '\n\n'
              '[core]\nnode_id = ' + json.dumps(node_id) + '\n\n')
    try:
        with path.open('x', encoding='utf-8') as f:
            f.write(prefix + template)
    except FileExistsError:
        pass
    return path


def daemon_running(path):
    return local_endpoint_running(path)


def legacy_running():
    # Avoid two monitors writing continue into the same panes during migration.
    from .config import tomllib
    path = Path.home() / '.config' / 'agent-hub' / 'config.toml'
    raw = tomllib.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    legacy_socket = Path(raw.get('daemon_socket_path', Path.home() / '.local/state/agent-hub/agent-hub.sock'))
    return daemon_running(legacy_socket)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='herdr-talkback-plugin')
    parser.add_argument('command', choices=['init', 'start', 'restart', 'serve', 'check', 'queue', 'update-worker'])
    args = parser.parse_args(argv)
    path = initialize()
    if args.command == 'init':
        print(path)
        return 0
    cfg = load_config(path)
    if args.command == 'update-worker':
        from .remote_update import run_next_update
        herdr = os.environ.get('HERDR_BIN_PATH', 'herdr')
        return run_next_update(cfg, herdr)
    if args.command == 'restart':
        from .cli import main as cli_main
        return cli_main(['--config', str(path), 'restart'])
    if args.command in {'start', 'serve'}:
        if daemon_running(cfg.daemon_socket_path):
            print('Talkback service is already running')
            return 0
        if legacy_running():
            print('Agent Hub is still running; stop it before starting Talkback to avoid duplicate recovery', file=sys.stderr)
            return 1
    if args.command == 'start':
        herdr = os.environ.get('HERDR_BIN_PATH', 'herdr')
        subprocess.run([herdr, 'plugin', 'pane', 'open', '--plugin', PLUGIN_ID,
                        '--entrypoint', 'service', '--placement', 'tab', '--no-focus'], check=True)
        return 0
    from .cli import main as cli_main
    command = {'check': ['serve', '--dry-run'], 'queue': ['queue'], 'serve': ['serve']}[args.command]
    if args.command != 'serve':
        return cli_main(['--config', str(path)] + command)
    cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
    # Held for the service's entire lifetime; simultaneous plugin actions cannot unlink a live inbox.
    try:
        with service_lock(cfg.state_path.parent / 'service.lock'):
            print('Herdr Talkback service\nConfig: ' + str(path) + '\nClose this pane to stop the service.', flush=True)
            return cli_main(['--config', str(path)] + command)
    except ServiceAlreadyRunning:
        print('Talkback service is already starting')
        return 0
