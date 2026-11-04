# Distribution Bundle Implementation Plan

**Goal:** Build one reviewable ZIP that installs the plugin and skill across supported platforms.
**Architecture:** A build script assembles the existing Python wheel, manifest, skill, installer and instructions. The installer creates a stable virtual environment and a Herdr manifest referencing its absolute plugin executable, then links it through Herdr.
**Tech Stack:** Python 3.9+, stdlib `venv`, `zipfile`, `subprocess`, existing setuptools wheel.
**Spec:** `docs/superpowers/specs/2026-11-04-distribution-bundle-design.md`.

- [x] Add failing tests for bundle contents, manifest rewriting, existing skill conflicts, installation failures and cross-platform paths.
- [x] Implement installer and bundle builder; keep config/state/keys and existing skills safe.
- [x] Document prerequisites and commands; run focused and full tests; build and inspect a real bundle.

Verification: Windows Python 3.13 and Linux Python 3.9 suites each passed 384 tests. A real Windows network-extra wheel installation imported aiohttp and cryptography. Isolated local-only installs and upgrades ran the stable CLI on Windows (including a Chinese path) and Linux (Python 3.11 and 3.9); Herdr link was simulated to avoid changing active registrations. macOS and real Herdr link/relink remain unverified. Independent review findings on stable PATH, rollback and selected Herdr executable were fixed and re-reviewed.
