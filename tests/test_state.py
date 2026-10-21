import json
import sqlite3

import pytest

from herdr_talkback.core_types import CoreMessage, CoreMessageState
from herdr_talkback.state import TalkbackState
from herdr_talkback.types import MessageState


def core_message(message_id="msg_core_1", target="linux-main/dev-back", state=CoreMessageState.QUEUED):
    return CoreMessage(
        message_id=message_id,
        source_endpoint="linux-main/lead",
        target_endpoint=target,
        body="ping",
        raw_text="[lead->dev-back] ping",
        state=state,
        conversation_id=message_id,
        reply_to="parent" if message_id == "reply" else None,
        metadata={"priority": 1},
    )


def test_message_lifecycle(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()

    state.create_message("msg_1", "lead", "dev-back", "p-back", "[lead→dev-back] ping")
    state.update_message_state("msg_1", MessageState.SENDING, "send started")
    state.record_attempt("msg_1", 1, "sending", "verifying", "pane.send_input ok")

    message = state.get_message("msg_1")
    attempts = state.list_attempts("msg_1")

    assert message["state"] == "sending"
    assert message["reason"] == "send started"
    assert attempts[0]["next_state"] == "verifying"


def test_list_active_messages_excludes_completed(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()

    state.create_message("msg_1", "lead", "dev-back", "p-back", "[lead→dev-back] retry")
    state.update_message_state("msg_1", MessageState.RETRYING, "target input occupied")
    state.record_attempt("msg_1", 1, "checking_input", "retrying", "target input occupied")
    state.create_message("msg_2", "lead", "runner", "p-runner", "[lead→runner] done")
    state.update_message_state("msg_2", MessageState.DELIVERED, "input cleared")
    state.create_message("msg_3", "lead", "ui", "p-ui", "[lead→ui] failed")
    state.update_message_state("msg_3", MessageState.FAILED, "max attempts reached")
    state.create_message("msg_4", "lead", "assistant", "p-assistant", "[lead→assistant] blocked")
    state.update_message_state("msg_4", MessageState.BLOCKED_BY_INPUT, "target input occupied")

    active = state.list_active_messages()

    assert [message["message_id"] for message in active] == ["msg_1"]
    assert active[0]["state"] == "retrying"
    assert active[0]["attempts"] == 1


def test_archive_moves_only_old_terminal_messages_and_keeps_active_messages(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    state.create_message("old-delivered", "lead", "runner", "p1", "old delivered")
    state.update_message_state("old-delivered", MessageState.DELIVERED, "done")
    state.create_message("old-unknown", "lead", "ui", "p2", "old unknown")
    state.update_message_state("old-unknown", CoreMessageState.DELIVERY_UNKNOWN, "ambiguous")
    state.create_message("old-queued", "lead", "dev-back", "p3", "old queued")
    state.create_message("recent-failed", "lead", "assistant", "p4", "recent failed")
    state.update_message_state("recent-failed", MessageState.FAILED, "failed")
    with sqlite3.connect(state.path) as db:
        db.execute(
            "update messages set updated_at = 100 where message_id in (?, ?, ?)",
            ("old-delivered", "old-unknown", "old-queued"),
        )
        db.execute("update messages set updated_at = 101 where message_id = ?", ("recent-failed",))

    result = state.archive_messages_older_than(100, archived_at=200)

    assert result == {"archived_count": 2, "skipped_active_count": 1}
    assert [message["message_id"] for message in state.list_messages()] == [
        "recent-failed",
        "old-queued",
    ]
    with sqlite3.connect(state.path) as db:
        archived = db.execute(
            "select message_id, message_json, archived_at from message_archive order by message_id"
        ).fetchall()
    assert [(row[0], row[2]) for row in archived] == [
        ("old-delivered", 200),
        ("old-unknown", 200),
    ]
    assert json.loads(archived[0][1])["raw_text"] == "old delivered"


def test_archive_is_idempotent_at_the_48_hour_cutoff_and_rolls_back_on_insert_failure(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    state.create_message("boundary", "lead", "runner", "p1", "boundary")
    state.update_message_state("boundary", MessageState.FAILED, "failed")
    with sqlite3.connect(state.path) as db:
        db.execute("update messages set updated_at = 100 where message_id = ?", ("boundary",))

    assert state.archive_messages_older_than(100, archived_at=200) == {
        "archived_count": 1,
        "skipped_active_count": 0,
    }
    assert state.archive_messages_older_than(100, archived_at=201) == {
        "archived_count": 0,
        "skipped_active_count": 0,
    }

    state.create_message("rollback", "lead", "runner", "p2", "rollback")
    state.update_message_state("rollback", MessageState.DELIVERED, "done")
    with sqlite3.connect(state.path) as db:
        db.execute("update messages set updated_at = 100 where message_id = ?", ("rollback",))
        db.execute(
            """
            create trigger reject_archive_insert before insert on message_archive
            when new.message_id = 'rollback'
            begin
                select raise(abort, 'archive rejected');
            end
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="archive rejected"):
        state.archive_messages_older_than(100, archived_at=202)
    assert state.get_message("rollback")["message_id"] == "rollback"


def test_recovery_cooldown(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()

    assert state.can_recover("p1", now=1000, cooldown_seconds=120, max_per_window=3, window_seconds=1800)
    state.record_recovery("runner", "p1", "blocked", "api error", "continue", now=1000)

    assert not state.can_recover("p1", now=1050, cooldown_seconds=120, max_per_window=3, window_seconds=1800)
    assert state.can_recover("p1", now=1121, cooldown_seconds=120, max_per_window=3, window_seconds=1800)


def test_init_migrates_legacy_messages_without_losing_terminal_rows(tmp_path):
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            create table messages (
                message_id text primary key,
                source text not null,
                target text not null,
                target_pane_id text not null,
                raw_text text not null,
                state text not null,
                reason text not null default '',
                attempts integer not null default 0,
                created_at real not null,
                updated_at real not null
            );
            insert into messages values
                ('unfinished', 'lead', 'dev-back', 'p1', '[lead->dev-back] ping', 'sending', '', 2, 10, 11),
                ('verifying', 'lead', 'dev-back', 'p1', '[lead->dev-back] verify', 'verifying', '', 0, 11, 12),
                ('delivered', 'lead', 'runner', 'p2', '[lead->runner] done', 'delivered', '', 1, 12, 13),
                ('failed', 'lead', 'ui', 'p3', '[lead->ui] fail', 'failed', '', 3, 14, 15);
            """
        )

    state = TalkbackState(path)
    state.init()
    state.init()

    unfinished = state.get_message("unfinished")
    assert unfinished["source_endpoint"] == "linux-main/lead"
    assert unfinished["target_endpoint"] == "linux-main/dev-back"
    assert unfinished["body"] == ""
    assert unfinished["conversation_id"] == "unfinished"
    assert unfinished["state"] == "delivery_unknown"
    assert unfinished["attempts"] == 2
    assert state.get_message("verifying")["state"] == "delivery_unknown"
    assert state.get_message("delivered")["state"] == "delivered"
    assert state.get_message("failed")["state"] == "failed"
    with sqlite3.connect(path) as db:
        assert db.execute("select value from schema_meta where key = 'version'").fetchone()[0] == "5"
        assert db.execute("select count(*) from messages").fetchone()[0] == 4
    assert unfinished["accepted_by_adapter"] is None


def test_init_classifies_legacy_retries_from_real_delivery_evidence(tmp_path):
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            create table messages (
                message_id text primary key,
                source text not null,
                target text not null,
                target_pane_id text not null,
                raw_text text not null,
                state text not null,
                reason text not null default '',
                attempts integer not null default 0,
                created_at real not null,
                updated_at real not null
            );
            create table delivery_attempts (
                id integer primary key autoincrement,
                message_id text not null,
                attempt integer not null,
                previous_state text not null,
                next_state text not null,
                reason text not null,
                created_at real not null
            );
            insert into messages values
                ('checking', 'lead', 'runner', 'p1', 'checking', 'checking_input', '', 1, 1, 1),
                ('blocked', 'lead', 'runner', 'p1', 'blocked', 'blocked_by_input', '', 1, 2, 2),
                ('checking-after-submit', 'lead', 'runner', 'p1', 'checking again', 'checking_input', 'checking target input', 2, 2.1, 2.1),
                ('blocked-after-submit', 'lead', 'runner', 'p1', 'blocked after submit', 'blocked_by_input', 'target input occupied', 2, 2.2, 2.2),
                ('safe-retry', 'lead', 'runner', 'p1', 'safe', 'retrying', 'target input occupied', 2, 3, 3),
                ('queued', 'lead', 'runner', 'p1', 'queued', 'queued', '', 1, 3.5, 3.5),
                ('verifying', 'lead', 'runner', 'p1', 'verify', 'verifying', '', 1, 4, 4),
                ('sent-retry', 'lead', 'runner', 'p1', 'sent', 'retrying', 'retry scheduled', 2, 5, 5),
                ('ambiguous-retry', 'lead', 'runner', 'p1', 'ambiguous', 'retrying', '', 2, 6, 6),
                ('residue-retry', 'lead', 'runner', 'p1', 'residue', 'retrying', '', 2, 7, 7),
                ('delivered', 'lead', 'runner', 'p1', 'done', 'delivered', '', 1, 8, 8),
                ('failed', 'lead', 'runner', 'p1', 'failed', 'failed', '', 1, 9, 9),
                ('unknown', 'lead', 'runner', 'p1', 'unknown', 'delivery_unknown', '', 1, 10, 10);
            insert into delivery_attempts(message_id, attempt, previous_state, next_state, reason, created_at) values
                ('checking', 1, 'queued', 'checking_input', 'target input occupied', 1),
                ('blocked', 1, 'checking_input', 'blocked_by_input', 'target input occupied', 2),
                ('checking-after-submit', 1, 'sending', 'verifying', 'pane.send_input sent', 2.1),
                ('checking-after-submit', 2, 'verifying', 'checking_input', 'checking target input', 2.11),
                ('blocked-after-submit', 1, 'checking_input', 'blocked_by_input', 'pane.send_input residue remains', 2.2),
                ('safe-retry', 2, 'checking_input', 'retrying', 'target input occupied', 3),
                ('queued', 1, 'sending', 'verifying', 'stale pane.send_input evidence', 3.5),
                ('verifying', 1, 'sending', 'verifying', 'pane.send_input sent', 4),
                ('sent-retry', 2, 'checking_input', 'retrying', 'pane.send_input sent before timeout', 5),
                ('ambiguous-retry', 2, 'verifying', 'retrying', 'delivery ambiguous after submit', 6),
                ('residue-retry', 2, 'verifying', 'retrying', 'residue not cleared', 7),
                ('delivered', 1, 'sending', 'verifying', 'pane.send_input sent', 8),
                ('failed', 1, 'sending', 'verifying', 'delivery ambiguous', 9),
                ('unknown', 1, 'sending', 'verifying', 'residue remains', 10);
            """
        )

    state = TalkbackState(path)
    state.init()

    assert state.get_message("checking")["state"] == "queued"
    assert state.get_message("blocked")["state"] == "queued"
    assert state.get_message("safe-retry")["state"] == "queued"
    assert state.get_message("queued")["state"] == "queued"
    for message_id in (
        "checking-after-submit",
        "blocked-after-submit",
        "verifying",
        "sent-retry",
        "ambiguous-retry",
        "residue-retry",
    ):
        assert state.get_message(message_id)["state"] == "delivery_unknown"
    assert state.get_message("delivered")["state"] == "delivered"
    assert state.get_message("failed")["state"] == "failed"
    assert state.get_message("unknown")["state"] == "delivery_unknown"
    with sqlite3.connect(path) as db:
        index = db.execute(
            "select name from sqlite_master where type = 'index' and name = ?",
            ("idx_delivery_attempts_message_id",),
        ).fetchone()
    assert index == ("idx_delivery_attempts_message_id",)


def test_v2_upgrade_uses_attempt_evidence_after_legacy_state_was_flattened(tmp_path):
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            create table messages (
                message_id text primary key,
                source text not null,
                target text not null,
                target_pane_id text not null,
                raw_text text not null,
                state text not null,
                reason text not null default '',
                attempts integer not null default 0,
                created_at real not null,
                updated_at real not null
            );
            create table delivery_attempts (
                id integer primary key autoincrement,
                message_id text not null,
                attempt integer not null,
                previous_state text not null,
                next_state text not null,
                reason text not null,
                created_at real not null
            );
            create table schema_meta (key text primary key, value text not null);
            insert into schema_meta values ('version', '2');
            insert into messages values
                ('flattened', 'lead', 'runner', 'p1', 'sent', 'queued', '', 1, 1, 1),
                ('flattened-no-detail', 'lead', 'runner', 'p1', 'unknown', 'queued', '', 2, 2, 2),
                ('flattened-sending-reason', 'lead', 'runner', 'p1', 'sending', 'queued', 'sending message', 0, 2.5, 2.5),
                ('flattened-safe', 'lead', 'runner', 'p1', 'blocked', 'queued', 'target input occupied', 2, 3, 3);
            insert into delivery_attempts(message_id, attempt, previous_state, next_state, reason, created_at) values
                ('flattened', 1, 'sending', 'verifying', 'pane.send_input sent', 1),
                ('flattened-safe', 2, 'checking_input', 'retrying', 'target input occupied', 3);
            """
        )

    state = TalkbackState(path)
    state.init()

    assert state.get_message("flattened")["state"] == "delivery_unknown"
    assert state.get_message("flattened-no-detail")["state"] == "delivery_unknown"
    assert state.get_message("flattened-sending-reason")["state"] == "delivery_unknown"
    assert state.get_message("flattened-safe")["state"] == "queued"
    with sqlite3.connect(path) as db:
        assert db.execute("select value from schema_meta where key = 'version'").fetchone()[0] == "5"


def test_v2_migration_loads_many_attempt_histories_in_one_set_query(tmp_path):
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            create table messages (
                message_id text primary key,
                source text not null,
                target text not null,
                target_pane_id text not null,
                raw_text text not null,
                state text not null,
                reason text not null default '',
                attempts integer not null default 0,
                created_at real not null,
                updated_at real not null
            );
            create table delivery_attempts (
                id integer primary key autoincrement,
                message_id text not null,
                attempt integer not null,
                previous_state text not null,
                next_state text not null,
                reason text not null,
                created_at real not null
            );
            create table schema_meta (key text primary key, value text not null);
            insert into schema_meta values ('version', '2');
            """
        )
        message_rows = []
        attempt_rows = []
        expected = {}
        for sequence in range(60):
            message_id = "message-{0:03d}".format((sequence * 37) % 60)
            post_submit = sequence % 2 == 0
            message_rows.append(
                (message_id, "lead", "runner", "p1", message_id, "queued", "", 1, sequence, sequence)
            )
            attempt_rows.append(
                (
                    message_id,
                    1,
                    "sending" if post_submit else "checking_input",
                    "verifying" if post_submit else "retrying",
                    "pane.send_input sent" if post_submit else "target input occupied",
                    sequence,
                )
            )
            expected[message_id] = "delivery_unknown" if post_submit else "queued"
        db.executemany(
            "insert into messages values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            message_rows,
        )
        db.executemany(
            """
            insert into delivery_attempts(
                message_id, attempt, previous_state, next_state, reason, created_at
            ) values (?, ?, ?, ?, ?, ?)
            """,
            reversed(attempt_rows),
        )

    evidence_queries = []

    class TracedTalkbackState(TalkbackState):
        def _connect(self):
            db = super()._connect()

            def trace(query):
                normalized = " ".join(query.lower().split())
                if normalized.startswith("select") and "delivery_attempts" in normalized:
                    evidence_queries.append(normalized)

            db.set_trace_callback(trace)
            return db

    TracedTalkbackState(path).init()

    with sqlite3.connect(path) as db:
        actual = dict(db.execute("select message_id, state from messages"))
    assert actual == expected
    assert len(evidence_queries) == 1


def test_init_uses_configured_local_node_for_legacy_backfill(tmp_path):
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            create table messages (
                message_id text primary key,
                source text not null,
                target text not null,
                target_pane_id text not null,
                raw_text text not null,
                state text not null,
                reason text not null default '',
                attempts integer not null default 0,
                created_at real not null,
                updated_at real not null
            );
            insert into messages values
                ('legacy', 'lead', 'runner', 'p1', '[lead->runner] ping', 'queued', '', 0, 10, 11);
            """
        )

    state = TalkbackState(path, local_node_id="workstation")
    state.init()

    row = state.get_message("legacy")
    assert row["source_endpoint"] == "workstation/lead"
    assert row["target_endpoint"] == "workstation/runner"


@pytest.mark.parametrize("node_id", ["", " ", "remote/node", "remote node", "-remote"])
def test_state_rejects_malformed_local_node_before_touching_database(tmp_path, node_id):
    path = tmp_path / "missing" / "state.db"

    with pytest.raises(ValueError, match="invalid node ID"):
        TalkbackState(path, local_node_id=node_id)

    assert not path.parent.exists()


def test_enqueue_is_durable_and_idempotent(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()

    assert state.enqueue_message(core_message("reply"), target_pane_id="pane-1") is True
    assert state.enqueue_message(core_message("reply"), target_pane_id="pane-2") is False

    row = state.get_message("reply")
    assert row["state"] == "queued"
    assert row["target_pane_id"] == "pane-1"
    assert row["body"] == "ping"
    assert row["metadata_json"] == '{"priority": 1}'
    assert row["reply_to"] == "parent"


def test_claim_next_is_fifo_and_scoped_to_endpoint(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    state.enqueue_message(core_message("first"))
    state.enqueue_message(core_message("second"))
    state.enqueue_message(core_message("other", "linux-main/runner"))

    claimed = state.claim_next("linux-main/dev-back", "lease-1", now=1000)

    assert claimed["message_id"] == "first"
    assert claimed["state"] == "offered"
    assert claimed["lease_id"] == "lease-1"
    assert claimed["lease_expires_at"] == 1300
    assert state.claim_next("linux-main/dev-back", "lease-2", now=1001) is None
    assert state.claim_next("linux-main/runner", "lease-3", now=1001)["message_id"] == "other"


def test_claim_next_preserves_insertion_order_when_timestamps_match(tmp_path):
    path = tmp_path / "state.db"
    state = TalkbackState(path)
    state.init()
    state.enqueue_message(core_message("z-first"))
    state.enqueue_message(core_message("a-second"))
    with sqlite3.connect(path) as db:
        db.execute("update messages set created_at = 1000 where message_id in ('z-first', 'a-second')")

    claimed = state.claim_next("linux-main/dev-back", "lease-1", now=1000)

    assert claimed["message_id"] == "z-first"


def test_claim_next_includes_waiting_endpoint_and_blocks_accepted_message(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    state.enqueue_message(core_message("waiting", state=CoreMessageState.WAITING_ENDPOINT))

    claimed = state.claim_next("linux-main/dev-back", "lease-1", now=1000)
    assert claimed["message_id"] == "waiting"
    assert state.transition_core_message(
        "waiting",
        (CoreMessageState.OFFERED,),
        CoreMessageState.ACCEPTED,
        "accepted",
        lease_id="lease-1",
    )
    assert state.claim_next("linux-main/dev-back", "lease-2", now=1001) is None


def test_transition_requires_expected_state_and_matching_lease(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    state.enqueue_message(core_message())
    state.claim_next("linux-main/dev-back", "lease-1", now=1000)

    assert not state.transition_core_message(
        "msg_core_1",
        (CoreMessageState.ACCEPTED,),
        CoreMessageState.DELIVERED,
        "wrong state",
        lease_id="lease-1",
    )
    assert not state.transition_core_message(
        "msg_core_1",
        (CoreMessageState.OFFERED,),
        CoreMessageState.ACCEPTED,
        "wrong lease",
        lease_id="lease-2",
    )
    assert state.transition_core_message(
        "msg_core_1",
        (CoreMessageState.OFFERED,),
        CoreMessageState.ACCEPTED,
        "accepted",
        lease_id="lease-1",
    )
    assert state.get_message("msg_core_1")["reason"] == "accepted"


def test_expired_offer_returns_to_queue_but_accepted_is_retained(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    state.enqueue_message(core_message("offered"))
    state.enqueue_message(core_message("accepted", "linux-main/runner"))
    state.claim_next("linux-main/dev-back", "lease-1", now=1000)
    state.claim_next("linux-main/runner", "lease-2", now=1000)
    assert state.transition_core_message(
        "accepted",
        (CoreMessageState.OFFERED,),
        CoreMessageState.ACCEPTED,
        "accepted",
        lease_id="lease-2",
    )

    assert state.recover_expired_leases(now=1301) == 1
    assert state.get_message("offered")["state"] == "queued"
    accepted = state.get_message("accepted")
    assert accepted["state"] == "accepted"
    assert accepted["lease_id"] == "lease-2"


def test_reconcile_incomplete_messages_is_adapter_aware(tmp_path):
    path = tmp_path / "state.db"
    state = TalkbackState(path)
    state.init()
    state.enqueue_message(core_message("herdr-accepted"))
    state.enqueue_message(core_message("remote-accepted", "windows-dev/protocol-test"))
    state.claim_next("linux-main/dev-back", "herdr-lease", now=1000)
    state.claim_next("windows-dev/protocol-test", "remote-lease", now=1000)
    with sqlite3.connect(path) as db:
        db.execute(
            "update messages set state = 'accepted', accepted_by_adapter = 'herdr' "
            "where message_id = 'herdr-accepted'"
        )
        db.execute(
            "update messages set state = 'accepted', accepted_by_adapter = 'remote-node' "
            "where message_id = 'remote-accepted'"
        )

    counts = state.reconcile_incomplete_messages(
        recoverable_adapters=("remote-node",),
        reason="dispatcher startup found persisted accepted delivery",
    )

    assert counts == {"queued": 1, "delivery_unknown": 1}
    herdr = state.get_message("herdr-accepted")
    assert herdr["state"] == "delivery_unknown"
    assert herdr["reason"] == "dispatcher startup found persisted accepted delivery"
    remote = state.get_message("remote-accepted")
    assert remote["state"] == "queued"
    assert remote["reason"] == "dispatcher startup found persisted accepted delivery"
    assert remote["lease_id"] is None
    assert remote["lease_expires_at"] is None
    assert remote["accepted_by_adapter"] is None


def test_list_dispatchable_endpoints_is_distinct_and_ordered(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    state.enqueue_message(core_message("runner", "linux-main/runner"))
    state.enqueue_message(core_message("dev-one"))
    state.enqueue_message(core_message("dev-two"))
    state.enqueue_message(core_message("delivered", "linux-main/ui", CoreMessageState.DELIVERED))

    assert state.list_dispatchable_endpoints() == ["linux-main/dev-back", "linux-main/runner"]
