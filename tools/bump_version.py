#!/usr/bin/env python3
"""Bump or sync the patch version across every file that declares it.

The version lives in three files that must always agree. Operations:

* ``--bump`` (default) adds one to the patch number and rewrites all files.
* ``--sync`` takes the newest declared version and rewrites the others to match,
  for when a version was edited by hand in one file only.
* ``--dry-run`` prints the result without writing anything.

Any inconsistency without ``--sync`` is an error rather than something to
resolve by guessing which file is authoritative.

Prints the resulting version on stdout so a git hook can report it.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SOURCES = (
    ("pyproject.toml", re.compile(r'(?m)^version = "([^"]+)"')),
    ("herdr-plugin.toml", re.compile(r'(?m)^version = "([^"]+)"')),
    ("herdr_talkback/__init__.py", re.compile(r'(?m)^__version__ = "([^"]+)"')),
)
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

_PATTERNS = dict(SOURCES)


def _read(path: Path) -> str:
    # newline="" keeps the file's existing line endings intact on rewrite.
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _write(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def extract_version(text: str, name: str) -> str | None:
    """Return the version declared in `text` for the file named `name`."""
    pattern = _PATTERNS.get(name)
    if pattern is None:
        return None
    match = pattern.search(text)
    return None if match is None else match.group(1)


def _parts(version: str) -> tuple[int, int, int]:
    match = VERSION_RE.match(version)
    if match is None:
        raise SystemExit(f"unsupported version {version!r}: expected MAJOR.MINOR.PATCH")
    return tuple(int(part) for part in match.groups())


def compare(left: str, right: str) -> int:
    """Order two versions: negative, zero, or positive like a sort key."""
    left_parts, right_parts = _parts(left), _parts(right)
    return (left_parts > right_parts) - (left_parts < right_parts)


def declared_versions(root: Path | None = None) -> dict[str, str]:
    root = ROOT if root is None else root
    versions = {}
    for name, pattern in SOURCES:
        match = pattern.search(_read(root / name))
        if match is None:
            raise SystemExit(f"no version declaration found in {name}")
        versions[name] = match.group(1)
    return versions


def bump_patch(version: str) -> str:
    major, minor, patch = _parts(version)
    return f"{major}.{minor}.{patch + 1}"


def write_versions(new_version: str, versions: dict[str, str], root: Path | None = None) -> None:
    root = ROOT if root is None else root
    for name, pattern in SOURCES:
        current = versions[name]
        if current == new_version:
            continue
        path = root / name

        def replace(match, current=current, new_version=new_version):
            return match.group(0).replace(current, new_version)

        _write(path, pattern.sub(replace, _read(path), count=1))


def resolve(argv, versions: dict[str, str]) -> str:
    """Return the version this invocation should produce, without writing it."""
    unique = set(versions.values())
    if "--sync" in argv:
        return max(unique, key=_parts)
    if len(unique) != 1:
        detail = ", ".join(f"{name}={version}" for name, version in sorted(versions.items()))
        raise SystemExit(
            f"version declarations disagree ({detail}); "
            "run tools/bump_version.py --sync to pick the newest")
    return bump_patch(next(iter(unique)))


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    versions = declared_versions()
    target = resolve(argv, versions)
    if "--dry-run" not in argv:
        write_versions(target, versions)
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
