import asyncio
from types import SimpleNamespace

from herdr_talkback import cli


def test_pane_list_includes_unnamed_and_service_panes(monkeypatch, capsys):
    requests = []

    class Client:
        async def request(self, method, params):
            requests.append((method, params))
            return {'panes': [
                {'pane_id': 'w1:p1', 'agent': 'codex', 'agent_status': 'working',
                 'tab_id': 'w1:t1', 'cwd': '/repo/项目'},
                {'pane_id': 'w1:p2', 'label': 'Talkback service',
                 'tab_id': 'w1:t2'},
            ]}

    monkeypatch.setattr(cli, 'load_config', lambda path: SimpleNamespace(socket_path='test'))
    monkeypatch.setattr(cli, 'HerdrClient', lambda path: Client())
    args = cli.build_parser().parse_args(['pane', 'list'])
    assert asyncio.run(cli._main_async(args)) == 0
    output = capsys.readouterr().out
    assert requests == [('pane.list', {})]
    assert 'PANE' in output and 'STATUS' in output
    assert 'w1:p1' in output and 'codex' in output and '/repo/项目' in output
    assert 'Talkback service' in output and 'w1:p2' in output


def test_pane_table_aligns_chinese_names_and_normalizes_line_breaks():
    output = cli._format_pane_table([
        {'pane_id': 'w1:p1', 'label': '开发', 'agent': 'codex'},
        {'pane_id': 'w1:p2', 'label': 'dev', 'agent': 'claude', 'cwd': '/repo\nproject'},
    ])
    lines = output.splitlines()
    assert len(lines) == 4  # Header, separator, two panes.
    # Chinese characters occupy two terminal columns.
    first_prefix = lines[2].split('codex')[0]
    second_prefix = lines[3].split('claude')[0]
    assert cli._display_width(first_prefix) == cli._display_width(second_prefix)
    assert '/repo project' in lines[3]


def test_pane_table_reports_empty_session():
    assert cli._format_pane_table([]) == 'No panes found.'


def test_pane_list_shows_tab_name_and_terminal_title(monkeypatch, capsys):
    requests = []

    class Client:
        async def request(self, method, params):
            requests.append((method, params))
            if method == 'pane.list':
                return {'panes': [
                    {'pane_id': 'w2:p5', 'workspace_id': 'w2', 'tab_id': 'w2:t5',
                     'terminal_title': 'raw title', 'terminal_title_stripped': 'npm prefix'},
                    {'pane_id': 'w2:p6', 'workspace_id': 'w2', 'tab_id': 'w2:t6',
                     'terminal_title': 'fallback title'},
                ]}
            assert method == 'tab.list'
            assert params == {'workspace_id': 'w2'}
            return {'tabs': [{'tab_id': 'w2:t5', 'label': 'jupyter'}]}

    monkeypatch.setattr(cli, 'load_config', lambda path: SimpleNamespace(socket_path='test'))
    monkeypatch.setattr(cli, 'HerdrClient', lambda path: Client())
    args = cli.build_parser().parse_args(['pane', 'list'])
    assert asyncio.run(cli._main_async(args)) == 0
    output = capsys.readouterr().out
    assert 'TAB NAME' in output and 'TITLE' in output
    assert 'jupyter' in output and 'npm prefix' in output
    assert 'raw title' not in output and 'fallback title' in output
    assert requests == [('pane.list', {}), ('tab.list', {'workspace_id': 'w2'})]
