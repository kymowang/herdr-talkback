import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import bump_version


def make_repo(tmp_path):
    subprocess.run(("git", "init", "--quiet"), cwd=tmp_path, check=True)
    for args in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(("git", "config", *args), cwd=tmp_path, check=True)
    for name, content in (
        ("pyproject.toml", '[project]\nversion = "0.2.0"\n'),
        ("herdr-plugin.toml", 'version = "0.2.0"\n'),
        ("herdr_talkback/__init__.py", '__version__ = "0.2.0"\n'),
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
    subprocess.run(("git", "add", "-A"), cwd=tmp_path, check=True)
    subprocess.run(("git", "commit", "--quiet", "-m", "init"), cwd=tmp_path, check=True)
    return tmp_path


def versions(root):
    return bump_version.declared_versions(root)


def test_bump_patch_increments_only_the_patch_number():
    assert bump_version.bump_patch("0.2.0") == "0.2.1"
    assert bump_version.bump_patch("1.9.9") == "1.9.10"


def test_bump_rejects_versions_without_three_parts():
    with pytest.raises(SystemExit):
        bump_version.bump_patch("1.0")


def test_compare_orders_versions_numerically():
    assert bump_version.compare("0.2.9", "0.2.10") < 0
    assert bump_version.compare("1.0.0", "0.9.9") > 0
    assert bump_version.compare("0.2.1", "0.2.1") == 0


def test_write_versions_updates_all_declarations(tmp_path):
    root = make_repo(tmp_path)

    bump_version.write_versions("0.3.0", versions(root), root)

    assert set(versions(root).values()) == {"0.3.0"}


def test_resolve_bumps_when_declarations_agree(tmp_path):
    root = make_repo(tmp_path)

    assert bump_version.resolve([], versions(root)) == "0.2.1"


def test_resolve_syncs_to_newest_when_asked(tmp_path):
    root = make_repo(tmp_path)
    (root / "herdr-plugin.toml").write_text('version = "0.9.9"\n', encoding="utf-8")

    assert bump_version.resolve(["--sync"], versions(root)) == "0.9.9"


def test_resolve_refuses_to_bump_inconsistent_declarations(tmp_path):
    root = make_repo(tmp_path)
    (root / "herdr-plugin.toml").write_text('version = "0.9.9"\n', encoding="utf-8")

    with pytest.raises(SystemExit, match="disagree"):
        bump_version.resolve([], versions(root))


def test_extract_version_reads_each_declaration_form():
    assert bump_version.extract_version('__version__ = "1.2.3"', "herdr_talkback/__init__.py") == "1.2.3"
    assert bump_version.extract_version('version = "1.2.3"', "pyproject.toml") == "1.2.3"
    assert bump_version.extract_version("no version here", "pyproject.toml") is None
