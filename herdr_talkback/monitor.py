from __future__ import annotations

import re
import time
from typing import Callable, Optional

from .notifier import NullNotifier, notify_alert
from .state import TalkbackState
from .rules import RecoveryRule
from .config import default_rules
from .rules import parse_recovery_rules


PROMPT_RE = re.compile(r"(?m)^[❯›]")


class Monitor:
    def __init__(
        self,
        client,
        registry,
        delivery,
        state: TalkbackState,
        cooldown_seconds: int = 120,
        unknown_threshold_seconds: int = 60,
        max_per_window: int = 3,
        clock: Callable[[], float] = time.time,
        notifier=None,
        rules=None,
    ) -> None:
        self.rules = parse_recovery_rules(default_rules()["recovery_rules"]) if rules is None else rules
        self.client = client
        self.registry = registry
        self.delivery = delivery
        self.state = state
        self.cooldown_seconds = cooldown_seconds
        self.unknown_threshold_seconds = unknown_threshold_seconds
        self.max_per_window = max_per_window
        self.clock = clock
        self.notifier = notifier or NullNotifier()
        self._unknown_since: dict[str, float] = {}

    async def handle_status_event(self, event: dict) -> None:
        data = event.get("data", {})
        pane_id = data.get("pane_id")
        status = data.get("agent_status")
        if not pane_id:
            return
        role = self.registry.role_for_pane(pane_id)
        if role is None:
            return

        text = await self._read_recent(pane_id)
        current_status = status or "unknown"
        get_pane = getattr(self.registry, 'get_pane', lambda _: None)
        info = get_pane(pane_id)
        agent_type = getattr(info, 'agent', None)
        rule = self._find_rule(text, current_status, role, agent_type)
        if rule is not None:
            self._unknown_since.pop(pane_id, None)
            await self._recover(role, pane_id, current_status, rule)
            return

        if status == "unknown":
            await self._handle_unknown(role, pane_id)
            return

        self._unknown_since.pop(pane_id, None)
        if status == "blocked":
            self.state.record_event("blocked_without_recoverable_pattern", {"pane_id": pane_id, "role": role})
            await notify_alert(
                self.notifier,
                "Herdr Talkback status blocked",
                f"{role} pane {pane_id} is blocked without a recoverable pattern",
                "status_blocked",
                {"pane_id": pane_id, "role": role},
            )

    async def _recover(self, role: str, pane_id: str, status: str, rule: RecoveryRule) -> None:
        now = self.clock()
        pattern = rule.id
        cap = self.max_per_window if rule.max_per_30m is None else rule.max_per_30m
        max_per_window = None if cap == 0 else cap
        cooldown = self.cooldown_seconds if rule.cooldown_seconds is None else rule.cooldown_seconds
        if not self.state.can_recover(pane_id, now, cooldown, max_per_window, 1800):
            self.state.record_event("recovery_suppressed", {"pane_id": pane_id, "role": role, "pattern": pattern})
            return
        result = await self.delivery.send_control(pane_id, rule.command)
        self.state.record_recovery(role, pane_id, status, pattern, rule.command, now=now)
        await notify_alert(
            self.notifier,
            f"Herdr Talkback recovered {role}",
            f"{role} matched {pattern} while {status}; sent {rule.command}",
            "recovery_sent",
            {"pane_id": pane_id, "role": role, "pattern": pattern, "status": status},
        )
        if not result.delivered:
            self.state.record_event(
                "recovery_failed",
                {
                    "pane_id": pane_id,
                    "role": role,
                    "pattern": pattern,
                    "reason": getattr(result, "reason", ""),
                    "attempts": getattr(result, "attempts", 0),
                },
            )

    async def _read_recent(self, pane_id: str) -> str:
        result = await self.client.request(
            "pane.read", {"pane_id": pane_id, "source": "recent", "lines": 80, "format": "text"}
        )
        return result["read"]["text"]

    def _find_rule(self, text, status, name, agent_type):
        for rule in self.rules:
            window = self._latest_turn_window(text) if rule.window == 'latest_turn' else text
            if rule.matches(name, agent_type, status, window):
                return rule
        return None

    def _latest_turn(self, text: str) -> str:
        prompts = list(PROMPT_RE.finditer(text))
        if len(prompts) < 2:
            return ""
        return text[prompts[-2].start() : prompts[-1].start()]

    def _latest_turn_window(self, text: str) -> str:
        latest_turn = self._latest_turn(text)
        if latest_turn:
            return latest_turn
        prompts = list(PROMPT_RE.finditer(text))
        if len(prompts) == 1:
            return text[: prompts[-1].start()]
        return ""

    async def _handle_unknown(self, role: str, pane_id: str) -> None:
        now = self.clock()
        first_seen = self._unknown_since.setdefault(pane_id, now)
        if now - first_seen >= self.unknown_threshold_seconds:
            self.state.record_event(
                "pane_unknown_threshold", {"pane_id": pane_id, "role": role, "seconds": now - first_seen}
            )
            await notify_alert(
                self.notifier,
                "Herdr Talkback status unknown",
                f"{role} pane {pane_id} stayed unknown for {now - first_seen:.0f}s",
                "status_unknown",
                {"pane_id": pane_id, "role": role, "seconds": now - first_seen},
            )
            self._unknown_since[pane_id] = now
