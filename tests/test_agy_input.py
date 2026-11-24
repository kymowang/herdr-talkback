import pytest

from herdr_talkback.input_parser import composer_block, inspect_input, sanitize_snapshot

RULE = "\u2500" * 60


def screen(draft="", footer="? for shortcuts", below=""):
    rows = [
        "  另一个窗口（D:\\repo\\herdr-talkback）启动 agy 后没能连上 Google 后端。",
        "",
        RULE,
        "> " + draft if draft else ">",
        RULE,
    ]
    if below:
        rows.append("")
        rows.append(below)
    rows += ["", footer]
    return "\n".join(rows)


@pytest.mark.parametrize("draft,expected", [("", "empty"), ("user draft", "occupied"), ("中文草稿", "occupied")])
def test_agy_composer_reads_draft(draft, expected):
    state = inspect_input(screen(draft), "")

    assert state.kind == expected
    assert state.text_after_prompt == draft


def test_agy_expected_message_is_residue():
    state = inspect_input(screen("[lead->dev] ping"), "[lead->dev] ping")

    assert state.kind == "residue"
    assert state.contains_expected_message is True


def test_agy_other_draft_is_occupied_not_residue():
    state = inspect_input(screen("something else"), "[lead->dev] ping")

    assert state.kind == "occupied"
    assert state.contains_expected_message is False


def test_agy_status_line_below_composer_still_reads():
    view = screen("draft", below="  ● [09:55:53] pytest -q running")

    state = inspect_input(view, "")

    assert state.kind == "occupied"
    assert state.text_after_prompt == "draft"


def test_agy_cancellable_footer_reads():
    assert inspect_input(screen("draft", footer="esc to cancel   Gemini 4 Neon"), "").kind == "occupied"


def test_agy_transcript_echo_without_composer_is_unknown():
    view = "\n".join([RULE, "> earlier user message", "", "assistant output", "more output"])

    state = inspect_input(view, "")

    assert state.kind == "unknown"
    assert state.text_after_prompt == ""


def test_agy_mismatched_rule_widths_are_not_a_composer():
    view = "\n".join(["\u2500" * 61, "> draft", "", RULE, "", "? for shortcuts"])

    assert inspect_input(view, "").kind == "unknown"


def test_agy_composer_requires_footer_hint():
    view = "\n".join([RULE, "> draft", "", RULE, "", "unrelated trailing output"])

    assert inspect_input(view, "").kind == "unknown"


def test_agy_rule_pair_around_diff_is_not_a_composer():
    view = "\n".join([RULE, "> old line", "+ added line", RULE, "", "? for shortcuts"])

    state = inspect_input(view, "")

    assert state.kind == "unknown"
    assert state.text_after_prompt == ""


def test_agy_composer_block_is_draft_only():
    view = screen("draft", below="  ● pytest -q running")

    assert composer_block(view) == "draft"


def wrapped_screen(*interior):
    return "\n".join(["assistant output", "", RULE, *interior, RULE, "", "? for shortcuts"])


def test_agy_wrapped_draft_is_occupied_and_block_keeps_wrap():
    view = wrapped_screen("> \u5b8c\u5584\u4e00\u4e0b\u6d88\u606f\u9001\u8fbe\u673a\u5236\uff0c", "  \u6536\u5230\u672c\u6d88\u606f\u540e\u8bf7\u6267\u884c")

    state = inspect_input(view, "")

    assert state.kind == "occupied"
    assert composer_block(view) == "\u5b8c\u5584\u4e00\u4e0b\u6d88\u606f\u9001\u8fbe\u673a\u5236\uff0c\n\u6536\u5230\u672c\u6d88\u606f\u540e\u8bf7\u6267\u884c"


def test_agy_wrapped_expected_message_is_residue():
    expected = "[lead\u2192dev] ensure the composer is detected correctly"
    view = wrapped_screen("> [lead\u2192dev] ensure the composer is", "  detected correctly")

    assert inspect_input(view, expected).kind == "residue"
    assert inspect_input(view, expected).contains_expected_message is True


def test_agy_truncated_long_draft_is_residue_only_after_send():
    expected = "[lead\u2192dev] this is a very long message that keeps going past the composer"
    view = wrapped_screen("> [lead\u2192dev] this is a very long message that keeps going")

    assert inspect_input(view, expected).kind == "occupied"
    assert inspect_input(view, expected, allow_transformed_residue=True).kind == "residue"


def test_sanitize_snapshot_drops_agy_draft_and_keeps_frame():
    out = sanitize_snapshot(screen("continue", below="  ● pytest -q running"))

    assert "continue" not in out
    assert out.count(RULE) == 2
    assert "? for shortcuts" in out


def test_sanitize_snapshot_leaves_unknown_agy_layout_untouched():
    text = "\n".join([RULE, "> draft", "", "no closing rule or footer"])

    assert sanitize_snapshot(text) == text
