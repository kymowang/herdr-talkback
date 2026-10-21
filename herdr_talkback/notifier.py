from __future__ import annotations

import time
from typing import Any, Optional


class NullNotifier:
    async def notify(self, title: str, body: str) -> None:
        return None


class HerdrNotifier:
    def __init__(self, client, enabled: bool = True, position: str = "bottom-right", sound: str = "none") -> None:
        self.client = client
        self.enabled = enabled
        self.position = position
        self.sound = sound

    async def notify(self, title: str, body: str) -> None:
        if not self.enabled:
            return
        await self.client.request(
            "notification.show",
            {
                "title": title,
                "body": body,
                "position": self.position,
                "sound": self.sound,
            },
        )


class AlertingNotifier:
    def __init__(self, notifier, state, client=None, registry=None, pane_role: Optional[str] = None) -> None:
        self.notifier = notifier
        self.state = state
        self.client = client
        self.registry = registry
        self.pane_role = pane_role

    async def notify(
        self,
        title: str,
        body: str,
        kind: str = "notification",
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        alert_id = self.state.record_alert(kind, title, body, payload or {})
        if self.pane_role and self.client is not None and self.registry is not None:
            try:
                await self._send_to_alert_pane(alert_id, title, body)
            except Exception:
                pass
        try:
            await self.notifier.notify(title, body)
        except Exception:
            pass

    async def _send_to_alert_pane(self, alert_id: int, title: str, body: str) -> None:
        pane_id = self.registry.resolve_pane(self.pane_role)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        text = f"[herdr-talkback alert #{alert_id}] {timestamp}\n{title}\n{body}"
        await self.client.request("pane.send_input", {"pane_id": pane_id, "text": text, "keys": ["enter"]})


async def notify_alert(notifier, title: str, body: str, kind: str, payload: dict[str, Any]) -> None:
    try:
        await notifier.notify(title, body, kind=kind, payload=payload)
    except TypeError:
        await notifier.notify(title, body)
