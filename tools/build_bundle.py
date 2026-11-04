#!/usr/bin/env python3
"""Build the portable Talkback distribution ZIP."""
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import zipfile


def assemble(root, wheel, destination):
    files = {
        'install.py': root / 'distribution' / 'install.py',
        'INSTALL.md': root / 'distribution' / 'INSTALL.md',
        'herdr-plugin.toml': root / 'herdr-plugin.toml',
        'skills/herdr-talkback/SKILL.md': root / 'skills' / 'herdr-talkback' / 'SKILL.md',
        wheel.name: wheel,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, path in files.items():
            archive.write(path, name)
    return destination


def main():
    root = Path(__file__).resolve().parent.parent
    content = (root / 'herdr_talkback' / '__init__.py').read_text(encoding='utf-8')
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)', content, re.MULTILINE)
    if not match:
        raise ValueError('package version not found')
    with tempfile.TemporaryDirectory(prefix='talkback-wheel-') as directory:
        subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-deps',
                        '--wheel-dir', directory, str(root)], check=True)
        wheels = list(Path(directory).glob('herdr_talkback-*.whl'))
        if len(wheels) != 1:
            raise ValueError('wheel build did not produce exactly one Talkback wheel')
        target = root / 'dist' / ('herdr-talkback-' + match.group(1) + '-bundle.zip')
        print('Bundle: ' + str(assemble(root, wheels[0], target)))


if __name__ == '__main__':
    main()
