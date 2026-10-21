import asyncio
from types import SimpleNamespace

from herdr_talkback.adapters.herdr import HerdrAdapter
from herdr_talkback.core_types import AdapterDeliveryState, CoreMessage, CoreMessageState
from herdr_talkback.delivery import DeliveryService
from herdr_talkback.state import TalkbackState
from herdr_talkback.types import AgentInfo


EMPTY_PROMPT = "❯ \x1b[38;2;136;136;136mType a message\x1b[0m"


def message(target="linux-main/dev-back"):
    return CoreMessage(
        message_id="msg_1",
        source_endpoint="linux-main/lead",
        target_endpoint=target,
        body="ping",
        raw_text="[lead->dev-back] ping",
        state=CoreMessageState.OFFERED,
        conversation_id="msg_1",
    )


class FakeRoleRegistry:
    def __init__(self):
        self.refreshes = 0

    async def refresh(self):
        self.refreshes += 1

    def snapshot(self):
        return {
            "dev-back": AgentInfo(
                name="dev-back",
                agent="codex",
                agent_status="working",
                pane_id="p-back",
                workspace_id="w1",
                tab_id="t1",
            ),
            "lead": AgentInfo(
                name="lead",
                agent="claude",
                agent_status="idle",
                pane_id="p-lead",
                workspace_id="w1",
                tab_id="t2",
            ),
        }


class FakeDelivery:
    def __init__(self, result):
        self.result = result
        self.messages = []

    async def deliver(self, routed):
        self.messages.append(routed)
        return self.result


class RecordingLease:
    def __init__(self, accept_result=True):
        self.accept_result = accept_result
        self.accepted = False
        self.reasons = []

    def accept(self, reason):
        self.reasons.append(reason)
        self.accepted = self.accept_result
        return self.accepted


class AcceptanceInspectingDelivery(FakeDelivery):
    def __init__(self, result, lease):
        super().__init__(result)
        self.lease = lease
        self.accepted_at_call = []

    async def deliver(self, routed):
        self.accepted_at_call.append(self.lease.accepted)
        return await super().deliver(routed)


class DeliveryRegistry:
    def resolve_pane(self, role):
        assert role == "dev-back"
        return "p-back"


class AmbiguousDeliveryClient:
    def __init__(self):
        self.reads = [EMPTY_PROMPT, "normal output only"]

    async def request(self, method, params):
        if method == "pane.read":
            return {"type": "pane_read", "read": {"text": self.reads.pop(0), "pane_id": "p-back"}}
        if method == "pane.send_input":
            return {"type": "ok"}
        raise AssertionError(method)


def test_herdr_adapter_maps_success_and_preserves_core_message_context():
    delivery = FakeDelivery(SimpleNamespace(delivered=True, reason="input cleared", attempts=1))
    result = asyncio.run(
        HerdrAdapter("linux-main", FakeRoleRegistry(), delivery).deliver(
            message(), RecordingLease()
        )
    )

    assert result.state == AdapterDeliveryState.DELIVERED
    assert result.reason == "input cleared"
    assert result.attempts == 1
    assert delivery.messages[0].source == "lead"
    assert delivery.messages[0].target == "dev-back"
    assert delivery.messages[0].resolved_target == "dev-back"
    assert delivery.messages[0].source_endpoint == "linux-main/lead"
    assert delivery.messages[0].target_endpoint == "linux-main/dev-back"
    assert delivery.messages[0].conversation_id == "msg_1"


def test_herdr_adapter_accepts_immediately_before_delivery_service_call():
    lease = RecordingLease()
    delivery = AcceptanceInspectingDelivery(
        SimpleNamespace(delivered=True, reason="input cleared", attempts=1),
        lease,
    )

    result = asyncio.run(
        HerdrAdapter("linux-main", FakeRoleRegistry(), delivery).deliver(message(), lease)
    )

    assert result.state == AdapterDeliveryState.DELIVERED
    assert lease.reasons == ["accepted for Herdr delivery"]
    assert delivery.accepted_at_call == [True]


def test_herdr_adapter_acceptance_failure_is_unknown_without_touching_pane():
    lease = RecordingLease(accept_result=False)
    delivery = FakeDelivery(SimpleNamespace(delivered=True, reason="input cleared", attempts=1))

    result = asyncio.run(
        HerdrAdapter("linux-main", FakeRoleRegistry(), delivery).deliver(message(), lease)
    )

    assert result.state == AdapterDeliveryState.DELIVERY_UNKNOWN
    assert result.attempts == 0
    assert lease.reasons == ["accepted for Herdr delivery"]
    assert delivery.messages == []


def test_herdr_adapter_maps_ambiguous_delivery_to_unknown():
    delivery = FakeDelivery(SimpleNamespace(delivered=False, reason="ambiguous after submit", attempts=1))

    result = asyncio.run(
        HerdrAdapter("linux-main", FakeRoleRegistry(), delivery).deliver(
            message(), RecordingLease()
        )
    )

    assert result.state == AdapterDeliveryState.DELIVERY_UNKNOWN


def test_herdr_adapter_maps_real_ambiguous_verification_to_unknown(tmp_path):
    state = TalkbackState(tmp_path / "state.db")
    state.init()
    delivery = DeliveryService(AmbiguousDeliveryClient(), DeliveryRegistry(), state, max_attempts=1)

    result = asyncio.run(
        HerdrAdapter("linux-main", FakeRoleRegistry(), delivery).deliver(
            message(), RecordingLease()
        )
    )

    assert result.state == AdapterDeliveryState.DELIVERY_UNKNOWN
    assert result.reason == "delivery ambiguous"


def test_herdr_adapter_maps_uncleared_residue_to_unknown():
    delivery = FakeDelivery(
        SimpleNamespace(delivered=False, reason="message residue not cleared", attempts=1)
    )

    result = asyncio.run(
        HerdrAdapter("linux-main", FakeRoleRegistry(), delivery).deliver(
            message(), RecordingLease()
        )
    )

    assert result.state == AdapterDeliveryState.DELIVERY_UNKNOWN


def test_herdr_adapter_maps_submission_state_unknown_to_unknown():
    delivery = FakeDelivery(
        SimpleNamespace(delivered=False, reason="submission state unknown", attempts=1)
    )

    result = asyncio.run(
        HerdrAdapter("linux-main", FakeRoleRegistry(), delivery).deliver(
            message(), RecordingLease()
        )
    )

    assert result.state == AdapterDeliveryState.DELIVERY_UNKNOWN


def test_herdr_adapter_maps_confirmed_failure_to_failed():
    delivery = FakeDelivery(SimpleNamespace(delivered=False, reason="target input occupied", attempts=2))

    result = asyncio.run(
        HerdrAdapter("linux-main", FakeRoleRegistry(), delivery).deliver(
            message(), RecordingLease()
        )
    )

    assert result.state == AdapterDeliveryState.FAILED
    assert result.reason == "target input occupied"
    assert result.attempts == 2


def test_herdr_adapter_rejects_messages_for_another_node():
    delivery = FakeDelivery(SimpleNamespace(delivered=True, reason="input cleared", attempts=1))
    lease = RecordingLease()

    result = asyncio.run(
        HerdrAdapter("linux-main", FakeRoleRegistry(), delivery).deliver(
            message("windows-dev/dev-back"), lease
        )
    )

    assert result.state == AdapterDeliveryState.FAILED
    assert result.reason == "target endpoint is not local to this adapter"
    assert result.attempts == 0
    assert delivery.messages == []
    assert lease.reasons == []


def test_herdr_adapter_refreshes_local_message_endpoints():
    registry = FakeRoleRegistry()

    endpoints = asyncio.run(HerdrAdapter("linux-main", registry, FakeDelivery(None)).refresh())

    assert registry.refreshes == 1
    assert [(endpoint.address.qualified, endpoint.status, endpoint.capabilities) for endpoint in endpoints] == [
        ("linux-main/dev-back", "working", ("message",)),
        ("linux-main/lead", "idle", ("message",)),
    ]
