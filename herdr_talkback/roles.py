from __future__ import annotations

from typing import Mapping, Optional, Sequence

from .addressing import SEGMENT_RE
from .types import AgentInfo


class RoleResolutionError(RuntimeError):
    pass


class RoleRegistry:
    def __init__(self, client, roles: Sequence[str], aliases: Mapping[str, str]):
        self.client = client
        self.roles = tuple(roles)
        self.aliases = dict(aliases)
        self._by_role: dict[str, AgentInfo] = {}
        self._by_pane: dict[str, str] = {}
        self._by_pane_info: dict[str, AgentInfo] = {}

    async def refresh(self) -> None:
        result = await self.client.request("pane.list", {})
        by_role: dict[str, AgentInfo] = {}
        by_pane: dict[str, str] = {}
        by_pane_info: dict[str, AgentInfo] = {}

        for raw_pane in result.get("panes", []):
            name = raw_pane.get("label")

            agent_info = AgentInfo(
                name=name,
                agent=raw_pane.get("agent"),
                agent_status=raw_pane.get("agent_status") or "unknown",
                pane_id=raw_pane["pane_id"],
                workspace_id=raw_pane["workspace_id"],
                tab_id=raw_pane["tab_id"],
                cwd=raw_pane.get("cwd"),
            )
            # Pane titles (including our own service pane) are not necessarily
            # valid message addresses. Keep them queryable by pane ID only.
            if isinstance(name, str) and SEGMENT_RE.fullmatch(name):
                by_role[name] = agent_info
                by_pane[agent_info.pane_id] = name
            by_pane_info[agent_info.pane_id] = agent_info

        self._by_role = by_role
        self._by_pane = by_pane
        self._by_pane_info = by_pane_info

    def resolve_role(self, role: str) -> str:
        return self.aliases.get(role, role)

    def resolve_pane(self, role: str) -> str:
        return self.get_agent(role).pane_id

    def get_agent(self, role: str) -> AgentInfo:
        resolved_role = self.resolve_role(role)
        agent = self._by_role.get(resolved_role)
        if agent is None:
            raise RoleResolutionError(f"role has no active pane: {role}")
        return agent

    def role_for_pane(self, pane_id: str) -> Optional[str]:
        return self._by_pane.get(pane_id)

    def get_pane(self, pane_id: str) -> Optional[AgentInfo]:
        return self._by_pane_info.get(pane_id)

    def snapshot(self) -> dict[str, AgentInfo]:
        return dict(self._by_role)
