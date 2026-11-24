#!/usr/bin/env python3
"""Bump the declared patch version alongside each committed code change.

Run by .githooks/pre-commit. The version lives in three files that must always
agree, so they are bumped together with the code they describe.

Rules:
  - no staged files: nothing to do;
  - only version files staged: treat it as a deliberate manual version edit and
    sync the other declarations to it instead of inventing a version;
  - anything else: bump the patch number.

Amending a commit re-runs this hook and bumps again, so a commit that was
amended twice skips a number. Versions stay unique and monotonic, which is what
the release flow relies on.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import bump_version  # noqa: E402  (import path prepared above)

VERSION_FILES = ("pyproject.toml", "herdr-plugin.toml", "herdr_talkback/__init__.py")


def staged_files() -> list[str]:
    result = subprocess.run(
        ("git", "diff", "--cached", "--name-only"),
        cwd=ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit("cannot read the staged file list")
    return [line for line in result.stdout.splitlines() if line.strip()]


def main() -> int:
    changed = staged_files()
    if not changed:
        return 0

    manual = all(name in VERSION_FILES for name in changed)
    versions = bump_version.declared_versions()
    try:
        target = bump_version.resolve(["--sync"] if manual else [], versions)
    except SystemExit as error:
        print(f"pre-commit: {error}", file=sys.stderr)
        return 1

    bump_version.write_versions(target, versions)
    subprocess.run(("git", "add", *VERSION_FILES), cwd=ROOT, check=False)

    if not manual:
        print(f"pre-commit: bumped version to {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
