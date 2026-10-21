import asyncio

import pytest

from herdr_talkback.monitor import Monitor
from herdr_talkback.state import TalkbackState


class FakeRegistry:
    def role_for_pane(self, pane_id):
        return "runner" if pane_id == "p-runner" else None


class FakeDelivery:
    def __init__(self):
        self.controls = []

    async def send_control(self, pane_id, text):
        self.controls.append((pane_id, text))
        return type("Result", (), {"delivered": True, "reason": "control sent", "attempts": 1})()


class FailingDelivery:
    def __init__(self):
        self.controls = []

    async def send_control(self, pane_id, text):
        self.controls.append((pane_id, text))
        return type("Result", (), {"delivered": False, "reason": "control not confirmed", "attempts": 1})()


class FakeNotifier:
    def __init__(self):
        self.notifications = []

    async def notify(self, title, body):
        self.notifications.append((title, body))


class FakeClient:
    def __init__(self, text="API Error: overloaded, try again"):
        self.text = text

    async def request(self, method, params):
        assert method == "pane.read"
        return {"read": {"text": self.text}}


def test_monitor_sends_continue_for_recoverable_blocked(tmp_path):
    asyncio.run(_monitor_sends_continue_for_recoverable_blocked(tmp_path))


async def _monitor_sends_continue_for_recoverable_blocked(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    delivery = FakeDelivery()
    monitor = Monitor(FakeClient(), FakeRegistry(), delivery, state, cooldown_seconds=120, max_per_window=3)

    await monitor.handle_status_event({"data": {"pane_id": "p-runner", "agent_status": "blocked"}})

    assert delivery.controls == [("p-runner", "continue")]


def test_monitor_ignores_recoverable_output_while_working(tmp_path):
    asyncio.run(_monitor_ignores_recoverable_output_while_working(tmp_path))


async def _monitor_ignores_recoverable_output_while_working(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    delivery = FakeDelivery()
    notifier = FakeNotifier()
    monitor = Monitor(FakeClient("Rate limit exceeded"), FakeRegistry(), delivery, state, notifier=notifier)

    await monitor.handle_status_event({"data": {"pane_id": "p-runner", "agent_status": "working"}})

    assert delivery.controls == []
    assert notifier.notifications == []


def test_monitor_ignores_recoverable_output_while_idle(tmp_path):
    asyncio.run(_monitor_ignores_recoverable_output_while_idle(tmp_path))


async def _monitor_ignores_recoverable_output_while_idle(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    delivery = FakeDelivery()
    notifier = FakeNotifier()
    monitor = Monitor(
        FakeClient("API Error: Connection closed mid-response"),
        FakeRegistry(),
        delivery,
        state,
        notifier=notifier,
    )

    await monitor.handle_status_event({"data": {"pane_id": "p-runner", "agent_status": "idle"}})

    assert delivery.controls == []
    assert notifier.notifications == []


def test_monitor_ignores_recoverable_output_while_done(tmp_path):
    asyncio.run(_monitor_ignores_recoverable_output_while_done(tmp_path))


async def _monitor_ignores_recoverable_output_while_done(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    delivery = FakeDelivery()
    notifier = FakeNotifier()
    monitor = Monitor(FakeClient("request timeout"), FakeRegistry(), delivery, state, notifier=notifier)

    await monitor.handle_status_event({"data": {"pane_id": "p-runner", "agent_status": "done"}})

    assert delivery.controls == []
    assert notifier.notifications == []


def test_monitor_sends_continue_for_latest_turn_error_while_idle(tmp_path):
    asyncio.run(_monitor_sends_continue_for_latest_turn_error_while_idle(tmp_path))


async def _monitor_sends_continue_for_latest_turn_error_while_idle(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    delivery = FakeDelivery()
    notifier = FakeNotifier()
    recent = (
        "❯ git commit -m test\n"
        "● API Error: Connection closed mid-response. The response above may be incomplete.\n"
        "✻ Crunched for 1m 4s\n"
        "❯ \n"
        "? for shortcuts\n"
    )
    monitor = Monitor(FakeClient(recent), FakeRegistry(), delivery, state, notifier=notifier)

    await monitor.handle_status_event({"data": {"pane_id": "p-runner", "agent_status": "idle"}})

    assert delivery.controls == [("p-runner", "continue")]
    assert notifier.notifications == [
        (
            "Herdr Talkback recovered runner",
            "runner matched api error while idle; sent continue",
        )
    ]


def test_monitor_sends_continue_for_truncated_latest_turn_error_while_idle(tmp_path):
    asyncio.run(_monitor_sends_continue_for_truncated_latest_turn_error_while_idle(tmp_path))


async def _monitor_sends_continue_for_truncated_latest_turn_error_while_idle(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    delivery = FakeDelivery()
    recent = (
        "● The robust approach is to stub table columns for jsdom.\n"
        "● API Error: Connection closed mid-response. The response above may be incomplete.\n"
        "✻ Cooked for 5m 44s\n"
        "※ recap: I am dev-fe on the current UI fix batch.\n"
        "❯ \n"
        "⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
    )
    monitor = Monitor(FakeClient(recent), FakeRegistry(), delivery, state)

    await monitor.handle_status_event({"data": {"pane_id": "p-runner", "agent_status": "idle"}})

    assert delivery.controls == [("p-runner", "continue")]


def test_monitor_sends_continue_for_latest_turn_error_while_done(tmp_path):
    asyncio.run(_monitor_sends_continue_for_latest_turn_error_while_done(tmp_path))


async def _monitor_sends_continue_for_latest_turn_error_while_done(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    delivery = FakeDelivery()
    recent = (
        "❯ run checks\n"
        "● API Error: request timeout while reading response\n"
        "✻ Worked for 40s\n"
        "❯ \n"
    )
    monitor = Monitor(FakeClient(recent), FakeRegistry(), delivery, state)

    await monitor.handle_status_event({"data": {"pane_id": "p-runner", "agent_status": "done"}})

    assert delivery.controls == [("p-runner", "continue")]


def test_monitor_ignores_task_report_timeout_while_done(tmp_path):
    asyncio.run(_monitor_ignores_task_report_timeout_while_done(tmp_path))


async def _monitor_ignores_task_report_timeout_while_done(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    delivery = FakeDelivery()
    notifier = FakeNotifier()
    recent = (
        "❯ finish sidebar polish\n"
        "● All done. Remote dev tip = d2e5a194, working tree clean.\n"
        "  pre-push timeout happened during full vitest, then I used --no-verify after local checks passed.\n"
        "  Optional backlog: speed up the pre-push verify gate.\n"
        "✻ Churned for 10m 32s\n"
        "❯ \n"
    )
    monitor = Monitor(FakeClient(recent), FakeRegistry(), delivery, state, notifier=notifier)

    await monitor.handle_status_event({"data": {"pane_id": "p-runner", "agent_status": "done"}})

    assert delivery.controls == []
    assert notifier.notifications == []


def test_monitor_ignores_explained_timeout_example_while_done(tmp_path):
    asyncio.run(_monitor_ignores_explained_timeout_example_while_done(tmp_path))


async def _monitor_ignores_explained_timeout_example_while_done(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    delivery = FakeDelivery()
    notifier = FakeNotifier()
    recent = (
        "❯ explain monitor fix\n"
        "● idle/done timeout matching can misread examples like request timeout while reading response.\n"
        "  The fix is to avoid recovering from plain timeout text after a normal prompt returns.\n"
        "✻ Worked for 2m 10s\n"
        "❯ \n"
    )
    monitor = Monitor(FakeClient(recent), FakeRegistry(), delivery, state, notifier=notifier)

    await monitor.handle_status_event({"data": {"pane_id": "p-runner", "agent_status": "done"}})

    assert delivery.controls == []
    assert notifier.notifications == []


def test_monitor_ignores_normal_answer_api_error_reference_while_done(tmp_path):
    asyncio.run(_monitor_ignores_normal_answer_api_error_reference_while_done(tmp_path))


async def _monitor_ignores_normal_answer_api_error_reference_while_done(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    delivery = FakeDelivery()
    notifier = FakeNotifier()
    recent = (
        "❯ continue\n"
        "• 已继续处理完。\n"
        "\n"
        "  - tests/test_monitor.py:171：正向恢复用例改成明确 API Error: request timeout...\n"
        "  - python -m pytest -q：115 passed\n"
        "❯ \n"
    )
    monitor = Monitor(FakeClient(recent), FakeRegistry(), delivery, state, notifier=notifier)

    await monitor.handle_status_event({"data": {"pane_id": "p-runner", "agent_status": "done"}})

    assert delivery.controls == []
    assert notifier.notifications == []


def test_monitor_ignores_stale_error_before_latest_turn_while_idle(tmp_path):
    asyncio.run(_monitor_ignores_stale_error_before_latest_turn_while_idle(tmp_path))


async def _monitor_ignores_stale_error_before_latest_turn_while_idle(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    delivery = FakeDelivery()
    notifier = FakeNotifier()
    recent = (
        "❯ old command\n"
        "API Error: Connection closed mid-response.\n"
        "❯ later command\n"
        "● completed normally\n"
        "❯ \n"
    )
    monitor = Monitor(FakeClient(recent), FakeRegistry(), delivery, state, notifier=notifier)

    await monitor.handle_status_event({"data": {"pane_id": "p-runner", "agent_status": "idle"}})

    assert delivery.controls == []
    assert notifier.notifications == []


def test_monitor_sends_continue_for_recoverable_output_while_unknown(tmp_path):
    asyncio.run(_monitor_sends_continue_for_recoverable_output_while_unknown(tmp_path))


async def _monitor_sends_continue_for_recoverable_output_while_unknown(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    delivery = FakeDelivery()
    monitor = Monitor(FakeClient("request timeout"), FakeRegistry(), delivery, state)

    await monitor.handle_status_event({"data": {"pane_id": "p-runner", "agent_status": "unknown"}})

    assert delivery.controls == [("p-runner", "continue")]


def test_monitor_respects_cooldown(tmp_path):
    asyncio.run(_monitor_respects_cooldown(tmp_path))


def test_monitor_notifies_blocked_without_recoverable_pattern(tmp_path):
    asyncio.run(_monitor_notifies_blocked_without_recoverable_pattern(tmp_path))


async def _monitor_notifies_blocked_without_recoverable_pattern(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    delivery = FakeDelivery()
    notifier = FakeNotifier()
    monitor = Monitor(FakeClient("waiting for approval"), FakeRegistry(), delivery, state, notifier=notifier)

    await monitor.handle_status_event({"data": {"pane_id": "p-runner", "agent_status": "blocked"}})

    assert delivery.controls == []
    assert notifier.notifications == [
        ("Herdr Talkback status blocked", "runner pane p-runner is blocked without a recoverable pattern")
    ]


async def _monitor_respects_cooldown(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    state.record_recovery("runner", "p-runner", "blocked", "API Error", "continue", now=1000)
    delivery = FakeDelivery()
    monitor = Monitor(
        FakeClient(),
        FakeRegistry(),
        delivery,
        state,
        cooldown_seconds=120,
        max_per_window=3,
        clock=lambda: 1050,
    )

    await monitor.handle_status_event({"data": {"pane_id": "p-runner", "agent_status": "blocked"}})

    assert delivery.controls == []


def test_monitor_cools_down_after_failed_control_attempt(tmp_path):
    asyncio.run(_monitor_cools_down_after_failed_control_attempt(tmp_path))


async def _monitor_cools_down_after_failed_control_attempt(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    delivery = FailingDelivery()
    times = iter([1000, 1050])
    monitor = Monitor(
        FakeClient(),
        FakeRegistry(),
        delivery,
        state,
        cooldown_seconds=120,
        max_per_window=3,
        clock=lambda: next(times),
    )

    await monitor.handle_status_event({"data": {"pane_id": "p-runner", "agent_status": "blocked"}})
    await monitor.handle_status_event({"data": {"pane_id": "p-runner", "agent_status": "blocked"}})

    events = list(state.tail_events())
    assert delivery.controls == [("p-runner", "continue")]
    assert not state.can_recover("p-runner", now=1050, cooldown_seconds=120, max_per_window=3, window_seconds=1800)
    assert events[-1]["kind"] == "recovery_suppressed"
    assert events[-2]["kind"] == "recovery_failed"
    assert events[-2]["payload"]["reason"] == "control not confirmed"


def test_monitor_records_unknown_after_threshold(tmp_path):
    asyncio.run(_monitor_records_unknown_after_threshold(tmp_path))


async def _monitor_records_unknown_after_threshold(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    delivery = FakeDelivery()
    notifier = FakeNotifier()
    times = iter([1000, 1070])
    monitor = Monitor(
        FakeClient("plain output"),
        FakeRegistry(),
        delivery,
        state,
        unknown_threshold_seconds=60,
        clock=lambda: next(times),
        notifier=notifier,
    )

    await monitor.handle_status_event({"data": {"pane_id": "p-runner", "agent_status": "unknown"}})
    await monitor.handle_status_event({"data": {"pane_id": "p-runner", "agent_status": "unknown"}})

    events = list(state.tail_events())
    assert events[-1]["kind"] == "pane_unknown_threshold"
    assert events[-1]["payload"]["pane_id"] == "p-runner"
    assert delivery.controls == []
    assert notifier.notifications == [
        ("Herdr Talkback status unknown", "runner pane p-runner stayed unknown for 70s")
    ]


def test_monitor_throttles_unknown_threshold_events(tmp_path):
    asyncio.run(_monitor_throttles_unknown_threshold_events(tmp_path))


async def _monitor_throttles_unknown_threshold_events(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    delivery = FakeDelivery()
    times = iter([1000, 1070, 1080])
    monitor = Monitor(
        FakeClient("plain output"),
        FakeRegistry(),
        delivery,
        state,
        unknown_threshold_seconds=60,
        clock=lambda: next(times),
    )

    await monitor.handle_status_event({"data": {"pane_id": "p-runner", "agent_status": "unknown"}})
    await monitor.handle_status_event({"data": {"pane_id": "p-runner", "agent_status": "unknown"}})
    await monitor.handle_status_event({"data": {"pane_id": "p-runner", "agent_status": "unknown"}})

    events = list(state.tail_events())
    unknown_events = [event for event in events if event["kind"] == "pane_unknown_threshold"]
    assert len(unknown_events) == 1
    assert unknown_events[0]["payload"]["pane_id"] == "p-runner"
    assert delivery.controls == []


@pytest.mark.parametrize("error", [
    "■ stream disconnected before completion: error sending request",
    "■ request timed out",
])
def test_codex_stream_disconnect_recovery(tmp_path, error):
    async def check():
        for status in ('idle', 'done', 'blocked', 'unknown', 'working'):
            state = TalkbackState(tmp_path / (status + '.db'))
            state.init()
            delivery = FakeDelivery()
            client = FakeClient('› task\n' + error + '\n› Ask Codex to do anything\n')
            now = [1000.0]
            monitor = Monitor(client, FakeRegistry(), delivery, state, clock=lambda: now[0])
            for _ in range(5):
                await monitor.handle_status_event({'data': {'pane_id': 'p-runner', 'agent_status': status}})
                now[0] += 121
            assert len(delivery.controls) == (0 if status == 'working' else 5)
            if status != 'working':
                now[0] -= 120
                await monitor.handle_status_event({'data': {'pane_id': 'p-runner', 'agent_status': status}})
                assert len(delivery.controls) == 5
    asyncio.run(check())


@pytest.mark.parametrize("error", [
    "■ stream disconnected before completion: error sending request",
    "■ request timed out",
])
def test_codex_stream_disconnect_ignores_stale_error_and_quoted_text(tmp_path, error):
    async def check():
        for index, text in enumerate((
            '› old\n' + error + '\n› continue\n• All done\n› ',
            '› Explain\n• Example: ' + error + '\n› ',
            '› task\n■ request time out\n› ',
        )):
            state = TalkbackState(tmp_path / (str(index) + '.db'))
            state.init()
            delivery = FakeDelivery()
            monitor = Monitor(FakeClient(text), FakeRegistry(), delivery, state)
            await monitor.handle_status_event({'data': {'pane_id': 'p-runner', 'agent_status': 'idle'}})
            assert delivery.controls == []
    asyncio.run(check())
