import asyncio
from types import SimpleNamespace

import pytest

from herdr_talkback import cli


@pytest.mark.parametrize('tab_name', ['jupyter', '开发环境'])
def test_pane_name_from_tab_uses_current_tab_label(monkeypatch, capsys, tab_name):
    requests = []

    class Client:
        async def request(self, method, params):
            requests.append((method, params))
            if method == 'pane.list':
                return {'panes': [{'pane_id': 'w2:p5', 'workspace_id': 'w2',
                                   'tab_id': 'w2:t5'}]}
            if method == 'tab.list':
                return {'tabs': [{'tab_id': 'w2:t5', 'label': tab_name}]}
            assert method == 'pane.rename'
            return {'type': 'ok'}

    monkeypatch.setattr(cli, 'load_config', lambda path: SimpleNamespace(socket_path='test'))
    monkeypatch.setattr(cli, 'HerdrClient', lambda path: Client())
    args = cli.build_parser().parse_args(['pane', 'name-from-tab', 'w2:p5'])
    assert asyncio.run(cli._main_async(args)) == 0
    assert requests == [
        ('pane.list', {}), ('tab.list', {'workspace_id': 'w2'}),
        ('pane.rename', {'pane_id': 'w2:p5', 'label': tab_name}),
    ]
    assert tab_name in capsys.readouterr().out


@pytest.mark.parametrize('panes,tabs,error', [
    ([], [], 'pane not found'),
    ([{'pane_id': 'w2:p5', 'workspace_id': 'w2', 'tab_id': 'w2:t5'}], [], 'Tab has no name'),
    ([{'pane_id': 'w2:p5', 'workspace_id': 'w2', 'tab_id': 'w2:t5'}],
     [{'tab_id': 'w2:t5', 'label': '  '}], 'Tab has no name'),
])
def test_pane_name_from_tab_does_not_rename_when_source_missing(monkeypatch, capsys, panes, tabs, error):
    class Client:
        async def request(self, method, params):
            if method == 'pane.list':
                return {'panes': panes}
            assert method == 'tab.list', 'must not rename without a source name'
            return {'tabs': tabs}

    monkeypatch.setattr(cli, 'load_config', lambda path: SimpleNamespace(socket_path='test'))
    monkeypatch.setattr(cli, 'HerdrClient', lambda path: Client())
    args = cli.build_parser().parse_args(['pane', 'name-from-tab', 'w2:p5'])
    assert asyncio.run(cli._main_async(args)) == 1
    assert error in capsys.readouterr().out


@pytest.mark.parametrize('force', [False, True])
def test_bulk_naming_preserves_existing_names_unless_forced(monkeypatch, capsys, force):
    renamed = []

    class Client:
        async def request(self, method, params):
            if method == 'pane.list':
                return {'panes': [
                    {'pane_id': 'w2:p1', 'workspace_id': 'w2', 'tab_id': 'w2:t1'},
                    {'pane_id': 'w2:p2', 'workspace_id': 'w2', 'tab_id': 'w2:t2', 'label': 'existing'},
                    {'pane_id': 'w3:p1', 'workspace_id': 'w3', 'tab_id': 'w3:t1'},
                ]}
            if method == 'tab.list':
                workspace = params['workspace_id']
                return {'tabs': [
                    {'tab_id': f'{workspace}:t1', 'label': f'{workspace}-first'},
                    {'tab_id': f'{workspace}:t2', 'label': f'{workspace}-second'},
                ]}
            assert method == 'pane.rename'
            renamed.append(params)
            return {'type': 'ok'}

    monkeypatch.setattr(cli, 'load_config', lambda path: SimpleNamespace(socket_path='test'))
    monkeypatch.setattr(cli, 'HerdrClient', lambda path: Client())
    argv = ['pane', 'name-from-tab', '--all'] + (['--force'] if force else [])
    assert asyncio.run(cli._main_async(cli.build_parser().parse_args(argv))) == 0
    expected = [{'pane_id': 'w2:p1', 'label': 'w2-first'}]
    if force:
        expected.append({'pane_id': 'w2:p2', 'label': 'w2-second'})
    expected.append({'pane_id': 'w3:p1', 'label': 'w3-first'})
    assert renamed == expected


def test_single_named_pane_is_skipped_by_default(monkeypatch, capsys):
    class Client:
        async def request(self, method, params):
            assert method == 'pane.list', 'named panes must not be modified by default'
            return {'panes': [{'pane_id': 'w2:p5', 'label': 'existing'}]}

    monkeypatch.setattr(cli, 'load_config', lambda path: SimpleNamespace(socket_path='test'))
    monkeypatch.setattr(cli, 'HerdrClient', lambda path: Client())
    args = cli.build_parser().parse_args(['pane', 'name-from-tab', 'w2:p5'])
    assert asyncio.run(cli._main_async(args)) == 0
    assert 'skipped' in capsys.readouterr().out
