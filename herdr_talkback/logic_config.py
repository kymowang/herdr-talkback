"""Validated opt-in settings for semantic pane monitoring."""
import math
from dataclasses import dataclass, fields
from typing import Tuple


@dataclass(frozen=True)
class LogicMonitorConfig:
    enabled: bool = False
    monitor_pane: str = ''
    targets: Tuple[str, ...] = ()
    observation_seconds: float = 10
    stable_seconds: float = 10
    poll_seconds: float = 10
    timeout_seconds: float = 120
    cooldown_seconds: float = 120
    max_continues_per_30m: int = 3


def parse_logic_config(raw):
    if not isinstance(raw, dict):
        raise ValueError('logic_monitor must be a table')
    if set(raw) - {f.name for f in fields(LogicMonitorConfig)}:
        raise ValueError('unknown logic_monitor setting')
    values = dict(raw)
    if type(values.get('enabled', False)) is not bool:
        raise ValueError('logic_monitor.enabled must be boolean')
    monitor = values.get('monitor_pane', '')
    targets = values.get('targets', [])
    if not isinstance(monitor, str) or monitor != monitor.strip():
        raise ValueError('monitor_pane must be a pane ID or name')
    if not isinstance(targets, (list, tuple)) or any(
        not isinstance(t, str) or not t.strip() or t != t.strip() for t in targets
    ) or len(set(targets)) != len(targets):
        raise ValueError('targets must be distinct nonempty pane selectors')
    if monitor and monitor in targets:
        raise ValueError('monitor pane cannot monitor itself')
    values['targets'] = tuple(targets)
    for name in ('observation_seconds', 'stable_seconds', 'poll_seconds',
                 'timeout_seconds', 'cooldown_seconds'):
        value = values.get(name, getattr(LogicMonitorConfig(), name))
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError('logic_monitor.' + name + ' must be positive and finite')
    cap = values.get('max_continues_per_30m', 3)
    if type(cap) is not int or cap < 1:
        raise ValueError('max_continues_per_30m must be a positive integer')
    cfg = LogicMonitorConfig(**values)
    if cfg.enabled and (not monitor or not targets):
        raise ValueError('enabled logic_monitor needs monitor_pane and targets')
    return cfg
