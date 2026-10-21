# Herdr Talkback migration

Source: Agent Hub b14021a; new independent repository, no changes to source checkout.

The existing socket client, input parser, role registry, SQLite state, endpoint dispatcher,
message queue, delivery verification and alert pipeline retain their behavior and tests.
Python modules and CLI are renamed to herdr_talkback / herdr-talkback.

The native herdr-plugin.toml manifest owns startup, actions and the service tab. Startup
is a short command opening a Herdr-owned service pane, not a detached daemon. A file lock
and inbox probe make repeated start requests harmless. The old Agent Hub inbox blocks
activation during migration. A Herdr-owned config directory contains editable TOML;
state lives under the plugin state directory with per-socket session separation.

RecoveryRule and MessageRule are validated immutable values. Each requires an explicit
agents label scope; glob matching is case-sensitive. Recovery may also restrict CLI types.
All default error regexes and append text live in a packaged TOML file. Config arrays
replace the corresponding defaults, including empty arrays to disable behavior. Working
agents cannot be targeted by automatic recovery. Existing input occupancy and delivery
verification protections are retained. Config changes take effect after service restart.

Rules have focused tests for name/type scopes, custom commands, bounds validation,
latest-turn isolation, unlimited recovery and append idempotence. The migrated suite
continues covering the complete Herdr communication pipeline. Plugin tests cover the
manifest, initialization without overwriting config, session isolation and duplicate startup.
Real Herdr plugin link and check validate the host contract without writing to agent panes.
