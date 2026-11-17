import subprocess
from pathlib import Path
from types import SimpleNamespace
import asyncio

from herdr_talkback.peer_state import PeerStore
from herdr_talkback.state import TalkbackState
from herdr_talkback import remote_update
import herdr_talkback.transport as transport


def make_job(tmp_path):
    state = TalkbackState(tmp_path / 'state.db', local_node_id='local'); state.init()
    store = PeerStore(state); assert store.start_update('remote', 'update_1')
    return state, store


def test_update_stages_install_and_restarts_without_touching_checkout(tmp_path, monkeypatch):
    state, store = make_job(tmp_path)
    commands = []; maintenance = []
    def run(args, timeout=600):
        commands.append(args)
        if 'wheel' in args:
            (Path(args[args.index('--wheel-dir')+1]) / 'herdr_talkback-1-py3-none-any.whl').write_bytes(b'wheel')
    monkeypatch.setattr(remote_update, '_run', run)
    monkeypatch.setattr(remote_update, '_backup_distribution', lambda backup: [Path('old')])
    monkeypatch.setattr(remote_update, '_runtime_info', lambda path: {'ok': True, 'module_path': str(tmp_path/'old.py')})
    monkeypatch.setattr(remote_update, '_set_maintenance', lambda path, db, paused: maintenance.append(paused))
    monkeypatch.setattr(remote_update, '_open_service', lambda herdr, update_id=None, maintenance=False: commands.append([herdr,'plugin','pane','open']) or 'new-pane')
    monkeypatch.setattr(remote_update, '_verify_runtime', lambda path, request_id: True)
    calls = iter([False,False,True,True])
    monkeypatch.setattr(transport, 'local_endpoint_running', lambda path: next(calls))
    assert remote_update.run_update(state.path, tmp_path/'socket', 'remote', 'update_1', 'pane-1', 'herdr') == 0
    assert store.update_job('remote','update_1')['state'] == 'completed'
    assert maintenance == [True, False]
    assert any(a[:3] == ['herdr','pane','close'] for a in commands)
    assert any(a[:4] == ['herdr','plugin','pane','open'] for a in commands)
    assert not any('pull' in a or 'reset' in a for a in commands)


def test_update_refuses_activation_when_message_is_in_progress(tmp_path, monkeypatch):
    state, store = make_job(tmp_path)
    state.create_message('msg_active','source','target','pane','text')
    commands=[]; maintenance=[]
    def run(args, timeout=600):
        commands.append(args)
        if 'wheel' in args:
            (Path(args[args.index('--wheel-dir')+1])/'herdr_talkback-1-py3-none-any.whl').write_bytes(b'wheel')
    monkeypatch.setattr(remote_update, '_run', run)
    monkeypatch.setattr(remote_update, '_backup_distribution', lambda backup: [Path('old')])
    monkeypatch.setattr(remote_update, '_runtime_info', lambda path: {'ok': True, 'module_path': str(tmp_path/'old.py')})
    monkeypatch.setattr(remote_update, '_set_maintenance', lambda path, db, paused: maintenance.append(paused))
    assert remote_update.run_update(state.path,tmp_path/'socket','remote','update_1','pane-1','herdr') == 1
    assert store.update_job('remote','update_1')['state'] == 'failed'
    assert maintenance == [True, False]
    assert not any('close' in a or 'install' in a for a in commands)


def test_update_resumes_admission_when_pause_response_is_lost(tmp_path, monkeypatch):
    state, store = make_job(tmp_path)
    commands = []
    maintenance = []
    def run(args, timeout=600):
        commands.append(args)
        if 'wheel' in args:
            (Path(args[args.index('--wheel-dir') + 1]) / 'herdr_talkback-1-py3-none-any.whl').write_bytes(b'wheel')
    def set_maintenance(path, db, paused):
        maintenance.append(paused)
        if paused:
            raise TimeoutError('response lost')
    monkeypatch.setattr(remote_update, '_run', run)
    monkeypatch.setattr(remote_update, '_backup_distribution', lambda backup: [Path('old')])
    monkeypatch.setattr(remote_update, '_runtime_info', lambda path: {'ok': True, 'module_path': str(tmp_path / 'old.py')})
    monkeypatch.setattr(remote_update, '_set_maintenance', set_maintenance)
    assert remote_update.run_update(state.path, tmp_path / 'socket', 'remote', 'update_1', 'pane-1', 'herdr') == 1
    assert maintenance == [True, False]
    assert store.update_job('remote', 'update_1')['state'] == 'failed'
    assert not any('close' in args for args in commands)


def test_update_restores_previous_install_when_activation_fails(tmp_path, monkeypatch):
    state, store = make_job(tmp_path)
    commands=[]; restored=[]
    def run(args, timeout=600):
        commands.append(args)
        if 'wheel' in args:
            (Path(args[args.index('--wheel-dir')+1])/'herdr_talkback-1-py3-none-any.whl').write_bytes(b'wheel')
        if 'install' in args: raise subprocess.CalledProcessError(1,args)
    monkeypatch.setattr(remote_update, '_run', run)
    monkeypatch.setattr(remote_update, '_backup_distribution', lambda backup: [Path('old')])
    monkeypatch.setattr(remote_update, '_restore_distribution', lambda backup, paths: restored.append(paths))
    monkeypatch.setattr(remote_update, '_runtime_info', lambda path: {'ok': True, 'module_path': str(tmp_path/'old.py')})
    monkeypatch.setattr(remote_update, '_set_maintenance', lambda path, db, paused: None)
    monkeypatch.setattr(remote_update, '_open_service', lambda herdr, update_id=None, maintenance=False: commands.append([herdr,'plugin','pane','open']) or 'old-pane')
    monkeypatch.setattr(remote_update.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0))
    checks = iter([False, False, False, True, True])
    monkeypatch.setattr(transport, 'local_endpoint_running', lambda path: next(checks))
    assert remote_update.run_update(state.path,tmp_path/'socket','remote','update_1','pane-1','herdr') == 1
    assert restored == [[Path('old')]]
    assert store.update_job('remote','update_1')['state'] == 'failed'
    assert any(a[:4] == ['herdr','plugin','pane','open'] for a in commands)


def test_updater_is_started_by_herdr_independently_of_service_pane(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setenv('HERDR_PANE_ID', 'service-pane')
    monkeypatch.setenv('HERDR_BIN_PATH', 'herdr-bin')
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout='{"result":{"type":"plugin_action_invoked"}}')
    monkeypatch.setattr(remote_update.subprocess, 'run', run)
    remote_update.launch_update(tmp_path/'state.db',tmp_path/'socket','peer','update_1')
    assert calls == [['herdr-bin','plugin','action','invoke','herdr-talkback.update-worker']]


def test_only_one_worker_can_claim_queued_update(tmp_path):
    state, store = make_job(tmp_path)
    first = store.claim_update()
    assert first['peer'] == 'remote' and first['request_id'] == 'update_1'
    assert store.claim_update() is None
    assert store.update_job('remote','update_1')['state'] == 'preparing'


def test_update_worker_claims_job_and_uses_saved_service_pane(tmp_path, monkeypatch):
    state = TalkbackState(tmp_path/'state.db', local_node_id='local'); state.init()
    store = PeerStore(state)
    assert store.start_update('remote','update_2','pane-service')
    config = SimpleNamespace(state_path=state.path, local_node_id='local', core_node_id='local',
                             daemon_socket_path=tmp_path/'daemon.sock')
    calls=[]
    monkeypatch.setattr(remote_update, '_windows_launcher_parent', lambda: None)
    monkeypatch.setattr(remote_update.subprocess, 'Popen', lambda args, **kwargs: calls.append((args, kwargs)))
    assert remote_update.run_next_update(config, 'herdr') == 0
    args, kwargs = calls[0]
    assert args[args.index('--pane-id') + 1] == 'pane-service'
    assert args[args.index('--request-id') + 1] == 'update_2'
    assert '--wait-pid' in args
    assert remote_update.run_next_update(config, 'herdr') == 0
    assert len(calls) == 1


def test_runtime_marker_reports_running_daemon_module(monkeypatch):
    from herdr_talkback.daemon import DaemonMessageRouter
    monkeypatch.setenv('HERDR_TALKBACK_UPDATE_ID','update_1')
    async def scenario():
        router = DaemonMessageRouter(SimpleNamespace(), None, None)
        result = await router.handle({'type':'runtime_info'})
        assert result['ok'] and result['update_id'] == 'update_1'
        assert result['module_path'].endswith('daemon.py')
    asyncio.run(scenario())


def test_update_rolls_back_if_new_runtime_is_not_the_installed_package(tmp_path, monkeypatch):
    state, store = make_job(tmp_path)
    commands=[]; opens=[]; restored=[]
    def run(args,timeout=600):
        commands.append(args)
        if 'wheel' in args:
            (Path(args[args.index('--wheel-dir')+1])/'herdr_talkback-1-py3-none-any.whl').write_bytes(b'wheel')
    def open_service(herdr, update_id=None, maintenance=False):
        opens.append(update_id)
        return 'new-pane' if update_id else 'old-pane'
    monkeypatch.setattr(remote_update,'_run',run)
    monkeypatch.setattr(remote_update,'_open_service',open_service)
    monkeypatch.setattr(remote_update,'_verify_runtime',lambda path,request_id:False)
    monkeypatch.setattr(remote_update,'_runtime_info',lambda path:{'ok':True,'module_path':str(tmp_path/'old.py')})
    monkeypatch.setattr(remote_update,'_set_maintenance',lambda path,db,paused:None)
    monkeypatch.setattr(remote_update,'_backup_distribution',lambda backup:[Path('old')])
    monkeypatch.setattr(remote_update,'_restore_distribution',lambda backup,paths:restored.append(paths))
    monkeypatch.setattr(remote_update.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=0))
    checks=iter([False,False,True,True,False,False,False,True,True])
    monkeypatch.setattr(transport,'local_endpoint_running',lambda path:next(checks))
    assert remote_update.run_update(state.path,tmp_path/'socket','remote','update_1','old-pane','herdr') == 1
    assert ['herdr','pane','close','new-pane'] in commands
    assert restored == [[Path('old')]] and opens == ['update_1',None]
    assert store.update_job('remote','update_1')['state'] == 'failed'


def test_update_jobs_migrate_older_schema(tmp_path):
    import sqlite3
    state = TalkbackState(tmp_path/'state.db', local_node_id='local'); state.init()
    with sqlite3.connect(state.path) as db:
        db.execute("create table peer_update_jobs (peer text not null, request_id text not null, state text not null, detail text not null default '', updated_at real not null, primary key(peer,request_id))")
    store = PeerStore(state)
    assert store.start_update('remote','req_legacy','service-pane')
    assert store.claim_update()['service_pane_id'] == 'service-pane'


def test_update_build_failure_leaves_service_running_and_records_reason(tmp_path, monkeypatch):
    state, store = make_job(tmp_path)
    calls=[]
    def run(args,timeout=600):
        calls.append(args)
        raise subprocess.CalledProcessError(1,args)
    monkeypatch.setattr(remote_update,'_run',run)
    assert remote_update.run_update(state.path,tmp_path/'socket','remote','update_1','pane-1','herdr') == 1
    job=store.update_job('remote','update_1')
    assert job['state']=='failed' and 'GitHub wheel build failed' in job['detail']
    assert len(calls)==1 and 'wheel' in calls[0]


def test_update_restarts_verified_previous_runtime_even_if_uninstall_failed(tmp_path, monkeypatch):
    state, store = make_job(tmp_path)
    opened=[]; gates=[]
    def run(args,timeout=600):
        if 'wheel' in args:
            (Path(args[args.index('--wheel-dir')+1])/'herdr_talkback-1-py3-none-any.whl').write_bytes(b'wheel')
        if 'install' in args: raise subprocess.CalledProcessError(1,args)
    monkeypatch.setattr(remote_update,'_run',run)
    monkeypatch.setattr(remote_update,'_backup_distribution',lambda backup:[Path('old')])
    monkeypatch.setattr(remote_update,'_restore_distribution',lambda backup,paths:None)
    monkeypatch.setattr(remote_update,'_runtime_info',lambda path:{'ok':True,'module_path':str(tmp_path/'old.py')})
    monkeypatch.setattr(remote_update,'_set_maintenance',lambda path,db,paused:gates.append(paused))
    monkeypatch.setattr(remote_update,'_open_service',lambda herdr,update_id=None,maintenance=False:opened.append(maintenance) or 'old-pane')
    monkeypatch.setattr(remote_update.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=1))
    checks=iter([False,False,False,True,True])
    monkeypatch.setattr(transport,'local_endpoint_running',lambda path:next(checks))
    assert remote_update.run_update(state.path,tmp_path/'socket','remote','update_1','pane-1','herdr') == 1
    job=store.update_job('remote','update_1')
    assert opened == [True] and gates == [True,False]
    assert job['state']=='failed' and 'rollback may be incomplete' in job['detail']
