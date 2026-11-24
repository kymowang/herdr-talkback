__version__ = "0.2.1"


def _build_id():
    """Identify package source at process startup, including editable installs."""
    import hashlib
    from pathlib import Path
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    files = sorted(list(root.rglob('*.py')) + list(root.glob('*.toml')))
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode('utf-8') + b'\0')
        digest.update(path.read_bytes().replace(b'\r\n', b'\n'))
        digest.update(b'\0')
    return 'sha256:' + digest.hexdigest()[:16]


__build__ = _build_id()
