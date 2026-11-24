import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import bump_version  # noqa: E402
import version_hook  # noqa: E402

VERSION_FILES = version_hook.VERSION_FILES


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    def run(*args):
        return subprocess.run(("git", *args), cwd=tmp_path, capture_output=True, text=True, check=True)

    run("init", "--quiet")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    # The outer repository may enable .githooks; test repos run the hook
    # explicitly through version_hook.main() instead.
    run("config", "core.hooksPath", str(tmp_path / "no-hooks"))
    for name, content in (
        ("pyproject.toml", '[project]\nversion = "0.2.0"\n'),
        ("herdr-plugin.toml", 'version = "0.2.0"\n'),
        ("herdr_talkback/__init__.py", '__version__ = "0.2.0"\n'),
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
    run("add", "-A")
    run("commit", "--quiet", "-m", "init")

    monkeypatch.setattr(version_hook, "ROOT", tmp_path)
    monkeypatch.setattr(bump_version, "ROOT", tmp_path)
    return tmp_path, run


def write(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def committed_version(repo, run) -> str:
    text = run("show", "HEAD:herdr_talkback/__init__.py").stdout
    return bump_version.extract_version(text, "herdr_talkback/__init__.py")


def test_hook_does_nothing_without_staged_files(repo):
    _, run = repo

    assert version_hook.main() == 0
    assert committed_version(repo, run) == "0.2.0"


def test_hook_bumps_when_code_is_staged(repo):
    root, run = repo
    write(root / "feature.py", "x = 1\n")
    run("add", "feature.py")

    assert version_hook.main() == 0

    staged = run("show", ":herdr_talkback/__init__.py").stdout
    assert bump_version.extract_version(staged, "herdr_talkback/__init__.py") == "0.2.1"


def test_hook_syncs_instead_of_bumping_for_version_only_commits(repo):
    root, run = repo
    write(root / "herdr-plugin.toml", 'version = "0.7.0"\n')
    run("add", "herdr-plugin.toml")

    assert version_hook.main() == 0

    for name in VERSION_FILES:
        staged = run("show", f":{name}").stdout
        assert bump_version.extract_version(staged, name) == "0.7.0"


def test_hook_reports_inconsistent_versions_without_sync(repo, capsys):
    root, run = repo
    write(root / "herdr-plugin.toml", 'version = "0.7.0"\n')
    write(root / "feature.py", "x = 1\n")
    run("add", "-A")

    assert version_hook.main() == 1
    assert "disagree" in capsys.readouterr().err


def test_write_versions_keeps_crlf_line_endings(repo):
    root, _ = repo
    path = root / "pyproject.toml"
    path.write_bytes(b'[project]\r\nversion = "0.2.1"\r\n')
    versions = bump_version.declared_versions(root)

    bump_version.write_versions("0.2.2", versions, root)

    assert path.read_bytes() == b'[project]\r\nversion = "0.2.2"\r\n'
