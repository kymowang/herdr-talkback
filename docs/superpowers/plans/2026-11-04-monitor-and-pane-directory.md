# Monitor Control and Pane Directory Implementation Plan

**Goal:** Let AI operators enable or disable logical monitoring immediately and address registered local or remote panes by name.
**Architecture:** Persist operator-controlled settings and pane registrations per Herdr session in SQLite. The running daemon handles monitor control and the existing router resolves registered aliases to canonical `node/pane` addresses. Peer IP, port and TLS identity remain in peer configuration; local panes retain local transport.
**Tech Stack:** Python 3.9+, SQLite, asyncio, existing local daemon and peer protocol.

- [x] Test persisted monitor control, running-service cancellation, disabled startup, enable validation and restart behavior; implement CLI and daemon control.
- [x] Test pane registration, removal, listing, local and configured remote alias routing, collisions and absent targets; implement CLI and durable directory.
- [x] Document commands, address semantics and existing configuration, run full suite and verify live behavior without messaging user agents.

The enabled override is stored in the session state database; it supersedes `[logic_monitor].enabled` until changed through the command. The monitor pane and target settings continue to come from the configured TOML file. To avoid a startup race, control commands require the daemon to be online; they report failure without changing the setting if it is unavailable. Remote pane registrations refer only to explicitly configured authenticated peer node IDs; arbitrary network destinations are not accepted.

Verification: 373 tests passed on Windows/Python 3.13 and Linux/Python 3.9; an actual local daemon socket was exercised using a simulated monitor. An independent review identified startup/control races, state-query side effects, direct dry-run writes, and pane-name collisions; fixes and regression tests were verified.
