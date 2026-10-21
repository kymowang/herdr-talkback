import pytest

from herdr_talkback.addressing import EndpointAddress
from herdr_talkback.core_types import EndpointDescriptor
from herdr_talkback.endpoints import AmbiguousEndpoint, EndpointNotFound, EndpointRegistry


def endpoint(node, name, status="online"):
    return EndpointDescriptor(
        address=EndpointAddress(node, name),
        adapter="herdr",
        label=name,
        status=status,
        capabilities=("message",),
    )


def test_exact_qualified_address_wins():
    registry = EndpointRegistry()
    registry.register("linux-main", [endpoint("linux-main", "lead")])
    registry.register("windows-dev", [endpoint("windows-dev", "lead")])

    assert (
        registry.resolve("windows-dev/lead", "linux-main").address.qualified
        == "windows-dev/lead"
    )


def test_globally_unique_unqualified_name_resolves():
    registry = EndpointRegistry()
    registry.register("windows-dev", [endpoint("windows-dev", "review")])

    assert registry.resolve("review", "linux-main").address.qualified == "windows-dev/review"


def test_duplicate_unqualified_name_prefers_sender_node():
    registry = EndpointRegistry()
    registry.register("linux-main", [endpoint("linux-main", "lead")])
    registry.register("windows-dev", [endpoint("windows-dev", "lead")])

    assert registry.resolve("lead", "windows-dev").address.qualified == "windows-dev/lead"


def test_duplicate_name_without_local_candidate_is_ambiguous():
    registry = EndpointRegistry()
    registry.register("node-b", [endpoint("node-b", "review")])
    registry.register("node-a", [endpoint("node-a", "review")])

    with pytest.raises(AmbiguousEndpoint, match="node-a/review, node-b/review"):
        registry.resolve("review", "node-c")


def test_unknown_endpoint_raises_not_found():
    registry = EndpointRegistry()

    with pytest.raises(EndpointNotFound, match="missing"):
        registry.resolve("missing", "linux-main")


def test_disconnected_node_remains_registered_but_offline():
    registry = EndpointRegistry()
    registry.register("windows-dev", [endpoint("windows-dev", "review")])

    registry.mark_node_offline("windows-dev")

    assert registry.resolve("windows-dev/review", "linux-main").status == "offline"


def test_register_marks_disappeared_node_endpoints_offline():
    registry = EndpointRegistry()
    registry.register("windows-dev", [endpoint("windows-dev", "old")])

    registry.register("windows-dev", [endpoint("windows-dev", "new")])

    assert registry.resolve("windows-dev/old", "linux-main").status == "offline"
    assert registry.resolve("windows-dev/new", "linux-main").address.qualified == "windows-dev/new"


def test_remote_snapshot_retires_omitted_endpoint_while_local_snapshot_offlines_it():
    registry = EndpointRegistry()
    registry.register("linux-main", [endpoint("linux-main", "removed-local")])
    registry.register_remote(
        "windows-dev",
        [endpoint("windows-dev", "removed-remote")],
    )

    registry.register("linux-main", [endpoint("linux-main", "replacement")])
    registry.register_remote(
        "windows-dev",
        [endpoint("windows-dev", "replacement")],
    )

    assert registry.resolve("linux-main/removed-local", "linux-main").status == "offline"
    assert registry.resolve("windows-dev/removed-remote", "linux-main").status == "retired"


def test_register_keeps_explicitly_retired_descriptors():
    registry = EndpointRegistry()
    registry.register(
        "windows-dev",
        [endpoint("windows-dev", "retired", status="retired"), endpoint("windows-dev", "active")],
    )

    registry.register("windows-dev", [endpoint("windows-dev", "replacement")])

    assert registry.resolve("windows-dev/retired", "linux-main").status == "retired"
    assert registry.resolve("windows-dev/active", "linux-main").status == "offline"


def test_marking_node_offline_does_not_change_retired_descriptor():
    registry = EndpointRegistry()
    registry.register(
        "windows-dev",
        [endpoint("windows-dev", "retired", status="retired")],
    )

    registry.mark_node_offline("windows-dev")
    registry.register("windows-dev", [endpoint("windows-dev", "replacement")])

    assert registry.resolve("windows-dev/retired", "linux-main").status == "retired"


def test_register_does_not_overwrite_retired_descriptor_with_active_descriptor():
    registry = EndpointRegistry()
    registry.register(
        "windows-dev",
        [endpoint("windows-dev", "retired", status="retired")],
    )

    registry.register("windows-dev", [endpoint("windows-dev", "retired")])

    assert registry.resolve("windows-dev/retired", "linux-main").status == "retired"


def test_register_rejects_descriptor_from_a_different_node_without_mutating_registry():
    registry = EndpointRegistry()
    registry.register("linux-main", [endpoint("linux-main", "lead")])

    with pytest.raises(ValueError, match="descriptor node windows-dev does not match linux-main"):
        registry.register(
            "linux-main",
            [endpoint("linux-main", "runner"), endpoint("windows-dev", "review")],
        )

    assert registry.resolve("linux-main/lead", "linux-main").status == "online"
    with pytest.raises(EndpointNotFound):
        registry.resolve("linux-main/runner", "linux-main")
