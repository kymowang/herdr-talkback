import asyncio

import pytest

from herdr_talkback.delivery import DeliveryService
from herdr_talkback.core_types import CoreMessage, CoreMessageState
from herdr_talkback.messages import parse_message
from herdr_talkback.state import TalkbackState
from herdr_talkback.types import MessageState, RoutedMessage


ROLES = ("lead", "dev-back", "system-bus")
ALIASES = {"system-bus": "lead"}
EMPTY_PROMPT = "❯ \x1b[38;2;136;136;136mType a message\x1b[0m"
MESSAGE_TEXT = "[lead→dev-back] ping"


def test_delivery_recognizes_codex_submitted_history(tmp_path):
    async def scenario():
        submitted = '\x1b[0m\x1b[1m\x1b[2m\x1b[48;2;50;50;50m› ' + MESSAGE_TEXT
        client = FakeClient([EMPTY_PROMPT, submitted, submitted])
        state = TalkbackState(tmp_path / 'state.db')
        state.init()
        delivery = DeliveryService(client, FakeRegistry(), state)
        result = await delivery.deliver(parse_message(MESSAGE_TEXT, ROLES, ALIASES))
        assert result.delivered
        sends = [params for method, params in client.requests if method == 'pane.send_input']
        assert sends == [{'pane_id': 'p-back', 'text': MESSAGE_TEXT, 'keys': ['enter']}]

    asyncio.run(scenario())


def test_old_codex_submission_does_not_confirm_a_new_delivery(tmp_path):
    async def scenario():
        submitted = '\x1b[0m\x1b[1m\x1b[2m› ' + MESSAGE_TEXT
        client = FakeClient([submitted, submitted])
        state = TalkbackState(tmp_path / 'state.db')
        state.init()
        result = await DeliveryService(client, FakeRegistry(), state, max_attempts=1).deliver(
            parse_message(MESSAGE_TEXT, ROLES, ALIASES))
        assert not result.delivered
        assert not [params for method, params in client.requests if method == 'pane.send_input']

    asyncio.run(scenario())


def test_delivery_waits_for_codex_to_render_submission(tmp_path):
    async def scenario():
        residue = '› ' + MESSAGE_TEXT
        submitted = '\x1b[0m\x1b[1m\x1b[2m› ' + MESSAGE_TEXT
        client = FakeClient([EMPTY_PROMPT, residue, submitted, submitted])
        state = TalkbackState(tmp_path / 'state.db')
        state.init()
        sleeper = FakeSleeper()
        result = await DeliveryService(client, FakeRegistry(), state, verify_timeout_ms=500,
                                       sleep=sleeper.sleep).deliver(parse_message(MESSAGE_TEXT, ROLES, ALIASES))
        assert result.delivered
        sends = [params for method, params in client.requests if method == 'pane.send_input']
        assert len(sends) == 1
        assert sends[0]['text'] == MESSAGE_TEXT

    asyncio.run(scenario())
OCCUPIED_PROMPT = "❯ \x1b[38;2;255;255;255mhalf typed\x1b[0m"


class FakeRegistry:
    def resolve_pane(self, role):
        assert role == "dev-back"
        return "p-back"


class FakeClient:
    def __init__(self, reads):
        self.reads = list(reads)
        self.requests = []

    async def request(self, method, params):
        self.requests.append((method, params))
        if method == "pane.read":
            text = self.reads.pop(0)
            if isinstance(text, BaseException):
                raise text
            return {"type": "pane_read", "read": {"text": text, "pane_id": params["pane_id"]}}
        if method == "pane.send_input":
            return {"type": "ok"}
        raise AssertionError(method)


class FakeSleeper:
    def __init__(self):
        self.delays = []

    async def sleep(self, delay):
        self.delays.append(delay)


class FakeNotifier:
    def __init__(self):
        self.notifications = []

    async def notify(self, title, body):
        self.notifications.append((title, body))


class RecordingState:
    def __init__(self):
        self.created = []
        self.updates = []
        self.attempts = []

    def create_message(self, message_id, source, target, pane_id, raw_text):
        self.created.append((message_id, source, target, pane_id, raw_text))

    def update_message_state(self, message_id, state, reason):
        self.updates.append((message_id, state, reason))

    def record_attempt(self, message_id, attempt, previous_state, next_state, reason):
        self.attempts.append((message_id, attempt, previous_state, next_state, reason))


def test_delivery_sends_when_input_empty(tmp_path):
    asyncio.run(_delivery_sends_when_input_empty(tmp_path))


async def _delivery_sends_when_input_empty(tmp_path):
    message = parse_message(MESSAGE_TEXT, ROLES, ALIASES)
    client = FakeClient(
        [
            EMPTY_PROMPT,
            EMPTY_PROMPT,
        ]
    )
    state = TalkbackState(tmp_path / "state.db")
    state.init()

    result = await DeliveryService(client, FakeRegistry(), state, max_attempts=2).deliver(message)

    assert result.delivered is True
    assert (
        "pane.send_input",
        {"pane_id": "p-back", "text": MESSAGE_TEXT, "keys": ["enter"]},
    ) in client.requests


def test_delivery_reads_enough_recent_lines_for_codex_prompt(tmp_path):
    asyncio.run(_delivery_reads_enough_recent_lines_for_codex_prompt(tmp_path))


async def _delivery_reads_enough_recent_lines_for_codex_prompt(tmp_path):
    message = parse_message(MESSAGE_TEXT, ROLES, ALIASES)
    client = FakeClient([EMPTY_PROMPT, EMPTY_PROMPT])
    state = TalkbackState(tmp_path / "state.db")
    state.init()

    await DeliveryService(client, FakeRegistry(), state, max_attempts=2).deliver(message)

    read_requests = [params for method, params in client.requests if method == "pane.read"]
    assert read_requests
    assert all(params["lines"] == 80 for params in read_requests)


def test_delivery_blocks_when_target_has_real_input(tmp_path):
    asyncio.run(_delivery_blocks_when_target_has_real_input(tmp_path))


async def _delivery_blocks_when_target_has_real_input(tmp_path):
    message = parse_message(MESSAGE_TEXT, ROLES, ALIASES)
    client = FakeClient([OCCUPIED_PROMPT, OCCUPIED_PROMPT])
    sleeper = FakeSleeper()
    state = TalkbackState(tmp_path / "state.db")
    state.init()

    result = await DeliveryService(
        client,
        FakeRegistry(),
        state,
        max_attempts=2,
        retry_interval_seconds=30,
        sleep=sleeper.sleep,
    ).deliver(message)

    assert result.delivered is False
    assert result.reason == "target input occupied"
    assert result.attempts == 2
    assert sleeper.delays == [30]
    assert all(method != "pane.send_input" for method, _ in client.requests)


def test_delivery_notifies_when_input_unavailable_after_retries(tmp_path):
    asyncio.run(_delivery_notifies_when_input_unavailable_after_retries(tmp_path))


async def _delivery_notifies_when_input_unavailable_after_retries(tmp_path):
    message = parse_message(MESSAGE_TEXT, ROLES, ALIASES)
    client = FakeClient([OCCUPIED_PROMPT, OCCUPIED_PROMPT])
    sleeper = FakeSleeper()
    notifier = FakeNotifier()
    state = TalkbackState(tmp_path / "state.db")
    state.init()

    result = await DeliveryService(
        client,
        FakeRegistry(),
        state,
        max_attempts=2,
        retry_interval_seconds=30,
        sleep=sleeper.sleep,
        notifier=notifier,
    ).deliver(message)

    assert result.delivered is False
    assert result.reason == "target input occupied"
    assert notifier.notifications == [
        (
            "Herdr Talkback delivery blocked",
            "lead -> dev-back failed after 2 attempts: target input occupied",
        )
    ]


def test_delivery_retries_unknown_input_until_empty(tmp_path):
    asyncio.run(_delivery_retries_unknown_input_until_empty(tmp_path))


async def _delivery_retries_unknown_input_until_empty(tmp_path):
    message = parse_message(MESSAGE_TEXT, ROLES, ALIASES)
    client = FakeClient(["normal output only", EMPTY_PROMPT, EMPTY_PROMPT])
    sleeper = FakeSleeper()
    state = TalkbackState(tmp_path / "state.db")
    state.init()

    result = await DeliveryService(
        client,
        FakeRegistry(),
        state,
        max_attempts=3,
        retry_interval_seconds=30,
        sleep=sleeper.sleep,
    ).deliver(message)

    assert result.delivered is True
    assert result.reason == "input cleared"
    assert result.attempts == 2
    assert sleeper.delays == [30]


def test_delivery_retries_occupied_input_until_empty(tmp_path):
    asyncio.run(_delivery_retries_occupied_input_until_empty(tmp_path))


async def _delivery_retries_occupied_input_until_empty(tmp_path):
    message = parse_message(MESSAGE_TEXT, ROLES, ALIASES)
    client = FakeClient([OCCUPIED_PROMPT, EMPTY_PROMPT, EMPTY_PROMPT])
    sleeper = FakeSleeper()
    state = TalkbackState(tmp_path / "state.db")
    state.init()

    result = await DeliveryService(
        client,
        FakeRegistry(),
        state,
        max_attempts=3,
        retry_interval_seconds=30,
        sleep=sleeper.sleep,
    ).deliver(message)

    assert result.delivered is True
    assert result.reason == "input cleared"
    assert result.attempts == 2
    assert sleeper.delays == [30]
    assert (
        "pane.send_input",
        {"pane_id": "p-back", "text": MESSAGE_TEXT, "keys": ["enter"]},
    ) in client.requests


def test_delivery_blocks_when_target_input_unknown(tmp_path):
    asyncio.run(_delivery_blocks_when_target_input_unknown(tmp_path))


async def _delivery_blocks_when_target_input_unknown(tmp_path):
    message = parse_message(MESSAGE_TEXT, ROLES, ALIASES)
    client = FakeClient(["normal output only", "normal output only"])
    sleeper = FakeSleeper()
    state = TalkbackState(tmp_path / "state.db")
    state.init()

    result = await DeliveryService(
        client,
        FakeRegistry(),
        state,
        max_attempts=2,
        retry_interval_seconds=30,
        sleep=sleeper.sleep,
    ).deliver(message)

    assert result.delivered is False
    assert result.reason == "target input unknown"
    assert result.attempts == 2
    assert sleeper.delays == [30]
    assert all(method != "pane.send_input" for method, _ in client.requests)


def test_delivery_dismisses_esc_to_cancel_overlay_before_sending(tmp_path):
    asyncio.run(_delivery_dismisses_esc_to_cancel_overlay_before_sending(tmp_path))


async def _delivery_dismisses_esc_to_cancel_overlay_before_sending(tmp_path):
    message = parse_message(MESSAGE_TEXT, ROLES, ALIASES)
    overlay = "Settings  Status   Config   Usage   Stats\n\nVersion: 2.1.197\n\nEsc to cancel"
    client = FakeClient([overlay, EMPTY_PROMPT, EMPTY_PROMPT])
    sleeper = FakeSleeper()
    state = TalkbackState(tmp_path / "state.db")
    state.init()

    result = await DeliveryService(
        client,
        FakeRegistry(),
        state,
        max_attempts=2,
        sleep=sleeper.sleep,
        retry_interval_seconds=30,
    ).deliver(message)

    assert result.delivered is True
    assert result.reason == "input cleared"
    assert ("pane.send_input", {"pane_id": "p-back", "text": "", "keys": ["esc"]}) in client.requests
    assert (
        "pane.send_input",
        {"pane_id": "p-back", "text": MESSAGE_TEXT, "keys": ["enter"]},
    ) in client.requests
    assert sleeper.delays == [0.25]


def test_delivery_flushes_own_residue_once(tmp_path):
    asyncio.run(_delivery_flushes_own_residue_once(tmp_path))


async def _delivery_flushes_own_residue_once(tmp_path):
    message = parse_message(MESSAGE_TEXT, ROLES, ALIASES)
    client = FakeClient(
        [
            EMPTY_PROMPT,
            f"❯ \x1b[38;2;255;255;255m{MESSAGE_TEXT}\x1b[0m",
            EMPTY_PROMPT,
        ]
    )
    state = TalkbackState(tmp_path / "state.db")
    state.init()

    result = await DeliveryService(client, FakeRegistry(), state, max_attempts=2).deliver(message)

    assert result.delivered is True
    assert client.requests.count(("pane.send_input", {"pane_id": "p-back", "text": "", "keys": ["enter"]})) == 1


def test_delivery_notifies_when_message_sticks_in_input_and_is_submitted(tmp_path):
    asyncio.run(_delivery_notifies_when_message_sticks_in_input_and_is_submitted(tmp_path))


async def _delivery_notifies_when_message_sticks_in_input_and_is_submitted(tmp_path):
    message = parse_message(MESSAGE_TEXT, ROLES, ALIASES)
    client = FakeClient(
        [
            EMPTY_PROMPT,
            f"❯ \x1b[38;2;255;255;255m{MESSAGE_TEXT}\x1b[0m",
            EMPTY_PROMPT,
        ]
    )
    notifier = FakeNotifier()
    state = TalkbackState(tmp_path / "state.db")
    state.init()

    result = await DeliveryService(client, FakeRegistry(), state, max_attempts=1, notifier=notifier).deliver(message)

    assert result.delivered is True
    assert result.reason == "residue flushed"
    assert notifier.notifications == [
        (
            "Herdr Talkback submitted stuck input",
            "lead -> dev-back remained in the input after send; sent Enter to submit",
        )
    ]


def test_delivery_flushes_multiline_message_collapsed_to_paste_block(tmp_path):
    asyncio.run(_delivery_flushes_multiline_message_collapsed_to_paste_block(tmp_path))


async def _delivery_flushes_multiline_message_collapsed_to_paste_block(tmp_path):
    raw_text = "[lead→dev-back] line 1\nline 2\nline 3"
    message = parse_message(raw_text, ROLES, ALIASES)
    client = FakeClient(
        [
            EMPTY_PROMPT,
            "❯ \x1b[38;2;255;255;255m[Pasted text #1 +3 lines]\x1b[0m",
            EMPTY_PROMPT,
        ]
    )
    notifier = FakeNotifier()
    state = TalkbackState(tmp_path / "state.db")
    state.init()

    result = await DeliveryService(client, FakeRegistry(), state, max_attempts=1, notifier=notifier).deliver(message)

    assert result.delivered is True
    assert result.reason == "residue flushed"
    assert client.requests.count(("pane.send_input", {"pane_id": "p-back", "text": "", "keys": ["enter"]})) == 1
    assert notifier.notifications == [
        (
            "Herdr Talkback submitted stuck input",
            "lead -> dev-back remained in the input after send; sent Enter to submit",
        )
    ]


def test_delivery_flushes_long_single_line_message_collapsed_to_paste_block(tmp_path):
    asyncio.run(_delivery_flushes_long_single_line_message_collapsed_to_paste_block(tmp_path))


async def _delivery_flushes_long_single_line_message_collapsed_to_paste_block(tmp_path):
    raw_text = "[lead→dev-back] " + "system-name-rename completed and ready for review " * 4
    message = parse_message(raw_text, ROLES, ALIASES)
    client = FakeClient(
        [
            EMPTY_PROMPT,
            "❯ \x1b[38;2;255;255;255m[Pasted text #3]\x1b[0m",
            EMPTY_PROMPT,
        ]
    )
    notifier = FakeNotifier()
    state = TalkbackState(tmp_path / "state.db")
    state.init()

    result = await DeliveryService(client, FakeRegistry(), state, max_attempts=1, notifier=notifier).deliver(message)

    assert result.delivered is True
    assert result.reason == "residue flushed"
    assert client.requests.count(("pane.send_input", {"pane_id": "p-back", "text": "", "keys": ["enter"]})) == 1
    assert notifier.notifications == [
        (
            "Herdr Talkback submitted stuck input",
            "lead -> dev-back remained in the input after send; sent Enter to submit",
        )
    ]


def test_delivery_flushes_soft_wrapped_message_residue(tmp_path):
    asyncio.run(_delivery_flushes_soft_wrapped_message_residue(tmp_path))


async def _delivery_flushes_soft_wrapped_message_residue(tmp_path):
    raw_text = "[lead→dev-back] long routed message with enough text to wrap at the terminal"
    message = parse_message(raw_text, ROLES, ALIASES)
    wrapped_residue = (
        "❯ \x1b[38;2;255;255;255m[lead→dev-back] long routed message with enough text\n"
        "  to wrap at the terminal\x1b[0m\n"
        "  \x1b[38;2;255;107;128m⏵⏵ bypass permissions on\x1b[0m"
    )
    client = FakeClient([EMPTY_PROMPT, wrapped_residue, EMPTY_PROMPT])
    notifier = FakeNotifier()
    state = TalkbackState(tmp_path / "state.db")
    state.init()

    result = await DeliveryService(client, FakeRegistry(), state, max_attempts=1, notifier=notifier).deliver(message)

    assert result.delivered is True
    assert result.reason == "residue flushed"
    assert client.requests.count(("pane.send_input", {"pane_id": "p-back", "text": "", "keys": ["enter"]})) == 1
    assert notifier.notifications == [
        (
            "Herdr Talkback submitted stuck input",
            "lead -> dev-back remained in the input after send; sent Enter to submit",
        )
    ]


def test_delivery_flushes_soft_wrapped_message_residue_with_inserted_space(tmp_path):
    asyncio.run(_delivery_flushes_soft_wrapped_message_residue_with_inserted_space(tmp_path))


async def _delivery_flushes_soft_wrapped_message_residue_with_inserted_space(tmp_path):
    raw_text = "[lead→dev-back] GlobalExceptionHandler前缀耦合修复对称"
    message = parse_message(raw_text, ROLES, ALIASES)
    wrapped_residue = (
        "❯ \x1b[38;2;255;255;255m[lead→dev-back] GlobalExceptionHandler\n"
        "  前缀耦合修复对称\x1b[0m\n"
        "  \x1b[38;2;255;107;128m⏵⏵ bypass permissions on\x1b[0m"
    )
    client = FakeClient([EMPTY_PROMPT, wrapped_residue, EMPTY_PROMPT])
    notifier = FakeNotifier()
    state = TalkbackState(tmp_path / "state.db")
    state.init()

    result = await DeliveryService(client, FakeRegistry(), state, max_attempts=1, notifier=notifier).deliver(message)

    assert result.delivered is True
    assert result.reason == "residue flushed"
    assert client.requests.count(("pane.send_input", {"pane_id": "p-back", "text": "", "keys": ["enter"]})) == 1
    assert notifier.notifications == [
        (
            "Herdr Talkback submitted stuck input",
            "lead -> dev-back remained in the input after send; sent Enter to submit",
        )
    ]


def test_delivery_flushes_image_placeholder_residue_without_retrying(tmp_path):
    asyncio.run(_delivery_flushes_image_placeholder_residue_without_retrying(tmp_path))


async def _delivery_flushes_image_placeholder_residue_without_retrying(tmp_path):
    raw_text = "[lead→dev-back] evidence: /tmp/inbox-richtext-e2e-bell-1440.png。请串行验收合并。"
    message = parse_message(raw_text, ROLES, ALIASES)
    image_residue = (
        "❯ \x1b[38;2;255;255;255m[Image #9] [Image #10]"
        "/tmp/inbox-richtext-e2e-bell-1440.png。请串行验收合并。\x1b[0m"
    )
    client = FakeClient([EMPTY_PROMPT, image_residue, EMPTY_PROMPT])
    sleeper = FakeSleeper()
    state = TalkbackState(tmp_path / "state.db")
    state.init()

    result = await DeliveryService(
        client,
        FakeRegistry(),
        state,
        max_attempts=2,
        retry_interval_seconds=30,
        sleep=sleeper.sleep,
    ).deliver(message)

    assert result.delivered is True
    assert result.reason == "residue flushed"
    assert sleeper.delays == []
    sends = [request for request in client.requests if request[0] == "pane.send_input"]
    assert sends == [
        ("pane.send_input", {"pane_id": "p-back", "text": raw_text, "keys": ["enter"]}),
        ("pane.send_input", {"pane_id": "p-back", "text": "", "keys": ["enter"]}),
    ]


def test_delivery_waits_before_retrying_ambiguous_delivery(tmp_path):
    asyncio.run(_delivery_waits_before_retrying_ambiguous_delivery(tmp_path))


async def _delivery_waits_before_retrying_ambiguous_delivery(tmp_path):
    message = parse_message(MESSAGE_TEXT, ROLES, ALIASES)
    client = FakeClient([EMPTY_PROMPT, OCCUPIED_PROMPT, EMPTY_PROMPT, EMPTY_PROMPT])
    sleeper = FakeSleeper()
    state = TalkbackState(tmp_path / "state.db")
    state.init()

    result = await DeliveryService(
        client,
        FakeRegistry(),
        state,
        max_attempts=2,
        retry_interval_seconds=30,
        sleep=sleeper.sleep,
    ).deliver(message)

    assert result.delivered is True
    assert result.attempts == 2
    assert sleeper.delays == [30]


def test_delivery_does_not_flush_paste_block_before_sending(tmp_path):
    asyncio.run(_delivery_does_not_flush_paste_block_before_sending(tmp_path))


async def _delivery_does_not_flush_paste_block_before_sending(tmp_path):
    raw_text = "[lead→dev-back] line 1\nline 2\nline 3"
    message = parse_message(raw_text, ROLES, ALIASES)
    client = FakeClient(
        [
            "❯ \x1b[38;2;255;255;255m[Pasted text #1 +3 lines]\x1b[0m",
            EMPTY_PROMPT,
            EMPTY_PROMPT,
        ]
    )
    sleeper = FakeSleeper()
    state = TalkbackState(tmp_path / "state.db")
    state.init()

    result = await DeliveryService(
        client,
        FakeRegistry(),
        state,
        max_attempts=2,
        retry_interval_seconds=30,
        sleep=sleeper.sleep,
    ).deliver(message)

    assert result.delivered is True
    assert result.reason == "input cleared"
    assert sleeper.delays == [30]
    assert client.requests.count(("pane.send_input", {"pane_id": "p-back", "text": "", "keys": ["enter"]})) == 0


def test_delivery_does_not_flush_single_line_paste_block_before_sending(tmp_path):
    asyncio.run(_delivery_does_not_flush_single_line_paste_block_before_sending(tmp_path))


async def _delivery_does_not_flush_single_line_paste_block_before_sending(tmp_path):
    raw_text = "[lead→dev-back] " + "system-name-rename completed and ready for review " * 4
    message = parse_message(raw_text, ROLES, ALIASES)
    client = FakeClient(
        [
            "❯ \x1b[38;2;255;255;255m[Pasted text #3]\x1b[0m",
            EMPTY_PROMPT,
            EMPTY_PROMPT,
        ]
    )
    sleeper = FakeSleeper()
    state = TalkbackState(tmp_path / "state.db")
    state.init()

    result = await DeliveryService(
        client,
        FakeRegistry(),
        state,
        max_attempts=2,
        retry_interval_seconds=30,
        sleep=sleeper.sleep,
    ).deliver(message)

    assert result.delivered is True
    assert result.reason == "input cleared"
    assert sleeper.delays == [30]
    assert client.requests.count(("pane.send_input", {"pane_id": "p-back", "text": "", "keys": ["enter"]})) == 0


def test_delivery_catches_message_that_appears_after_initial_empty_verify_read(tmp_path):
    asyncio.run(_delivery_catches_message_that_appears_after_initial_empty_verify_read(tmp_path))


async def _delivery_catches_message_that_appears_after_initial_empty_verify_read(tmp_path):
    message = parse_message(MESSAGE_TEXT, ROLES, ALIASES)
    client = FakeClient(
        [
            EMPTY_PROMPT,
            EMPTY_PROMPT,
            f"❯ \x1b[38;2;255;255;255m{MESSAGE_TEXT}\x1b[0m",
            EMPTY_PROMPT,
            EMPTY_PROMPT,
        ]
    )
    sleeper = FakeSleeper()
    notifier = FakeNotifier()
    state = TalkbackState(tmp_path / "state.db")
    state.init()

    result = await DeliveryService(
        client,
        FakeRegistry(),
        state,
        max_attempts=2,
        sleep=sleeper.sleep,
        notifier=notifier,
        verify_timeout_ms=250,
        verify_poll_interval_seconds=0.25,
    ).deliver(message)

    assert result.delivered is True
    assert result.reason == "residue flushed"
    assert sleeper.delays == [0.25, 0.25]
    assert client.requests.count(("pane.send_input", {"pane_id": "p-back", "text": "", "keys": ["enter"]})) == 1
    assert notifier.notifications == [
        (
            "Herdr Talkback submitted stuck input",
            "lead -> dev-back remained in the input after send; sent Enter to submit",
        )
    ]


def test_delivery_verify_polls_until_delayed_residue_appears(tmp_path):
    asyncio.run(_delivery_verify_polls_until_delayed_residue_appears(tmp_path))


async def _delivery_verify_polls_until_delayed_residue_appears(tmp_path):
    residue = f"❯ \x1b[38;2;255;255;255m{MESSAGE_TEXT}\x1b[0m"
    client = FakeClient([EMPTY_PROMPT, EMPTY_PROMPT, EMPTY_PROMPT, residue])
    sleeper = FakeSleeper()
    state = TalkbackState(tmp_path / "state.db")
    state.init()

    service = DeliveryService(
        client,
        FakeRegistry(),
        state,
        sleep=sleeper.sleep,
        verify_timeout_ms=1000,
        verify_poll_interval_seconds=0.25,
    )

    result = await service._verify("p-back", MESSAGE_TEXT)

    assert result == "residue"
    assert sleeper.delays == [0.25, 0.25, 0.25]


def test_delivery_does_not_resend_when_residue_persists(tmp_path):
    asyncio.run(_delivery_does_not_resend_when_residue_persists(tmp_path))


async def _delivery_does_not_resend_when_residue_persists(tmp_path):
    message = parse_message(MESSAGE_TEXT, ROLES, ALIASES)
    residue = f"❯ \x1b[38;2;255;255;255m{MESSAGE_TEXT}\x1b[0m"
    client = FakeClient([EMPTY_PROMPT, residue, residue, residue, residue, residue])
    state = TalkbackState(tmp_path / "state.db")
    state.init()

    result = await DeliveryService(client, FakeRegistry(), state, max_attempts=2).deliver(message)

    raw_send = ("pane.send_input", {"pane_id": "p-back", "text": MESSAGE_TEXT, "keys": ["enter"]})
    flush_send = ("pane.send_input", {"pane_id": "p-back", "text": "", "keys": ["enter"]})
    assert result.delivered is False
    assert result.reason == "message residue not cleared"
    assert client.requests.count(raw_send) == 1
    assert client.requests.count(flush_send) == 1


def test_send_control_flushes_own_residue_once(tmp_path):
    asyncio.run(_send_control_flushes_own_residue_once(tmp_path))


async def _send_control_flushes_own_residue_once(tmp_path):
    client = FakeClient(
        [
            EMPTY_PROMPT,
            "❯ \x1b[38;2;255;255;255mcontinue\x1b[0m",
            EMPTY_PROMPT,
        ]
    )
    state = TalkbackState(tmp_path / "state.db")
    state.init()

    result = await DeliveryService(client, FakeRegistry(), state, max_attempts=2).send_control("p-back", "continue")

    assert result.delivered is True
    assert result.reason == "control sent"
    assert client.requests.count(("pane.send_input", {"pane_id": "p-back", "text": "", "keys": ["enter"]})) == 1


def test_delivery_updates_message_state_to_verifying_before_reading_after_send(tmp_path):
    asyncio.run(_delivery_updates_message_state_to_verifying_before_reading_after_send(tmp_path))


async def _delivery_updates_message_state_to_verifying_before_reading_after_send(tmp_path):
    message = parse_message(MESSAGE_TEXT, ROLES, ALIASES)
    client = FakeClient([EMPTY_PROMPT, RuntimeError("verification read failed")])
    state = TalkbackState(tmp_path / "state.db")
    state.init()

    with pytest.raises(RuntimeError, match="verification read failed"):
        await DeliveryService(client, FakeRegistry(), state, max_attempts=2).deliver(message)

    assert state.get_message(message.message_id)["state"] == MessageState.VERIFYING.value


def test_core_mode_preserves_attempt_evidence_without_top_level_state_updates():
    asyncio.run(_core_mode_preserves_attempt_evidence_without_top_level_state_updates())


async def _core_mode_preserves_attempt_evidence_without_top_level_state_updates():
    message = parse_message(MESSAGE_TEXT, ROLES, ALIASES)
    state = RecordingState()
    client = FakeClient([EMPTY_PROMPT, EMPTY_PROMPT])

    result = await DeliveryService(
        client, FakeRegistry(), state, max_attempts=2, manage_message_state=False
    ).deliver(message)

    assert result.delivered is True
    assert state.updates == []
    assert state.attempts == [
        (message.message_id, 1, "sending", "verifying", "pane.send_input sent"),
    ]


def test_core_mode_delivers_pre_enqueued_message_without_replacing_core_state(tmp_path):
    asyncio.run(_core_mode_delivers_pre_enqueued_message_without_replacing_core_state(tmp_path))


async def _core_mode_delivers_pre_enqueued_message_without_replacing_core_state(tmp_path):
    core_message = CoreMessage(
        message_id="msg_core_1",
        source_endpoint="linux-main/lead",
        target_endpoint="linux-main/dev-back",
        body="ping",
        raw_text=MESSAGE_TEXT,
        state=CoreMessageState.OFFERED,
        conversation_id="msg_core_1",
    )
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    assert state.enqueue_message(core_message) is True
    routed = RoutedMessage(
        message_id=core_message.message_id,
        source="lead",
        target="dev-back",
        resolved_target="dev-back",
        body=core_message.body,
        raw_text=core_message.raw_text,
        source_endpoint=core_message.source_endpoint,
        target_endpoint=core_message.target_endpoint,
        conversation_id=core_message.conversation_id,
    )

    result = await DeliveryService(
        FakeClient([EMPTY_PROMPT, EMPTY_PROMPT]),
        FakeRegistry(),
        state,
        max_attempts=1,
        manage_message_state=False,
    ).deliver(routed)

    assert result.delivered is True
    assert state.get_message(core_message.message_id)["state"] == CoreMessageState.OFFERED.value
    assert [attempt["reason"] for attempt in state.list_attempts(core_message.message_id)] == [
        "pane.send_input sent",
    ]
