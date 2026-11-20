from pathlib import Path

import install_skill


def test_install_skill_copies_and_is_idempotent(tmp_path):
    source = tmp_path / 'source' / 'SKILL.md'
    source.parent.mkdir()
    source.write_text('---\nname: herdr-talkback\n---\n', encoding='utf-8')
    target_dir = tmp_path / 'skills'

    first = install_skill.install_skill(source, target_dir)
    assert 'installed' in first
    assert (target_dir / 'herdr-talkback' / 'SKILL.md').read_text(encoding='utf-8') == source.read_text(encoding='utf-8')

    second = install_skill.install_skill(source, target_dir)
    assert 'unchanged' in second


def test_install_skill_backs_up_different_content(tmp_path):
    source = tmp_path / 'SKILL.md'
    source.write_text('new skill', encoding='utf-8')
    target_dir = tmp_path / 'skills' / 'herdr-talkback'
    target_dir.mkdir(parents=True)
    (target_dir / 'SKILL.md').write_text('old skill', encoding='utf-8')

    install_skill.install_skill(source, tmp_path / 'skills')

    assert (target_dir / 'SKILL.md').read_text(encoding='utf-8') == 'new skill'
    backups = list(target_dir.glob('SKILL.md.before-*'))
    assert backups and backups[0].read_text(encoding='utf-8') == 'old skill'


def test_skill_dirs_include_codex_and_existing_agent_roots(tmp_path):
    (tmp_path / '.agents' / 'skills').mkdir(parents=True)
    (tmp_path / '.claude' / 'skills').mkdir(parents=True)

    roots = install_skill.skill_dirs(tmp_path, {})

    assert tmp_path / '.codex' / 'skills' in roots
    assert tmp_path / '.agents' / 'skills' in roots
    assert tmp_path / '.claude' / 'skills' in roots


def test_skill_dirs_respects_codex_home(tmp_path):
    custom = tmp_path / 'codex-home'
    roots = install_skill.skill_dirs(tmp_path, {'CODEX_HOME': str(custom)})

    assert custom / 'skills' in roots
