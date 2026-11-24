from herdr_talkback.input_parser import InputState, inspect_input, sanitize_snapshot


def test_codex_submission_requires_dim_prompt_and_post_send_verification():
    message = '[lead->dev-back] ping'
    submitted = '\x1b[0m\x1b[1m\x1b[2m› ' + message
    assert inspect_input(submitted, message, allow_transformed_residue=True).kind == 'submitted'
    assert inspect_input(submitted, message).kind == 'unknown'
    for prefix in ('\x1b[48;2;50;50;50m', '\x1b[2m\x1b[22m', '\x1b[2m\x1b[0m'):
        assert inspect_input(prefix + '› ' + message, message, allow_transformed_residue=True).kind == 'residue'


def test_gray_placeholder_is_empty():
    text = "\x1b[38;2;255;255;255m●\x1b[0m ❯ \x1b[38;2;136;136;136mType a message\x1b[0m"

    state = inspect_input(text, expected_message="[lead→dev-back] ping")

    assert state.kind == "empty"
    assert state.text_after_prompt == "Type a message"


def test_bright_text_after_prompt_is_occupied():
    text = "\x1b[38;2;255;255;255m●\x1b[0m ❯ \x1b[38;2;255;255;255mhalf written user text\x1b[0m"

    state = inspect_input(text, expected_message="[lead→dev-back] ping")

    assert state.kind == "occupied"
    assert state.text_after_prompt == "half written user text"


def test_expected_message_after_prompt_is_residue():
    text = "❯ \x1b[38;2;255;255;255m[lead→dev-back] ping\x1b[0m"

    state = inspect_input(text, expected_message="[lead→dev-back] ping")

    assert state.kind == "residue"
    assert state.contains_expected_message is True


def test_expected_message_with_status_footer_is_residue():
    text = (
        "› \x1b[38;2;255;255;255m[codex1→codex2] ping\x1b[0m"
        "\n  \x1b[38;2;246;226;183mgpt-5.5 medium\x1b[0m · ~/arc"
    )

    state = inspect_input(text, expected_message="[codex1→codex2] ping")

    assert state.kind == "residue"
    assert state.contains_expected_message is True


def test_expected_message_soft_wrapped_after_prompt_is_residue_when_transformed_residue_allowed():
    expected = "[assistant->dev-fe] long routed message with enough text to wrap at the terminal"
    text = (
        "❯ \x1b[38;2;255;255;255m[assistant->dev-fe] long routed message with enough text\n"
        "  to wrap at the terminal\x1b[0m\n"
        "  \x1b[38;2;255;107;128m⏵⏵ bypass permissions on\x1b[0m"
    )

    state = inspect_input(text, expected_message=expected, allow_transformed_residue=True)

    assert state.kind == "residue"
    assert state.contains_expected_message is True


def test_expected_message_soft_wrap_inserted_space_is_residue_when_transformed_residue_allowed():
    expected = "[assistant->dev-be] GlobalExceptionHandler前缀耦合修复对称"
    text = (
        "❯ \x1b[38;2;255;255;255m[assistant->dev-be] GlobalExceptionHandler\n"
        "  前缀耦合修复对称\x1b[0m\n"
        "  \x1b[38;2;255;107;128m⏵⏵ bypass permissions on\x1b[0m"
    )

    state = inspect_input(text, expected_message=expected, allow_transformed_residue=True)

    assert state.kind == "residue"
    assert state.contains_expected_message is True


def test_expected_message_soft_wrap_prefix_is_occupied_before_sending():
    expected = "[assistant->dev-fe] long routed message with enough text to wrap at the terminal"
    text = "❯ \x1b[38;2;255;255;255m[assistant->dev-fe] long routed message with enough text\x1b[0m"

    state = inspect_input(text, expected_message=expected)

    assert state.kind == "occupied"
    assert state.contains_expected_message is False


def test_expected_image_placeholder_tail_is_residue_only_when_transformed_residue_allowed():
    expected = "[dev-fe->scheduler] review evidence: /tmp/inbox-richtext-e2e-bell-1440.png。请串行验收合并。"
    text = "❯ \x1b[38;2;255;255;255m[Image #9] [Image #10]/tmp/inbox-richtext-e2e-bell-1440.png。请串行验收合并。\x1b[0m"

    before_sending = inspect_input(text, expected_message=expected)
    after_sending = inspect_input(text, expected_message=expected, allow_transformed_residue=True)

    assert before_sending.kind == "occupied"
    assert after_sending.kind == "residue"
    assert after_sending.contains_expected_message is True


def test_expected_long_single_line_paste_block_is_residue_when_transformed_residue_allowed():
    expected = "[dev-be->lead] " + "system-name-rename completed and ready for review " * 4
    text = "❯ \x1b[38;2;255;255;255m[Pasted text #3]\x1b[0m"

    state = inspect_input(text, expected_message=expected, allow_transformed_residue=True)

    assert state.kind == "residue"
    assert state.contains_expected_message is True


def test_expected_long_single_line_paste_block_is_occupied_before_sending():
    expected = "[dev-be->lead] " + "system-name-rename completed and ready for review " * 4
    text = "❯ \x1b[38;2;255;255;255m[Pasted text #3]\x1b[0m"

    state = inspect_input(text, expected_message=expected)

    assert state.kind == "occupied"
    assert state.contains_expected_message is False


def test_short_expected_message_paste_block_is_occupied_when_transformed_residue_allowed():
    text = "❯ \x1b[38;2;255;255;255m[Pasted text #3]\x1b[0m"

    state = inspect_input(text, expected_message="[lead->dev-back] ping", allow_transformed_residue=True)

    assert state.kind == "occupied"
    assert state.contains_expected_message is False


def test_no_prompt_is_unknown():
    state = inspect_input("normal output only", expected_message="[lead→dev-back] ping")

    assert state.kind == "unknown"


def test_esc_to_cancel_overlay_is_dismissible():
    text = (
        "Settings  Status   Config   Usage   Stats\n\n"
        "Version:          2.1.197\n"
        "Session ID:       81b5b2d5-cdb5-408f-9608-299f0644119e\n\n"
        "\x1b[38;2;153;153;153mEsc to cancel\x1b[0m"
    )

    state = inspect_input(text, expected_message="[lead→dev-back] ping")

    assert state.kind == "dismissible_overlay"
    assert state.text_after_prompt == "Esc to cancel"


def test_codex_dim_placeholder_is_empty():
    text = "› \x1b[2m\x1b[48;2;59;59;59mWrite tests for @filename\x1b[0m"

    state = inspect_input(text, expected_message="[codex1→codex2] ping")

    assert state.kind == "empty"
    assert state.text_after_prompt == "Write tests for @filename"


def test_codex_placeholder_with_status_footer_is_empty():
    text = (
        "\x1b[1m\x1b[48;2;59;59;59m›\x1b[0m"
        "\x1b[48;2;59;59;59m \x1b[0m"
        "\x1b[2m\x1b[48;2;59;59;59mWrite tests for @filename\x1b[0m"
        "\n  \x1b[38;2;246;226;183mgpt-5.5 medium\x1b[0m · ~/arc"
    )

    state = inspect_input(text, expected_message="[codex1→codex2] ping")

    assert state.kind == "empty"
    assert state.text_after_prompt == "Write tests for @filename"


def test_codex_explain_codebase_placeholder_is_empty():
    text = (
        "\x1b[1m\x1b[48;2;59;59;59m›\x1b[0m"
        "\x1b[48;2;59;59;59m \x1b[0m"
        "\x1b[2m\x1b[48;2;59;59;59mExplain this codebase\x1b[0m"
        "\n  \x1b[38;2;246;226;183mgpt-5.5 medium\x1b[0m · ~/arc"
    )

    state = inspect_input(text, expected_message="[codex1→codex2] ping")

    assert state.kind == "empty"
    assert state.text_after_prompt == "Explain this codebase"


def test_dim_suggested_action_after_prompt_is_empty():
    text = "❯\u00a0\x1b[0m\x1b[2mping 一下 dev-back 看护栏修复进度\x1b[0m"

    state = inspect_input(text, expected_message="[lead→assistant] protocol update")

    assert state.kind == "empty"
    assert state.text_after_prompt == "ping 一下 dev-back 看护栏修复进度"


def test_codex_real_text_after_prompt_is_occupied():
    text = "› \x1b[38;2;255;255;255mactual user draft\x1b[0m"

    state = inspect_input(text, expected_message="[codex1→codex2] ping")

    assert state.kind == "occupied"
    assert state.text_after_prompt == "actual user draft"


def test_bright_placeholder_prefix_text_is_occupied():
    text = "❯ \x1b[38;2;255;255;255mType a message now\x1b[0m"

    state = inspect_input(text, expected_message="[lead→dev-back] ping")

    assert state.kind == "occupied"
    assert state.text_after_prompt == "Type a message now"


def test_expected_message_with_extra_text_is_occupied():
    text = "❯ \x1b[38;2;255;255;255m[lead→dev-back] ping plus extra\x1b[0m"

    state = inspect_input(text, expected_message="[lead→dev-back] ping")

    assert state.kind == "occupied"
    assert state.text_after_prompt == "[lead→dev-back] ping plus extra"
    assert state.contains_expected_message is False


def _sep(width=40):
    return "\u2500" * width


CLAUDE_GHOST_PROMPT = "\n".join([
    _sep(),
    "\u276f\xa0\x1b[0m\x1b[2mcontinue\x1b[0m",
    _sep(),
    "  \x1b[2m\x1b[38;2;153;153;153m~/arc (u) | ctx 50% | gpt-6.1-terra\x1b[0m",
    "  \x1b[38;2;255;107;128m\u23f5\u23f5 bypass permissions on (shift+tab to cycle)\x1b[0m",
])

CODEX_PLACEHOLDER_PROMPT = "\n".join([
    "  earlier transcript line",
    "",
    "\u203a \x1b[2mAsk Codex to do anything\x1b[0m",
    "  GPT-6.1 Terra low \xb7 ~/arc \xb7 u \xb7 Main [default]",
    "  Earlier messages available. enter/esc latest \xb7 ? shortcuts",
])


def test_dim_ghost_prompt_matching_control_word_is_empty_not_residue():
    # Claude 空输入框画 dim 的 ghost ``continue``;监控的控制词也是 continue。
    # 顺序错了会把 ghost 当成本方残留(strict 发送被自己拒绝)。
    assert inspect_input(CLAUDE_GHOST_PROMPT, "continue").kind == "empty"
    assert inspect_input(CLAUDE_GHOST_PROMPT, "").kind == "empty"


def test_sanitize_snapshot_drops_claude_ghost_prompt():
    out = sanitize_snapshot(CLAUDE_GHOST_PROMPT)
    assert "continue" not in out
    assert "bypass permissions" in out


def test_sanitize_snapshot_drops_codex_placeholder():
    out = sanitize_snapshot(CODEX_PLACEHOLDER_PROMPT)
    assert "Ask Codex to do anything" not in out
    assert "GPT-6.1" in out


def test_sanitize_snapshot_keeps_dim_transcript_echo_before_assistant_output():
    # Codex 生成中会把最后一条用户消息以 dim 回显在历史里;后面是助手输出 → 不是输入框,保留。
    text = "\n".join([
        "earlier assistant text",
        "",
        "\u203a \x1b[2mcontinue\x1b[0m",
        "  assistant is still working on it",
        "  more output",
    ])
    out = sanitize_snapshot(text)
    assert "continue" in out
    assert "assistant is still working" in out


def test_sanitize_snapshot_drops_idle_composer_draft():
    # 输入框在屏幕底部、上下都是 chrome → 属于输入框;不进判断快照(输入是否可写由 _empty 单独把关)。
    text = "\n".join([
        _sep(),
        "\u276f \x1b[38;2;248;248;242ma user draft\x1b[0m",
        _sep(),
        "  ~/arc (u) | ctx 20% | gpt-6.1-terra",
    ])
    out = sanitize_snapshot(text)
    assert "a user draft" not in out
def test_wrapped_message_with_invisible_zero_width_space_matches_after_send():
    expected = '[sender->target] dependency sc2+A\u200bAD/cacheFixedSalt requires verification and a regression test'
    screen = ('\u203a [sender->target] dependency sc2+AAD/cacheFixedSalt requires\n'
              '  verification and a regression test\n  tab to queue message')
    assert inspect_input(screen, expected, allow_transformed_residue=True).kind == 'residue'
    assert inspect_input(screen, expected).kind == 'occupied'


def test_wrapped_message_with_different_visible_text_is_not_residue():
    expected = '[sender->target] dependency sc2+A\u200bAD/cacheFixedSalt requires verification and a regression test'
    screen = ('\u203a [sender->target] dependency sc2+AAD/cacheFixedSalt requires\n'
              '  verification and a different regression test\n  tab to queue message')
    assert inspect_input(screen, expected, allow_transformed_residue=True).kind == 'occupied'


def test_codex_collapsed_paste_is_residue_only_after_sending():
    message = '[lead->dev-back] ' + 'long task text ' * 100
    screen = '\u203a [Pasted Content 1099 chars]remaining text\n  tab to queue message'
    assert inspect_input(screen, message).kind == 'occupied'
    assert inspect_input(screen, message, allow_transformed_residue=True).kind == 'residue'


def test_dim_codex_collapsed_history_is_not_composer_residue():
    message = '[lead->dev-back] ' + 'long task text ' * 100
    screen = '\x1b[2m\u203a [Pasted Content 1099 chars]\x1b[0m'
    assert inspect_input(screen, message, allow_transformed_residue=True).kind != 'residue'


def test_codex_collapsed_paste_on_wrapped_composer_line_is_recognized():
    screen = '\u203a a visible prefix\n  [Pasted Content 1099 chars]remaining text\n  tab to queue message'
    assert inspect_input(screen, 'message', allow_transformed_residue=True).kind == 'residue'
    assert inspect_input(screen, 'message').kind == 'occupied'


def test_folded_multiline_draft_is_occupied_not_empty():
    # deepseek `cmd` keeps an unsubmitted multi-line draft folded in the composer.
    screen = "❯ [[lead2→lea… +2L]"

    state = inspect_input(screen, expected_message="[scheduler→pc-76] 另一条要求\n第二行\n第三行")

    assert state.kind == "occupied"
    assert state.text_after_prompt == "[[lead2→lea… +2L]"


def test_folded_multiline_draft_matching_expected_is_residue():
    message = "[lead2→lead3] 收到第三轮。请继续\n第二行\n第三行"
    screen = "❯ \x1b[38;2;255;255;255m[[lead2→lea… +2L]\x1b[0m"

    state = inspect_input(screen, expected_message=message, agent="cmd")

    assert state.kind == "residue"
    assert state.contains_expected_message is True
    # Other agents never get the residue interpretation for folded drafts.
    assert inspect_input(screen, expected_message=message).kind == "occupied"
    assert inspect_input(screen, expected_message=message, agent="codex").kind == "occupied"


def test_folded_draft_with_fewer_lines_than_message_is_occupied():
    message = "[lead2→lead3] 收到第三轮。请继续\n第二行\n第三行\n第四行"  # 4 lines -> at least +3L

    state = inspect_input("❯ [[lead2→lea… +1L]", expected_message=message)

    assert state.kind == "occupied"


def test_folded_draft_with_short_prefix_is_occupied():
    message = "[a] x\n第二行"

    state = inspect_input("❯ [[a] x… +1L]", expected_message=message)

    assert state.kind == "occupied"


def test_folded_draft_is_residue_after_send_with_stale_agent_metadata():
    # Herdr still reports agent=codex while the pane runs Command Code, which
    # folds the just-sent draft. Delivery verification must trust the rendered
    # fold marker instead of the pane's agent name.
    message = "[openclaw→codex2] 任务交接：剩余 2 条红\n请确认改测试还是记已知红"
    screen = "❯ [[openclaw→… +1L]"

    state = inspect_input(screen, expected_message=message,
                          allow_transformed_residue=True, agent="codex")

    assert state.kind == "residue"
    assert state.contains_expected_message is True
    # Before sending, another agent's folded draft stays occupied.
    assert inspect_input(screen, expected_message=message, agent="codex").kind == "occupied"


def test_folded_draft_wrapped_opening_line_is_residue():
    # The CLI counts rendered lines, so soft wrapping makes the marker larger
    # than the logical newline count.
    message = "[lead2→lead3] " + "收到。按用户已授权的完整升级链继续：" + "x" * 600 + "\n回执说明"
    screen = "❯ [[lead2→lea… +2L]"

    state = inspect_input(screen, expected_message=message, agent="cmd")

    assert state.kind == "residue"
    assert state.contains_expected_message is True


def test_head_truncated_long_opening_line_is_residue():
    message = "[lead2→lead3] 收到。按用户已授权的完整升级链继续：" + "x" * 600 + "\n回执说明"
    head = message.split("\n")[0][:80]

    state = inspect_input("❯ " + head, expected_message=message,
                          allow_transformed_residue=True, agent="cmd")

    assert state.kind == "residue"
    assert state.contains_expected_message is True
    assert inspect_input("❯ " + head, expected_message=message).kind == "occupied"


def test_short_visible_head_of_expected_message_stays_occupied():
    message = "[lead2→lead3] 收到。按用户已授权的完整升级链继续：" + "x" * 600

    state = inspect_input("❯ [lead2→lea", expected_message=message)

    assert state.kind == "occupied"


def test_codex_chevron_composer_placeholder_is_empty():
    screen = "\n".join([
        "\u00bb Ask Codex to do anything",
        "  gpt-6.2-sol ultra · ~\\code\\vmsOps · Main [default]",
        "  \u2190 for agents · ? for shortcuts",
    ])

    state = inspect_input(screen, "")

    assert state.kind == "empty"
    assert state.text_after_prompt == "Ask Codex to do anything"


def test_codex_chevron_composer_draft_is_occupied():
    screen = "\n".join([
        "\u00bb 给openclaw/talkback发一条测试消息",
        "  gpt-6.2-sol ultra · ~\\code\\vmsOps · Main [default]",
    ])

    state = inspect_input(screen, "")

    assert state.kind == "occupied"
    assert state.text_after_prompt == "给openclaw/talkback发一条测试消息"


def test_indented_chevron_chrome_is_not_a_prompt():
    screen = "\n".join([
        "\u276f [lead\u2192dev] ping",
        "  \u00bb permission bypass on [shift+tab]",
        "  ? for shortcuts · taste on",
    ])

    state = inspect_input(screen, "")

    assert state.kind == "occupied"
    assert state.text_after_prompt == "[lead\u2192dev] ping"


CODEX_PROMPT_SGR = "\x1b[0m\x1b[1m\x1b[38;2;188;140;247m"


def test_styled_codex_chevron_placeholder_behind_history_prompt_is_empty():
    # The composer marker carries SGR codes; the dim U+203A line above is history.
    screen = "\n".join([
        "\x1b[2m\u203a 给openclaw/talkback发一条测试消息\x1b[0m",
        "assistant output",
        CODEX_PROMPT_SGR + "\u00bb \x1b[0m\x1b[38;2;154;160;166mAsk Codex to do anything\x1b[0m",
        "  gpt-6.2-sol ultra · ~\\code\\vmsOps · Main [default]",
        "  \u2190 for agents · ? for shortcuts",
    ])

    state = inspect_input(screen, "")

    assert state.kind == "empty"
    assert state.text_after_prompt == "Ask Codex to do anything"


def test_styled_codex_chevron_real_draft_is_occupied():
    screen = "\n".join([
        "\x1b[2m\u203a 上一条已提交的消息\x1b[0m",
        "assistant output",
        CODEX_PROMPT_SGR + "\u00bb 请检查 pc-76 的状态",
        "  gpt-6.2-sol ultra · ~\\code\\vmsOps · Main [default]",
        "  \u2190 for agents · ? for shortcuts",
    ])

    state = inspect_input(screen, "")

    assert state.kind == "occupied"
    assert state.text_after_prompt == "请检查 pc-76 的状态"


def test_styled_indented_chevron_chrome_is_still_not_a_prompt():
    screen = "\n".join([
        "\u276f [lead\u2192dev] ping",
        "  " + CODEX_PROMPT_SGR + "\u00bb permission bypass on [shift+tab]",
        "  ? for shortcuts · taste on",
    ])

    assert inspect_input(screen, "").text_after_prompt == "[lead\u2192dev] ping"


def test_scrolled_draft_with_dim_up_arrow_is_occupied_not_empty():
    # Real Codex scrolled composer: normal-brightness draft text with a dim `↑`
    # scroll indicator at the right edge must never be mistaken for an empty placeholder.
    screen = (
        "\x1b[0m\x1b[1m\x1b[48;2;41;41;41m›\x1b[0m"
        "\x1b[48;2;41;41;41m   ✓ \"先逐方法对照\" —— 与双方 ACK 的要求一致      "
        "\x1b[0m\x1b[2m\x1b[48;2;41;41;41m↑\x1b[0m\n"
        "\x1b[0m\x1b[48;2;41;41;41m  [ast-dev5->ast-dev2] 后续未提交正文\x1b[0m"
    )

    state = inspect_input(screen, expected_message="[lead→ast-dev2] new message")

    assert state.kind == "occupied"


def test_scrolled_draft_with_blank_first_line_and_dim_up_arrow_is_occupied():
    screen = (
        "\x1b[0m\x1b[1m\x1b[48;2;41;41;41m›\x1b[0m"
        "\x1b[48;2;41;41;41m          \x1b[0m\x1b[2m\x1b[48;2;41;41;41m↑\x1b[0m\n"
        "\x1b[0m\x1b[48;2;41;41;41m  第二行仍在输入框里\x1b[0m"
    )

    state = inspect_input(screen, expected_message="[lead→ast-dev2] new message")

    assert state.kind == "occupied"


def test_partial_dim_autocomplete_suffix_after_real_text_is_occupied():
    screen = "❯ \x1b[38;2;255;255;255mgit st\x1b[2matus\x1b[0m"

    state = inspect_input(screen, expected_message="[lead→ast-dev2] ping")

    assert state.kind == "occupied"


def test_scrolled_just_sent_message_tail_is_residue_only_after_sending():
    expected = (
        "[monitor→ast-dev2] 很长的第一段已经滚出视口\n"
        "✓ \"先逐方法对照\" —— 与双方 ACK 的要求一致\n"
        "收到本消息后，请执行 herdr-talkback message ack msg_123 置消息状态为delivered"
    )
    screen = (
        "\x1b[0m\x1b[1m\x1b[48;2;41;41;41m›\x1b[0m"
        "\x1b[48;2;41;41;41m  ✓ \"先逐方法对照\" —— 与双方 ACK 的要求一致   "
        "\x1b[0m\x1b[2m\x1b[48;2;41;41;41m↑\x1b[0m\n"
        "\x1b[0m\x1b[48;2;41;41;41m  收到本消息后，请执行 herdr-talkback message ack msg_123 置消息状态为delivered\x1b[0m\n"
        "  GPT-6.1 Terra low · D:\\arc · Main [default]"
    )

    assert inspect_input(screen, expected_message=expected).kind == "occupied"
    after = inspect_input(screen, expected_message=expected, allow_transformed_residue=True)
    assert after.kind == "residue"
    assert after.contains_expected_message is True
