import asyncio
import itertools
import time

import pytest

import herdr_talkback.core_types as core_types
from herdr_talkback.addressing import EndpointAddress
from herdr_talkback.core_dispatcher import CoreDispatcher
from herdr_talkback.core_types import (
    AdapterDeliveryResult,
    AdapterDeliveryState,
    CoreMessage,
    CoreMessageState,
    EndpointDescriptor,
)
from herdr_talkback.endpoints import EndpointRegistry
from herdr_talkback.state import TalkbackState


def test_delivery_lease_accept_is_idempotent():
    reasons = []
    lease = core_types.DeliveryLease(
        message_id="msg-1",
        lease_id="lease-1",
        adapter="fake",
        accept_callback=lambda reason: reasons.append(reason) or True,
    )

    assert lease.accepted is False
    assert lease.accept("durably stored") is True
    assert lease.accept("duplicate") is True
    assert lease.accepted is True
    assert reasons == ["durably stored"]


def test_blocked_endpoint_does_not_block_other_endpoint(tmp_path):
    asyncio.run(_blocked_endpoint_does_not_block_other_endpoint(tmp_path))


async def _blocked_endpoint_does_not_block_other_endpoint(tmp_path):
    state, registry = configured_state_and_registry(tmp_path)
    adapter = BlockingByTargetAdapter("linux-main/runner")
    dispatcher = CoreDispatcher(
        state,
        registry,
        {"fake": adapter},
        lease_id_factory=lease_ids(),
    )
    enqueue(state, "stuck", "linux-main/runner")
    enqueue(state, "free", "linux-main/dev-back")

    dispatcher.wake("linux-main/runner")
    await adapter.block_started.wait()
    dispatcher.wake("linux-main/dev-back")
    await asyncio.wait_for(adapter.delivered("free"), timeout=0.5)

    assert state.get_message("free")["state"] == "delivered"
    adapter.release.set()
    await dispatcher.drain()
    assert adapter.message_ids == ["stuck", "free"]


def test_recover_schedules_persisted_queued_messages(tmp_path):
    asyncio.run(_recover_schedules_persisted_queued_messages(tmp_path))


async def _recover_schedules_persisted_queued_messages(tmp_path):
    state, registry = configured_state_and_registry(tmp_path)
    adapter = RecordingAdapter()
    enqueue(state, "persisted", "linux-main/dev-back")
    dispatcher = CoreDispatcher(
        state,
        registry,
        {"fake": adapter},
        lease_id_factory=lease_ids(),
    )

    await dispatcher.recover()
    await dispatcher.drain()

    assert adapter.message_ids == ["persisted"]
    assert state.get_message("persisted")["state"] == "delivered"


def test_same_endpoint_is_delivered_fifo(tmp_path):
    asyncio.run(_same_endpoint_is_delivered_fifo(tmp_path))


async def _same_endpoint_is_delivered_fifo(tmp_path):
    state, registry = configured_state_and_registry(tmp_path)
    adapter = RecordingAdapter()
    dispatcher = CoreDispatcher(state, registry, {"fake": adapter}, lease_ids())
    enqueue(state, "first", "linux-main/dev-back")
    enqueue(state, "second", "linux-main/dev-back")

    dispatcher.wake("linux-main/dev-back")
    await dispatcher.drain()

    assert adapter.message_ids == ["first", "second"]
    assert state.get_message("first")["state"] == "delivered"
    assert state.get_message("second")["state"] == "delivered"


def test_dispatcher_keeps_message_offered_until_adapter_accepts(tmp_path):
    asyncio.run(_dispatcher_keeps_message_offered_until_adapter_accepts(tmp_path))


async def _dispatcher_keeps_message_offered_until_adapter_accepts(tmp_path):
    state, registry = configured_state_and_registry(tmp_path)
    adapter = InspectingAdapter(
        state,
        accept=False,
        result=AdapterDeliveryResult("deferred", "node offline", 0),
    )
    dispatcher = CoreDispatcher(state, registry, {"fake": adapter}, lease_ids())
    enqueue(state, "msg-1", "linux-main/dev-back")

    dispatcher.wake("linux-main/dev-back")
    await dispatcher.drain()

    assert adapter.states_at_call == ["offered"]
    row = state.get_message("msg-1")
    assert row["state"] == "waiting_endpoint"
    assert row["lease_id"] is None
    assert row["lease_expires_at"] is None
    assert AdapterDeliveryState.DEFERRED.value == "deferred"


def test_online_deferred_delivery_retries_without_external_wake(tmp_path):
    asyncio.run(_online_deferred_delivery_retries_without_external_wake(tmp_path))


async def _online_deferred_delivery_retries_without_external_wake(tmp_path):
    state, registry = configured_state_and_registry(tmp_path)
    adapter = DeferredOnceAdapter()
    dispatcher = CoreDispatcher(
        state,
        registry,
        {"fake": adapter},
        lease_ids(),
        retry_delay_seconds=0.02,
    )
    enqueue(state, "retry-online", "linux-main/dev-back")

    dispatcher.wake("linux-main/dev-back")
    await adapter.first_deferred.wait()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert adapter.message_ids == ["retry-online"]

    await asyncio.wait_for(adapter.delivered_event.wait(), timeout=0.5)
    await dispatcher.drain()

    assert adapter.message_ids == ["retry-online", "retry-online"]
    assert state.get_message("retry-online")["state"] == "delivered"
    await dispatcher.close()


def test_wake_during_active_delivery_is_latched_for_retired_endpoint(tmp_path):
    asyncio.run(_wake_during_active_delivery_is_latched_for_retired_endpoint(tmp_path))


async def _wake_during_active_delivery_is_latched_for_retired_endpoint(tmp_path):
    state, registry = configured_state_and_registry(tmp_path)
    adapter = BlockingDeferredAdapter()
    dispatcher = CoreDispatcher(state, registry, {"fake": adapter}, lease_ids())
    enqueue(state, "retired-during-delivery", "linux-main/dev-back")

    dispatcher.wake("linux-main/dev-back")
    await asyncio.wait_for(adapter.started.wait(), timeout=0.5)
    registry.register_remote("linux-main", [endpoint("runner", "online")])
    assert registry.resolve("linux-main/dev-back", "linux-main").status == "retired"
    dispatcher.wake("linux-main/dev-back")
    adapter.release.set()

    await asyncio.wait_for(dispatcher.drain(), timeout=0.5)

    row = state.get_message("retired-during-delivery")
    assert row["state"] == "failed"
    assert row["reason"] == "endpoint retired"
    await dispatcher.close()


def test_adapter_accept_callback_owns_offered_to_accepted_transition(tmp_path):
    asyncio.run(_adapter_accept_callback_owns_offered_to_accepted_transition(tmp_path))


async def _adapter_accept_callback_owns_offered_to_accepted_transition(tmp_path):
    state, registry = configured_state_and_registry(tmp_path)
    adapter = InspectingAdapter(
        state,
        accept=True,
        result=AdapterDeliveryResult(AdapterDeliveryState.DELIVERED, "done", 1),
    )
    dispatcher = CoreDispatcher(state, registry, {"fake": adapter}, lease_ids())
    enqueue(state, "msg-1", "linux-main/dev-back")

    dispatcher.wake("linux-main/dev-back")
    await dispatcher.drain()

    assert adapter.states_at_call == ["offered"]
    assert adapter.accepted_state == "accepted"
    assert adapter.accept_result is True
    row = state.get_message("msg-1")
    assert row["state"] == "delivered"
    assert row["accepted_by_adapter"] == "fake"


@pytest.mark.parametrize(
    ("adapter_state", "expected_state"),
    [
        (AdapterDeliveryState.DELIVERED, "delivered"),
        (AdapterDeliveryState.FAILED, "failed"),
        (AdapterDeliveryState.DELIVERY_UNKNOWN, "delivery_unknown"),
    ],
)
def test_adapter_terminal_result_maps_exactly(tmp_path, adapter_state, expected_state):
    state, registry = configured_state_and_registry(tmp_path)
    adapter = RecordingAdapter(AdapterDeliveryResult(adapter_state, "terminal reason", 2))
    dispatcher = CoreDispatcher(state, registry, {"fake": adapter}, lease_ids())
    enqueue(state, "terminal", "linux-main/dev-back")

    async def run():
        dispatcher.wake("linux-main/dev-back")
        await dispatcher.drain()

    asyncio.run(run())

    row = state.get_message("terminal")
    assert row["state"] == expected_state
    assert row["reason"] == "terminal reason"
    assert row["attempts"] == 2
    assert adapter.message_ids == ["terminal"]


def test_adapter_exception_after_acceptance_becomes_delivery_unknown(tmp_path):
    state, registry = configured_state_and_registry(tmp_path)
    adapter = RaisingAdapter()
    dispatcher = CoreDispatcher(state, registry, {"fake": adapter}, lease_ids())
    enqueue(state, "raises", "linux-main/dev-back")

    async def run():
        dispatcher.wake("linux-main/dev-back")
        await dispatcher.drain()

    asyncio.run(run())

    row = state.get_message("raises")
    assert row["state"] == "delivery_unknown"
    assert row["reason"] == "adapter delivery failed: fake adapter crashed"
    assert adapter.message_ids == ["raises"]


def test_adapter_result_does_not_reduce_attempts_recorded_by_delivery_service(tmp_path):
    state, registry = configured_state_and_registry(tmp_path)
    adapter = RecordingAdapter(
        AdapterDeliveryResult(AdapterDeliveryState.DELIVERED, "terminal reason", 2)
    )
    dispatcher = CoreDispatcher(state, registry, {"fake": adapter}, lease_ids())
    enqueue(state, "attempt-max", "linux-main/dev-back")
    state.record_attempt(
        "attempt-max",
        4,
        "sending",
        "verifying",
        "pane.send_input sent",
    )

    async def run():
        dispatcher.wake("linux-main/dev-back")
        await dispatcher.drain()

    asyncio.run(run())

    row = state.get_message("attempt-max")
    assert row["state"] == "delivered"
    assert row["attempts"] == 4


def test_missing_adapter_fails_without_classifying_delivery_as_unknown(tmp_path):
    state, registry = configured_state_and_registry(tmp_path)
    dispatcher = CoreDispatcher(state, registry, {}, lease_ids())
    enqueue(state, "missing-adapter", "linux-main/dev-back")

    async def run():
        dispatcher.wake("linux-main/dev-back")
        await dispatcher.drain()

    asyncio.run(run())

    row = state.get_message("missing-adapter")
    assert row["state"] == "failed"
    assert row["reason"] == "adapter delivery failed: 'fake'"


def test_duplicate_wake_does_not_duplicate_execution(tmp_path):
    asyncio.run(_duplicate_wake_does_not_duplicate_execution(tmp_path))


async def _duplicate_wake_does_not_duplicate_execution(tmp_path):
    state, registry = configured_state_and_registry(tmp_path)
    adapter = BlockingByTargetAdapter("linux-main/dev-back")
    dispatcher = CoreDispatcher(state, registry, {"fake": adapter}, lease_ids())
    enqueue(state, "once", "linux-main/dev-back")

    dispatcher.wake("linux-main/dev-back")
    dispatcher.wake("linux-main/dev-back")
    await adapter.block_started.wait()
    assert state.get_message("once")["state"] == "accepted"
    adapter.release.set()
    await dispatcher.drain()

    assert adapter.message_ids == ["once"]
    assert state.get_message("once")["state"] == "delivered"


def test_offline_endpoint_waits_until_later_wake(tmp_path):
    asyncio.run(_offline_endpoint_waits_until_later_wake(tmp_path))


async def _offline_endpoint_waits_until_later_wake(tmp_path):
    state, registry = configured_state_and_registry(
        tmp_path,
        statuses={"dev-back": "offline"},
    )
    adapter = RecordingAdapter()
    dispatcher = CoreDispatcher(state, registry, {"fake": adapter}, lease_ids())
    enqueue(state, "offline", "linux-main/dev-back")

    dispatcher.wake("linux-main/dev-back")
    await dispatcher.drain()

    assert state.get_message("offline")["state"] == "waiting_endpoint"
    assert adapter.message_ids == []

    registry.register(
        "linux-main",
        [endpoint("dev-back", "online"), endpoint("runner", "online")],
    )
    dispatcher.wake("linux-main/dev-back")
    await dispatcher.drain()

    assert state.get_message("offline")["state"] == "delivered"
    assert adapter.message_ids == ["offline"]


def test_missing_endpoint_waits_without_unhandled_task_exception(tmp_path):
    asyncio.run(_missing_endpoint_waits_without_unhandled_task_exception(tmp_path))


async def _missing_endpoint_waits_without_unhandled_task_exception(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    adapter = RecordingAdapter()
    dispatcher = CoreDispatcher(state, EndpointRegistry(), {"fake": adapter}, lease_ids())
    enqueue(state, "not-registered", "linux-main/dev-back")
    loop = asyncio.get_running_loop()
    unhandled = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        dispatcher.wake("linux-main/dev-back")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await dispatcher.drain()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    row = state.get_message("not-registered")
    assert row["state"] == "waiting_endpoint"
    assert row["reason"] == "endpoint not registered"
    assert adapter.message_ids == []
    assert unhandled == []


def test_retired_endpoint_fails_queued_messages_without_adapter_call(tmp_path):
    asyncio.run(_retired_endpoint_fails_queued_messages_without_adapter_call(tmp_path))


async def _retired_endpoint_fails_queued_messages_without_adapter_call(tmp_path):
    state, registry = configured_state_and_registry(
        tmp_path,
        statuses={"dev-back": "retired"},
    )
    adapter = RecordingAdapter()
    dispatcher = CoreDispatcher(state, registry, {"fake": adapter}, lease_ids())
    enqueue(state, "retired-first", "linux-main/dev-back")
    enqueue(state, "retired-second", "linux-main/dev-back")

    dispatcher.wake("linux-main/dev-back")
    await dispatcher.drain()

    for message_id in ("retired-first", "retired-second"):
        row = state.get_message(message_id)
        assert row["state"] == "failed"
        assert row["reason"] == "endpoint retired"
    assert adapter.message_ids == []


def test_offline_cas_false_stops_without_adapter_side_effect(tmp_path):
    asyncio.run(_offline_cas_false_stops_without_adapter_side_effect(tmp_path))


async def _offline_cas_false_stops_without_adapter_side_effect(tmp_path):
    state, registry = configured_state_and_registry(
        tmp_path,
        statuses={"dev-back": "offline"},
    )
    adapter = RecordingAdapter()
    racing_registry = OwnershipChangingRegistry(registry, state, "offline-cas", "lease-1")
    dispatcher = CoreDispatcher(state, racing_registry, {"fake": adapter}, lease_ids())
    enqueue(state, "offline-cas", "linux-main/dev-back")

    dispatcher.wake("linux-main/dev-back")
    await dispatcher.drain()

    row = state.get_message("offline-cas")
    assert row["state"] == "accepted"
    assert row["reason"] == "ownership moved before endpoint decision"
    assert adapter.message_ids == []


def test_terminal_cas_false_is_retained_and_surfaced_by_drain(tmp_path):
    asyncio.run(_terminal_cas_false_is_retained_and_surfaced_by_drain(tmp_path))


async def _terminal_cas_false_is_retained_and_surfaced_by_drain(tmp_path):
    state, registry = configured_state_and_registry(tmp_path)
    adapter = TerminalOwnershipChangingAdapter(state, "lease-1")
    dispatcher = CoreDispatcher(state, registry, {"fake": adapter}, lease_ids())
    enqueue(state, "terminal-cas", "linux-main/dev-back")
    loop = asyncio.get_running_loop()
    unhandled = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        dispatcher.wake("linux-main/dev-back")
        dispatcher.wake("linux-main/dev-back")
        await adapter.transitioned.wait()
        await asyncio.sleep(0)

        with pytest.raises(RuntimeError, match="lost terminal transition ownership.*terminal-cas"):
            await dispatcher.drain()

        dispatcher.wake("linux-main/dev-back")
        await dispatcher.drain()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    row = state.get_message("terminal-cas")
    assert row["state"] == "delivery_unknown"
    assert row["reason"] == "newer owner recorded terminal state"
    assert adapter.message_ids == ["terminal-cas"]
    assert unhandled == []


def test_recover_requeues_expired_offered_once(tmp_path):
    asyncio.run(_recover_requeues_expired_offered_once(tmp_path))


async def _recover_requeues_expired_offered_once(tmp_path):
    state, registry = configured_state_and_registry(tmp_path)
    adapter = RecordingAdapter()
    enqueue(state, "expired-offer", "linux-main/dev-back")
    claimed = state.claim_next("linux-main/dev-back", "crashed-lease", now=1)
    assert claimed["state"] == "offered"
    dispatcher = CoreDispatcher(state, registry, {"fake": adapter}, lease_ids())

    await dispatcher.recover()
    await dispatcher.drain()

    row = state.get_message("expired-offer")
    assert row["state"] == "delivered"
    assert row["lease_id"] == "lease-1"
    assert adapter.message_ids == ["expired-offer"]


def test_restart_requeues_persisted_offered_message_with_future_lease(tmp_path):
    asyncio.run(_restart_requeues_persisted_offered_message_with_future_lease(tmp_path))


async def _restart_requeues_persisted_offered_message_with_future_lease(tmp_path):
    state, registry = configured_state_and_registry(tmp_path)
    enqueue(state, "future-offer", "linux-main/dev-back")
    enqueue(state, "later", "linux-main/dev-back")
    claimed = state.claim_next(
        "linux-main/dev-back",
        "orphaned-lease",
        now=time.time(),
    )
    assert claimed["state"] == "offered"
    assert claimed["lease_expires_at"] > time.time()

    restarted_state = TalkbackState(state.path)
    restarted_state.init()
    adapter = RecordingAdapter()
    dispatcher = CoreDispatcher(
        restarted_state,
        registry,
        {"fake": adapter},
        lease_ids(),
    )

    await dispatcher.recover()
    await dispatcher.drain()

    assert adapter.message_ids == ["future-offer", "later"]
    recovered = restarted_state.get_message("future-offer")
    assert recovered["state"] == "delivered"
    assert recovered["lease_id"] == "lease-1"


def test_recover_marks_every_accepted_unknown_before_dispatching_later_fifo_rows(tmp_path):
    asyncio.run(_recover_marks_every_accepted_unknown_before_dispatching_later_fifo_rows(tmp_path))


async def _recover_marks_every_accepted_unknown_before_dispatching_later_fifo_rows(tmp_path):
    state, registry = configured_state_and_registry(tmp_path)
    adapter = RecordingAdapter()
    enqueue(state, "accepted-crash", "linux-main/dev-back")
    state.claim_next("linux-main/dev-back", "accepted-lease", now=1)
    assert state.transition_core_message(
        "accepted-crash",
        (CoreMessageState.OFFERED,),
        CoreMessageState.ACCEPTED,
        "adapter call may have started",
        lease_id="accepted-lease",
    )
    enqueue(state, "later", "linux-main/dev-back")
    enqueue(state, "accepted-runner", "linux-main/runner")
    state.claim_next("linux-main/runner", "runner-lease", now=1)
    assert state.transition_core_message(
        "accepted-runner",
        (CoreMessageState.OFFERED,),
        CoreMessageState.ACCEPTED,
        "runner adapter call may have started",
        lease_id="runner-lease",
    )
    dispatcher = CoreDispatcher(state, registry, {"fake": adapter}, lease_ids())

    await dispatcher.recover()
    await dispatcher.drain()

    for message_id in ("accepted-crash", "accepted-runner"):
        row = state.get_message(message_id)
        assert row["state"] == "delivery_unknown"
        assert row["reason"] == "dispatcher startup found persisted accepted delivery"
    assert state.get_message("later")["state"] == "delivered"
    assert adapter.message_ids == ["later"]


def test_close_reconciles_adapter_delivery_left_accepted(tmp_path):
    asyncio.run(_close_reconciles_adapter_delivery_left_accepted(tmp_path))


async def _close_reconciles_adapter_delivery_left_accepted(tmp_path):
    state, registry = configured_state_and_registry(tmp_path)
    adapter = BlockingByTargetAdapter("linux-main/dev-back")
    dispatcher = CoreDispatcher(state, registry, {"fake": adapter}, lease_ids())
    enqueue(state, "shutdown", "linux-main/dev-back")

    dispatcher.wake("linux-main/dev-back")
    await adapter.block_started.wait()
    assert state.get_message("shutdown")["state"] == "accepted"

    await dispatcher.close()

    row = state.get_message("shutdown")
    assert row["state"] == "delivery_unknown"
    assert row["reason"] == "dispatcher shutdown left accepted delivery"
    assert adapter.message_ids == ["shutdown"]


def test_close_requeues_delivery_cancelled_before_adapter_acceptance(tmp_path):
    asyncio.run(_close_requeues_delivery_cancelled_before_adapter_acceptance(tmp_path))


async def _close_requeues_delivery_cancelled_before_adapter_acceptance(tmp_path):
    state, registry = configured_state_and_registry(tmp_path)
    adapter = BlockingBeforeAcceptanceAdapter()
    dispatcher = CoreDispatcher(state, registry, {"fake": adapter}, lease_ids())
    enqueue(state, "shutdown-before-accept", "linux-main/dev-back")

    dispatcher.wake("linux-main/dev-back")
    await adapter.started.wait()
    offered = state.get_message("shutdown-before-accept")
    assert offered["state"] == "offered"
    assert offered["lease_expires_at"] > time.time()

    await dispatcher.close()

    recovered = state.get_message("shutdown-before-accept")
    assert recovered["state"] == "queued"
    assert recovered["lease_id"] is None
    assert recovered["lease_expires_at"] is None
    assert recovered["accepted_by_adapter"] is None


def configured_state_and_registry(tmp_path, statuses=None):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    statuses = statuses or {}
    registry = EndpointRegistry()
    registry.register(
        "linux-main",
        [
            endpoint("dev-back", statuses.get("dev-back", "online")),
            endpoint("runner", statuses.get("runner", "online")),
        ],
    )
    return state, registry


def endpoint(name, status):
    return EndpointDescriptor(
        address=EndpointAddress("linux-main", name),
        adapter="fake",
        label=name,
        status=status,
        capabilities=("message",),
    )


def enqueue(state, message_id, target, message_state=CoreMessageState.QUEUED):
    assert state.enqueue_message(
        CoreMessage(
            message_id=message_id,
            source_endpoint="linux-main/lead",
            target_endpoint=target,
            body=message_id,
            raw_text="[lead->{0}] {1}".format(target.rsplit("/", 1)[-1], message_id),
            state=message_state,
            conversation_id=message_id,
            metadata={"test": True},
        )
    )


def lease_ids():
    values = itertools.count(1)
    return lambda: "lease-{0}".format(next(values))


class RecordingAdapter:
    def __init__(self, result=None):
        self.result = result or AdapterDeliveryResult(
            AdapterDeliveryState.DELIVERED,
            "delivered by fake adapter",
            1,
        )
        self.message_ids = []

    async def deliver(self, message, lease):
        self.message_ids.append(message.message_id)
        assert lease.accept("accepted by fake adapter")
        await asyncio.sleep(0)
        return self.result


class InspectingAdapter(RecordingAdapter):
    def __init__(self, state, accept, result):
        super().__init__(result)
        self.state = state
        self.should_accept = accept
        self.states_at_call = []
        self.accept_result = None
        self.accepted_state = None

    async def deliver(self, message, lease):
        self.states_at_call.append(self.state.get_message(message.message_id)["state"])
        if self.should_accept:
            self.accept_result = lease.accept("accepted by fake adapter")
            self.accepted_state = self.state.get_message(message.message_id)["state"]
        return self.result


class DeferredOnceAdapter(RecordingAdapter):
    def __init__(self):
        super().__init__()
        self.first_deferred = asyncio.Event()
        self.delivered_event = asyncio.Event()

    async def deliver(self, message, lease):
        self.message_ids.append(message.message_id)
        if len(self.message_ids) == 1:
            self.first_deferred.set()
            return AdapterDeliveryResult(
                AdapterDeliveryState.DEFERRED,
                "online endpoint did not accept in time",
                0,
            )
        assert lease.accept("accepted on bounded retry")
        self.delivered_event.set()
        return self.result


class BlockingDeferredAdapter(RecordingAdapter):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def deliver(self, message, lease):
        self.message_ids.append(message.message_id)
        assert lease.accept("accepted before endpoint retirement")
        self.started.set()
        await self.release.wait()
        return AdapterDeliveryResult(
            AdapterDeliveryState.DEFERRED,
            "connection replaced during delivery",
            0,
        )


class RaisingAdapter(RecordingAdapter):
    async def deliver(self, message, lease):
        self.message_ids.append(message.message_id)
        assert lease.accept("accepted by fake adapter")
        raise RuntimeError("fake adapter crashed")


class OwnershipChangingRegistry:
    def __init__(self, registry, state, message_id, lease_id):
        self.registry = registry
        self.state = state
        self.message_id = message_id
        self.lease_id = lease_id

    def resolve(self, value, sender_node_id):
        assert self.state.transition_core_message(
            self.message_id,
            (CoreMessageState.OFFERED,),
            CoreMessageState.ACCEPTED,
            "ownership moved before endpoint decision",
            lease_id=self.lease_id,
        )
        return self.registry.resolve(value, sender_node_id)


class TerminalOwnershipChangingAdapter(RecordingAdapter):
    def __init__(self, state, lease_id):
        super().__init__()
        self.state = state
        self.lease_id = lease_id
        self.transitioned = asyncio.Event()

    async def deliver(self, message, lease):
        self.message_ids.append(message.message_id)
        assert lease.accept("accepted by fake adapter")
        assert self.state.transition_core_message(
            message.message_id,
            (CoreMessageState.ACCEPTED,),
            CoreMessageState.DELIVERY_UNKNOWN,
            "newer owner recorded terminal state",
            lease_id=self.lease_id,
        )
        self.transitioned.set()
        return self.result


class BlockingByTargetAdapter(RecordingAdapter):
    def __init__(self, blocked_target):
        super().__init__()
        self.blocked_target = blocked_target
        self.block_started = asyncio.Event()
        self.release = asyncio.Event()
        self._delivered = {}

    async def deliver(self, message, lease):
        self.message_ids.append(message.message_id)
        assert lease.accept("accepted by fake adapter")
        if message.target_endpoint == self.blocked_target:
            self.block_started.set()
            await self.release.wait()
        self._delivered.setdefault(message.message_id, asyncio.Event()).set()
        return self.result

    async def delivered(self, message_id):
        await self._delivered.setdefault(message_id, asyncio.Event()).wait()


class BlockingBeforeAcceptanceAdapter(RecordingAdapter):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()

    async def deliver(self, message, lease):
        self.message_ids.append(message.message_id)
        self.started.set()
        await asyncio.Event().wait()
