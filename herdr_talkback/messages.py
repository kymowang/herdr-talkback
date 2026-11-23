from __future__ import annotations

import re
import uuid
from typing import Mapping, Optional, Sequence

from .addressing import EndpointAddress, is_pane_id, parse_endpoint_address, validate_node_id
from .types import RoutedMessage
from .rules import matches_scope


HEADER_RE = re.compile(r"^\[([^\]→]+?)\s*(?:→|->)\s*([^\]]+)\]\s*(.+)$", re.DOTALL)


class MessageParseError(ValueError):
    pass


def _unknown_source(source_name: str, local_node_id: str, hint: str) -> str:
    if source_name == local_node_id:
        return (
            "source is the node id, not a sender: {0}. "
            "Use your own pane label as source{1}; see 'herdr-talkback pane list'".format(
                source_name, hint
            )
        )
    return (
        "unknown source role: {0}. Source must be a live local pane label, "
        "alias or registered pane name{1}".format(source_name, hint)
    )


def parse_message(
    text: str,
    roles: Sequence[str],
    aliases: Mapping[str, str],
    local_node_id: str = "linux-main",
    message_rules=(),
    known_sources: Optional[Sequence[str]] = None,
    source_hint: str = "",
) -> RoutedMessage:
    match = HEADER_RE.match(text.strip())
    if not match:
        raise MessageParseError("message must start with [source→target] or [source->target] and non-empty body")

    source = match.group(1).strip()
    target = match.group(2).strip()
    body = match.group(3).strip()
    allowed = set(roles)
    try:
        local_node_id = validate_node_id(local_node_id)
        source_address = parse_endpoint_address(source)
        target_address = parse_endpoint_address(target)
    except ValueError as error:
        raise MessageParseError(str(error)) from error

    if isinstance(source_address, EndpointAddress):
        if source_address.node_id != local_node_id:
            raise MessageParseError(
                "source must belong to local node {0}: {1}".format(
                    local_node_id, source_address.qualified
                )
            )
        source_name = source_address.endpoint_id
        source_endpoint = source_address.qualified
    else:
        source_name = source_address.endpoint_id
        source_endpoint = EndpointAddress(local_node_id, source_name).qualified
    if not is_pane_id(source_name):
        if allowed and source_name not in allowed and source_name not in aliases:
            raise MessageParseError(f"unknown source role or alias: {source_name}")
        if known_sources is not None and source_name not in set(known_sources):
            raise MessageParseError(_unknown_source(source_name, local_node_id, source_hint))

    if isinstance(target_address, EndpointAddress):
        resolved_target = target
        target_endpoint = target_address.qualified
    elif is_pane_id(target):
        # A pane id is the only stable target; it bypasses role-name rules.
        resolved_target = target
        target_endpoint = EndpointAddress(local_node_id, target).qualified
    elif target in aliases:
        resolved_target = aliases[target]
        if allowed and resolved_target not in allowed:
            raise MessageParseError(f"alias target {target} resolves to unknown role: {resolved_target}")
        target_endpoint = EndpointAddress(local_node_id, resolved_target).qualified
    elif target in allowed or not allowed:
        resolved_target = target
        target_endpoint = EndpointAddress(local_node_id, resolved_target).qualified
    else:
        raise MessageParseError(f"unknown target role: {target}")
    if not body:
        raise MessageParseError("message body is empty")

    raw_text = text.strip()
    suffixes = []
    for rule in message_rules:
        if rule.enabled and matches_scope(source_name, rule.agents) and matches_scope(target_endpoint.split('/', 1)[-1], rule.targets):
            if rule.append not in suffixes:
                suffixes.append(rule.append)
    # A retry containing the complete suffix block must remain byte-for-byte identical.
    suffix = "\n".join(suffixes)
    if suffix and not body.endswith(suffix):
        body += "\n" + suffix
        raw_text += "\n" + suffix

    message_id = 'msg_' + uuid.uuid4().hex
    return RoutedMessage(
        message_id=message_id,
        source=source,
        target=target,
        resolved_target=resolved_target,
        body=body,
        raw_text=raw_text,
        source_endpoint=source_endpoint,
        target_endpoint=target_endpoint,
        conversation_id=message_id,
    )
