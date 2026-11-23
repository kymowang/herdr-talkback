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
        self._ambiguous: dict[str, tuple[str, ...]] = {}

    async def refresh(self) -> None:
        result = await self.client.request("pane.list", {})
        candidates: dict[str, list[AgentInfo]] = {}
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
                candidates.setdefault(name, []).append(agent_info)
                by_pane[agent_info.pane_id] = name
            by_pane_info[agent_info.pane_id] = agent_info

        # A name shared by several panes cannot address any of them: routing
        # must never silently pick the last registered pane.
        self._by_role = {name: infos[0] for name, infos in candidates.items() if len(infos) == 1}
        self._ambiguous = {
            name: tuple(info.pane_id for info in infos)
            for name, infos in candidates.items() if len(infos) > 1
        }
        self._by_pane = by_pane
        self._by_pane_info = by_pane_info

    def panes(self) -> tuple[AgentInfo, ...]:
        return tuple(self._by_pane_info.values())

    def addressable_names(self) -> set[str]:
        """Every live pane label, including names that are ambiguous."""
        return set(self._by_role) | set(self._ambiguous)

    def resolve_role(self, role: str) -> str:
        return self.aliases.get(role, role)

    def resolve_pane(self, role: str) -> str:
        info = self._by_pane_info.get(role)
        if info is not None:
            return info.pane_id
        return self.get_agent(role).pane_id

    def check_source(self, name: str) -> str:
        """A source must name exactly one live pane; return the resolved name."""
        if name in self._by_pane_info:
            return name
        resolved = self.resolve_role(name)
        self._raise_if_ambiguous(resolved, 'source pane')
        if resolved not in self._by_role:
            raise RoleResolutionError(f"source pane not found: {name}")
        return resolved

    def get_agent(self, role: str) -> AgentInfo:
        resolved_role = self.resolve_role(role)
        self._raise_if_ambiguous(resolved_role, 'pane name')
        agent = self._by_role.get(resolved_role)
        if agent is None:
            raise RoleResolutionError(f"role has no active pane: {role}")
        return agent

    def _raise_if_ambiguous(self, name: str, kind: str) -> None:
        conflicts = self._ambiguous.get(name)
        if conflicts:
            raise RoleResolutionError(
                "{0} is ambiguous: {1} ({2}); use a pane ID".format(
                    kind, name, ", ".join(conflicts)))

    def role_for_pane(self, pane_id: str) -> Optional[str]:
        return self._by_pane.get(pane_id)

    def get_pane(self, pane_id: str) -> Optional[AgentInfo]:
        return self._by_pane_info.get(pane_id)

    def panes_named(self, name: str) -> tuple[AgentInfo, ...]:
        return tuple(info for info in self._by_pane_info.values() if info.name == name)

    def snapshot(self) -> dict[str, AgentInfo]:
        return dict(self._by_role)
