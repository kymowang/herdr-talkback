import importlib.util
import json
import zipfile
from pathlib import Path

import pytest


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def installer():
    return load('talkback_installer', Path('distribution/install.py'))


def test_manifest_uses_absolute_runtime_executable_and_preserves_actions(installer):
    from herdr_talkback.config import tomllib
    source = Path('herdr-plugin.toml').read_text(encoding='utf-8')
    path = 'C:\\Users\\Name With Space\\runtime\\herdr-talkback-plugin.exe'
    entry = 'C:\\Users\\Name With Space\\plugin\\entry.py'
    result = tomllib.loads(installer.render_manifest(source, path, entry))
    assert result['startup'][0]['command'] == [path, entry, 'start']
    assert all(action['command'][:2] == [path, entry] for action in result['actions'])
    assert result['panes'][0]['command'] == [path, entry, 'serve']
    assert result['id'] == 'herdr-talkback'


def test_skill_conflict_is_preserved_until_explicit_replacement(installer, tmp_path):
    source = tmp_path / 'source.md'; source.write_text('new', encoding='utf-8')
    target = tmp_path / 'skills' / 'herdr-talkback' / 'SKILL.md'
    target.parent.mkdir(parents=True); target.write_text('custom', encoding='utf-8')
    with pytest.raises(ValueError, match='replace-skill'):
        installer.install_skill(source, target, replace=False)
    assert target.read_text(encoding='utf-8') == 'custom'
    installer.install_skill(source, target, replace=True)
    assert target.read_text(encoding='utf-8') == 'new'


def test_same_skill_can_be_installed_repeatedly(installer, tmp_path):
    source = tmp_path / 'source.md'; source.write_text('same', encoding='utf-8')
    target = tmp_path / 'skill' / 'SKILL.md'
    installer.install_skill(source, target, replace=False)
    installer.install_skill(source, target, replace=False)
    assert target.read_text(encoding='utf-8') == 'same'


def test_bundle_contains_wheel_plugin_skill_and_installer_without_credentials(tmp_path):
    builder = load('talkback_builder', Path('tools/build_bundle.py'))
    wheel = tmp_path / 'herdr_talkback-0.1.0-py3-none-any.whl'; wheel.write_bytes(b'wheel')
    archive = tmp_path / 'bundle.zip'
    builder.assemble(Path.cwd(), wheel, archive)
    with zipfile.ZipFile(archive) as z:
        assert set(z.namelist()) == {'install.py', 'INSTALL.md', 'herdr-plugin.toml',
                                     'skills/herdr-talkback/SKILL.md', wheel.name}
        assert b'name: herdr-talkback' in z.read('skills/herdr-talkback/SKILL.md')
        assert not any(name.endswith(('.db', '.key', '.token')) for name in z.namelist())


def test_installer_preflights_missing_herdr_before_writing(installer, tmp_path, monkeypatch):
    monkeypatch.setattr(installer.shutil, 'which', lambda _: None)
    monkeypatch.delenv('HERDR_BIN_PATH', raising=False)
    prefix = tmp_path / 'prefix'
    with pytest.raises(ValueError, match='Herdr'):
        installer.install(tmp_path, prefix, tmp_path / 'skills', herdr=None, network=False, replace_skill=False)
    assert not prefix.exists()


def test_failed_link_restores_existing_manifest_entry_launcher_and_skill(installer, tmp_path, monkeypatch):
    import subprocess
    import sys
    bundle = tmp_path / 'bundle'; bundle.mkdir()
    (bundle / 'herdr_talkback-0.1.0-py3-none-any.whl').write_bytes(b'wheel')
    (bundle / 'herdr-plugin.toml').write_bytes(Path('herdr-plugin.toml').read_bytes())
    source = bundle / 'skills' / 'herdr-talkback' / 'SKILL.md'
    source.parent.mkdir(parents=True); source.write_text('new', encoding='utf-8')
    prefix, skills = tmp_path / 'prefix', tmp_path / 'skills'
    existing = [prefix / 'plugin' / 'herdr-plugin.toml', prefix / 'plugin' / 'entry.py',
                prefix / 'bin' / ('herdr-talkback.exe' if installer.os.name == 'nt' else 'herdr-talkback'),
                skills / 'herdr-talkback' / 'SKILL.md']
    for path in existing:
        path.parent.mkdir(parents=True, exist_ok=True); path.write_text('old', encoding='utf-8')
    monkeypatch.setattr(installer.shutil, 'which', lambda _: sys.executable)
    def run(args, **kwargs):
        if args[1:3] == ['-m', 'venv']:
            folder = Path(args[3]) / ('Scripts' if installer.os.name == 'nt' else 'bin')
            folder.mkdir(parents=True)
            for name in (('herdr-talkback-plugin.exe', 'herdr-talkback.exe') if installer.os.name == 'nt'
                         else ('herdr-talkback-plugin', 'herdr-talkback')):
                (folder / name).touch()
        elif args[1:3] == ['plugin', 'link']:
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(installer.subprocess, 'run', run)
    with pytest.raises(subprocess.CalledProcessError):
        installer.install(bundle, prefix, skills, herdr=sys.executable, network=False, replace_skill=True)
    assert [path.read_text(encoding='utf-8') for path in existing] == ['old'] * 4


def test_pip_failure_does_not_replace_existing_plugin_or_skill(installer, tmp_path, monkeypatch):
    import subprocess
    import sys
    bundle = tmp_path / 'bundle'; bundle.mkdir()
    (bundle / 'herdr_talkback-0.1.0-py3-none-any.whl').write_bytes(b'wheel')
    (bundle / 'herdr-plugin.toml').write_bytes(Path('herdr-plugin.toml').read_bytes())
    source = bundle / 'skills' / 'herdr-talkback' / 'SKILL.md'
    source.parent.mkdir(parents=True); source.write_text('new', encoding='utf-8')
    prefix, skills = tmp_path / 'prefix', tmp_path / 'skills'
    manifest = prefix / 'plugin' / 'herdr-plugin.toml'; manifest.parent.mkdir(parents=True)
    manifest.write_text('old manifest', encoding='utf-8')
    target = skills / 'herdr-talkback' / 'SKILL.md'; target.parent.mkdir(parents=True)
    target.write_text('old skill', encoding='utf-8')
    monkeypatch.setattr(installer.shutil, 'which', lambda _: sys.executable)
    def run(args, **kwargs):
        if args[1:3] == ['-m', 'pip']:
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(installer.subprocess, 'run', run)
    with pytest.raises(subprocess.CalledProcessError):
        installer.install(bundle, prefix, skills, sys.executable, False, True)
    assert manifest.read_text(encoding='utf-8') == 'old manifest'
    assert target.read_text(encoding='utf-8') == 'old skill'


def test_entry_binds_selected_herdr_and_launcher_uses_stable_path(installer):
    entry = installer.render_entry('C:\\Program Files\\Herdr\\herdr.exe')
    assert 'HERDR_BIN_PATH' in entry
    assert 'herdr.exe' in entry
    assert 'herdr_talkback' in installer.render_cli_launcher('/tmp/runtime with spaces/python')
    assert installer.display_herdr_command('C:\\Program Files\\Herdr\\herdr.exe', windows=True).startswith("& 'C:")
    assert installer.display_herdr_command('/opt/Herdr Tools/herdr', windows=False) == "'/opt/Herdr Tools/herdr'"


def test_windows_launcher_copies_native_executable_bytes(installer, tmp_path):
    runtime_cli = tmp_path / 'cli.exe'
    runtime_cli.write_bytes(b'MZ\x00fake-native-launcher')
    assert installer.launcher_content('C:\\path', runtime_cli, windows=True) == runtime_cli.read_bytes()


def test_successful_installs_use_network_extra_and_keep_cli_path_stable(installer, tmp_path, monkeypatch):
    import subprocess
    import sys
    bundle = tmp_path / 'bundle'; bundle.mkdir()
    wheel = bundle / 'herdr_talkback-0.1.0-py3-none-any.whl'; wheel.write_bytes(b'wheel')
    (bundle / 'herdr-plugin.toml').write_bytes(Path('herdr-plugin.toml').read_bytes())
    skill = bundle / 'skills' / 'herdr-talkback' / 'SKILL.md'
    skill.parent.mkdir(parents=True); skill.write_text('skill', encoding='utf-8')
    monkeypatch.setattr(installer.shutil, 'which', lambda _: sys.executable)
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if args[1:3] == ['-m', 'venv']:
            folder = Path(args[3]) / ('Scripts' if installer.os.name == 'nt' else 'bin')
            folder.mkdir(parents=True)
            for name in (('herdr-talkback-plugin.exe', 'herdr-talkback.exe') if installer.os.name == 'nt'
                         else ('herdr-talkback-plugin', 'herdr-talkback')):
                (folder / name).write_bytes(str(folder).encode('utf-8'))
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(installer.subprocess, 'run', run)
    a = installer.install(bundle, tmp_path / 'prefix', tmp_path / 'skills', sys.executable, True)
    first = Path(a['cli']).read_bytes()
    b = installer.install(bundle, tmp_path / 'prefix', tmp_path / 'skills', sys.executable, True)
    assert a['cli'] == b['cli'] and first != Path(b['cli']).read_bytes()
    assert sum('[network]' in ' '.join(map(str, call)) for call in calls) == 2
    assert (Path(b['plugin_dir']) / 'entry.py').read_text(encoding='utf-8').find('HERDR_BIN_PATH') >= 0
