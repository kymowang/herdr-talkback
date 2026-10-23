import re

import pytest

from herdr_talkback.rules import parse_message_rules
from herdr_talkback.messages import MessageParseError, parse_message


ROLES = ("lead", "assistant", "dev-back", "dev-front", "ui", "runner", "system-bus")
ALIASES = {"system-bus": "lead"}


def test_parse_valid_role_message():
    msg = parse_message("[lead→dev-back] 新任务 TASK-0001", (), ALIASES)

    assert msg.source == "lead"
    assert msg.target == "dev-back"
    assert msg.resolved_target == "dev-back"
    assert msg.body == "新任务 TASK-0001"
    assert re.match(r"^msg_[0-9a-f]{32}$", msg.message_id)


def test_parse_valid_ascii_arrow_message():
    msg = parse_message("[lead->dev-back] ascii arrow", ROLES, ALIASES)

    assert msg.source == "lead"
    assert msg.target == "dev-back"
    assert msg.resolved_target == "dev-back"
    assert msg.body == "ascii arrow"
    assert msg.raw_text == "[lead->dev-back] ascii arrow"


def test_parse_alias_target():
    msg = parse_message("[assistant→system-bus] 诊断完成", (), ALIASES)

    assert msg.target == "system-bus"
    assert msg.resolved_target == "lead"


def test_parse_alias_target_not_listed_as_role():
    msg = parse_message("[lead→system-bus] 诊断完成", ("lead", "runner"), {"system-bus": "runner"})

    assert msg.target == "system-bus"
    assert msg.resolved_target == "runner"


def test_accepts_roles_outside_configured_list():
    msg = parse_message("[ad-hoc-source→ad-hoc-target] hi", (), {})

    assert msg.source == "ad-hoc-source"
    assert msg.target == "ad-hoc-target"
    assert msg.resolved_target == "ad-hoc-target"


@pytest.mark.parametrize(
    "text",
    [
        "没有头",
        "[lead→dev-back]",
    ],
)
def test_reject_invalid_messages(text):
    with pytest.raises(MessageParseError):
        parse_message(text, ROLES, ALIASES)


def test_parse_message_accepts_cross_node_addresses():
    message = parse_message(
        "[linux-main/lead->windows-dev/codex-review] inspect",
        roles=("lead",),
        aliases={},
        local_node_id="linux-main",
    )

    assert message.source_endpoint == "linux-main/lead"
    assert message.target_endpoint == "windows-dev/codex-review"


def test_parse_message_qualifies_legacy_roles_on_local_node():
    message = parse_message(
        "[lead->dev-back] inspect",
        roles=("lead", "dev-back"),
        aliases={},
        local_node_id="linux-main",
    )

    assert message.source_endpoint == "linux-main/lead"
    assert message.target_endpoint == "linux-main/dev-back"
    assert message.source == "lead"
    assert message.target == "dev-back"


def test_parse_message_rejects_qualified_remote_source():
    with pytest.raises(MessageParseError, match="source must belong to local node linux-main"):
        parse_message(
            "[windows-dev/lead->windows-dev/codex-review] inspect",
            roles=("lead",),
            aliases={},
            local_node_id="linux-main",
        )


def test_parse_message_rejects_unknown_qualified_local_source():
    with pytest.raises(MessageParseError, match="unknown source role or alias: missing"):
        parse_message(
            "[linux-main/missing->windows-dev/codex-review] inspect",
            roles=("lead",),
            aliases={},
            local_node_id="linux-main",
        )


def test_parse_message_accepts_configured_local_alias_as_source():
    message = parse_message(
        "[linux-main/system-bus->windows-dev/codex-review] inspect",
        roles=("lead",),
        aliases={"system-bus": "lead"},
        local_node_id="linux-main",
    )

    assert message.source_endpoint == "linux-main/system-bus"
    assert message.target_endpoint == "windows-dev/codex-review"


@pytest.mark.parametrize('source,append', [('lead', True), ('lead2', True), ('scheduler', True), ('dev-be2', False), ('linux-main/lead2', True)])
def test_configured_receipt_reminder(source, append):
    reminder = '上述内容如果是任务而非通知，完成后请发回执给我'
    raw = f'[{source}->runner] 请处理'
    msg = parse_message(raw, (), {}, message_rules=parse_message_rules([dict(id='receipt', agents=['lead', 'lead2', 'scheduler'], append=reminder)]))
    assert msg.raw_text == raw + ('\n' + reminder if append else '')
    assert msg.body == '请处理' + ('\n' + reminder if append else '')
    retried = parse_message(msg.raw_text, (), {}, message_rules=parse_message_rules([dict(id='receipt', agents=['lead', 'lead2', 'scheduler'], append=reminder)]))
    assert retried.raw_text == msg.raw_text


def test_receipt_reminder_disabled_by_empty_configuration():
    msg = parse_message('[lead->runner] task', (), {}, message_rules=())
    assert msg.raw_text == '[lead->runner] task'
