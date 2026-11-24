import re
from pathlib import Path

import herdr_talkback

ROOT = Path(__file__).resolve().parent.parent


def _declared(pattern: str, name: str) -> str:
    match = re.search(pattern, (ROOT / name).read_text(encoding="utf-8"), re.MULTILINE)
    assert match is not None, f"no version declared in {name}"
    return match.group(1)


def test_package_exports_version():
    assert re.fullmatch(r"\d+\.\d+\.\d+", herdr_talkback.__version__)


def test_version_declarations_agree():
    # The pre-commit hook bumps all three together; a mismatch means one file was
    # edited by hand and the release flow would publish a confusing version.
    pyproject = _declared(r'^version = "([^"]+)"', "pyproject.toml")
    plugin = _declared(r'^version = "([^"]+)"', "herdr-plugin.toml")

    assert herdr_talkback.__version__ == pyproject
    assert herdr_talkback.__version__ == plugin
