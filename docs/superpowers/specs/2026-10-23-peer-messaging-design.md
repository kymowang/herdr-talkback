# Peer Messaging over Short HTTPS Requests

## Intent and agreed direction

Enable reliable bidirectional Talkback messages between the Windows workstation
and `openclaw@192.168.90.18`, without SSH transport, persistent network connections,
or an intermediary. Cross-host traffic is infrequent. The user selected independent,
mutually reachable peers, delegated port selection, and requested configuration-file
control and firewall assistance. Preserve existing local messaging and recovery.

The selected port is **TCP 18765** on each host; both ports were unoccupied when
inspected. Observed addresses are Windows `192.168.60.55` and Linux `192.168.90.18`.
Both remain deployment settings, not constants in routing code.

## Approaches

1. **Peer HTTPS short requests, selected by the user:** each node listens and can
   initiate requests. No idle traffic; both hosts must allow the peer's inbound
   connection. Durable queues and status queries carry reliability.
2. **SSH duplex stream:** no Windows inbound SSH server is necessary if Windows
   dials Linux, but an established stream and SSH deployment become dependencies.
   The user selected independent networking instead.
3. **One accessible relay with polling:** supports one-way reachability, but adds
   idle polling, latency, and a relay. Not needed for these mutually reachable hosts.

## Guarantees and boundaries

- Either node can originate a message to an explicitly configured peer.
- Local SQLite commits precede local enqueue success; receiver SQLite commits
  precede network acceptance. Retries reuse the same immutable message identity.
- Duplicate transfers do not create another delivery. Conflicting reuse of an ID
  is rejected instead of overwriting content.
- Remote queue acceptance and confirmed Pane delivery remain distinct facts.
- Pending messages survive either service restarting. A disconnected peer does
  not block local delivery or another destination's lane.
- Preserve per-destination FIFO, including waiting for a preceding message's final
  receipt. Different destinations may progress independently.
- A crash after a local Pane may have received input retains `delivery_unknown`;
  it does not automatically submit again. No exactly-once execution claim is made.
- Agent replies are new messages sent using Talkback commands. This does not scrape
  terminal output, infer completed tasks, or automatically forward an agent's answer.
- No routing through third nodes, remote shell execution, or cross-host recovery
  commands. The local recovery monitor continues to control local panes only.

## Configuration and deployment

Network features are opt-in. Existing configurations remain local-only. Keep the
currently stored local node ID unchanged on machines with existing message data;
the remote node must have a different ID. For this deployment, the existing Windows
ID may remain `linux-main`, and the Linux node can use `openclaw`.

Proposed configuration (file paths are examples; resolve relative paths against
the configuration file's directory):

```toml
[core]
node_id = "linux-main"

[network]
enabled = true
listen_host = "192.168.60.55"
listen_port = 18765
cert_file = "tls/local.crt"
key_file = "tls/local.key"

[peers.openclaw]
url = "https://192.168.90.18:18765"
ca_file = "tls/openclaw.crt"
token_file = "secrets/openclaw.token"
```

The Linux configuration reverses peer URLs, certificates and IDs, and binds its
own LAN address. Ports must be in 1..65535. Reject duplicate local/peer IDs, URL
credentials, queries/fragments, non-HTTPS peer URLs, and missing credential files
when networking is enabled. Do not silently reactivate removed legacy `[node]`
or `[core].listen/public_url` configuration.

TLS with certificate/hostname verification and a random 256-bit shared token per
peer pair is the proposed implementation choice. The same pair token is installed
out of band on the two nodes; each node has its own private key. Tokens and keys
remain outside Git and are never printed in status, errors, or request logs.
Each configured peer certificate is explicitly trusted through `ca_file`.
Certificates must include the configured IP or hostname as a SAN. A provisioning
command generates certificates locally without requiring an external OpenSSL tool;
only public certificates and the pair token are exchanged, never private keys.
Credential creation must refuse to overwrite existing files unless explicitly forced.

Use an optional `network` dependency extra, with `aiohttp>=3.9,<4` for HTTP and
`cryptography>=42` for certificate provisioning. The resolver must select versions
compatible with Python 3.9; local-only installs must still work without this extra.
Do not implement a custom HTTP parser or disable TLS verification.

One network listener belongs to one Talkback/Herdr session. Two enabled sessions
must have different ports; binding failure must be visible and leave the conflicting
session untouched. Config changes require a service restart. Existing local-only
sessions remain unaffected unless networking is enabled in their configuration.

## Network protocol, version 1

All operations are ordinary bounded HTTPS requests; close the transport after the
response rather than holding an application connection. No WebSocket, long polling,
idle heartbeat, or idle endpoint-refresh loop.

Authenticate requests with `Authorization: Bearer <pair-token>` and
`X-Talkback-Node: <sender-node-id>`. Resolve the declared node to an explicitly
configured peer and compare tokens in constant time. Reject before processing
untrusted message data. The node ID in an envelope's source must match the
authenticated peer; the target must belong to this receiving node. Disallow relaying
and redirects to another host. Replies identify the receiving node and protocol
version so misconfigured destinations fail explicitly.

Operations:

- `GET /v1/info`: authenticated node identity, persistent receiver instance ID and
  protocol version. Use before a peer's first pending transfer and on reconnect,
  or for explicit diagnostics; never as an idle heartbeat. Persist the observed
  instance ID before the first POST so even a lost acceptance response cannot
  hide a later receiver storage reset.
- `POST /v1/messages`: immutable envelope containing `message_id`, qualified source
  and target addresses, body, conversation ID, optional reply-to, and bounded
  metadata. New durable acceptance returns 202; an identical retry returns 200 and
  current status. Conflicting identity/content returns 409. The receiver constructs
  the displayed header from validated qualified addresses and body, without
  reapplying the sender's append rules.
- `GET /v1/messages/{message_id}`: durable status and reason, authorized only for
  the originating peer. Results include node ID, receiver instance ID, protocol
  version and message ID.
  Unknown IDs return 404 without exposing other peers' messages.

The request-body limit is 128 KiB; message body UTF-8 size is at most 64 KiB.
Reject malformed JSON, non-string IDs/addresses, invalid states, excessive metadata,
and unsupported protocol versions. Requests have a 3-second connection timeout and
10-second total timeout; slow/incomplete bodies are bounded too. Limit concurrent
outbound requests to four per peer and inbound handlers to sixteen globally.
Enforce a limit of 10,000 nonterminal inbound peer messages per configured peer;
return 429 with Retry-After rather than accepting unbounded queued work.

An unavailable local role is not data loss: accept valid authorized messages into
`waiting_endpoint` and wake their lane when the role appears. A disappeared role
after acceptance follows the existing local dispatcher rules.

## Persistence, retries and receipts

Reuse the core message table, endpoint registry, dispatcher and local Herdr adapter.
Add a peer adapter for explicitly qualified configured destinations. Sending to a
configured peer must not require an online role directory: the named destination
can be queued while its host or role is unavailable. Local targets retain existing
resolution behavior. `send --direct` remains local-only and must reject a remote
target rather than bypassing the durable peer path.

Add durable peer progress and inbound receipt records. Inbound deduplication is
scoped by authenticated origin and message ID; it stores an immutable envelope hash
and authoritative terminal result. Existing globally keyed local message IDs must
never be overwritten by a cross-peer collision: reject a conflicting collision.
Generate new message IDs with 128 bits of randomness while continuing to read
existing IDs. Receiver receipt records survive message archival; do not automatically
expire deduplication tombstones in version 1.

Receipt updates and terminal local delivery transitions must commit atomically.
Admission-limit checks, deduplication and enqueue must also use one transaction.
The source records remote acceptance separately from its local adapter-accepted
state. It only marks `delivered` after an authoritative matching remote receipt;
remote acceptance alone never implies Pane delivery.

The peer adapter owns the endpoint lane until remote terminal status, deferring
later messages for that endpoint while other lanes run. It persists remote state,
attempt count, latest error and next scheduled request time. Retries and status
queries use delays 1, 2, 5, 10, 30, then 60 seconds, multiplied by a random factor
between 0.8 and 1.0 to desynchronize peers while retaining the 60-second cap, and a
Retry-After floor for throttling. A successful network response resets transport
failure backoff; pending remote delivery queries have their own capped schedule.
Do not impose a finite retry count or silently discard offline messages.

Connection failures, timeouts, 408, 429 and 5xx remain pending. Authentication,
identity/version mismatch, invalid payload and conflicting identity are visible
failures requiring correction/manual retry. If a receiver no longer knows a
previously accepted ID, resubmit the identical envelope; a lost pre-acceptance
response follows the same idempotent path. If remote storage was deliberately
reset, deduplication guarantees no longer apply and diagnostics must say so;
persist a receiver instance ID to detect that reset and stop automatic replay.

Mark only the peer adapter as recoverable after restart: its accepted messages
can query/resubmit the same identity safely. Preserve the existing conservative
recovery of accepted Herdr deliveries as `delivery_unknown`. Terminal unknown or
failed remote receipts propagate without automatic resubmission. A deliberate
manual retry is explicitly a new delivery with a new ID.

## CLI and observability

- Keep `send '[source->openclaw/target] body'` and support reverse qualified routing.
- Add `message status <message_id>` showing local status, peer, remote acceptance,
  final receipt, latest error and next retry; include JSON output for scripts.
- Add `peers list` and explicit `peers check <node-id>` for configuration/identity
  diagnostics without requiring continuous peer connections.
- Add the TLS/token provisioning command and document secure credential exchange.
- Network lifecycle and send/receive/duplicate/retry/final-receipt events go into
  the existing event pipeline with IDs and reasons, not credentials.
- Plugin `check` validates enabled network configuration and local TLS material.
  Offline peers do not prevent local service startup. A missing optional dependency
  or an unusable configured bind/certificate fails enabled startup clearly.

## Firewall operations

Only allow inbound TCP 18765 from the configured counterpart address. Windows is
currently non-administrator. The remote account's passwordless sudo allowlist does
not include `firewall-cmd`. Do not bypass those boundaries, alter unrelated rules,
disable a firewall, or restart the Herdr server to obtain access.

After implementation and listener validation, supply an administrator PowerShell
rule for source `192.168.90.18`, and a Linux firewalld rich rule for source
`192.168.60.55` only if firewalld is already active, verifying the active zone first.
Inspection currently shows that Windows firewall profiles are enabled, but Linux
firewalld is inactive. Do not enable Linux firewalld as part of this change; other
host/network filtering is still possible and requires an actual connectivity test.
The user can execute needed rules with the necessary rights. If suitable
administrative access becomes available, apply only these scoped rules and verify
the result. No firewall changes have been made yet.

## Acceptance and validation

1. Existing local tests pass without network dependencies or enabled network config.
2. Loopback HTTPS tests run on Windows, Linux and macOS; Python 3.9 and 3.13 remain
   supported through the CI matrix, including certificate provisioning.
3. Two actual service instances send in both directions; each receiver inserts and
   dispatches once and each sender reaches the matching final receipt.
4. Lost POST acknowledgement, duplicate and simultaneous POSTs, concurrent capacity
   admission, changed payload under an ID, and cross-peer ID reuse are covered.
5. Disconnect before acceptance, after acceptance, and during receipt query survives
   source/receiver restarts without another Pane submission.
6. Missing target role waits durably; a later pane rename wakes it. Other destination
   lanes and local delivery remain available.
7. Receiver crash during Pane delivery yields unknown, not automatic duplicate input.
8. Archival does not lose deduplication or terminal receipts; receiver storage-reset
   identity stops unsafe automatic replay.
9. Unauthorized peers, forged source/target nodes, wrong certificates/tokens, bad SAN,
   cross-peer status queries, oversized/slow bodies, redirect, saturation and occupied
   listener ports fail with bounded resources and redacted diagnostics.
10. Idle peers make no outbound requests. Offline retry timing is bounded and survives
    restart. Status requests stop at terminal state.
11. Deploy to Windows `192.168.60.55` and Linux `192.168.90.18` after prerequisites and
    scoped firewall access are ready. Use distinct test destinations, send unique
    harmless test markers, verify receiver evidence and sender receipt, and leave
    user agent work undisturbed.

## Scope of the next step

This document is the proposed complete behavior and deployment contract, not an
implementation. After review/approval, write a task-sized implementation plan, then
implement and test the protocol, durable state, adapter and CLI before real deployment.
