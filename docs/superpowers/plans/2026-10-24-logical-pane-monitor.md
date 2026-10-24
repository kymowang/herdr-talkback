# Logical Pane Monitor Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans inline, with one final independent review. Track steps below.

**Goal:** Observe selected panes and drive their next steps through a dedicated monitor agent.
**Architecture:** Separate SQLite observation/request/attempt tables, a deterministic observation state machine, and a serial asynchronous judgment loop. Existing recovery and logical continuation share a per-pane action coordinator.
**Tech Stack:** Python 3.9+, SQLite, asyncio, existing Herdr local transport; no new dependencies.
**Spec:** `docs/superpowers/specs/2026-10-24-logical-pane-monitor-design.md`.

## Constraints and review focus

- Opt in only; no automatic live activation or model startup. Keep all existing networking behavior.
- Ten-second observation/stability/poll defaults; 120-second timeout/cooldown; three continues/30m by default.
- Reject ambiguous selectors, exclude monitor, invalidate changed snapshots and old requests.
- Parse fresh ID-correlated JSON answers only; never prompt echoes, examples or previous results.
- Persist before terminal writes; no automatic replay after restart or uncertain delivery.
- Avoid concurrent recovery/continuation and respect occupied inputs. Python 3.9 and all supported OSes.

## Task 1: Configuration and persistent state

Files: `logic_config.py`, `logic_state.py`, `config.py`, tests `test_logic_state.py`, `test_logic_config.py`.
- [x] RED: config validation/defaults; observations, stability, result cooldown, persistent attempt deduplication, request invalidation/restart.
- [x] GREEN: implement config and independent SQLite tables with transactional writes and audit events.
- [x] Verify focused tests and existing configuration/state tests; commit.

## Task 2: Monitor protocol and execution

Files: `logic_monitor.py`, `pane_actions.py`, `monitor.py`; tests `test_logic_monitor.py`.
- [x] RED: no-event observation, fresh correlated results, echo/late/conflicting rejection, monitor contention, timeout, snapshot/status changes and occupied input.
- [x] GREEN: implement bounded snapshots, exact JSON protocol, serial fair requests, execution rechecks and persistent single attempts. Coordinate legacy recovery.
- [x] Verify restart, retry limits, ambiguity, continue suggestions prompt and isolation; commit.

## Task 3: Service, CLI and documentation

Files: `service.py`, `cli.py`, `examples/config.toml`, `README.md`; tests existing service/CLI plus new command tests.
- [x] RED/GREEN: opt-in lifecycle, cancellation and read-only `monitor status [--json]` without Herdr socket.
- [x] Document configuration and status semantics; full suite and wheel build.
- [x] Independent review, resolve findings with regression tests, verify clean diff and record results.

## Execution rulings

Continue in the existing checkout on `feat/logical-pane-monitor`, consistent with the previous in-place development workflow. The new monitor remains disabled unless explicitly configured. Do not restart or configure live services as part of this implementation.

## Verification evidence

- Windows/Python 3.13: `python -m pytest -q` ? 346 passed.
- Linux/Python 3.9: same 346 tests passed in an isolated `/tmp` source copy; the deployed Linux plugin was not changed.
- `python -m pip wheel --no-deps . -w dist` succeeded; `git diff --check` clean.
- Dedicated temporary Windows target/judge panes exercised real Herdr transport using a simulated judge. The target physically received one `continue`, with a persisted `sent` attempt; both panes were closed afterward. This verifies transport and execution, not live model classification quality.
- Independent review identified fence-context parsing and crash-time uncertainty visibility. Both received failing regressions, fixes and passing reviewer verification. No remaining review findings.
- No live user pane monitoring enabled; existing services/configuration untouched. macOS execution remains for CI.

Ruling: Use a strict-input option on existing control delivery for this feature, so an existing `continue` draft or an overlay is never submitted. Preserve legacy callers' behavior.
Ruling: Fresh protocol output preserves baseline fence context and bounded-window overlap; expose reserved-attempt uncertainty in the same transaction before terminal writes.
