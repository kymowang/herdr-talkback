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

## Install on a new machine

Requires Herdr 0.9.1+, Python 3.9+, Git, and Python package access. Herdr must be able to find the Python-installed `herdr-talkback` and `herdr-talkback-plugin` entry points on its PATH. Install both the Python package and the GitHub-managed Herdr plugin; Herdr marketplace installation alone does not install Python dependencies:

```text
python -m pip install "herdr-talkback[network] @ git+https://github.com/kymowang/herdr-talkback.git"
herdr plugin install kymowang/herdr-talkback
herdr plugin action invoke herdr-talkback.check
herdr plugin log list --plugin herdr-talkback
herdr plugin action invoke herdr-talkback.start
```

Use `python3` where appropriate. Check the asynchronous check action's logs before starting; run from the intended Herdr session. Remote communication requires the reciprocal peer setup below. A local checkout can instead use `pip install -e '.[network]'` and `herdr plugin link <checkout>`; do not install over an existing linked plugin. To update, update both the Python package and Herdr-managed plugin and restart the service. For an offline-friendly portable bundle, follow its included `INSTALL.md`. Preserve existing configuration, state, and credentials.

## Configure peers before cross-host messaging

Both machines need Talkback network support (`python -m pip install -e '.[network]'` for an editable checkout), distinct existing `core.node_id` values, network listeners, and reciprocal `[peers.<node_id>]` entries. Do not change an established node ID without considering its persisted messages. A pane registration or identity card does not configure trust.

On each machine, generate a certificate for the IP or hostname the other machine will connect to. Choose the actual config directory and reachable address; never use `--force` unless explicitly authorized to replace existing credentials:

```text
herdr-talkback peer init-tls --directory <plugin-config-dir>/tls --host <this-machine-ip-or-hostname>
```

This creates `node.crt`, `node.key`, and `pair.token`. Keep each machine's private key on that machine. Exchange the public certificates, and securely distribute the same token to both machines; never expose tokens or private keys in chat, logs, identity cards, or Git. If both machines generated tokens, select one token for that peer pair and securely copy it to the peer's token file. A certificate must include the exact IP/hostname used in the peer URL.

Example in the first machine's existing plugin `config.toml`; merge with existing TOML sections rather than duplicating them:

```toml
[core]
node_id = "linux-main"

[network]
enabled = true
listen_host = "192.168.60.55"
listen_port = 18765
cert_file = "tls/node.crt"
key_file = "tls/node.key"

[peers.openclaw]
url = "https://192.168.90.18:18765"
ca_file = "tls/openclaw.crt"
token_file = "tls/pair.token"
```

On the other machine use `node_id = "openclaw"`, its own listener and certificate/key, and `[peers.linux-main]` with the first machine's HTTPS URL, public certificate and matching token. Paths are relative to the config file. The default listener port is 18765; allow each configured TCP port through the appropriate firewall. Run the plugin `check`, restart the correct Talkback service to load configuration, then check in both directions:

```text
herdr-talkback --config $cfg peers list
herdr-talkback --config $cfg peers check openclaw
```

Use the actual remote node ID on each side. A successful peer check verifies connectivity and authentication, not delivery to a particular pane. If the network dependencies are absent or a check fails, report the actual error and check configuration, certificate hostname, port and firewall. Do not expose credentials during diagnosis.

## Find and register panes

```text
herdr-talkback --config $cfg pane list
herdr-talkback --config $cfg peers list
herdr-talkback --config $cfg pane registered --json
herdr-talkback --config $cfg pane register-all
herdr-talkback --config $cfg whoami monitor 192.168.90.18 lead
herdr-talkback --config $cfg pane identity monitor --url https://192.168.60.55:18765
herdr-talkback --config $cfg pane import '<identity-json>'
herdr-talkback --config $cfg pane register local_dev linux-main/ast-dev1
herdr-talkback --config $cfg pane register remote_lead openclaw/lead
herdr-talkback --config $cfg pane unregister remote_lead
```

The `register-all`, `whoami`, `identity`, and `import` commands require a Talkback version that supports them; check `herdr-talkback pane --help` and `herdr-talkback --help` before use, and ensure both peers support `whoami`. The addresses above are examples: use the actual local `core.node_id`, configured peer node ID, and pane name. Registrations are per machine and Herdr session; whoami explicitly introduces the sender to the remote peer, but other registrations do not automatically synchronize. A local pane may be registered under its own name; other local names and configured aliases cannot conflict. Whoami registers the target locally and introduces the authenticated sender to the remote Talkback; check message status to confirm delivery. Identity cards contain no credentials and require a reachable HTTPS URL. A remote node must already be configured as a peer with HTTPS, certificates and token; registration does not configure authentication. `pane registered` identifies local/remote and shows the configured peer URL. A local `node/pane` uses the local Herdr socket, not an IP connection. Do not infer a global host from `127.0.0.1`.

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
