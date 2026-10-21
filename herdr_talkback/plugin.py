"""Native Herdr entrypoints. The long-running service belongs to a Herdr pane."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

from .config import load_config, default_config_path, default_state_path


PLUGIN_ID = 'herdr-talkback'


def initialize():
    path = default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    state_root = Path(os.environ.get('HERDR_PLUGIN_STATE_DIR', default_state_path().parent))
    template = Path(__file__).with_name('default-rules.toml').read_text()
    # The Herdr socket identifies a session, so multiple sessions never share queues/locks.
    prefix = ('# Generated once; changes are never overwritten.\nroles = []\n'
              'state_path = ' + json.dumps(str(state_root / '{session}' / 'state.db')) + '\n'
              'daemon_socket_path = ' + json.dumps(str(state_root / '{session}' / 'talkback.sock')) + '\n\n')
    try:
        with path.open('x') as f:
            f.write(prefix + template)
    except FileExistsError:
        pass
    return path


def daemon_running(path):
    try:
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(0.3)
            sock.connect(str(path))
            return True
    except OSError:
        return False


def legacy_running():
    # Avoid two monitors writing continue into the same panes during migration.
    from .config import tomllib
    path = Path.home() / '.config' / 'agent-hub' / 'config.toml'
    raw = tomllib.loads(path.read_text()) if path.exists() else {}
    legacy_socket = Path(raw.get('daemon_socket_path', Path.home() / '.local/state/agent-hub/agent-hub.sock'))
    return daemon_running(legacy_socket)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='herdr-talkback-plugin')
    parser.add_argument('command', choices=['init', 'start', 'serve', 'check', 'queue'])
    args = parser.parse_args(argv)
    path = initialize()
    if args.command == 'init':
        print(path)
        return 0
    cfg = load_config(path)
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
    with (cfg.state_path.parent / 'service.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Talkback service is already starting')
            return 0
        print('Herdr Talkback service\nConfig: ' + str(path) + '\nClose this pane to stop the service.', flush=True)
        return cli_main(['--config', str(path)] + command)
