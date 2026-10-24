import pytest

from herdr_talkback.input_parser import inspect_input


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
