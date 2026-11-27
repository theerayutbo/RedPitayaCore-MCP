from __future__ import annotations

import asyncio
import os

import pytest

from redpitayacore_mcp import transport
from redpitayacore_mcp.config import Settings, ssh_password_for


def live(**kw):
    return Settings(mode="live", host="192.0.2.10", ssh_user="root", **kw)


def test_key_mode_is_default_and_has_no_password():
    assert ssh_password_for(live()) is None


def test_password_mode_requires_a_password_or_explicit_factory_default():
    with pytest.raises(ValueError, match="RP_SSH_PASSWORD"):
        live(ssh_auth="password")
    assert ssh_password_for(live(ssh_auth="password", ssh_use_factory_default=True)) == "root"
    assert ssh_password_for(live(ssh_auth="password", ssh_password="s3cret")) == "s3cret"


def test_password_never_in_repr_or_from_env_defaults():
    cfg = live(ssh_auth="password", ssh_password="s3cret")
    assert "s3cret" not in repr(cfg)
    env = {"RP_MODE": "live", "RP_HOST": "192.0.2.10", "RP_SSH_USER": "root",
           "RP_SSH_AUTH": "password", "RP_SSH_USE_FACTORY_DEFAULT": "true"}
    assert ssh_password_for(Settings.from_env(env)) == "root"


def _exists(path: str) -> bool:
    return os.path.exists(path)


class _FakeProc:
    returncode = 0

    def __init__(self):
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(b"temp_raw=1\n")
        self.stdout.feed_eof()

    async def wait(self):
        return 0


async def test_password_goes_through_askpass_env_not_argv(monkeypatch):
    seen = {}

    async def fake_exec(*args, **kwargs):
        seen["args"], seen["env"] = args, kwargs.get("env")
        seen["helper_exists"] = _exists(kwargs["env"]["SSH_ASKPASS"])
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    cfg = live(ssh_auth="password", ssh_password="s3cret")
    res = await transport._ssh_command(cfg, transport.BOARD_HEALTH_COMMAND)
    assert res["available"] is True
    assert all("s3cret" not in str(a) for a in seen["args"])
    assert seen["env"]["SSH_ASKPASS_REQUIRE"] == "force"
    assert seen["env"]["RP_ASKPASS_SECRET"] == "s3cret"
    assert "BatchMode=no" in seen["args"] and "StrictHostKeyChecking=yes" in seen["args"]
    assert seen["helper_exists"] and not _exists(seen["env"]["SSH_ASKPASS"])  # cleaned up


async def test_key_mode_keeps_batch_mode(monkeypatch):
    seen = {}

    async def fake_exec(*args, **kwargs):
        seen["args"], seen["env"] = args, kwargs.get("env")
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await transport._ssh_command(live(), transport.BOARD_HEALTH_COMMAND)
    assert "BatchMode=yes" in seen["args"] and seen["env"] is None
