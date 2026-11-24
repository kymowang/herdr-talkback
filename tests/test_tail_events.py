from herdr_talkback.core_types import CoreMessage, CoreMessageState
from herdr_talkback.state import TalkbackState


def _message(message_id, source, target, state=CoreMessageState.QUEUED):
    return CoreMessage(
        message_id=message_id,
        source_endpoint=source,
        target_endpoint=target,
        body="ping",
        raw_text=f"[{source}->{target}] ping",
        state=state,
        conversation_id=message_id,
    )


def test_tail_names_sender_and_target_for_message_events(tmp_path):
    state = TalkbackState(tmp_path / "state.db", local_node_id="linux-main")
    state.init()
    state.enqueue_message(_message("msg_1", "linux-main/lead", "linux-main/dev-back"), "p1")
    state.record_event("peer_pending", {"peer": "pc-54", "message_id": "msg_1", "remote_state": "queued"})

    event = list(state.tail_events())[-1]

    assert event["payload"]["source"] == "lead"
    assert event["payload"]["target"] == "dev-back"
    assert event["payload"]["peer"] == "pc-54"


def test_tail_names_endpoints_for_archived_messages(tmp_path):
    state = TalkbackState(tmp_path / "state.db", local_node_id="linux-main")
    state.init()
    state.enqueue_message(_message("msg_2", "linux-main/monitor", "linux-main/arc-bus"), "p2")
    state.record_event("peer_receipt", {"peer": "pc-54", "message_id": "msg_2", "state": "delivered"})
    with state._connect() as db:
        db.execute("update messages set updated_at = 0 where message_id = 'msg_2'")
    state.archive_messages_older_than(cutoff=1, archived_at=1)

    event = list(state.tail_events())[-1]

    assert event["payload"]["source"] == "monitor"
    assert event["payload"]["target"] == "arc-bus"


def test_tail_leaves_events_without_message_id_untouched(tmp_path):
    state = TalkbackState(tmp_path / "state.db", local_node_id="linux-main")
    state.init()
    state.record_event("logic_result", {"request_id": "r1", "pane_id": "p1", "result": "continue"})

    event = list(state.tail_events())[-1]

    assert event["payload"] == {"request_id": "r1", "pane_id": "p1", "result": "continue"}


def test_tail_keeps_events_whose_message_row_is_gone(tmp_path):
    state = TalkbackState(tmp_path / "state.db", local_node_id="linux-main")
    state.init()
    state.record_event("peer_received", {"peer": "pc-54", "message_id": "msg_unknown"})

    event = list(state.tail_events())[-1]

    assert event["payload"]["message_id"] == "msg_unknown"
    assert "source" not in event["payload"]
    assert "target" not in event["payload"]
