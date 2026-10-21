import asyncio

import pytest

from herdr_talkback.roles import RoleRegistry, RoleResolutionError


class FakeClient:
    def __init__(self):
        self.requests = []

    async def request(self, method, params):
        self.requests.append((method, params))
        assert method == "pane.list"
        assert params == {}
        return {
            "type": "pane_list",
            "panes": [
                {"label": "lead", "agent": "claude", "agent_status": "idle", "pane_id": "p-lead", "workspace_id": "w1", "tab_id": "t1"},
                {"label": "dev-back", "agent": "claude", "agent_status": "done", "pane_id": "p-back", "workspace_id": "w1", "tab_id": "t2"},
                {"label": "alerts", "agent_status": "unknown", "pane_id": "p-alerts", "workspace_id": "w1", "tab_id": "t3"},
                {"agent": "codex", "agent_status": "working", "pane_id": "p-codex", "workspace_id": "w1", "tab_id": "t3"},
            ],
        }


def test_refresh_and_resolve_roles():
    asyncio.run(_refresh_and_resolve_roles())


async def _refresh_and_resolve_roles():
    registry = RoleRegistry(FakeClient(), roles=(), aliases={"system-bus": "lead"})
    await registry.refresh()

    assert registry.resolve_pane("lead") == "p-lead"
    assert registry.resolve_pane("system-bus") == "p-lead"
    assert registry.resolve_pane("dev-back") == "p-back"
    assert registry.resolve_pane("alerts") == "p-alerts"
    assert registry.role_for_pane("p-back") == "dev-back"


def test_missing_role_raises():
    asyncio.run(_missing_role_raises())


async def _missing_role_raises():
    registry = RoleRegistry(FakeClient(), roles=("runner",), aliases={})
    await registry.refresh()

    with pytest.raises(RoleResolutionError):
        registry.resolve_pane("runner")
