"""Validated, agent-scoped policy; commands are prompt text, never shell commands."""
from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Optional, Tuple
import re


def matches_scope(name, patterns):
    return any(fnmatchcase(name or '', pattern) for pattern in patterns)


@dataclass(frozen=True)
class RecoveryRule:
    id: str
    agents: Tuple[str, ...]
    agent_types: Tuple[str, ...]
    patterns: tuple
    statuses: Tuple[str, ...]
    command: str = 'continue'
    window: str = 'latest_turn'
    enabled: bool = True
    cooldown_seconds: Optional[int] = None
    max_per_30m: Optional[int] = None

    def matches(self, name, agent_type, status, text):
        return (self.enabled and matches_scope(name, self.agents)
                and (not self.agent_types or matches_scope(agent_type, self.agent_types))
                and status in self.statuses and any(p.search(text) for p in self.patterns))


@dataclass(frozen=True)
class MessageRule:
    id: str
    agents: Tuple[str, ...]
    targets: Tuple[str, ...]
    append: str
    enabled: bool = True


def _strings(value, name, empty=False):
    if not isinstance(value, list) or (not empty and not value) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValueError(name + ' must be an array of nonempty strings')
    return tuple(value)


def _rows(rows):
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise ValueError('rules must be an array of tables')
    ids = [r.get('id') for r in rows]
    if not all(isinstance(i, str) and i.strip() for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('rule ids must be nonempty and unique')
    for r in rows:
        if not isinstance(r.get('enabled', True), bool):
            raise ValueError('enabled must be a boolean')
    return rows


def parse_recovery_rules(rows):
    result = []
    allowed = {'id', 'agents', 'agent_types', 'patterns', 'statuses', 'command', 'window', 'enabled', 'cooldown_seconds', 'max_per_30m'}
    for r in _rows(rows):
        if set(r) - allowed:
            raise ValueError('unknown recovery rule fields: ' + ', '.join(sorted(set(r) - allowed)))
        patterns = _strings(r.get('patterns'), 'patterns')
        try:
            compiled = tuple(re.compile(p, re.IGNORECASE | re.MULTILINE) for p in patterns)
        except re.error as e:
            raise ValueError('invalid pattern in rule ' + r['id'] + ': ' + str(e)) from e
        statuses = _strings(r.get('statuses', ['idle', 'done', 'blocked', 'unknown']), 'statuses')
        if set(statuses) - {'idle', 'done', 'blocked', 'unknown'}:
            raise ValueError('automatic recovery cannot target working or invalid statuses')
        window = r.get('window', 'latest_turn')
        if window not in {'latest_turn', 'recent'}:
            raise ValueError('window must be latest_turn or recent')
        command = r.get('command', 'continue')
        if not isinstance(command, str) or not command.strip():
            raise ValueError('command must be nonempty text')
        for key in ('cooldown_seconds', 'max_per_30m'):
            v = r.get(key)
            if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
                raise ValueError(key + ' must be a nonnegative integer')
        result.append(RecoveryRule(r['id'], _strings(r.get('agents'), 'agents'),
            _strings(r.get('agent_types', []), 'agent_types', empty=True), compiled, statuses,
            command, window, r.get('enabled', True), r.get('cooldown_seconds'), r.get('max_per_30m')))
    return tuple(result)


def parse_message_rules(rows):
    result = []
    allowed = {'id', 'agents', 'targets', 'append', 'enabled'}
    for r in _rows(rows):
        if set(r) - allowed:
            raise ValueError('unknown message rule fields: ' + ', '.join(sorted(set(r) - allowed)))
        text = r.get('append')
        if not isinstance(text, str) or not text.strip():
            raise ValueError('append must be nonempty text')
        result.append(MessageRule(r['id'], _strings(r.get('agents'), 'agents'),
            _strings(r.get('targets', ['*']), 'targets'), text.strip(), r.get('enabled', True)))
    return tuple(result)
