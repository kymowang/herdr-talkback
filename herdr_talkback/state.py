from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from .addressing import validate_node_id
from .core_types import CoreMessage, CoreMessageState
from .types import MessageState


CORE_SCHEMA_VERSION = "5"
LEASE_SECONDS = 300
POST_SUBMIT_ATTEMPT_STATES = {
    MessageState.SENDING.value,
    MessageState.VERIFYING.value,
    CoreMessageState.DELIVERY_UNKNOWN.value,
}
POST_SUBMIT_REASON_MARKERS = (
    "pane.send_input",
    "delivery ambiguous",
    "ambiguous",
    "residue",
    "submission state unknown",
)
DELIVERY_ATTEMPT_MESSAGE_INDEX = "idx_delivery_attempts_message_id"


class TalkbackState:
    def __init__(self, path: Path, local_node_id: str = "linux-main") -> None:
        self.path = Path(path)
        self.local_node_id = validate_node_id(local_node_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def init(self) -> None:
        with self._connect() as db:
            db.execute("begin immediate")
            try:
                self._create_tables(db)
                self._add_core_message_columns(db)
                version = db.execute("select value from schema_meta where key = 'version'").fetchone()
                if version is None or int(version["value"]) < int(CORE_SCHEMA_VERSION):
                    self._migrate_legacy_messages(
                        db,
                        recover_flattened_queued=(
                            version is not None and int(version["value"]) == 2
                        ),
                    )
                    db.execute(
                        """
                        insert into schema_meta(key, value) values ('version', ?)
                        on conflict(key) do update set value = excluded.value
                        """,
                        (CORE_SCHEMA_VERSION,),
                    )
                db.commit()
            except Exception:
                db.rollback()
                raise

    def _create_tables(self, db: sqlite3.Connection) -> None:
        db.execute(
            """
            create table if not exists messages (
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
            )
            """
        )
        db.execute(
            """
            create table if not exists delivery_attempts (
                id integer primary key autoincrement,
                message_id text not null,
                attempt integer not null,
                previous_state text not null,
                next_state text not null,
                reason text not null,
                created_at real not null
            )
            """
        )
        db.execute(
            "create index if not exists {0} on delivery_attempts(message_id)".format(
                DELIVERY_ATTEMPT_MESSAGE_INDEX
            )
        )
        db.execute(
            """
            create table if not exists recoveries (
                id integer primary key autoincrement,
                role text not null,
                pane_id text not null,
                previous_status text not null,
                pattern text not null,
                action text not null,
                created_at real not null
            )
            """
        )
        db.execute(
            """
            create table if not exists events (
                id integer primary key autoincrement,
                kind text not null,
                payload_json text not null,
                created_at real not null
            )
            """
        )
        db.execute(
            """
            create table if not exists alerts (
                id integer primary key autoincrement,
                kind text not null,
                title text not null,
                body text not null,
                payload_json text not null,
                created_at real not null,
                cleared_at real
            )
            """
        )
        db.execute("create table if not exists schema_meta (key text primary key, value text not null)")
        db.execute(
            """
            create table if not exists message_archive (
                message_id text primary key,
                message_json text not null,
                archived_at real not null
            )
            """
        )

    def _add_core_message_columns(self, db: sqlite3.Connection) -> None:
        columns = {row["name"] for row in db.execute("pragma table_info(messages)")}
        definitions = {
            "source_endpoint": "text not null default ''",
            "target_endpoint": "text not null default ''",
            "body": "text not null default ''",
            "metadata_json": "text not null default '{}'",
            "conversation_id": "text not null default ''",
            "reply_to": "text",
            "lease_id": "text",
            "lease_expires_at": "real",
            "accepted_by_adapter": "text",
        }
        for name, definition in definitions.items():
            if name not in columns:
                db.execute("alter table messages add column {0} {1}".format(name, definition))

    def _migrate_legacy_messages(
        self,
        db: sqlite3.Connection,
        recover_flattened_queued: bool,
    ) -> None:
        rows = db.execute(
            """
            select message_id, source, target, source_endpoint, target_endpoint,
                   conversation_id, state, reason, attempts
            from messages
            """
        ).fetchall()
        attempt_evidence = self._load_migration_attempt_evidence(db)
        for row in rows:
            db.execute(
                """
                update messages
                set source_endpoint = ?, target_endpoint = ?, conversation_id = ?, state = ?
                where message_id = ?
                """,
                (
                    row["source_endpoint"] or self.local_node_id + "/" + row["source"],
                    row["target_endpoint"] or self.local_node_id + "/" + row["target"],
                    row["conversation_id"] or row["message_id"],
                    self._migrated_message_state(
                        row,
                        recover_flattened_queued,
                        attempt_evidence,
                    ),
                    row["message_id"],
                ),
            )

    def _migrated_message_state(
        self,
        row: sqlite3.Row,
        recover_flattened_queued: bool,
        attempt_evidence: dict[str, bool],
    ) -> str:
        current = str(row["state"])
        if current in {
            CoreMessageState.DELIVERED.value,
            CoreMessageState.FAILED.value,
            CoreMessageState.DELIVERY_UNKNOWN.value,
        }:
            return current
        if current in {MessageState.SENDING.value, MessageState.VERIFYING.value}:
            return CoreMessageState.DELIVERY_UNKNOWN.value

        message_id = str(row["message_id"])
        message_reason = str(row["reason"])
        has_attempt_details = message_id in attempt_evidence
        has_post_submit_evidence = attempt_evidence.get(message_id, False) or any(
            marker in message_reason.lower() for marker in POST_SUBMIT_REASON_MARKERS
        )
        if current in {
            MessageState.CHECKING_INPUT.value,
            MessageState.BLOCKED_BY_INPUT.value,
            MessageState.RETRYING.value,
        }:
            return (
                CoreMessageState.DELIVERY_UNKNOWN.value
                if has_post_submit_evidence
                else CoreMessageState.QUEUED.value
            )

        # Schema v2 flattened all legacy nonterminal states to queued. Attempt
        # evidence is the only safe way to recover post-submit ambiguity here.
        if (
            recover_flattened_queued
            and current == CoreMessageState.QUEUED.value
        ):
            if has_post_submit_evidence:
                return CoreMessageState.DELIVERY_UNKNOWN.value
            no_detail_is_ambiguous = (
                int(row["attempts"]) > 0
                or message_reason.strip().lower() == "sending message"
            )
            if not has_attempt_details and no_detail_is_ambiguous:
                return CoreMessageState.DELIVERY_UNKNOWN.value

        core_states = {state.value for state in CoreMessageState}
        if current in core_states:
            return current
        return CoreMessageState.QUEUED.value

    @staticmethod
    def _load_migration_attempt_evidence(
        db: sqlite3.Connection,
    ) -> dict[str, bool]:
        state_placeholders = ", ".join("?" for _ in POST_SUBMIT_ATTEMPT_STATES)
        reason_checks = " or ".join(
            "instr(lower(attempt.reason), ?) > 0" for _ in POST_SUBMIT_REASON_MARKERS
        )
        terminal_states = (
            CoreMessageState.DELIVERED.value,
            CoreMessageState.FAILED.value,
            CoreMessageState.DELIVERY_UNKNOWN.value,
        )
        terminal_placeholders = ", ".join("?" for _ in terminal_states)
        rows = db.execute(
            """
            select attempt.message_id,
                   max(case
                       when attempt.previous_state in ({state_placeholders})
                         or attempt.next_state in ({state_placeholders})
                         or {reason_checks}
                       then 1 else 0
                   end) as has_post_submit
            from messages as message
            join delivery_attempts as attempt indexed by {index_name}
              on attempt.message_id = message.message_id
            where message.state not in ({terminal_placeholders})
            group by attempt.message_id
            """.format(
                state_placeholders=state_placeholders,
                reason_checks=reason_checks,
                index_name=DELIVERY_ATTEMPT_MESSAGE_INDEX,
                terminal_placeholders=terminal_placeholders,
            ),
            tuple(POST_SUBMIT_ATTEMPT_STATES)
            + tuple(POST_SUBMIT_ATTEMPT_STATES)
            + tuple(POST_SUBMIT_REASON_MARKERS)
            + terminal_states,
        ).fetchall()
        return {
            str(row["message_id"]): bool(row["has_post_submit"])
            for row in rows
        }

    def create_message(
        self,
        message_id: str,
        source: str,
        target: str,
        target_pane_id: str,
        raw_text: str,
    ) -> None:
        now = time.time()
        with self._connect() as db:
            db.execute(
                """
                insert or ignore into messages(
                    message_id, source, target, target_pane_id, raw_text, state, created_at, updated_at,
                    source_endpoint, target_endpoint, body, metadata_json, conversation_id
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '{}', ?)
                """,
                (
                    message_id,
                    source,
                    target,
                    target_pane_id,
                    raw_text,
                    MessageState.QUEUED.value,
                    now,
                    now,
                    self.local_node_id + "/" + source,
                    self.local_node_id + "/" + target,
                    message_id,
                ),
            )

    def enqueue_message(self, message: CoreMessage, target_pane_id: str = "") -> bool:
        now = time.time()
        with self._connect() as db:
            cursor = db.execute(
                """
                insert into messages(
                    message_id, source, target, target_pane_id, raw_text, state, reason, attempts,
                    created_at, updated_at, source_endpoint, target_endpoint, body, metadata_json,
                    conversation_id, reply_to
                ) values (?, ?, ?, ?, ?, ?, '', 0, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(message_id) do nothing
                """,
                (
                    message.message_id,
                    self._legacy_endpoint_name(message.source_endpoint),
                    self._legacy_endpoint_name(message.target_endpoint),
                    target_pane_id,
                    message.raw_text,
                    message.state.value,
                    now,
                    now,
                    message.source_endpoint,
                    message.target_endpoint,
                    message.body,
                    json.dumps(message.metadata, ensure_ascii=False),
                    message.conversation_id,
                    message.reply_to,
                ),
            )
        return cursor.rowcount == 1

    def claim_next(self, endpoint: str, lease_id: str, now: float) -> Optional[dict[str, Any]]:
        with self._connect() as db:
            db.execute("begin immediate")
            try:
                active = db.execute(
                    """
                    select 1 from messages
                    where target_endpoint = ? and state in (?, ?)
                    limit 1
                    """,
                    (endpoint, CoreMessageState.OFFERED.value, CoreMessageState.ACCEPTED.value),
                ).fetchone()
                if active is not None:
                    db.commit()
                    return None
                row = db.execute(
                    """
                    select message_id from messages
                    where target_endpoint = ? and state in (?, ?)
                    order by created_at, rowid
                    limit 1
                    """,
                    (endpoint, CoreMessageState.QUEUED.value, CoreMessageState.WAITING_ENDPOINT.value),
                ).fetchone()
                if row is None:
                    db.commit()
                    return None
                cursor = db.execute(
                    """
                    update messages
                    set state = ?, lease_id = ?, lease_expires_at = ?, updated_at = ?
                    where message_id = ? and state in (?, ?)
                    """,
                    (
                        CoreMessageState.OFFERED.value,
                        lease_id,
                        now + LEASE_SECONDS,
                        now,
                        row["message_id"],
                        CoreMessageState.QUEUED.value,
                        CoreMessageState.WAITING_ENDPOINT.value,
                    ),
                )
                if cursor.rowcount != 1:
                    db.rollback()
                    return None
                claimed = db.execute("select * from messages where message_id = ?", (row["message_id"],)).fetchone()
                db.commit()
                return dict(claimed)
            except Exception:
                db.rollback()
                raise

    def transition_core_message(
        self,
        message_id: str,
        expected: Sequence[CoreMessageState],
        next_state: CoreMessageState,
        reason: str,
        lease_id: Optional[str] = None,
        attempts: Optional[int] = None,
        clear_lease: bool = False,
    ) -> bool:
        if not expected:
            return False
        placeholders = ", ".join("?" for _ in expected)
        assignments = ["state = ?", "reason = ?"]
        parameters = [next_state.value, reason]
        if attempts is not None:
            assignments.append("attempts = max(attempts, ?)")
            parameters.append(attempts)
        if clear_lease:
            assignments.extend(
                ("lease_id = null", "lease_expires_at = null", "accepted_by_adapter = null")
            )
        assignments.append("updated_at = ?")
        parameters.extend((time.time(), message_id))
        parameters.extend(state.value for state in expected)
        query = "update messages set {0} where message_id = ? and state in ({1})".format(
            ", ".join(assignments),
            placeholders,
        )
        if lease_id is not None:
            query += " and lease_id = ?"
            parameters.append(lease_id)
        with self._connect() as db:
            cursor = db.execute(query, parameters)
        return cursor.rowcount == 1

    def accept_core_message(
        self,
        message_id: str,
        lease_id: str,
        adapter: str,
        reason: str,
    ) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                """
                update messages
                set state = ?, reason = ?, accepted_by_adapter = ?, updated_at = ?
                where message_id = ? and state = ? and lease_id = ?
                """,
                (
                    CoreMessageState.ACCEPTED.value,
                    reason,
                    adapter,
                    time.time(),
                    message_id,
                    CoreMessageState.OFFERED.value,
                    lease_id,
                ),
            )
        return cursor.rowcount == 1

    def list_dispatchable_endpoints(self) -> list[str]:
        with self._connect() as db:
            rows = db.execute(
                """
                select distinct target_endpoint from messages
                where state in (?, ?)
                order by target_endpoint
                """,
                (CoreMessageState.QUEUED.value, CoreMessageState.WAITING_ENDPOINT.value),
            ).fetchall()
        return [str(row["target_endpoint"]) for row in rows]

    def recover_expired_leases(self, now: float) -> int:
        with self._connect() as db:
            cursor = db.execute(
                """
                update messages
                set state = ?, lease_id = null, lease_expires_at = null,
                    reason = ?, updated_at = ?
                where state = ? and lease_expires_at is not null and lease_expires_at <= ?
                """,
                (
                    CoreMessageState.QUEUED.value,
                    "offered lease expired",
                    now,
                    CoreMessageState.OFFERED.value,
                    now,
                ),
            )
        return cursor.rowcount

    def requeue_offered_messages(self, reason: str) -> int:
        now = time.time()
        with self._connect() as db:
            cursor = db.execute(
                """
                update messages
                set state = ?, reason = ?, lease_id = null,
                    lease_expires_at = null, accepted_by_adapter = null,
                    updated_at = ?
                where state = ?
                """,
                (
                    CoreMessageState.QUEUED.value,
                    reason,
                    now,
                    CoreMessageState.OFFERED.value,
                ),
            )
        return cursor.rowcount

    def reconcile_incomplete_messages(
        self,
        recoverable_adapters: Sequence[str],
        reason: str,
    ) -> Mapping[str, int]:
        recoverable = tuple(dict.fromkeys(recoverable_adapters))
        with self._connect() as db:
            db.execute("begin immediate")
            try:
                queued_count = 0
                if recoverable:
                    placeholders = ", ".join("?" for _ in recoverable)
                    cursor = db.execute(
                        """
                        update messages
                        set state = ?, reason = ?, lease_id = null,
                            lease_expires_at = null, accepted_by_adapter = null,
                            updated_at = ?
                        where state = ? and accepted_by_adapter in ({0})
                        """.format(placeholders),
                        (
                            CoreMessageState.QUEUED.value,
                            reason,
                            time.time(),
                            CoreMessageState.ACCEPTED.value,
                        )
                        + recoverable,
                    )
                    queued_count = cursor.rowcount
                cursor = db.execute(
                    """
                    update messages
                    set state = ?, reason = ?, updated_at = ?
                    where state = ?
                    """,
                    (
                        CoreMessageState.DELIVERY_UNKNOWN.value,
                        reason,
                        time.time(),
                        CoreMessageState.ACCEPTED.value,
                    ),
                )
                unknown_count = cursor.rowcount
                db.commit()
            except Exception:
                db.rollback()
                raise
        return {
            CoreMessageState.QUEUED.value: queued_count,
            CoreMessageState.DELIVERY_UNKNOWN.value: unknown_count,
        }

    def reconcile_accepted_messages(self, reason: str) -> int:
        return self.reconcile_incomplete_messages((), reason)[
            CoreMessageState.DELIVERY_UNKNOWN.value
        ]

    @staticmethod
    def _legacy_endpoint_name(endpoint: str) -> str:
        return endpoint.rsplit("/", 1)[-1]

    def update_message_state(self, message_id: str, state: MessageState, reason: str) -> None:
        with self._connect() as db:
            db.execute(
                "update messages set state = ?, reason = ?, updated_at = ? where message_id = ?",
                (state.value, reason, time.time(), message_id),
            )

    def mark_message_superseded(self, message_id: str, new_message_id: str) -> None:
        self.update_message_state(message_id, MessageState.FAILED, f"superseded by retry {new_message_id}")

    def record_attempt(
        self,
        message_id: str,
        attempt: int,
        previous_state: str,
        next_state: str,
        reason: str,
    ) -> None:
        now = time.time()
        with self._connect() as db:
            db.execute(
                """
                insert into delivery_attempts(message_id, attempt, previous_state, next_state, reason, created_at)
                values (?, ?, ?, ?, ?, ?)
                """,
                (message_id, attempt, previous_state, next_state, reason, now),
            )
            db.execute(
                "update messages set attempts = max(attempts, ?), updated_at = ? where message_id = ?",
                (attempt, now, message_id),
            )

    def get_message(self, message_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("select * from messages where message_id = ?", (message_id,)).fetchone()
        if row is None:
            raise KeyError(message_id)
        return dict(row)

    def list_active_messages(self) -> list[dict[str, Any]]:
        active_states = (
            MessageState.QUEUED.value,
            MessageState.CHECKING_INPUT.value,
            MessageState.SENDING.value,
            MessageState.VERIFYING.value,
            MessageState.RETRYING.value,
            CoreMessageState.WAITING_ENDPOINT.value,
            CoreMessageState.OFFERED.value,
            CoreMessageState.ACCEPTED.value,
            CoreMessageState.DELIVERY_UNKNOWN.value,
        )
        placeholders = ", ".join("?" for _ in active_states)
        with self._connect() as db:
            rows = db.execute(
                """
                select * from messages
                where state in ({0})
                order by created_at, message_id
                """.format(placeholders),
                active_states,
            ).fetchall()
        return [dict(row) for row in rows]

    def list_messages(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "select * from messages order by updated_at desc, message_id desc"
            ).fetchall()
        return [dict(row) for row in rows]

    def archive_messages_older_than(
        self,
        cutoff: float,
        archived_at: Optional[float] = None,
    ) -> dict[str, int]:
        terminal_states = (
            MessageState.DELIVERED.value,
            MessageState.FAILED.value,
            CoreMessageState.DELIVERY_UNKNOWN.value,
        )
        placeholders = ", ".join("?" for _ in terminal_states)
        archive_time = time.time() if archived_at is None else archived_at
        with self._connect() as db:
            db.execute("begin immediate")
            try:
                skipped_active_count = db.execute(
                    """
                    select count(*) as count from messages
                    where updated_at <= ? and state not in ({0})
                    """.format(placeholders),
                    (cutoff,) + terminal_states,
                ).fetchone()["count"]
                rows = db.execute(
                    """
                    select * from messages
                    where updated_at <= ? and state in ({0})
                    order by updated_at, message_id
                    """.format(placeholders),
                    (cutoff,) + terminal_states,
                ).fetchall()
                for row in rows:
                    message = dict(row)
                    db.execute(
                        """
                        insert into message_archive(message_id, message_json, archived_at)
                        values (?, ?, ?)
                        """,
                        (
                            message["message_id"],
                            json.dumps(message, ensure_ascii=False, sort_keys=True),
                            archive_time,
                        ),
                    )
                if rows:
                    db.executemany(
                        "delete from messages where message_id = ?",
                        ((row["message_id"],) for row in rows),
                    )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return {
            "archived_count": len(rows),
            "skipped_active_count": int(skipped_active_count),
        }

    def list_attempts(self, message_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "select * from delivery_attempts where message_id = ? order by id",
                (message_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def can_recover(
        self,
        pane_id: str,
        now: float,
        cooldown_seconds: int,
        max_per_window: Optional[int],
        window_seconds: int,
    ) -> bool:
        with self._connect() as db:
            latest = db.execute(
                "select created_at from recoveries where pane_id = ? order by created_at desc limit 1",
                (pane_id,),
            ).fetchone()
            count = db.execute(
                "select count(*) as count from recoveries where pane_id = ? and created_at >= ?",
                (pane_id, now - window_seconds),
            ).fetchone()
        if latest is not None and now - float(latest["created_at"]) < cooldown_seconds:
            return False
        return max_per_window is None or int(count["count"]) < max_per_window

    def record_recovery(
        self,
        role: str,
        pane_id: str,
        previous_status: str,
        pattern: str,
        action: str,
        now: Optional[float] = None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                insert into recoveries(role, pane_id, previous_status, pattern, action, created_at)
                values (?, ?, ?, ?, ?, ?)
                """,
                (role, pane_id, previous_status, pattern, action, now if now is not None else time.time()),
            )

    def record_event(self, kind: str, payload: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute(
                "insert into events(kind, payload_json, created_at) values (?, ?, ?)",
                (kind, json.dumps(payload, ensure_ascii=False), time.time()),
            )

    def tail_events(self, limit: int = 50) -> Iterable[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("select * from events order by id desc limit ?", (limit,)).fetchall()
        for row in reversed(rows):
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            yield item

    def record_alert(self, kind: str, title: str, body: str, payload: dict[str, Any]) -> int:
        with self._connect() as db:
            cursor = db.execute(
                """
                insert into alerts(kind, title, body, payload_json, created_at)
                values (?, ?, ?, ?, ?)
                """,
                (kind, title, body, json.dumps(payload, ensure_ascii=False), time.time()),
            )
            return int(cursor.lastrowid)

    def list_open_alerts(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """
                select * from (
                    select * from alerts
                    where cleared_at is null
                    order by id desc
                    limit ?
                )
                order by id
                """,
                (limit,),
            ).fetchall()
        alerts = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            alerts.append(item)
        return alerts

    def list_open_alerts_after(self, alert_id: int) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """
                select * from alerts
                where cleared_at is null and id > ?
                order by id
                """,
                (alert_id,),
            ).fetchall()
        alerts = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            alerts.append(item)
        return alerts

    def clear_alerts(self, alert_ids: Optional[list[int]] = None) -> int:
        now = time.time()
        with self._connect() as db:
            if alert_ids:
                placeholders = ",".join("?" for _ in alert_ids)
                cursor = db.execute(
                    f"update alerts set cleared_at = ? where cleared_at is null and id in ({placeholders})",
                    (now, *alert_ids),
                )
            else:
                cursor = db.execute("update alerts set cleared_at = ? where cleared_at is null", (now,))
            return int(cursor.rowcount)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db
