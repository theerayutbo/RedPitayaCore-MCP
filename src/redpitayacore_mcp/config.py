"""Operator-only configuration. Tools cannot select a target or enable permissions."""

from __future__ import annotations

import ipaddress
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast


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
    allow_tx: bool = False
    tx_min_hz: int | None = None
    tx_max_hz: int | None = None
    tx_max_amplitude: float = 0.1
    tx_max_seconds: float = 10.0
    tx_lead_s: float = 0.1
    rx_sample_order: Literal["glibc_memcpy", "raw"] = "glibc_memcpy"
    allow_lifecycle: bool = False
    ssh_user: str | None = None
    ssh_port: int = 22
    project_root: Path | None = None
    allow_project_write: bool = False
    allow_local_build: bool = False
    vivado_executable: Path | None = None
    build_timeout: float = 300

    def __post_init__(self) -> None:
        if self.rx_sample_order not in {"glibc_memcpy", "raw"}:
            raise ValueError("RP_RX_SAMPLE_ORDER must be glibc_memcpy or raw")
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
        if self.allow_tx and (self.tx_min_hz is None or self.tx_max_hz is None):
            raise ValueError("RP_ALLOW_TX requires RP_TX_MIN_HZ and RP_TX_MAX_HZ")
        if self.tx_min_hz is not None or self.tx_max_hz is not None:
            if self.tx_min_hz is None or self.tx_max_hz is None:
                raise ValueError("Set RP_TX_MIN_HZ and RP_TX_MAX_HZ together")
            if not 1 <= self.tx_min_hz <= self.tx_max_hz <= 60_000_000:
                raise ValueError("RP_TX_MIN_HZ and RP_TX_MAX_HZ must be ordered, 1 to 60000000")
        if not math.isfinite(self.tx_max_amplitude) or not 0 < self.tx_max_amplitude <= 1:
            raise ValueError("RP_TX_MAX_AMPLITUDE must be finite, above 0 and at most 1")
        if not math.isfinite(self.tx_lead_s) or not 0.05 <= self.tx_lead_s <= 1:
            raise ValueError("RP_TX_LEAD_S must be finite, from 0.05 to 1 second")
        if not math.isfinite(self.tx_max_seconds) or not 0.1 <= self.tx_max_seconds <= 180:
            raise ValueError("RP_TX_MAX_SECONDS must be finite, from 0.1 to 180 seconds")
        if not math.isfinite(self.timeout) or not 0.1 <= self.timeout <= 10:
            raise ValueError("RP_TIMEOUT must be finite, from 0.1 to 10 seconds")
        if self.ssh_user is not None and not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", self.ssh_user):
            raise ValueError("RP_SSH_USER must be a simple Unix account name")
        if self.project_root is not None and not self.project_root.is_absolute():
            raise ValueError("RP_PROJECT_ROOT must be an absolute local project directory")
        if self.vivado_executable is not None and not self.vivado_executable.is_absolute():
            raise ValueError("RP_VIVADO_EXECUTABLE must be an absolute operator-selected path")
        if not math.isfinite(self.build_timeout) or not 0.1 <= self.build_timeout <= 3600:
            raise ValueError("RP_BUILD_TIMEOUT must be finite, from 0.1 to 3600 seconds")

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
            allow_tx=_flag(env, "RP_ALLOW_TX"),
            tx_min_hz=int(env["RP_TX_MIN_HZ"]) if env.get("RP_TX_MIN_HZ") else None,
            tx_max_hz=int(env["RP_TX_MAX_HZ"]) if env.get("RP_TX_MAX_HZ") else None,
            tx_max_amplitude=float(env.get("RP_TX_MAX_AMPLITUDE", "0.1")),
            tx_max_seconds=float(env.get("RP_TX_MAX_SECONDS", "10")),
            tx_lead_s=float(env.get("RP_TX_LEAD_S", "0.1")),
            rx_sample_order=cast(
                Literal["glibc_memcpy", "raw"], env.get("RP_RX_SAMPLE_ORDER", "glibc_memcpy")
            ),
            allow_lifecycle=_flag(env, "RP_ALLOW_LIFECYCLE"),
            ssh_user=env.get("RP_SSH_USER") or None,
            ssh_port=int(env.get("RP_SSH_PORT", "22")),
            project_root=Path(env["RP_PROJECT_ROOT"]) if env.get("RP_PROJECT_ROOT") else None,
            allow_project_write=_flag(env, "RP_ALLOW_PROJECT_WRITE"),
            allow_local_build=_flag(env, "RP_ALLOW_LOCAL_BUILD"),
            vivado_executable=Path(env["RP_VIVADO_EXECUTABLE"])
            if env.get("RP_VIVADO_EXECUTABLE")
            else None,
            build_timeout=float(env.get("RP_BUILD_TIMEOUT", "300")),
        )
