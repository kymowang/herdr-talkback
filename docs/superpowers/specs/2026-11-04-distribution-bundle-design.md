# Talkback distribution bundle

Produce a portable ZIP containing the Talkback wheel, Herdr plugin manifest,
the Talkback skill, an installer and concise installation instructions. One
bundle supports Windows, Linux and macOS with Python 3.9+ and Herdr installed.

`python install.py` creates a dedicated virtual environment in a stable user
directory, installs the bundled wheel with network dependencies, copies a
Herdr manifest with an absolute executable path, links the plugin, and installs
the skill in the Codex skills directory. `--local-only` omits optional network
dependencies. Dependencies not present in the ZIP require a Python package
index or another configured pip source; this is not an offline installer.

The installer never replaces Herdr's existing configuration, state, TLS keys
or tokens. It must not silently overwrite a different existing skill; report
the conflict and require an explicit `--replace-skill`. Installation prints the
actual CLI path and PATH instructions, plus the plugin check/start commands.
Linking uses Herdr's actual binary selected by `--herdr`, `HERDR_BIN_PATH`, or
PATH. Missing Herdr, incompatible Python, failed installs or link failures
produce a nonzero exit and do not claim success.

`tools/build_bundle.py` builds the wheel and a versioned ZIP in `dist/` without
including runtime databases, configuration, credentials or private keys. Test
the archive contents, install plan, existing-skill conflict and manifest path
rendering with isolated temporary directories and fakes; exercise the wheel in
an isolated environment and run the complete existing test suite. Building the
bundle must not install or reconfigure the user's running plugin.
