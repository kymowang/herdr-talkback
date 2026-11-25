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


def test_agy_queued_draft_footer_reads():
    # While a turn runs, agy replaces its footer with the queued-draft hint.
    # Not recognizing it made the composer unreadable exactly when a message
    # had to be queued behind the running turn.
    view = "\n".join([
        RULE,
        ">",
        RULE,
        "  \u25cf Agent(self: dev) \xb7 Working \xb7 27m51s",
        RULE,
        "  Press up to edit queued messages",
    ])

    state = inspect_input(view, "")

    assert state.kind == "empty"
    assert composer_block(view) == ""


def test_agy_composer_block_handles_bare_prompt_line():
    # A bare `>` has no captured draft; composer_block must not crash on it.
    view = "\n".join(["assistant output", RULE, ">", RULE, "? for shortcuts"])

    assert composer_block(view) == ""


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


AGY_AUTO_PLACEHOLDER = "Auto mode: edits & safe commands auto-approved (shift+tab to cycle)"
AGY_FOOTER = "? for shortcuts" + " " * 40 + "auto · Gemini 4 Neon · high"


def test_agy_auto_mode_placeholder_is_empty_not_occupied():
    # Antigravity paints the prompt hint inside the composer; it is not a draft.
    view = screen(AGY_AUTO_PLACEHOLDER, footer=AGY_FOOTER)

    state = inspect_input(view, "")

    assert state.kind == "empty"
    assert state.text_after_prompt == ""
    assert composer_block(view) == ""


def test_agy_auto_mode_placeholder_with_ansi_colors_is_empty():
    composer = ("\x1b[0m\x1b[38;2;138;180;248m>\x1b[0m \x1b[0m\x1b[38;2;154;160;166m"
                + AGY_AUTO_PLACEHOLDER + "\x1b[0m")
    view = "\n".join([
        "\x1b[0m\x1b[38;2;60;64;67m" + RULE + "\x1b[0m",
        composer,
        "\x1b[0m\x1b[38;2;60;64;67m" + RULE + "\x1b[0m",
        "\x1b[0m\x1b[38;2;154;160;166m" + AGY_FOOTER + "\x1b[0m",
    ])

    assert inspect_input(view, "").kind == "empty"


def test_agy_real_draft_still_occupied_next_to_placeholder_screen():
    view = screen("real text typed by the user", footer=AGY_FOOTER)

    state = inspect_input(view, "")

    assert state.kind == "occupied"
    assert state.text_after_prompt == "real text typed by the user"


def scrolled_screen():
    return "\n".join([
        RULE,
        "COMMENT ON COLUMN package_builder.vuln_package_build_history.remark IS '可选备注（如 custom\u2191",
        "  定制包的客户或工单说明）';",
        "[openclaw/agy->pc-76/kb] 补充两个关键问题请 kb 确认",
        "  1. builder 原有 package_builder 建表语句在哪？",
        RULE,
        "esc to cancel" + " " * 30 + "auto · Gemini 4 Neon · high",
    ])


def test_agy_scrolled_draft_is_occupied_not_empty():
    # The `>` line scrolled out; the up arrow on the first visible line is the
    # only evidence that this rule pair holds the live draft.
    state = inspect_input(scrolled_screen(), "")

    assert state.kind == "occupied"
    assert "补充两个关键问题" in state.text_after_prompt


def test_agy_scrolled_draft_is_a_drainable_talkback_draft():
    from herdr_talkback.input_parser import is_talkback_draft

    assert is_talkback_draft(composer_block(scrolled_screen()))


def test_agy_rule_pair_without_prompt_or_scroll_marker_is_not_a_composer():
    view = "\n".join([RULE, "plain transcript text", RULE, "", "? for shortcuts"])

    assert inspect_input(view, "").kind != "occupied"


def tall_screen():
    # The draft outgrew the composer: the top rule scrolled away too.
    return "\n".join([
        " " * 110 + "\u2191",
        "  COMMENT ON TABLE package_builder.vuln_package_build_history IS '漏洞升级包制作历史';",
        "  [openclaw/agy->pc-76/kb] 补充两个关键问题请 kb 确认",
        RULE,
        "esc to cancel" + " " * 30 + "auto · Gemini 4 Neon · high",
    ])


def test_agy_tall_draft_without_top_rule_is_occupied():
    state = inspect_input(tall_screen(), "")

    assert state.kind == "occupied"
    assert "COMMENT ON TABLE" in state.text_after_prompt
    assert state.text_after_prompt.startswith("\u2191")


def test_agy_tall_draft_is_still_a_drainable_talkback_draft():
    from herdr_talkback.input_parser import is_talkback_draft

    assert is_talkback_draft(composer_block(tall_screen()))


def test_command_code_screen_is_not_read_as_a_tall_agy_composer():
    screen = "\n".join([
        "❯ [lead→dev] ping",
        RULE,
        "  » permission bypass on [shift+tab]",
        "  ? for shortcuts · taste on",
    ])

    state = inspect_input(screen, "")

    assert state.kind == "occupied"
    assert state.text_after_prompt == "[lead→dev] ping"


def test_agy_draft_taller_than_viewport_is_occupied():
    # Real capture from pc-76/kb (w1:p1): composer fills the viewport, no rules
    # or footer are visible, first line is `›` with a right-aligned up arrow.
    from pathlib import Path

    raw = Path("tests/fixtures/agy_tall_kb.ansi").read_text(encoding="utf-8")

    state = inspect_input(raw, "")

    assert state.kind == "occupied"
    assert "COMMENT ON TABLE" in state.text_after_prompt
    assert "\u2191" in state.text_after_prompt


def test_agy_draft_taller_than_viewport_is_drainable():
    from pathlib import Path
    from herdr_talkback.input_parser import is_talkback_draft

    raw = Path("tests/fixtures/agy_tall_kb.ansi").read_text(encoding="utf-8")

    assert is_talkback_draft(composer_block(raw))
