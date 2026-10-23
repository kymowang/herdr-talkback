# Peer deployment verification — 2026-10-23

The Windows and Linux Herdr plugins run the same `feat/peer-messaging` implementation,
including the pane discovery fix in `9fcc971`.

| Host | Node ID | HTTPS listener |
| --- | --- | --- |
| Windows | `linux-main` (existing identity) | `192.168.60.55:18765` |
| Linux | `openclaw` | `192.168.90.18:18765` |

Each host has its own private key and certificate. Public certificates and a shared
pair token were exchanged through the existing administrative SSH connection;
message transport uses authenticated, verified HTTPS short requests. Credentials
remain outside the repository. The user opened the Windows port; Linux firewalld
was inactive. No firewall rules were changed by this deployment.

Both hosts originally used the default `linux-main` identity. Before changing Linux
to `openclaw`, its database was checked: all 15 historical messages were delivered,
with no active deliveries. Configuration and SQLite backups were created as
`config.toml.before-peer-20261023` and `state.db.before-peer-20261023`. Historical
rows and their original qualified addresses were preserved; new messages use
`openclaw`. This is a deployment-specific identity transition, not an automatic
migration procedure for nodes with pending messages.

## Verification

- Windows Python 3.13: **306 tests passed**.
- Linux Python 3.9: **306 tests passed**.
- Wheel build succeeded; macOS execution remains covered by configured CI, not
  a live macOS deployment in this session.
- Both directions passed `peers check`, including certificate and node identity.
- Dedicated temporary probe panes received the actual message text in each
  direction, and both sender receipts reached `delivered`:
  - Windows → Linux: `msg_9111783125334cb08ce6884795a36e6e`.
  - Linux → Windows: `msg_cee958b2ebb6405e93e60b1851dbca7b`.
- Restarting only the Talkback services resumed those queued transfers. User agent
  panes were preserved; temporary probe panes were closed after verification.

The live test exposed missing pane label events in the installed Herdr version.
Talkback now refreshes the local endpoint registry every five seconds as a fallback
and wakes waiting messages when a named pane appears. This creates no idle peer
network requests. Fault tests additionally exercise lost acknowledgements,
deduplication, sender restart and receiver identity changes.
