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
