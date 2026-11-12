# Pane Registration and Whoami Design

## Goal

Register local and remote panes for Talkback routing. A whoami request delivered to a remote pane also registers the authenticated sender in the remote Talkback directory, so the remote pane can reply by name without a separate registration command.

## Commands

- `herdr-talkback pane register-all`: register currently named, addressable local panes; report unnamed panes, duplicate labels and conflicts individually. Repeating the command is safe.
- `herdr-talkback pane register NAME NODE/PANE`: retain manual registration of local and remote panes, including a local pane registered under its own name.
- `herdr-talkback whoami SOURCE HOST TARGET`: send a durable registration request from a named local pane to a remote pane. Resolve HOST to an already configured, authenticated peer; accept its node ID or uniquely matching configured host/IP, including the configured port. Reject unknown or ambiguous hosts. An optional port disambiguates peers.

## Whoami flow

The sender registers the requested remote target locally under its pane name if there is no conflict, then sends a structured whoami request through the existing durable peer message path. The receiver authenticates the peer and validates that the source endpoint belongs to that peer before registering it under the source pane name. The human-readable pane message uses `[monitor?lead][whoami]` and states the sender address and name. The message includes the source node address when it is reliably available from configuration; routing uses the receiver's configured peer URL and credentials rather than trusting text in the message.

The request and registration outcome must be visible in command output and message status. Duplicate requests for the same name and address are idempotent. If the name is already assigned elsewhere, conflicts with another local pane or alias, or cannot be resolved, do not overwrite it; record the conflict and show the fully qualified address. Receiving a normal message or an unauthenticated request must never register a pane. Enqueue and automatic registration must be coordinated so retries and restarts do not lose registration or falsely report success.

## Alternatives

- Send a free-form prompt and have the remote AI register manually: easy to add, but relies on AI behavior and does not guarantee automatic registration.
- Use an authenticated Talkback message with structured registration metadata (chosen): preserves the visible pane message and lets the receiving service register the verified sender independently of the AI.

Existing peer HTTPS configuration, trusted certificates, and credentials remain required on both machines. Knowing only an IP address does not establish peer trust or configure firewall access. Existing local routing and registered aliases must continue to work; matching local self-registrations are not treated as conflicts. Pane names that cannot form a valid endpoint are reported rather than guessed.

## Verification

Cover batch registration, manual local and remote registration, repeated and conflicting names, host/port resolution, authenticated whoami admission, duplicate delivery and crash recovery, spoofed ordinary messages, and delivery to the destination pane. Run the full pytest suite and update CLI and skill usage documentation. Test cross-platform paths and preserve current-session isolation.

## Manual forwarding

`pane identity PANE` prints a portable JSON registration card containing the node ID, pane name and, when provided or configured, the advertised HTTPS address and port. It never contains credentials. `pane import CARD` validates the card against an already configured peer and registers the pane; mismatching addresses and name conflicts are reported rather than silently accepted. This allows manual forwarding even when the remote pane cannot receive the whoami message.
