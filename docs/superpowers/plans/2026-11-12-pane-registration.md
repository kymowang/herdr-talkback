# Pane registration implementation plan

1. Extend operator directory with idempotent local and remote registrations, safe conflict handling, and bulk registration of addressable local panes. Preserve routing precedence and test conflicts and duplicates.
2. Add portable identity card generation and import with strict peer and address validation; do not export credentials. Test malformed, mismatched, conflicting and repeated cards.
3. Extend daemon submission and authenticated peer admission for structured whoami requests. Resolve the destination by configured node or host and port; persist registration atomically with the received message and expose conflicts without overwriting. Test authentication, duplicates, conflicts, and normal-message isolation.
4. Add CLI commands, documentation and skill examples. Test the actual entry points, then run the complete test suite.
