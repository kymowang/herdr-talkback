# Peer Messaging Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to execute this approved-design continuation task by task. Steps use checkbox syntax for tracking.

**Goal:** Bidirectional durable peer messaging over authenticated short HTTPS requests.
**Architecture:** An optional HTTPS receiver feeds the existing SQLite queue and Herdr adapter. A recoverable peer adapter submits/query-retries the same envelope and propagates authoritative receipts. Idle nodes initiate no requests.
**Tech Stack:** Python 3.9+, SQLite, asyncio, optional aiohttp and cryptography.
**Spec:** `docs/superpowers/specs/2026-10-23-peer-messaging-design.md`.

## Global Constraints

- Default networking disabled; TCP 18765 configurable; preserve local-only installs.
- Verified HTTPS, 256-bit pair tokens, authenticated source/target validation.
- 128 KiB requests, 64 KiB UTF-8 message bodies, 10,000 pending inbound messages per peer.
- Four outbound requests per peer; sixteen concurrent inbound handlers; 3s connect/10s total deadlines.
- Persistent identity, receipts, deduplication and scheduled retry; never automatically replay uncertain Pane input.
- No firewall disabling, no SSH transport, and no long network connections.

## Review Focus

- Lost acknowledgement followed by receiver reset: pin receiver instance before POST.
- Archival and concurrent duplicates: durable receipts and atomic admission transactions.
- Configured but absent role: persist, wait and wake through existing lifecycle events.
- Cross-peer spoofing and ID reuse: enforce identity, hash and status ownership.
- Cancellation/restart: recover peer acceptance without replaying accepted Herdr input.

## Task 1: Configuration and provisioning

Files: `network_config.py`, `certificates.py`, `config.py`, `pyproject.toml`; tests `test_network_config.py`.
Interfaces: immutable `NetworkConfig`, `PeerConfig`; `load_network(raw, config_path, node_id)`; `provision(directory, hosts)`.
- [ ] Write and run failing tests for opt-in defaults, paths, malformed URLs/ports/IDs, TLS and no-overwrite provisioning.
- [ ] Implement config parsing and optional dependency extra; generate SAN certificates and pair tokens without external tools.
- [ ] Verify config and certificate tests plus existing config tests.

## Task 2: Durable peer storage

Files: `peer_state.py`, `state.py`; tests `test_peer_state.py`.
Interfaces: `PeerStore(state)`; transactional `receive(peer, envelope)`, `receipt(peer,id)`, `instance_id`, progress and pinned-peer methods.
- [ ] Write failing admission, deduplication, hash-conflict, capacity, trigger/archival and reset-identity tests.
- [ ] Implement separate peer tables and transactional insertion/receipt triggers using the existing database.
- [ ] Verify concurrent receives, receipt ownership, archival and existing state behavior.

## Task 3: HTTPS protocol

Files: `peer_http.py`; tests `test_peer_http.py`.
Interfaces: `PeerServer(config, store, dispatcher)`, `PeerClient(local_node, peer).request(method,path,envelope=None)`.
- [ ] Write failing verified-TLS, mutual request/receipt, auth, spoofing, body/handler limits and bind-conflict tests.
- [ ] Implement bounded authenticated aiohttp endpoints and short-request client; reject redirects and unexpected identity/version.
- [ ] Run real loopback HTTPS tests, including timeout and duplicate transfer cases.

## Task 4: Routing and retry

Files: `adapters/peer.py`, `daemon.py`, `service.py`, `core_dispatcher.py`, `messages.py`; tests `test_peer_delivery.py`.
Interfaces: `PeerAdapter.ensure_endpoint(address)`, `deliver(message,lease)`; add optional peer adapter to daemon routing.
- [ ] Write failing offline/reconnect, lost ACK, receiver reset, sender restart, reverse routing and absent-role tests.
- [ ] Implement recoverable peer adapter, durable retry/query progress, safe identity pinning and per-destination ordering.
- [ ] Assemble listener/adapter with local service; local-only direct mode rejects remote addresses.
- [ ] Verify full suite and fault/restart tests.

## Task 5: CLI and documentation

Files: `cli.py`, `README.md`, `examples/config.toml`, CI; tests `test_peer_cli.py`.
- [ ] Write failing provisioning, peers diagnostics and message-status command tests.
- [ ] Implement `peer init-tls`, `peers list/check`, `message status` and config examples, including JSON outputs and credential handling.
- [ ] Verify network-disabled startup, enabled dry-run validation, package build and all tests.

## Task 6: Deployment and final review

- [ ] Fresh review of complete change; resolve correctness findings with regression tests.
- [ ] Install optional dependencies and provision two distinct certificates; exchange public certs and pair token securely.
- [ ] Configure port 18765 on both machines without overwriting unrelated rules/configuration.
- [ ] Verify mutual reachability and harmless two-way messages through real Herdr services, plus sender receipts.
- [ ] Record deployment limitations and final test evidence; preserve active user work.
