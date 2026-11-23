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

When preparing configuration information for another machine to connect to this Talkback node, run `herdr-talkback pane identity <local-pane>` in the current Herdr session and provide the resulting identity card to the other operator. It supplies the node ID, pane, reachable HTTPS URL, local public-certificate path, and per-peer token file paths for cross-host configuration. The paths refer to files on the issuing machine; the recipient cannot use them as local paths. Exchange the public certificate and the token selected for that peer separately through a trusted channel, then configure the corresponding `[peers.<node_id>]` entry and verify connectivity. Never transfer the private key or disclose token contents in chat. Identity does not create peer trust; when the listener is wildcard or loopback, supply `--url` for the reachable HTTPS origin, and use `--config` if plugin configuration discovery is unavailable. Check command availability on older installations.

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
herdr-talkback pane identity monitor
herdr-talkback --config $cfg pane import '<identity-json>'
herdr-talkback --config $cfg pane register local_dev linux-main/ast-dev1
herdr-talkback --config $cfg pane register remote_lead openclaw/lead
herdr-talkback --config $cfg pane unregister remote_lead
```

The `register-all`, `whoami`, `identity`, and `import` commands require a Talkback version that supports them; check `herdr-talkback pane --help` and `herdr-talkback --help` before use, and ensure both peers support `whoami`. The addresses above are examples: use the actual local `core.node_id`, configured peer node ID, and pane name. Registrations are per machine and Herdr session; whoami explicitly introduces the sender to the remote peer, but other registrations do not automatically synchronize. A local pane may be registered under its own name; other local names and configured aliases cannot conflict. Whoami registers the target locally and introduces the authenticated sender to the remote Talkback; check message status to confirm delivery. Identity auto-discovers the active plugin configuration and uses its concrete listen address and port. For wildcard or loopback listeners, specify a reachable HTTPS origin with --url; --config overrides discovery. Identity cards list the local certificate path and per-peer token file paths, never their contents; these paths are on the sender and do not transfer the files. A newly generated plugin configuration uses the machine hostname for node ID; existing configurations are not renamed. Identity cards contain no credentials. A remote node must already be configured as a peer with HTTPS, certificates and token; registration does not configure authentication. `pane registered` identifies local/remote and shows the configured peer URL. A local `node/pane` uses the local Herdr socket, not an IP connection. Do not infer a global host from `127.0.0.1`.

## Send and verify

Send to another pane only when the user has authorized the communication. Set the source to the actual sender pane; select a current pane name, registered alias or configured `node/pane` as target. Both header arrows are accepted: `[source->target]` and `[source→target]`. **When generating a message automatically, prefer the single-character `→`; accept `->` when the user types it.**

The source is your **own Herdr pane label**, not the machine identity. `core.node_id`, the hostname, `$USER` and the pane title are not sender names; using one makes the message unrepliable and is rejected. Resolve your label before sending:

```text
herdr-talkback --config $cfg pane list   # find the label of the pane you are running in
echo "$HERDR_PANE_ID"                    # this pane's id; match it against pane list
```

If `send` fails with `source is the node id, not a sender`, the error names your actual pane label — resend using it. `send --dry-run` prints the resolved source, target and pane id without sending.

Use the actual message or receipt supplied by the user or established in context; do not invent its content. Quote shell arguments safely, especially when the text contains apostrophes, dollar signs or line breaks. Read a pane's visible screen only if needed to find the content.

```text
herdr-talkback --config $cfg send '[ast-dev3→ast-dev1] 回执内容'
herdr-talkback --config $cfg send '[ast-dev3→remote_lead] 回执内容'
herdr-talkback --config $cfg send '[ast-dev3→openclaw/lead] 回执内容'
herdr-talkback --config $cfg message status msg_实际消息ID --json
```

Use `send --dry-run` to check routing without sending. `queued` and `accepted` do not prove delivery: query the returned message ID and report its actual state. `delivered` is the final delivery receipt; `waiting_endpoint` means the target is unavailable or unregistered; `failed` or `delivery_unknown` requires inspection before retrying, because the message may already be in or submitted to a pane. The configured unattended delivery fallback may submit an occupied input while a message is waiting, but only when the **input box text** (not the status bar or hints below it) has not changed for `[delivery].stale_input_seconds` (default 600 seconds), whatever the draft contains: a Talkback header, a folded `[Pasted Content …]` block, or scrolled text with the `↑` marker all qualify. Set it to 0 to disable the fallback. It checks again before Enter and restarts timing whenever the composer text changes or the service restarts. Pending delivery continues only after the input clears. Cross-host fallback runs on the receiving host. Do not manually submit a draft without user instruction. Registered aliases require daemon delivery; do not use `--direct` with them.

## Diagnosing a broken peer link

Run `herdr-talkback peer diag <node_id>` before changing configuration. It walks the layers in order and reports the first broken one: TCP reachability, a TLS handshake with the peer certificate fingerprint, certificate/SAN match with the peer URL, the credential-free `/v1/ping` probe, an authenticated `/v1/info` call (which separates a wrong token, 401, from a permission denial, 403), and local cert/token file health. `--json` prints the same report for scripting.

The ping probe needs no token, so it works while credentials are still being exchanged. Peers on older builds answer it with 401; the report marks that as an unsupported probe rather than a failure. The report never prints token contents or private keys: it prints file paths, SHA-256 certificate fingerprints, key lengths and instructions for exchanging the public certificate and the pair token manually.

## Remote service control

Remote administration is opt-in per authenticated peer: on the receiving node, set `allow_remote_control = true` in the trusted sender's `[peers.<node>]` section and restart the service. It grants input inspection, submit, and installation update; use distinct peer tokens because shared tokens cannot distinguish peers securely. Both sides need a version supporting these commands.

```text
herdr-talkback peer input pc-54 arc-bus
herdr-talkback peer submit pc-54 arc-bus --pane-id <pane-id> --screen-sha256 <hash>
herdr-talkback peer submit-status pc-54 <request-id>
herdr-talkback peer update pc-54
herdr-talkback peer update-status pc-54 <request-id>
```

`input` reads only the currently visible screen and returns the input state, a visible excerpt, actual pane ID and a screen hash. Inspect it before manually submitting. `submit` refuses an empty, unknown, changed, or remapped input. Verification and key delivery are separate calls, so concurrent input can still race. Folded content is allowed by default but cannot be verified; explain this limitation before submitting. Use `--reject-folded` to refuse it. A submitted result means the key request was accepted, not that the agent processed the content. Submit and update operations use request IDs for deduplication: after a timeout query the original ID; never start a new request blindly.

`peer update` builds and installs the Python package from the fixed GitHub repository, backs up the previous installation, refuses activation while messages are in progress, and restarts the remote Herdr-owned service. It does not modify any remote Git checkout, user configuration or credentials. The Herdr plugin manifest registration and installed skill are not updated by this command; update those separately if needed. Query update status and report failures or uncertain results; a remote target must already support this protocol.

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
