from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Union


SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# Herdr pane ids look like "w2:p1A"; they are the only stable pane identity.
PANE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*:[A-Za-z0-9][A-Za-z0-9._-]*$")
ENDPOINT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def _validate_segment(value: str, name: str) -> None:
    if not isinstance(value, str) or not SEGMENT_RE.fullmatch(value):
        raise ValueError(f"invalid {name}: {value!r}")


def is_pane_id(value: str) -> bool:
    return isinstance(value, str) and PANE_ID_RE.fullmatch(value) is not None


def _validate_endpoint(value: str, name: str) -> None:
    if not isinstance(value, str) or not ENDPOINT_RE.fullmatch(value):
        raise ValueError(f"invalid {name}: {value!r}")


def validate_node_id(value: str) -> str:
    _validate_segment(value, "node ID")
    return value


@dataclass(frozen=True)
class EndpointAddress:
    node_id: str
    endpoint_id: str

    def __post_init__(self) -> None:
        validate_node_id(self.node_id)
        _validate_endpoint(self.endpoint_id, "endpoint ID")

    @property
    def qualified(self) -> str:
        return f"{self.node_id}/{self.endpoint_id}"


@dataclass(frozen=True)
class UnqualifiedEndpoint:
    endpoint_id: str

    def __post_init__(self) -> None:
        _validate_endpoint(self.endpoint_id, "endpoint ID")


def parse_endpoint_address(value: str) -> Union[EndpointAddress, UnqualifiedEndpoint]:
    if not isinstance(value, str):
        raise ValueError("endpoint address must be a string")

    if "/" not in value:
        return UnqualifiedEndpoint(endpoint_id=value)

    if value.count("/") != 1:
        raise ValueError(f"invalid endpoint address: {value!r}")

    node_id, endpoint_id = value.split("/")
    return EndpointAddress(node_id=node_id, endpoint_id=endpoint_id)
