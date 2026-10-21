from herdr_talkback.input_parser import InputState, inspect_input


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
