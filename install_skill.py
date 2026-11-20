#!/usr/bin/env python3
"""Install the bundled Talkback skill into known agent skill directories.

Herdr runs this as a plugin build command, so installing or updating the
plugin also refreshes the skill copy that agents read. Existing content is
backed up instead of being overwritten silently.
"""
from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path


def skill_dirs(home: Path, environ) -> list:
    """Return the skill roots that exist (or should exist) on this machine."""
    roots = []
    codex_home = Path(environ.get("CODEX_HOME") or (home / ".codex"))
    roots.append(codex_home / "skills")
    for candidate in (home / ".agents" / "skills", home / ".claude" / "skills"):
        if candidate.parent.is_dir():
            roots.append(candidate)
    return roots


def install_skill(source: Path, directory: Path) -> str:
    target = directory / "herdr-talkback" / "SKILL.md"
    if target.is_file() and target.read_bytes() == source.read_bytes():
        return f"unchanged {target}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "herdr-talkback").mkdir(parents=True, exist_ok=True)
    if target.is_file():
        backup = target.with_name("SKILL.md.before-" + datetime.now().strftime("%Y%m%d"))
        shutil.copy2(target, backup)
    shutil.copyfile(source, target)
    return f"installed {target}"


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = Path(argv[0]).resolve() if argv else Path(__file__).resolve().parent
    source = root / "skills" / "herdr-talkback" / "SKILL.md"
    if not source.is_file():
        print(f"skill source missing: {source}", file=sys.stderr)
        return 1
    home = Path(os.path.expanduser("~"))
    for directory in skill_dirs(home, os.environ):
        print(install_skill(source, directory))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
