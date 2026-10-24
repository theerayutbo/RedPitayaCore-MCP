"""Operator-only configuration. Tools cannot select a target or enable permissions."""

from __future__ import annotations

import ipaddress
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal


def _flag(env: Mapping[str, str], key: str) -> bool:
    value = env.get(key, "false").lower()
    if value not in {"true", "false", "1", "0"}:
        raise ValueError(f"{key} must be true/false or 1/0")
    return value in {"true", "1"}


@dataclass(frozen=True)
class Settings:
    mode: Literal["mock", "live"] = "mock"
    host: str | None = None
    port: int = 1001
    http_port: int = 80
    timeout: float = 3.0
    allow_rx: bool = False
    allow_lifecycle: bool = False
    ssh_user: str | None = None
    ssh_port: int = 22

    def __post_init__(self) -> None:
        if self.mode not in {"mock", "live"}:
            raise ValueError("RP_MODE must be mock or live")
        if self.mode == "live" and not self.host:
            raise ValueError("Live mode requires RP_HOST set to the board IPv4 address")
        if self.host is not None:
            address = ipaddress.IPv4Address(self.host)
            if address.is_multicast or address.is_unspecified or int(address) == 0xFFFFFFFF:
                raise ValueError("RP_HOST must be a unicast IPv4 address")
        for port in (self.port, self.http_port, self.ssh_port):
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError("Ports must be integers from 1 to 65535")
        if not math.isfinite(self.timeout) or not 0.1 <= self.timeout <= 10:
            raise ValueError("RP_TIMEOUT must be finite, from 0.1 to 10 seconds")
        if self.ssh_user is not None and not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", self.ssh_user):
            raise ValueError("RP_SSH_USER must be a simple Unix account name")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        mode = env.get("RP_MODE", "mock")
        if mode not in {"mock", "live"}:
            raise ValueError("RP_MODE must be mock or live")
        return cls(
            mode=mode,  # type: ignore[arg-type]
            host=env.get("RP_HOST") or None,
            port=int(env.get("RP_PORT", "1001")),
            http_port=int(env.get("RP_HTTP_PORT", "80")),
            timeout=float(env.get("RP_TIMEOUT", "3")),
            allow_rx=_flag(env, "RP_ALLOW_RX"),
            allow_lifecycle=_flag(env, "RP_ALLOW_LIFECYCLE"),
            ssh_user=env.get("RP_SSH_USER") or None,
            ssh_port=int(env.get("RP_SSH_PORT", "22")),
        )
