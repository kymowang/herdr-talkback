import pytest

from herdr_talkback.input_parser import inspect_input, strip_opencode_sidebar


def screen(draft='', sidebar='Connect provider /connect'):
    rows = ['  ┃' + draft.ljust(40) + '  ' + sidebar,
            '  ┃' + ' ' * 40 + '  sidebar',
            '  ┃  Build · DeepSeek OpenCode'.ljust(43) + '  sidebar',
            '  ╹' + '▀' * 40 + '  sidebar',
            '  ~/arc    ctrl+p commands']
    return '\n'.join(rows)


@pytest.mark.parametrize('draft,expected', [('', 'empty'), ('user draft', 'occupied'), ('中文草稿', 'occupied')])
def test_opencode_composer_excludes_sidebar(draft, expected):
    assert inspect_input(screen(draft), '').kind == expected


def test_opencode_expected_residue_and_partial_text():
    assert inspect_input(screen('continue'), 'continue').kind == 'residue'
    assert inspect_input(screen('contin'), 'continue').kind == 'occupied'


def test_opencode_layout_requires_footer_and_active_bar():
    assert inspect_input(screen().replace('ctrl+p commands', 'unrelated output'), '').kind == 'unknown'
    assert inspect_input(screen().replace('  ┃', '   '), '').kind == 'unknown'
    assert inspect_input(screen() + '\nApproval required: choose an option', '').kind == 'unknown'


def test_opencode_multiline_and_interrupted_sidebar():
    text = screen('first').replace('  ┃' + ' ' * 40, '  ┃second'.ljust(43))
    assert inspect_input(text, 'first\nsecond').kind == 'residue'


def test_opencode_wrapped_long_paste_tail_is_own_residue():
    expected = 'request_id: abc\n' + 'abcdefghij' * 40
    tail = ('abcdefghij' * 20)
    rows = ['  ┃' + tail[i:i+40] for i in range(0, len(tail), 40)]
    rows += ['  ┃  Build · DeepSeek OpenCode', '  ╹' + '▀' * 40, '  ctrl+p commands']
    assert inspect_input('\n'.join(rows), expected).kind == 'residue'
    assert inspect_input(screen('small fragment'), expected).kind == 'occupied'


def test_strip_sidebar_from_monitor_json_without_touching_other_terminals():
    line = '{"request_id":"r1","status":"continue"}'
    view = line.ljust(43) + '  use other models\n' + screen()
    assert strip_opencode_sidebar(view).splitlines()[0].strip() == line
    assert strip_opencode_sidebar('normal terminal line') == 'normal terminal line'


def test_unwrapped_json_with_full_request_id_survives_narrow_viewport():
    from herdr_talkback.logic_monitor import parse_result
    line = '{"request_id":"' + 'a' * 32 + '","status":"continue"}'
    view = line + '\n' + screen()
    assert strip_opencode_sidebar(view).splitlines()[0] == line
    assert parse_result(view, '', 'a' * 32) == 'continue'


def home_screen(input_text, color='128;128;128'):
    return '\n'.join([
        '         OpenCode',
        '    ┃' + ' ' * 50,
        '    ┃  \x1b[38;2;' + color + 'm\x1b[48;2;30;30;30m' + input_text + '\x1b[0m',
        '    ┃' + ' ' * 50,
        '    ┃  Sisyphus - Ultraworker auto · DeepSeek OpenCode Go',
        '    ╹' + '▀' * 50,
        '      tab agents  ctrl+p commands',
        '      ● Tip Use up / ctrl+x down / left / right for parent/child session',
        '  D:\\arc:master  ⊙ 4 MCP /status      1.19.3',
    ])


def test_opencode_home_screen_gray_placeholder_is_empty():
    screen = home_screen('Ask anything… "Fix a TODO in the codebase"')
    assert inspect_input(screen, '').kind == 'empty'


def test_opencode_home_screen_draft_and_overlay_are_not_empty():
    assert inspect_input(home_screen('continue', '255;255;255'), '').kind == 'occupied'
    assert inspect_input(home_screen('Ask anything… "Fix a TODO in the codebase"', '255;255;255'), '').kind == 'occupied'
    assert inspect_input(home_screen('') + '\nApproval required', '').kind == 'unknown'
    stale = '\x1b[38;2;128;128;128mAsk anything… old placeholder\x1b[0m\n'
    assert inspect_input(stale + home_screen('Ask anything… "Fix a TODO in the codebase"', '255;255;255'), '').kind == 'occupied'
