---
name: herdr-talkback
description: Use when sending or inspecting Herdr Talkback messages, managing logical pane monitoring, or registering local and remote pane names.
---

# Herdr Talkback

Talkback is a Herdr plugin with a terminal command, `herdr-talkback`. It does not need to appear in the agent's tool list. Run it from a Herdr session with `HERDR_SOCKET_PATH` set. Identify the active plugin configuration before issuing commands: the CLI's default `~/.config/herdr-talkback/config.toml` may differ from the plugin configuration and point to a different session database or daemon. Pass the plugin configuration explicitly with `--config`.

Where supported, `herdr plugin config-dir herdr-talkback` reports the plugin configuration directory; append `config.toml` and verify it belongs to the current session.

Typical configuration paths, subject to the installation's actual settings:

- Windows PowerShell: `$cfg = "$env:APPDATA\herdr\plugins\config\herdr-talkback\config.toml"`
- Linux: `cfg="$HOME/.config/herdr/plugins/config/herdr-talkback/config.toml"`

On PowerShell use `$cfg` in the examples below; on POSIX shells use `"$cfg"`. Keep peer tokens and private keys private.

## Find and register panes

```text
herdr-talkback --config $cfg pane list
herdr-talkback --config $cfg peers list
herdr-talkback --config $cfg pane registered --json
herdr-talkback --config $cfg pane register local_dev linux-main/ast-dev1
herdr-talkback --config $cfg pane register remote_lead openclaw/lead
herdr-talkback --config $cfg pane unregister remote_lead
```

The addresses above are examples: use the actual local `core.node_id`, configured peer node ID, and pane name. Registrations are per machine and Herdr session; they do not synchronize to peers. Registered names cannot conflict with a local pane or configured alias. A remote node must already be configured as a peer with HTTPS, certificates and token; registration does not configure authentication. `pane registered` identifies local/remote and shows the configured peer URL. A local `node/pane` uses the local Herdr socket, not an IP connection. Do not infer a global host from `127.0.0.1`.

## Send and verify

Send to another pane only when the user has authorized the communication. Set the source to the actual sender pane; select a current pane name, registered alias or configured `node/pane` as target. Both header arrows are accepted: `[source->target]` and `[source→target]`. **When generating a message automatically, prefer the single-character `→`; accept `->` when the user types it.**

Use the actual message or receipt supplied by the user or established in context; do not invent its content. Quote shell arguments safely, especially when the text contains apostrophes, dollar signs or line breaks. Read a pane's visible screen only if needed to find the content.

```text
herdr-talkback --config $cfg send '[ast-dev3→ast-dev1] 回执内容'
herdr-talkback --config $cfg send '[ast-dev3→remote_lead] 回执内容'
herdr-talkback --config $cfg send '[ast-dev3→openclaw/lead] 回执内容'
herdr-talkback --config $cfg message status msg_实际消息ID --json
```

Use `send --dry-run` to check routing without sending. `queued` and `accepted` do not prove delivery: query the returned message ID and report its actual state. `delivered` is the final delivery receipt; `waiting_endpoint` means the target is unavailable or unregistered; `failed` or `delivery_unknown` requires inspection before retrying, because the message may already be in or submitted to a pane. Do not submit, clear or replace an occupied input box without the user's instruction. Registered aliases require daemon delivery; do not use `--direct` with them.

## Logical monitoring

```text
herdr-talkback --config $cfg monitor state
herdr-talkback --config $cfg monitor status --json
herdr-talkback --config $cfg monitor disable
herdr-talkback --config $cfg monitor enable
```

Only enable or disable monitoring when requested. `state` shows the persisted choice and whether the service and monitor task are running; `status` shows each pane's Herdr and inferred states. The commands apply to the running service immediately and persist per session in its state database, overriding `[logic_monitor].enabled` on later starts. The configured monitor pane and targets must exist in the plugin configuration before enabling. If the daemon is unreachable, check `--config` and `HERDR_SOCKET_PATH`; do not claim the setting changed. Logical monitoring is separate from message delivery and existing recovery rules.

The target observation interval is `[logic_monitor].observation_seconds` in seconds; `300` means five minutes. `poll_seconds` independently controls how often a pending monitor answer is checked and must remain compatible with `timeout_seconds`. Herdr `working`/`blocked` states take precedence; the monitor can infer `continue`, `waiting`, `blocked`, `done`, `unknown` or `timeout`. Treat `blocked` as requiring attention rather than automatically sending `continue`.

When inspecting a pane directly, read only its current visible screen; do not request history or trigger scrolling unless the user explicitly asks. This matters while another person or agent is using the pane.
