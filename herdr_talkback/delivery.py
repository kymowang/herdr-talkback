from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

from .input_parser import inspect_input
from .notifier import NullNotifier, notify_alert
from .state import TalkbackState
from .types import MessageState, RoutedMessage


@dataclass(frozen=True)
class DeliveryResult:
    delivered: bool
    reason: str
    attempts: int


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

    async def deliver(self, message: RoutedMessage) -> DeliveryResult:
        pane_id = self.registry.resolve_pane(message.resolved_target)
        if self.manage_message_state:
            self.state.create_message(message.message_id, message.source, message.target, pane_id, message.raw_text)

        for attempt in range(1, self.max_attempts + 1):
            self._update_message_state(message.message_id, MessageState.CHECKING_INPUT, "checking target input")
            before = await self._read_input(pane_id)
            before_state = inspect_input(before, message.raw_text)
            if before_state.kind == "dismissible_overlay":
                self.state.record_attempt(
                    message.message_id, attempt, "checking_input", "checking_input", "dismissed overlay"
                )
                await self._dismiss_overlay(pane_id)
                await self.sleep(self.verify_poll_interval_seconds)
                before = await self._read_input(pane_id)
                before_state = inspect_input(before, message.raw_text)
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
                if attempt < self.max_attempts:
                    self._update_message_state(
                        message.message_id, MessageState.RETRYING, "target input unknown"
                    )
                    self.state.record_attempt(
                        message.message_id, attempt, "checking_input", "retrying", "target input unknown"
                    )
                    await self.sleep(self.retry_interval_seconds)
                    continue

                self._update_message_state(message.message_id, MessageState.FAILED, "target input unknown")
                self.state.record_attempt(
                    message.message_id, attempt, "checking_input", "failed", "target input unknown"
                )
                await self._notify_delivery_failed(message, attempt, "target input unknown")
                return DeliveryResult(False, "target input unknown", attempt)
            if before_state.kind == "residue":
                self._update_message_state(message.message_id, MessageState.VERIFYING, "flushing residue")
                self.state.record_attempt(
                    message.message_id, attempt, "checking_input", "verifying", "flushing residue"
                )
                return await self._flush_message_residue(message.message_id, pane_id, message.raw_text, attempt)

            self._update_message_state(message.message_id, MessageState.SENDING, "sending message")
            await self.client.request(
                "pane.send_input", {"pane_id": pane_id, "text": message.raw_text, "keys": ["enter"]}
            )
            self.state.record_attempt(message.message_id, attempt, "sending", "verifying", "pane.send_input sent")
            self._update_message_state(message.message_id, MessageState.VERIFYING, "verifying delivery")

            verified = await self._verify(pane_id, message.raw_text)
            if verified == "delivered":
                self._update_message_state(message.message_id, MessageState.DELIVERED, "input cleared")
                return DeliveryResult(True, "input cleared", attempt)
            if verified == "residue":
                await self._notify_stuck_input_submitted(message, attempt)
                return await self._flush_message_residue(message.message_id, pane_id, message.raw_text, attempt)

            self._update_message_state(message.message_id, MessageState.RETRYING, "delivery ambiguous")
            self.state.record_attempt(message.message_id, attempt, "verifying", "retrying", "delivery ambiguous")
            if attempt < self.max_attempts:
                await self.sleep(self.retry_interval_seconds)
                continue

            self.state.update_message_state(message.message_id, MessageState.FAILED, "delivery ambiguous")
            self.state.record_attempt(message.message_id, attempt, "retrying", "failed", "delivery ambiguous")
            await self._notify_delivery_failed(message, attempt, "delivery ambiguous")
            return DeliveryResult(False, "delivery ambiguous", attempt)

        self._update_message_state(message.message_id, MessageState.FAILED, "delivery ambiguous")
        await self._notify_delivery_failed(message, self.max_attempts, "delivery ambiguous")
        return DeliveryResult(False, "delivery ambiguous", self.max_attempts)

    async def send_control(self, pane_id: str, text: str) -> DeliveryResult:
        before = await self._read_input(pane_id)
        before_state = inspect_input(before, text)
        if before_state.kind == "occupied":
            return DeliveryResult(False, "target input occupied", 0)
        if before_state.kind == "unknown":
            return DeliveryResult(False, "target input unknown", 0)
        if before_state.kind == "residue":
            return await self._flush_control_residue(pane_id, text)

        await self.client.request("pane.send_input", {"pane_id": pane_id, "text": text, "keys": ["enter"]})
        after = await self._read_input(pane_id)
        after_state = inspect_input(after, text)
        if after_state.kind == "empty":
            return DeliveryResult(True, "control sent", 1)
        if after_state.kind == "residue":
            return await self._flush_control_residue(pane_id, text)
        return DeliveryResult(False, "control not confirmed", 1)

    async def _read_input(self, pane_id: str) -> str:
        result = await self.client.request(
            "pane.read", {"pane_id": pane_id, "source": "recent", "lines": 80, "format": "ansi"}
        )
        return result["read"]["text"]

    def _update_message_state(self, message_id: str, state: MessageState, reason: str) -> None:
        if self.manage_message_state:
            self.state.update_message_state(message_id, state, reason)

    async def _dismiss_overlay(self, pane_id: str) -> None:
        await self.client.request("pane.send_input", {"pane_id": pane_id, "text": "", "keys": ["esc"]})

    async def _verify(self, pane_id: str, expected_message: str) -> str:
        after = await self._read_input(pane_id)
        state = inspect_input(after, expected_message, allow_transformed_residue=True)
        if state.kind == "residue":
            return "residue"
        if state.kind != "empty":
            return "ambiguous"

        timeout_seconds = self.verify_timeout_ms / 1000
        if timeout_seconds <= 0:
            return "delivered"

        elapsed_seconds = 0.0
        poll_interval = self.verify_poll_interval_seconds if self.verify_poll_interval_seconds > 0 else timeout_seconds
        while elapsed_seconds < timeout_seconds:
            delay = min(poll_interval, timeout_seconds - elapsed_seconds)
            await self.sleep(delay)
            elapsed_seconds += delay

            after = await self._read_input(pane_id)
            state = inspect_input(after, expected_message, allow_transformed_residue=True)
            if state.kind == "empty":
                continue
            if state.kind == "residue":
                return "residue"
            return "ambiguous"

        return "delivered"

    async def _flush_message_residue(
        self, message_id: str, pane_id: str, expected_message: str, attempt: int
    ) -> DeliveryResult:
        await self.client.request("pane.send_input", {"pane_id": pane_id, "text": "", "keys": ["enter"]})
        verified_after_flush = await self._verify(pane_id, expected_message)
        if verified_after_flush == "delivered":
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
