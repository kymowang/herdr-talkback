from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from .input_parser import composer_block, inspect_input, is_folded_paste_block
from .notifier import NullNotifier, notify_alert
from .state import TalkbackState
from .types import MessageState, RoutedMessage


@dataclass(frozen=True)
class DeliveryResult:
    delivered: bool
    reason: str
    attempts: int


def format_ack_instruction(message_id: str) -> str:
    return f"收到本消息后，请执行 herdr-talkback message ack {message_id} 置消息状态为delivered"


def append_ack_instruction(raw_text: str, message_id: str) -> str:
    if not message_id:
        return raw_text
    instruction = format_ack_instruction(message_id)
    if instruction in raw_text:
        return raw_text
    return f"{raw_text}\n{instruction}"


class DeliveryService:
    def __init__(
        self,
        client,
        registry,
        state: TalkbackState,
        max_attempts: int = 5,
        retry_interval_seconds: float = 30,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        notifier=None,
        manage_message_state: bool = True,
        verify_timeout_ms: int = 0,
        verify_poll_interval_seconds: float = 0.25,
        post_send_check_seconds: float = 0,
        verify_read_schedule: tuple = (5, 10, 30, 60, 120),
        stale_input_seconds: float = 0,
        stale_poll_seconds: float = 30,
        key_delay_seconds: float = 0,
        clock: Callable[[], float] = time.monotonic,
        admission_gate=None,
    ) -> None:
        self.client = client
        self.registry = registry
        self.state = state
        self.max_attempts = max_attempts
        self.retry_interval_seconds = retry_interval_seconds
        self.sleep = sleep
        self.notifier = notifier or NullNotifier()
        self.manage_message_state = manage_message_state
        self.verify_timeout_ms = verify_timeout_ms
        self.verify_poll_interval_seconds = verify_poll_interval_seconds
        self.post_send_check_seconds = post_send_check_seconds
        self.verify_read_schedule = tuple(verify_read_schedule)
        if stale_input_seconds < 0 or (stale_input_seconds > 0 and stale_poll_seconds <= 0):
            raise ValueError('invalid stale input timing')
        self.stale_input_seconds = stale_input_seconds
        self.stale_poll_seconds = stale_poll_seconds
        self.key_delay_seconds = key_delay_seconds
        self.clock = clock
        self.admission_gate = admission_gate

    async def deliver(self, message: RoutedMessage) -> DeliveryResult:
        pane_id = self.registry.resolve_pane(message.resolved_target)
        agent = self._agent_for(message.resolved_target)
        wire_text = append_ack_instruction(message.raw_text, message.message_id)
        if self.manage_message_state:
            self.state.create_message(message.message_id, message.source, message.target, pane_id, message.raw_text)

        for attempt in range(1, self.max_attempts + 1):
            self._update_message_state(message.message_id, MessageState.CHECKING_INPUT, "checking target input")
            before = await self._read_input(pane_id)
            before_state = inspect_input(before, wire_text, agent=agent)
            if before_state.kind == "dismissible_overlay":
                self.state.record_attempt(
                    message.message_id, attempt, "checking_input", "checking_input", "dismissed overlay"
                )
                await self._dismiss_overlay(pane_id)
                await self.sleep(self.verify_poll_interval_seconds)
                before = await self._read_input(pane_id)
                before_state = inspect_input(before, wire_text, agent=agent)
            if before_state.kind == "occupied" and self.stale_input_seconds > 0:
                recovered = await self._recover_stale_input(pane_id, before, message, attempt)
                if recovered == 'empty':
                    before_state = inspect_input('❯ ', wire_text, agent=agent)
                elif recovered == 'unconfirmed':
                    self._update_message_state(message.message_id, MessageState.FAILED, 'stale input submission not confirmed')
                    self.state.record_attempt(message.message_id, attempt, 'checking_input', 'failed', 'stale input submission not confirmed')
                    return DeliveryResult(False, 'stale input submission not confirmed', attempt)
            if before_state.kind == "occupied":
                if attempt < self.max_attempts:
                    self._update_message_state(
                        message.message_id, MessageState.RETRYING, "target input occupied"
                    )
                    self.state.record_attempt(
                        message.message_id, attempt, "checking_input", "retrying", "target input occupied"
                    )
                    await self.sleep(self.retry_interval_seconds)
                    continue

                self._update_message_state(
                    message.message_id, MessageState.BLOCKED_BY_INPUT, "target input occupied"
                )
                self.state.record_attempt(
                    message.message_id, attempt, "checking_input", "blocked_by_input", "target input occupied"
                )
                await self._notify_delivery_failed(message, attempt, "target input occupied")
                return DeliveryResult(False, "target input occupied", attempt)
            if before_state.kind == "unknown":
                # Cannot read the composer at all: report it instead of writing blind.
                self._update_message_state(message.message_id, MessageState.FAILED, "pane input unreadable")
                self.state.record_attempt(
                    message.message_id, attempt, "checking_input", "failed", "pane input unreadable"
                )
                await self._notify_delivery_failed(message, attempt, "pane input unreadable")
                return DeliveryResult(False, "pane input unreadable", attempt)
            if before_state.kind == "residue":
                self._update_message_state(message.message_id, MessageState.VERIFYING, "flushing residue")
                self.state.record_attempt(
                    message.message_id, attempt, "checking_input", "verifying", "flushing residue"
                )
                return await self._flush_message_residue(
                    message.message_id, pane_id, wire_text, attempt, agent=agent)

            self._update_message_state(message.message_id, MessageState.SENDING, "sending message")
            await self._write(pane_id, wire_text)
            self.state.record_attempt(message.message_id, attempt, "sending", "verifying", "pane.send_input sent")
            self._update_message_state(message.message_id, MessageState.VERIFYING, "verifying delivery")

            verified = await self._verify(
                pane_id, wire_text, agent=agent, message_id=message.message_id
            )
            if verified == "acknowledged":
                return DeliveryResult(True, "acknowledged by recipient", attempt)
            if verified == "delivered":
                self._update_message_state(message.message_id, MessageState.DELIVERED, "input cleared")
                return DeliveryResult(True, "input cleared", attempt)
            if verified == "residue":
                await self._notify_stuck_input_submitted(message, attempt)
                return await self._flush_message_residue(
                    message.message_id, pane_id, wire_text, attempt, agent=agent)

            if self._is_delivered(message.message_id):
                return DeliveryResult(True, "acknowledged by recipient", attempt)
            if verified == "unreadable":
                self._update_message_state(message.message_id, MessageState.FAILED, "pane input unreadable")
                self.state.record_attempt(
                    message.message_id, attempt, "verifying", "failed", "pane input unreadable")
                await self._notify_delivery_failed(message, attempt, "pane input unreadable")
                return DeliveryResult(False, "pane input unreadable", attempt)
            # The message was written exactly once: an unconfirmable result must
            # never be resent, or a busy pane receives the same text repeatedly.
            self._update_message_state(message.message_id, MessageState.FAILED, "delivery ambiguous")
            self.state.record_attempt(
                message.message_id, attempt, "verifying", "failed", "delivery ambiguous")
            await self._notify_delivery_failed(message, attempt, "delivery ambiguous")
            return DeliveryResult(False, "delivery ambiguous", attempt)

        self._update_message_state(message.message_id, MessageState.FAILED, "delivery ambiguous")
        await self._notify_delivery_failed(message, self.max_attempts, "delivery ambiguous")
        return DeliveryResult(False, "delivery ambiguous", self.max_attempts)

    async def send_control(self, pane_id: str, text: str, *, strict: bool = False) -> DeliveryResult:
        if self.admission_gate is not None:
            async with self.admission_gate.lock:
                if self.admission_gate.paused:
                    return DeliveryResult(False, 'Talkback is updating', 0)
                return await self._send_control_unlocked(pane_id, text, strict=strict)
        return await self._send_control_unlocked(pane_id, text, strict=strict)

    async def _send_control_unlocked(self, pane_id: str, text: str, *, strict: bool = False) -> DeliveryResult:
        before = await self._read_input(pane_id)
        before_state = inspect_input(before, text)
        if strict and before_state.kind != "empty":
            return DeliveryResult(False, "target input is not empty", 0)
        if before_state.kind == "occupied":
            return DeliveryResult(False, "target input occupied", 0)
        if before_state.kind == "unknown":
            return DeliveryResult(False, "target input unknown", 0)
        if before_state.kind == "residue":
            return await self._flush_control_residue(pane_id, text)

        await self._write(pane_id, text)
        if strict:
            verified = await self._verify(pane_id, text, timeout_ms=max(500, self.verify_timeout_ms))
            if verified == 'delivered':
                return DeliveryResult(True, 'control sent', 1)
            if verified == 'residue':
                return await self._flush_control_residue(pane_id, text)
            return DeliveryResult(False, 'control not confirmed', 1)
        after = await self._read_input(pane_id)
        after_state = inspect_input(after, text)
        if after_state.kind == "empty":
            return DeliveryResult(True, "control sent", 1)
        if after_state.kind == "residue":
            return await self._flush_control_residue(pane_id, text)
        return DeliveryResult(False, "control not confirmed", 1)

    def _agent_for(self, role: str) -> Optional[str]:
        getter = getattr(self.registry, "get_agent", None)
        if getter is None:
            return None
        try:
            return getattr(getter(role), "agent", None)
        except Exception:
            return None

    async def _write(self, pane_id: str, text: str) -> None:
        """Type the text, then submit it.

        Pressing Enter in the same write can be swallowed by a CLI that treats
        the whole write as a paste, leaving the message in the composer as a
        trailing newline. key_delay_seconds > 0 writes and submits separately.
        """
        if self.key_delay_seconds <= 0:
            await self.client.request(
                "pane.send_input", {"pane_id": pane_id, "text": text, "keys": ["enter"]}
            )
            return
        await self.client.request("pane.send_input", {"pane_id": pane_id, "text": text})
        await self.sleep(self.key_delay_seconds)
        await self.client.request(
            "pane.send_input", {"pane_id": pane_id, "text": "", "keys": ["enter"]}
        )

    async def _read_input(self, pane_id: str) -> str:
        source = "visible"
        scroll = await self._scroll_offset(pane_id)
        if scroll:
            # The viewport is parked away from the live composer, so the visible
            # screen holds transcript text instead of the input box. Reading it
            # reports unrelated history as the draft (or nothing as unreadable).
            # `recent` renders the tail of the session regardless of scroll, so
            # the composer is readable while the user keeps their scroll position.
            source = "recent"
        result = await self.client.request(
            "pane.read", {"pane_id": pane_id, "source": source, "format": "ansi"}
        )
        return result["read"]["text"]

    async def _scroll_offset(self, pane_id: str) -> int:
        try:
            result = await self.client.request("pane.get", {"pane_id": pane_id})
        except Exception:
            # Older servers may not expose pane.get; fall back to the visible read.
            return 0
        pane = result.get("pane") if isinstance(result, dict) else None
        scroll = pane.get("scroll") if isinstance(pane, dict) else None
        offset = scroll.get("offset_from_bottom") if isinstance(scroll, dict) else None
        return offset if isinstance(offset, int) and offset > 0 else 0

    async def _recover_stale_input(self, pane_id, initial, message, attempt):
        # Only the composer counts: status lines, token counters and spinners
        # change constantly while an agent works and must not reset the timer.
        # An occupied composer that stays unchanged for the whole window is
        # stuck, so submit it even when the CLI folded its content.
        def drainable(screen):
            return inspect_input(screen, '').kind == 'occupied'

        if not drainable(initial):
            return 'unavailable'
        previous = composer_block(initial)
        stable_since = self.clock()
        while True:
            remaining = self.stale_input_seconds - (self.clock() - stable_since)
            if remaining > 0:
                await self.sleep(min(self.stale_poll_seconds, remaining))
            current = await self._read_input(pane_id)
            if inspect_input(current, '').kind == 'empty':
                return 'empty'
            if not drainable(current):
                return 'unavailable'
            current_text = composer_block(current)
            if current_text != previous:
                previous = current_text
                stable_since = self.clock()
                continue
            if self.clock() - stable_since < self.stale_input_seconds:
                continue
            # One last read ensures no observable edit occurred after the timer.
            final = await self._read_input(pane_id)
            if composer_block(final) != previous or not drainable(final):
                return 'unavailable'
            self.state.record_attempt(message.message_id, attempt, 'checking_input', 'checking_input', 'stale input submitted')
            await self.client.request('pane.send_input', {'pane_id': pane_id, 'text': '', 'keys': ['enter']})
            await notify_alert(self.notifier, 'Herdr Talkback submitted stale input',
                               f'{message.target} input stayed unchanged and was submitted',
                               'delivery_stale_input_submitted',
                               {'message_id': message.message_id, 'target': message.target,
                                'pane_id': pane_id, 'stable_seconds': self.stale_input_seconds})
            await self.sleep(self.verify_poll_interval_seconds)
            after = await self._read_input(pane_id)
            return 'empty' if inspect_input(after, '').kind == 'empty' else 'unconfirmed'

    def _is_delivered(self, message_id: Optional[str]) -> bool:
        if not message_id:
            return False
        getter = getattr(self.state, "get_message", None)
        if getter is None:
            return False
        try:
            row = getter(message_id)
        except KeyError:
            return False
        return row.get("state") == MessageState.DELIVERED.value

    def _update_message_state(self, message_id: str, state: MessageState, reason: str) -> None:
        if self.manage_message_state:
            self.state.update_message_state(message_id, state, reason)

    async def _dismiss_overlay(self, pane_id: str) -> None:
        await self.client.request("pane.send_input", {"pane_id": pane_id, "text": "", "keys": ["esc"]})

    async def _verify(
        self,
        pane_id: str,
        expected_message: str,
        timeout_ms=None,
        agent=None,
        message_id: Optional[str] = None,
    ) -> str:
        if timeout_ms is not None:
            return await self._verify_polling(
                pane_id, expected_message, timeout_ms, agent, message_id=message_id
            )
        # Re-read the composer on a fixed schedule. The input box is the only
        # evidence that matters: unusable screens are an error, never a resend.
        schedule = tuple(self.verify_read_schedule)
        last_kind = "unknown"
        for delay in schedule:
            if self._is_delivered(message_id):
                return "acknowledged"
            await self.sleep(delay)
            if self._is_delivered(message_id):
                return "acknowledged"
            after = await self._read_input(pane_id)
            state = inspect_input(after, expected_message, allow_transformed_residue=True, agent=agent)
            last_kind = state.kind
            if state.kind == "submitted":
                return "delivered"
            if state.kind == "empty":
                return "delivered"
            if state.kind == "residue":
                return "residue"
        if self._is_delivered(message_id):
            return "acknowledged"
        return "unreadable" if last_kind == "unknown" else "ambiguous"

    async def _verify_polling(
        self,
        pane_id: str,
        expected_message: str,
        timeout_ms,
        agent,
        message_id: Optional[str] = None,
    ) -> str:
        if self._is_delivered(message_id):
            return "acknowledged"
        after = await self._read_input(pane_id)
        state = inspect_input(after, expected_message, allow_transformed_residue=True, agent=agent)
        if state.kind == "submitted":
            return "delivered"
        timeout_seconds = (self.verify_timeout_ms if timeout_ms is None else timeout_ms) / 1000
        if state.kind == "residue" and (timeout_seconds <= 0 or after.rfind("›") <= after.rfind("❯")):
            return "residue"
        if state.kind not in {"empty", "residue"}:
            return "ambiguous"
        if timeout_seconds <= 0:
            return "delivered"

        elapsed_seconds = 0.0
        poll_interval = self.verify_poll_interval_seconds if self.verify_poll_interval_seconds > 0 else timeout_seconds
        while elapsed_seconds < timeout_seconds:
            delay = min(poll_interval, timeout_seconds - elapsed_seconds)
            await self.sleep(delay)
            elapsed_seconds += delay
            if self._is_delivered(message_id):
                return "acknowledged"
            after = await self._read_input(pane_id)
            state = inspect_input(after, expected_message, allow_transformed_residue=True, agent=agent)
            if state.kind == "submitted":
                return "delivered"
            if state.kind == "empty":
                continue
            if state.kind == "residue":
                if after.rfind("›") > after.rfind("❯"):
                    continue
                return "residue"
            return "ambiguous"
        return "residue" if state.kind == "residue" else "delivered"

    async def _flush_message_residue(
        self, message_id: str, pane_id: str, expected_message: str, attempt: int, agent=None
    ) -> DeliveryResult:
        if self._is_delivered(message_id):
            return DeliveryResult(True, "acknowledged by recipient", attempt)
        await self.client.request("pane.send_input", {"pane_id": pane_id, "text": "", "keys": ["enter"]})
        # A folded draft clears only after the CLI repaints the composer, so a
        # single immediate read reports residue that is already being submitted.
        deadline_seconds = max(self.verify_timeout_ms, 1000) / 1000
        waited = 0.0
        polls = 0
        raw = await self._read_input(pane_id)
        state = inspect_input(raw, expected_message, allow_transformed_residue=True, agent=agent)
        # Every CLI needs a beat to repaint after Enter; a folded draft needs
        # the full timeout because the fold may collapse late.
        while state.kind == "residue" and waited < deadline_seconds:
            if self._is_delivered(message_id):
                return DeliveryResult(True, "acknowledged by recipient", attempt)
            if not is_folded_paste_block(raw) and polls >= 2:
                break
            await self.sleep(self.verify_poll_interval_seconds)
            waited += self.verify_poll_interval_seconds
            polls += 1
            raw = await self._read_input(pane_id)
            state = inspect_input(raw, expected_message, allow_transformed_residue=True, agent=agent)
        if self._is_delivered(message_id):
            return DeliveryResult(True, "acknowledged by recipient", attempt)
        if state.kind in {"empty", "submitted"}:
            self._update_message_state(message_id, MessageState.DELIVERED, "residue flushed")
            return DeliveryResult(True, "residue flushed", attempt)

        self._update_message_state(message_id, MessageState.FAILED, "message residue not cleared")
        self.state.record_attempt(message_id, attempt, "verifying", "failed", "message residue not cleared")
        return DeliveryResult(False, "message residue not cleared", attempt)

    async def _flush_control_residue(self, pane_id: str, text: str) -> DeliveryResult:
        await self.client.request("pane.send_input", {"pane_id": pane_id, "text": "", "keys": ["enter"]})
        after = await self._read_input(pane_id)
        after_state = inspect_input(after, text)
        if after_state.kind == "empty":
            return DeliveryResult(True, "control sent", 1)
        return DeliveryResult(False, "control residue not cleared", 1)

    async def _notify_delivery_failed(self, message: RoutedMessage, attempts: int, reason: str) -> None:
        await notify_alert(
            self.notifier,
            "Herdr Talkback delivery blocked",
            f"{message.source} -> {message.target} failed after {attempts} attempts: {reason}",
            "delivery_failed",
            {
                "message_id": message.message_id,
                "source": message.source,
                "target": message.target,
                "resolved_target": message.resolved_target,
                "attempts": attempts,
                "reason": reason,
            },
        )

    async def _notify_stuck_input_submitted(self, message: RoutedMessage, attempt: int) -> None:
        await notify_alert(
            self.notifier,
            "Herdr Talkback submitted stuck input",
            f"{message.source} -> {message.target} remained in the input after send; sent Enter to submit",
            "delivery_stuck_input_submitted",
            {
                "message_id": message.message_id,
                "source": message.source,
                "target": message.target,
                "resolved_target": message.resolved_target,
                "attempt": attempt,
                "reason": "message remained in input after send",
            },
        )
