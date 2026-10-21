import asyncio

from herdr_talkback.notifier import AlertingNotifier
from herdr_talkback.state import TalkbackState


class FakeNotifier:
    def __init__(self):
        self.notifications = []

    async def notify(self, title, body):
        self.notifications.append((title, body))


class FakeClient:
    def __init__(self):
        self.requests = []

    async def request(self, method, params):
        self.requests.append((method, params))
        return {"ok": True}


class FakeRegistry:
    def resolve_pane(self, role):
        assert role == "alerts"
        return "p-alerts"


def test_alerting_notifier_records_alert_and_delegates(tmp_path):
    asyncio.run(_alerting_notifier_records_alert_and_delegates(tmp_path))


async def _alerting_notifier_records_alert_and_delegates(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    notifier = FakeNotifier()

    await AlertingNotifier(notifier, state).notify("Herdr Talkback status unknown", "runner unknown")

    alerts = state.list_open_alerts()
    assert alerts[0]["title"] == "Herdr Talkback status unknown"
    assert alerts[0]["body"] == "runner unknown"
    assert notifier.notifications == [("Herdr Talkback status unknown", "runner unknown")]


def test_alerting_notifier_optionally_writes_to_alert_pane(tmp_path):
    asyncio.run(_alerting_notifier_optionally_writes_to_alert_pane(tmp_path))


async def _alerting_notifier_optionally_writes_to_alert_pane(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    client = FakeClient()

    await AlertingNotifier(
        FakeNotifier(),
        state,
        client=client,
        registry=FakeRegistry(),
        pane_role="alerts",
    ).notify("Herdr Talkback delivery blocked", "lead -> runner failed")

    assert client.requests
    method, params = client.requests[0]
    assert method == "pane.send_input"
    assert params["pane_id"] == "p-alerts"
    assert "Herdr Talkback delivery blocked" in params["text"]
    assert "lead -> runner failed" in params["text"]
