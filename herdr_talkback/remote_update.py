"""Detached, audited update of the current Talkback Python installation."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import sysconfig
import tempfile
import time


SOURCE = 'git+https://github.com/kymowang/herdr-talkback.git@main'
ACTIVE = ('queued', 'offered', 'accepted', 'sending', 'verifying', 'retrying', 'checking_input')


def _set_status(database, peer, request_id, state, detail=''):
    with sqlite3.connect(database, timeout=10) as db:
        db.execute('update peer_update_jobs set state=?,detail=?,updated_at=? where peer=? and request_id=?',
                   (state, detail[:512], time.time(), peer, request_id))


def _run(args, timeout=600):
    return subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, check=True, timeout=timeout)


def _open_service(herdr, update_id=None, maintenance=False):
    args = [herdr, 'plugin', 'pane', 'open', '--plugin', 'herdr-talkback',
            '--entrypoint', 'service', '--no-focus']
    if update_id is not None:
        args.extend(['--env', 'HERDR_TALKBACK_UPDATE_ID=' + update_id])
    if maintenance:
        args.extend(['--env', 'HERDR_TALKBACK_MAINTENANCE=1'])
    result = subprocess.run(args,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, check=True, timeout=30, encoding='utf-8')
    data = json.loads(result.stdout)
    pane_id = data['result']['plugin_pane']['pane']['pane_id']
    if not isinstance(pane_id, str) or not pane_id:
        raise RuntimeError('new service pane ID is unavailable')
    return pane_id


def _runtime_info(daemon_path):
    from .daemon import submit_daemon_request
    return asyncio.run(submit_daemon_request(daemon_path, {'type': 'runtime_info'}))


def _set_maintenance(daemon_path, database, paused):
    from .daemon import submit_daemon_request
    response = asyncio.run(submit_daemon_request(daemon_path, {'type': 'update_quiesce',
        'paused': paused, 'state_path': str(Path(database).resolve())}))
    if response.get('ok') is not True or response.get('paused') is not paused:
        raise RuntimeError('could not change service maintenance state')


def _verify_runtime(daemon_path, request_id):
    response = _runtime_info(daemon_path)
    result = subprocess.run([sys.executable, '-c',
                             'from pathlib import Path; import herdr_talkback.daemon as d; print(Path(d.__file__).resolve())'],
                            cwd=tempfile.gettempdir(), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
                            timeout=15, encoding='utf-8')
    return (response.get('ok') is True and response.get('update_id') == request_id
            and Path(response.get('module_path', '')).resolve() == Path(result.stdout.strip()).resolve())


def _is_within(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _backup_distribution(backup):
    """Save installed distribution files without following paths outside the interpreter prefix."""
    search_paths = list(dict.fromkeys(filter(None, (sysconfig.get_path('purelib'), sysconfig.get_path('platlib')))))
    matches = [item for item in importlib.metadata.distributions(path=search_paths)
               if (item.metadata.get('Name') or '').lower().replace('_', '-') == 'herdr-talkback'
               and not Path(str(getattr(item, '_path', ''))).name.startswith('~')]
    if len(matches) != 1:
        raise RuntimeError('could not uniquely identify the installed Talkback package in this Python environment')
    dist = matches[0]
    root = Path(sys.prefix).resolve()
    paths = []
    for item in dist.files or ():
        path = Path(dist.locate_file(item)).resolve()
        if not _is_within(path, root):
            raise RuntimeError('installed package has files outside the Python environment')
        if path.is_file():
            relative = path.relative_to(root)
            destination = backup / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            paths.append(relative)
    if not paths:
        raise RuntimeError('installed package files could not be backed up')
    return paths


def _restore_distribution(backup, paths):
    root = Path(sys.prefix).resolve()
    for relative in paths:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup / relative, target)


def run_update(database, daemon_path, peer, request_id, pane_id, herdr):
    """Update only this interpreter's installed Talkback; preserve configuration and source checkouts."""
    from .transport import local_endpoint_running

    stopped = False
    installed = False
    quiesced = False
    previous_module = None
    rollback_warning = ''
    backup = None
    _set_status(database, peer, request_id, 'preparing')
    try:
        if not pane_id or not herdr:
            raise RuntimeError('service pane or Herdr executable is unavailable')
        with tempfile.TemporaryDirectory(prefix='herdr-talkback-update-') as temporary:
            temp = Path(temporary)
            wheel_dir = temp / 'wheel'
            wheel_dir.mkdir()
            try:
                _run([sys.executable, '-m', 'pip', 'wheel', '--no-deps', '--wheel-dir', str(wheel_dir), SOURCE])
            except (subprocess.SubprocessError, OSError) as exc:
                raise RuntimeError('GitHub wheel build failed; check Git, pip, and network access') from exc
            wheels = list(wheel_dir.glob('herdr_talkback-*.whl'))
            if len(wheels) != 1:
                raise RuntimeError('GitHub did not produce exactly one Talkback wheel')
            key = hashlib.sha256((peer + '\0' + request_id).encode('utf-8')).hexdigest()[:24]
            backup = Path(database).resolve().parent / 'update-backups' / key
            backup.mkdir(parents=True, exist_ok=False)
            paths = _backup_distribution(backup)
            (backup / 'manifest.json').write_text(json.dumps([str(path) for path in paths]), encoding='utf-8')
            previous = _runtime_info(daemon_path)
            if previous.get('ok') is not True or not previous.get('module_path'):
                raise RuntimeError('could not identify the running Talkback runtime')
            previous_module = Path(previous['module_path']).resolve()
            quiesced = True
            _set_maintenance(daemon_path, database, True)
            with sqlite3.connect(database, timeout=10) as db:
                placeholders = ','.join('?' for _ in ACTIVE)
                active = db.execute('select count(*) from messages where state in (' + placeholders + ')', ACTIVE).fetchone()[0]
            if active:
                raise RuntimeError('messages are in progress; update was not activated')
            _set_status(database, peer, request_id, 'activating')
            _run([herdr, 'pane', 'close', pane_id], timeout=30)
            stopped = True
            deadline = time.monotonic() + 20
            while local_endpoint_running(daemon_path) and time.monotonic() < deadline:
                time.sleep(0.25)
            if local_endpoint_running(daemon_path):
                raise RuntimeError('old Talkback service did not stop')
            new_pane = None
            try:
                # From this point onward the environment may be partially modified.
                installed = True
                try:
                    _run([sys.executable, '-m', 'pip', 'install', '--no-deps', '--force-reinstall', str(wheels[0])])
                except (subprocess.SubprocessError, OSError) as exc:
                    raise RuntimeError('Talkback Python installation failed') from exc
                _set_status(database, peer, request_id, 'restarting')
                new_pane = _open_service(herdr, request_id, maintenance=True)
                deadline = time.monotonic() + 30
                while not local_endpoint_running(daemon_path) and time.monotonic() < deadline:
                    time.sleep(0.25)
                if not local_endpoint_running(daemon_path):
                    raise RuntimeError('updated Talkback service did not start')
                if not _verify_runtime(daemon_path, request_id):
                    raise RuntimeError('updated Talkback runtime could not be verified')
                _set_maintenance(daemon_path, database, False)
            except Exception:
                if new_pane:
                    try:
                        _run([herdr, 'pane', 'close', new_pane], timeout=30)
                        deadline = time.monotonic() + 20
                        while local_endpoint_running(daemon_path) and time.monotonic() < deadline:
                            time.sleep(0.25)
                        if local_endpoint_running(daemon_path):
                            raise RuntimeError('updated service did not stop')
                    except Exception:
                        raise RuntimeError('updated service could not be stopped for rollback')
                # Remove partial installation before restoring the saved version.
                uninstall = subprocess.run([sys.executable, '-m', 'pip', 'uninstall', '-y', 'herdr-talkback'],
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           timeout=120, check=False)
                _restore_distribution(backup, paths)
                if uninstall.returncode != 0:
                    rollback_warning = 'rollback may be incomplete; backup: ' + str(backup)
                installed = False
                raise
        _set_status(database, peer, request_id, 'completed',
                    'Python installation updated and service restarted; Herdr plugin registration and skill unchanged')
        return 0
    except Exception as exc:
        if quiesced and not stopped:
            try:
                _set_maintenance(daemon_path, database, False)
            except Exception:
                _set_status(database, peer, request_id, 'failed', 'update failed; service remains in maintenance mode')
                return 1
        if stopped and not installed:
            try:
                if not local_endpoint_running(daemon_path):
                    _open_service(herdr, maintenance=True)
                    deadline = time.monotonic() + 30
                    while not local_endpoint_running(daemon_path) and time.monotonic() < deadline:
                        time.sleep(0.25)
                    if not local_endpoint_running(daemon_path):
                        raise RuntimeError('previous service did not restart')
                restored = _runtime_info(daemon_path)
                if restored.get('ok') is not True or Path(restored.get('module_path', '')).resolve() != previous_module:
                    raise RuntimeError('previous runtime could not be verified')
                _set_maintenance(daemon_path, database, False)
            except Exception:
                detail = 'update failed; service restart also failed'
                if backup is not None:
                    detail += '; backup: ' + str(backup)
                _set_status(database, peer, request_id, 'failed', detail)
                return 1
        detail = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        if rollback_warning:
            detail += '; ' + rollback_warning
        if installed and backup is not None:
            detail += '; installation may need manual recovery; backup: ' + str(backup)
        _set_status(database, peer, request_id, 'failed', detail)
        return 1


def launch_update(database, daemon_path, peer, request_id):
    """Ask Herdr to launch the updater independently of the service pane."""
    pane_id = os.environ.get('HERDR_PANE_ID', '')
    herdr = os.environ.get('HERDR_BIN_PATH') or shutil.which('herdr') or ''
    if not pane_id or not herdr:
        raise RuntimeError('updates require a Herdr-owned Talkback service pane')
    result = subprocess.run([herdr, 'plugin', 'action', 'invoke', 'herdr-talkback.update-worker'],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            encoding='utf-8', timeout=8, check=True)
    if json.loads(result.stdout).get('result', {}).get('type') != 'plugin_action_invoked':
        raise RuntimeError('Herdr did not start the update worker')


def run_next_update(config, herdr):
    from .state import TalkbackState
    from .peer_state import PeerStore
    state = TalkbackState(config.state_path, local_node_id=config.core_node_id)
    state.init()
    job = PeerStore(state).claim_update()
    if job is None:
        return 0
    try:
        parent_pids = [os.getpid()]
        launcher = _windows_launcher_parent()
        if launcher:
            parent_pids.append(launcher)
        args = [sys.executable, '-m', 'herdr_talkback.remote_update', '--database', str(state.path),
                '--daemon-path', str(config.daemon_socket_path), '--peer', job['peer'],
                '--request-id', job['request_id'], '--pane-id', job['service_pane_id'], '--herdr', herdr]
        for pid in parent_pids:
            args.extend(['--wait-pid', str(pid)])
        kwargs = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL,
                  'close_fds': True, 'cwd': tempfile.gettempdir()}
        if os.name == 'nt':
            kwargs['creationflags'] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                                       | subprocess.CREATE_BREAKAWAY_FROM_JOB)
        else:
            kwargs['start_new_session'] = True
        subprocess.Popen(args, **kwargs)
        return 0
    except Exception:
        PeerStore(state).set_update(job['peer'], job['request_id'], 'failed', 'could not start detached updater')
        return 1


def _windows_launcher_parent():
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    pid = os.getppid()
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise RuntimeError('cannot inspect update action parent process')
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            raise RuntimeError('cannot identify update action parent process')
        return pid if Path(buffer.value).name.lower() == 'herdr-talkback-plugin.exe' else None
    finally:
        kernel.CloseHandle(handle)


def _wait_for_exit(pid, seconds=30):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:  # process has already exited
                return
            raise RuntimeError('cannot check update launcher process')
        try:
            if kernel.WaitForSingleObject(handle, int(seconds * 1000)) != 0:
                raise RuntimeError('update launcher did not exit')
        finally:
            kernel.CloseHandle(handle)
        return
    deadline = time.monotonic() + seconds
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError('update launcher did not exit')
        time.sleep(0.1)


def main():
    parser = argparse.ArgumentParser()
    for name in ('database', 'daemon-path', 'peer', 'request-id', 'pane-id', 'herdr'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--wait-pid', type=int, action='append', default=[])
    args = parser.parse_args()
    try:
        for pid in args.wait_pid:
            _wait_for_exit(pid)
    except Exception:
        _set_status(args.database, args.peer, args.request_id, 'failed', 'update launcher did not exit safely')
        return 1
    return run_update(args.database, Path(args.daemon_path), args.peer, args.request_id, args.pane_id, args.herdr)


if __name__ == '__main__':
    raise SystemExit(main())
