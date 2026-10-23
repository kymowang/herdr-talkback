# Repository Guidelines

## Project Structure & Module Organization

`herdr_talkback/` contains the Python package: CLI and plugin entrypoints, message routing and delivery, monitoring, configuration, and persistent state. Herdr integration lives in `herdr_talkback/adapters/`. The root `plugin.py` and `herdr-plugin.toml` expose the Herdr plugin. Packaged defaults live in `herdr_talkback/default-rules.toml`; update `examples/config.toml` when configuration changes. Tests are in `tests/`, architecture notes in `docs/design.md`, and usage instructions in `README.md`.

## Build, Test, and Development Commands

Use Python 3.9 or newer. Live plugin operation requires Linux/macOS and Herdr 0.9.1 or newer.

- `python3 -m pip install -e '.[test]'`: install the editable package and pytest.
- `python3 -m pytest -q`: run the full test suite.
- `python3 -m pytest tests/test_delivery.py -q`: run focused delivery tests.
- `python3 -m pip wheel --no-deps .`: build a distributable wheel.
- `python3 -m herdr_talkback --help`: inspect CLI commands without installing the console script.
- `herdr plugin link "$PWD"`: register the checkout with Herdr.
- `herdr plugin action invoke herdr-talkback.check`: validate configuration and connectivity before invoking `herdr-talkback.start`.

## Coding Style & Naming Conventions

Follow existing Python style: four-space indentation, `snake_case` functions and modules, `PascalCase` classes, and `UPPER_SNAKE_CASE` constants. Preserve Python 3.9 compatibility and use type annotations consistent with nearby code. Keep transport-specific behavior in adapters. No formatter or linter is configured; avoid unrelated formatting changes.

## Testing Guidelines

Use pytest with files named `tests/test_<module>.py` and functions named `test_<behavior>`. Follow existing fake-client and temporary-state patterns to test routing, retries, configuration validation, and asynchronous behavior without a live Herdr session. Add regression tests for behavior changes. No numerical coverage threshold is configured; run the full suite before submitting.

## Commit & Pull Request Guidelines

History is short and mixes plain imperative summaries with `feat:` prefixes. Use concise, action-oriented subjects; use a scope or prefix when helpful. Pull requests should describe the problem, resulting behavior, related issues, and validation commands/results. Update configuration examples and documentation when changing public behavior.

## Configuration & Runtime Safety

Keep runtime databases and socket paths out of commits. Preserve session-specific state isolation and input-occupancy checks. Talkback supports local Herdr panes; do not introduce remote routing implicitly. Avoid running the legacy Agent Hub monitor alongside Talkback.
