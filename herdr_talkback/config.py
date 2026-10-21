from __future__ import annotations

import os
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from .addressing import validate_node_id
from .rules import parse_recovery_rules, parse_message_rules, RecoveryRule, MessageRule

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.9/3.10
    import tomli as tomllib  # type: ignore[import, no-redef]


DEFAULT_ROLES = ("lead", "assistant", "dev-back", "dev-front", "ui", "runner", "codex1", "codex2", "system-bus")
DEFAULT_ALIASES = {"system-bus": "lead"}


def default_rules():
    with Path(__file__).with_name("default-rules.toml").open("rb") as handle:
        return tomllib.load(handle)


@dataclass(frozen=True)
class TalkbackConfig:
    socket_path: Path
    state_path: Path
    daemon_socket_path: Path
    core_node_id: str = "linux-main"
    roles: Tuple[str, ...] = DEFAULT_ROLES
    aliases: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_ALIASES))
    recovery_rules: Tuple[RecoveryRule, ...] = field(default_factory=lambda: parse_recovery_rules(default_rules()['recovery_rules']))
    message_rules: Tuple[MessageRule, ...] = field(default_factory=lambda: parse_message_rules(default_rules()['message_rules']))
    delivery_max_attempts: int = 5
    delivery_retry_interval_seconds: float = 30
    delivery_verify_timeout_ms: int = 5000
    monitor_unknown_threshold_seconds: int = 60
    monitor_continue_cooldown_seconds: int = 120
    monitor_max_continues_per_30m: int = 3
    notify_enabled: bool = True
    notify_position: str = "bottom-right"
    notify_sound: str = "none"
    alerts_pane_role: Optional[str] = None


def default_state_path() -> Path:
    root = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    return (Path(root) if root else Path.home() / ".local" / "state" / "herdr-talkback") / "state.db"


def default_daemon_socket_path() -> Path:
    return default_state_path().parent / "herdr-talkback.sock"


def default_config_path() -> Path:
    root = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    return (Path(root) if root else Path.home() / ".config" / "herdr-talkback") / "config.toml"


def configured_path(value, default):
    session = hashlib.sha256(os.environ.get('HERDR_SOCKET_PATH', 'default').encode()).hexdigest()[:12]
    return Path(str(value or default).replace('{session}', session)).expanduser()


def default_socket_path() -> Path:
    value = os.environ.get("HERDR_SOCKET_PATH")
    if not value:
        raise RuntimeError(
            "HERDR_SOCKET_PATH is not set; start Herdr Talkback inside a Herdr pane or set socket_path in config"
        )
    return Path(value)


def load_config(path: Optional[Path]) -> TalkbackConfig:
    raw: Dict[str, Any] = {}
    resolved_path = _resolve_config_path(path)
    if resolved_path is not None:
        with resolved_path.open("rb") as handle:
            raw = tomllib.load(handle)

    delivery = _mapping(raw.get("delivery"))
    monitor = _mapping(raw.get("monitor"))
    notify = _mapping(raw.get("notify"))
    core = _mapping(raw.get("core"))
    alerts = _mapping(raw.get("alerts"))
    if "node" in raw or "listen" in core or "public_url" in core:
        raise ValueError("remote Node configuration is no longer supported; use local Herdr panes")
    aliases = dict(DEFAULT_ALIASES)
    aliases.update(_mapping(raw.get("aliases")))

    return TalkbackConfig(
        socket_path=Path(raw.get("socket_path") or default_socket_path()),
        state_path=configured_path(raw.get("state_path"), default_state_path()),
        daemon_socket_path=configured_path(raw.get("daemon_socket_path"), default_daemon_socket_path()),
        core_node_id=validate_node_id(core.get("node_id", "linux-main")),
        roles=tuple(raw.get("roles", DEFAULT_ROLES)),
        aliases=aliases,
        recovery_rules=parse_recovery_rules(raw.get('recovery_rules', default_rules()['recovery_rules'])),
        message_rules=parse_message_rules(raw.get('message_rules', default_rules()['message_rules'])),
        delivery_max_attempts=int(delivery.get("max_attempts", 5)),
        delivery_retry_interval_seconds=float(delivery.get("retry_interval_seconds", 30)),
        delivery_verify_timeout_ms=int(delivery.get("verify_timeout_ms", 5000)),
        monitor_unknown_threshold_seconds=int(monitor.get("unknown_threshold_seconds", 60)),
        monitor_continue_cooldown_seconds=int(monitor.get("continue_cooldown_seconds", 120)),
        monitor_max_continues_per_30m=int(monitor.get("max_continues_per_30m", 3)),
        notify_enabled=bool(notify.get("enabled", True)),
        notify_position=str(notify.get("position", "bottom-right")),
        notify_sound=str(notify.get("sound", "none")),
        alerts_pane_role=_optional_string(alerts.get("pane_role")),
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"expected mapping, got {type(value).__name__}")
    return value


def _optional_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _resolve_config_path(path: Optional[Path]) -> Optional[Path]:
    if path is not None:
        return Path(path)
    default_path = default_config_path()
    if default_path.exists():
        return default_path
    return None
