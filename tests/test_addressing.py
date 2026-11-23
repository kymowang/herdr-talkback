import pytest

from herdr_talkback.addressing import (
    EndpointAddress,
    UnqualifiedEndpoint,
    is_pane_id,
    parse_endpoint_address,
)


def test_parses_qualified_endpoint():
    assert parse_endpoint_address("windows-dev/codex-review") == EndpointAddress(
        node_id="windows-dev", endpoint_id="codex-review"
    )


def test_preserves_unqualified_endpoint_for_registry_resolution():
    assert parse_endpoint_address("lead") == UnqualifiedEndpoint(endpoint_id="lead")


def test_accepts_pane_ids_as_endpoints():
    assert parse_endpoint_address("w2:p1A") == UnqualifiedEndpoint(endpoint_id="w2:p1A")
    assert parse_endpoint_address("pc55/w2:p1A") == EndpointAddress(
        node_id="pc55", endpoint_id="w2:p1A"
    )
    assert is_pane_id("w2:p1A")
    assert not is_pane_id("p-back")
    assert not is_pane_id("node/lead")


@pytest.mark.parametrize("value", ["", "/lead", "linux-main/", "a/b/c", "node id/lead"])
def test_rejects_invalid_endpoint_address(value):
    with pytest.raises(ValueError):
        parse_endpoint_address(value)
