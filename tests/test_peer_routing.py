import asyncio
from types import SimpleNamespace

from herdr_talkback.daemon import DaemonMessageRouter
from herdr_talkback.state import TalkbackState


def test_daemon_accepts_configured_remote_target_without_local_pane(tmp_path):
    async def scenario():
        state = TalkbackState(tmp_path / 'state.db', local_node_id='sender')
        state.init()
        ensured, wakes = [], []

        class Registry:
            async def refresh(self):
                pass

            def resolve_pane(self, role):
                raise AssertionError('remote destinations must not resolve as local panes')

        cfg = SimpleNamespace(core_node_id='sender', roles=(), aliases={}, message_rules=())
        router = DaemonMessageRouter(cfg, Registry(), SimpleNamespace(wake=wakes.append), state=state,
                                     peer_adapter=SimpleNamespace(ensure_endpoint=ensured.append))
        response = await router.handle({'type': 'send', 'message': '[agent->receiver/agent] hello'})
        assert response['ok'] and response['queued']
        assert ensured[0].qualified == 'receiver/agent'
        assert wakes == ['receiver/agent']
        assert state.get_message(response['message_id'])['target_pane_id'] == ''

    asyncio.run(scenario())
