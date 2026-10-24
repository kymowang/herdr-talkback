# Logical Pane Monitoring

## Goal and agreed behavior

Monitor explicitly selected local Herdr panes using a dedicated AI monitor pane.
Keep Herdr's observed status (`herdr_status`) separate from Talkback's inferred
status (`logic_status`). Automatically submit `continue` when the AI is waiting
to proceed, including optional next steps offered after completing a task.
User decisions, missing information and explicit authorization requests remain
`blocked`. Do not modify Herdr's reported status.

## Configuration and operation

Add an opt-in `[logic_monitor]` section, disabled by default, with a monitor pane
selector and an explicit list of target selectors. Selectors accept a pane ID or
an unambiguous pane name. Reject duplicate names and exclude the monitor itself.
Resolve selectors each cycle and bind pending requests to concrete pane IDs;
never apply an old result to a replacement pane. A dedicated AI agent must already
be running in the monitor pane; Talkback does not choose or start its model.

Proposed defaults: observation interval 10 seconds, content stability 10 seconds,
monitor response polling 10 seconds, response timeout 120 seconds, retry cooldown
120 seconds, at most three automatic continuations per target per 30 minutes.
These limits are configurable. Only local panes are monitored in this iteration.

## Persistent records and state transitions

Store per-pane observations in SQLite: both statuses, normalized content and hash,
stable-since timestamp, request ID, request snapshot hash, monitor pane ID, result,
deadline and continuation attempt/outcome. Preserve an audit trail.

Read only the currently visible screen (`source=visible`, no history line count;
up to 64 KiB), stripping ANSI and normalizing line endings. All automatic monitor
and input checks use visible snapshots without scrolling or collecting history.
Ignore trailing blank lines; preserve meaningful text.
This is a bounded observation, not the pane's complete conversation history.

1. `working` or `blocked` takes precedence: copy the observed status into
   `logic_status`, update the snapshot, and invalidate any outstanding judgment.
2. A first non-working/non-blocked observation enters `todo` and saves the snapshot.
3. Changed content enters `init`, replaces the snapshot and resets stability.
   On the next unchanged observation, enter `todo`; classify only after stability.
4. Leaving `working/blocked` starts a new `todo` observation window.
5. Once stable, enqueue a judgment. The single monitor processes targets serially
   with a fair queue; other targets continue being observed while it is busy.
6. A valid result sets `logic_status` to `blocked`, `continue`, `waiting`, `done`
   or `unknown`. A 120-second unanswered request becomes `timeout`.
7. `waiting`, `unknown` and `timeout` may be reconsidered after cooldown, provided
   the target is still eligible. Unchanged `blocked` and `done` remain recorded
   until content or observed status changes. Each snapshot gets at most one
   automatic continuation attempt, including across service restarts.

## Judgment protocol

Send the monitor a request containing a unique request ID, concrete target pane
ID, snapshot hash and the captured transcript as quoted data. The monitor judges
that exact snapshot rather than independently reading a possibly changed pane.
Transcript instructions are data and must not override the classification task.

Classification guidance:

- `blocked`: requires a user decision, information or explicit authorization.
- `continue`: waits for permission to proceed with a next step, including an
  offered optional next task, or a transient environmental failure worth retrying.
- `waiting`: awaits a subagent or another pane's response.
- `done`: complete, with no proposed next action.
- `unknown`: insufficient evidence; permanent access/configuration failures are
  not automatically assumed recoverable.

Require a single-line structured response containing the exact request ID and
one enum value. The prompt must not contain a complete valid response for its
own ID. Accept only fresh output after submission, with an exact schema match;
ignore echoed input, quoted examples, earlier responses and mismatched IDs.
Conflicting results are invalid. Late answers to expired requests are ignored.
If the monitor is busy or has occupied/unknown input, wait without overwriting it;
the response deadline starts only when the request is submitted. If submission
is uncertain, mark the request unknown and do not blindly resubmit it.

## Executing continue safely

Before sending, re-read the target's identity, Herdr status and content. Discard a
stale result when the pane changed, resumed work or became blocked. Require an
empty input composer and use the existing input-aware delivery primitives to
submit plain `continue` plus Enter. Do not clear user input or dismiss overlays
for this feature. Recheck all conditions when an occupied input becomes empty.

Persist the attempt before writing to the terminal. After success, await new
output/status; an unchanged page cannot trigger another attempt. A crash or
uncertain submission does not cause automatic replay. Record and expose that
uncertainty. Existing automatic recovery rules and logical continuation share
per-pane exclusion and continuation accounting so neither races the other;
recovery invalidates an outstanding logical judgment.

## Integration and validation

Run observation and judgment tasks alongside the existing service, independent
of Herdr event availability. Persist records in separate tables without changing
existing message receipts. Add a read-only `monitor status [--json]` command;
update configuration examples and README. Preserve Python 3.9+, Windows, Linux
and macOS compatibility. Do not enable on real user panes before configuration.

Tests cover transitions, absent/renamed panes, content changes during judgment,
monitor contention, echoed prompts, stale/late/conflicting replies, timeout,
optional next-step classification instructions, occupied input, restart safety,
single-attempt semantics, limits and interaction with existing recovery rules.
Use fake clients and deterministic clocks; run the full existing suite. A live
test should use dedicated temporary target and monitor panes only.
