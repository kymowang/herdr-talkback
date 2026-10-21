import pytest

from herdr_talkback.addressing import EndpointAddress, UnqualifiedEndpoint, parse_endpoint_address


def test_parses_qualified_endpoint():
    assert parse_endpoint_address("windows-dev/codex-review") == EndpointAddress(
        node_id="windows-dev", endpoint_id="codex-review"
    )


def test_preserves_unqualified_endpoint_for_registry_resolution():
    assert parse_endpoint_address("lead") == UnqualifiedEndpoint(endpoint_id="lead")


@pytest.mark.parametrize("value", ["", "/lead", "linux-main/", "a/b/c", "node id/lead"])
def test_rejects_invalid_endpoint_address(value):
    with pytest.raises(ValueError):
        parse_endpoint_address(value)
