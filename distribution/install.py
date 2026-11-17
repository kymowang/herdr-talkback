#!/usr/bin/env python3
"""Install a Talkback bundle into a user-owned, stable location."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import uuid


def render_manifest(source, executable, entry):
    marker = '"herdr-talkback-plugin"'
    if source.count(marker) != 6:
        raise ValueError('unexpected plugin manifest: command entries not found')
    return source.replace(marker, json.dumps(str(executable), ensure_ascii=False) + ', ' +
                          json.dumps(str(entry), ensure_ascii=False))


def render_entry(herdr_bin):
    return ('import os\n'
            'os.environ["HERDR_BIN_PATH"] = ' + json.dumps(str(herdr_bin)) + '\n'
            'from herdr_talkback.plugin import main\n'
            'raise SystemExit(main())\n')


def render_cli_launcher(python):
    return '#!/bin/sh\nexec ' + shlex.quote(str(python)) + ' -m herdr_talkback "$@"\n'


def launcher_content(python, runtime_cli, windows):
    if windows:
        return runtime_cli.read_bytes()
    return render_cli_launcher(python).encode('utf-8')


def display_herdr_command(path, windows):
    if windows:
        return "& '" + str(path).replace("'", "''") + "'"
    return shlex.quote(str(path))


def write_atomic(path, content, executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        staged.write_bytes(content)
        if executable:
            staged.chmod(0o755)
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


def check_skill(source, target, replace):
    if target.exists() and target.read_bytes() != source.read_bytes() and not replace:
        raise ValueError('a different skill already exists at ' + str(target) + '; use --replace-skill to replace it')


def install_skill(source, target, replace):
    check_skill(source, target, replace)
    if target.exists() and target.read_bytes() == source.read_bytes():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def locate_herdr(value):
    candidate = value or os.environ.get('HERDR_BIN_PATH') or 'herdr'
    resolved = shutil.which(str(candidate))
    if not resolved:
        raise ValueError('Herdr executable not found; use --herdr with its path')
    return str(Path(resolved).resolve())


def install(bundle, prefix, skills_dir, herdr=None, network=True, replace_skill=False):
    if sys.version_info < (3, 9):
        raise ValueError('Python 3.9 or newer is required')
    bundle, prefix, skills_dir = Path(bundle), Path(prefix).expanduser().resolve(), Path(skills_dir).expanduser().resolve()
    herdr_bin = locate_herdr(herdr)
    wheels = sorted(bundle.glob('herdr_talkback-*.whl'))
    if len(wheels) != 1:
        raise ValueError('bundle must contain exactly one Talkback wheel')
    manifest_file = bundle / 'herdr-plugin.toml'
    skill_file = bundle / 'skills' / 'herdr-talkback' / 'SKILL.md'
    if not manifest_file.is_file() or not skill_file.is_file():
        raise ValueError('bundle is missing the plugin manifest or skill')
    skill_target = skills_dir / 'herdr-talkback' / 'SKILL.md'
    check_skill(skill_file, skill_target, replace_skill)
    source_manifest = manifest_file.read_text(encoding='utf-8')
    render_manifest(source_manifest, 'preflight', 'entry.py')

    runtime = prefix / 'runtimes' / uuid.uuid4().hex
    subprocess.run([sys.executable, '-m', 'venv', str(runtime)], check=True)
    scripts = runtime / ('Scripts' if os.name == 'nt' else 'bin')
    python = scripts / ('python.exe' if os.name == 'nt' else 'python')
    plugin = scripts / ('herdr-talkback-plugin.exe' if os.name == 'nt' else 'herdr-talkback-plugin')
    runtime_cli = scripts / ('herdr-talkback.exe' if os.name == 'nt' else 'herdr-talkback')
    wheel_requirement = str(wheels[0].resolve()) + ('[network]' if network else '')
    pip_env = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK='1', PIP_NO_INPUT='1')
    subprocess.run([str(python), '-m', 'pip', 'install', wheel_requirement], check=True, env=pip_env)
    if not plugin.is_file() or not runtime_cli.is_file():
        raise RuntimeError('wheel installation did not create the expected command entrypoints')

    plugin_dir = prefix / 'plugin'
    manifest_path = plugin_dir / 'herdr-plugin.toml'
    entry = plugin_dir / 'entry.py'
    cli = prefix / 'bin' / ('herdr-talkback.exe' if os.name == 'nt' else 'herdr-talkback')
    targets = (manifest_path, entry, cli, skill_target)
    previous = {path: path.read_bytes() if path.exists() else None for path in targets}
    try:
        write_atomic(entry, render_entry(herdr_bin).encode('utf-8'))
        write_atomic(manifest_path, render_manifest(source_manifest, python, entry).encode('utf-8'))
        write_atomic(cli, launcher_content(python, runtime_cli, os.name == 'nt'), executable=os.name != 'nt')
        install_skill(skill_file, skill_target, replace_skill)
        subprocess.run([herdr_bin, 'plugin', 'link', str(plugin_dir)], check=True)
    except BaseException:
        for path, content in previous.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                write_atomic(path, content, executable=path == cli and os.name != 'nt')
        raise
    return {'cli': str(cli), 'plugin_dir': str(plugin_dir), 'skill': str(skill_target),
            'herdr': herdr_bin, 'network': network}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Install Herdr Talkback and its Codex skill')
    default_prefix = (Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')) / 'herdr-talkback'
                      if os.name == 'nt' else Path.home() / '.local' / 'share' / 'herdr-talkback')
    default_skills = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'skills'
    parser.add_argument('--prefix', type=Path, default=default_prefix)
    parser.add_argument('--skills-dir', type=Path, default=default_skills)
    parser.add_argument('--herdr', help='Path to the Herdr executable')
    parser.add_argument('--local-only', action='store_true', help='Do not install optional network dependencies')
    parser.add_argument('--replace-skill', action='store_true', help='Replace an existing different Talkback skill')
    args = parser.parse_args(argv)
    try:
        result = install(Path(__file__).resolve().parent, args.prefix, args.skills_dir,
                         args.herdr, not args.local_only, args.replace_skill)
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print('Installation failed: ' + str(exc), file=sys.stderr)
        return 1
    print('Talkback and skill installed successfully.')
    print('CLI: ' + result['cli'])
    print('Plugin: ' + result['plugin_dir'])
    print('Skill: ' + result['skill'])
    print('Add the CLI directory to PATH, or invoke the displayed CLI path directly.')
    command = display_herdr_command(result['herdr'], os.name == 'nt')
    print('Then run: ' + command + ' plugin action invoke herdr-talkback.check')
    print('To start: ' + command + ' plugin action invoke herdr-talkback.start')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
