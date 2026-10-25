from __future__ import annotations

import hashlib
import multiprocessing
import os
import queue
from pathlib import Path
from typing import Any

import pytest

from redpitayacore_mcp import workspace as workspace_module
from redpitayacore_mcp.workspace import FPGAWorkspace


def test_non_posix_keeps_unconfigured_mock_usable_but_refuses_workspace(tmp_path, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(workspace_module.os, "name", "nt")
        assert FPGAWorkspace(None).info()["available"] is False
        with pytest.raises(NotImplementedError, match="require POSIX"):
            FPGAWorkspace(tmp_path)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _concurrent_writer(
    root: str,
    expected: str,
    content: str,
    pause_during_write: bool,
    entered_write: Any,
    release_write: Any,
    results: Any,
) -> None:
    if pause_during_write:
        original_write_all = FPGAWorkspace._write_all

        def paused_write_all(descriptor: int, data: bytes) -> None:
            entered_write.set()
            if not release_write.wait(5):
                raise TimeoutError("test did not release paused writer")
            original_write_all(descriptor, data)

        FPGAWorkspace._write_all = staticmethod(paused_write_all)
    try:
        FPGAWorkspace(Path(root), allow_write=True).write_source(
            "top.v", content, expected_sha256=expected
        )
    except PermissionError:
        results.put("stale")
    else:
        results.put("written")


class _NoiseEntry:
    def __init__(self, name: str) -> None:
        self.name = name

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        return False

    def is_file(self, *, follow_symlinks: bool = True) -> bool:
        return True


class _BoundedScandir:
    def __init__(self) -> None:
        self.calls = 0

    def __enter__(self) -> _BoundedScandir:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def __iter__(self) -> _BoundedScandir:
        return self

    def __next__(self) -> _NoiseEntry:
        self.calls += 1
        if self.calls > 2_000:
            raise AssertionError("info() consumed beyond its global scan budget")
        return _NoiseEntry(f"noise_{self.calls}.txt")


def test_unconfigured_workspace_is_inert() -> None:
    workspace = FPGAWorkspace(None)

    info = workspace.info()

    assert info["available"] is False
    assert info["root"] is None
    assert info["allow_write"] is False
    with pytest.raises(PermissionError, match="FPGA workspace root is not configured"):
        workspace.read_source("rtl/top.v")
    with pytest.raises(PermissionError, match="FPGA workspace root is not configured"):
        workspace.read_report("reports/timing.rpt")
    with pytest.raises(PermissionError, match="FPGA workspace root is not configured"):
        workspace.inspect_artifact("build/top.bit")


def test_root_must_be_an_existing_directory(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    regular_file = tmp_path / "file"
    regular_file.write_text("x")

    with pytest.raises(ValueError, match="existing directory"):
        FPGAWorkspace(missing)
    with pytest.raises(ValueError, match="existing directory"):
        FPGAWorkspace(regular_file)


def test_operator_configuration_is_available_as_read_only_properties(tmp_path: Path) -> None:
    workspace = FPGAWorkspace(tmp_path, allow_write=True)

    assert workspace.root == tmp_path.resolve()
    assert workspace.allow_write is True
    with pytest.raises(AttributeError):
        workspace.root = tmp_path.parent  # type: ignore[misc]
    with pytest.raises(AttributeError):
        workspace.allow_write = False  # type: ignore[misc]


def test_info_lists_only_bounded_safe_workspace_files(tmp_path: Path) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "top.v").write_text("module top; endmodule\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "ignored.bit").write_bytes(b"bit")
    (tmp_path / ".secret.v").write_text("secret")
    (tmp_path / "notes.txt").write_text("notes")

    info = FPGAWorkspace(tmp_path).info()

    assert info == {
        "available": True,
        "root": str(tmp_path.resolve()),
        "allow_write": False,
        "source_extensions": [".sv", ".v", ".vhd", ".vhdl", ".xdc"],
        "artifact_extensions": [".bin", ".bit", ".dcp", ".dtbo", ".ltx", ".rpt"],
        "report_extensions": [".ltx", ".rpt"],
        "source_max_bytes": 256 * 1024,
        "report_max_bytes": 256 * 1024,
        "artifact_max_bytes": 64 * 1024 * 1024,
        "files": ["rtl/top.v"],
        "files_truncated": False,
        "file_limit": 500,
        "write_consistency": "serialized_cooperating_writers_only",
        "requires_quiescent_external_editors": True,
    }


def test_info_caps_file_listing_at_500(tmp_path: Path) -> None:
    for index in range(501):
        (tmp_path / f"source_{index:03}.v").write_text("module x; endmodule\n")

    info = FPGAWorkspace(tmp_path).info()

    assert len(info["files"]) == 500
    assert info["files_truncated"] is True


def test_info_bounds_scanning_of_unrecognized_entries(tmp_path: Path) -> None:
    for index in range(2_001):
        (tmp_path / f"noise_{index:04}.txt").touch()

    info = FPGAWorkspace(tmp_path).info()

    assert info["files"] == []
    assert info["files_truncated"] is True


def test_info_consumes_scandir_lazily_to_enforce_global_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = _BoundedScandir()
    monkeypatch.setattr(workspace_module.os, "scandir", lambda _fd: entries)

    info = FPGAWorkspace(tmp_path).info()

    assert entries.calls == 2_000
    assert info["files"] == []
    assert info["files_truncated"] is True


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/etc/passwd",
        "../secret.v",
        "rtl/../secret.v",
        "rtl\\top.v",
        "rtl//top.v",
        "rtl/./top.v",
        "rtl/.hidden/top.v",
        "rtl/top.v\x00ignored",
    ],
)
def test_tool_paths_reject_traversal_and_ambiguous_forms(tmp_path: Path, path: str) -> None:
    workspace = FPGAWorkspace(tmp_path)

    with pytest.raises(ValueError):
        workspace.resolve_source(path)


def test_source_read_returns_utf8_snapshot_and_hash(tmp_path: Path) -> None:
    content = "module top; // สวัสดี\nendmodule\n"
    (tmp_path / "top.sv").write_text(content, encoding="utf-8")

    result = FPGAWorkspace(tmp_path).read_source("top.sv")

    assert result == {
        "path": "top.sv",
        "content": content,
        "sha256": sha256(content.encode()),
        "size": len(content.encode()),
    }


@pytest.mark.parametrize("path", ["top.txt", "top.SV", "top.bit"])
def test_source_read_rejects_unapproved_extensions(tmp_path: Path, path: str) -> None:
    (tmp_path / path).write_text("text")

    with pytest.raises(ValueError, match="source extension"):
        FPGAWorkspace(tmp_path).read_source(path)


def test_source_read_rejects_invalid_utf8_and_oversize(tmp_path: Path) -> None:
    (tmp_path / "binary.v").write_bytes(b"\xff")
    (tmp_path / "large.v").write_bytes(b"x" * (256 * 1024 + 1))
    workspace = FPGAWorkspace(tmp_path)

    with pytest.raises(ValueError, match="UTF-8"):
        workspace.read_source("binary.v")
    with pytest.raises(ValueError, match="256 KiB"):
        workspace.read_source("large.v")


def test_read_rejects_symlinks_and_hardlinks(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.v"
    outside.write_text("secret")
    (tmp_path / "link.v").symlink_to(outside)
    os.link(outside, tmp_path / "hard.v")
    workspace = FPGAWorkspace(tmp_path)

    with pytest.raises(PermissionError, match="link"):
        workspace.read_source("link.v")
    with pytest.raises(PermissionError, match="link"):
        workspace.read_source("hard.v")


def test_read_rejects_symlinked_parent_directory(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-dir"
    outside.mkdir()
    (outside / "top.v").write_text("secret")
    (tmp_path / "rtl").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PermissionError, match="link"):
        FPGAWorkspace(tmp_path).read_source("rtl/top.v")


def test_write_requires_explicit_permission(tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="writes are disabled"):
        FPGAWorkspace(tmp_path).write_source("rtl/top.v", "module top; endmodule\n")


def test_write_creates_new_source_and_nested_directories_atomically(tmp_path: Path) -> None:
    content = "module top; endmodule\n"

    result = FPGAWorkspace(tmp_path, allow_write=True).write_source("rtl/core/top.v", content)

    assert (tmp_path / "rtl/core/top.v").read_text() == content
    assert result == {
        "path": "rtl/core/top.v",
        "sha256": sha256(content.encode()),
        "size": len(content),
        "created": True,
        "write_consistency": "serialized_cooperating_writers_only",
        "requires_quiescent_external_editors": True,
    }
    assert list((tmp_path / "rtl/core").glob(".*.tmp-*")) == []


def test_existing_write_requires_matching_hash(tmp_path: Path) -> None:
    original = b"module old; endmodule\n"
    target = tmp_path / "top.v"
    target.write_bytes(original)
    workspace = FPGAWorkspace(tmp_path, allow_write=True)

    with pytest.raises(PermissionError, match="expected_sha256 is required"):
        workspace.write_source("top.v", "module new; endmodule\n")
    with pytest.raises(PermissionError, match="stale"):
        workspace.write_source("top.v", "module new; endmodule\n", "0" * 64)
    assert target.read_bytes() == original

    result = workspace.write_source(
        "top.v", "module new; endmodule\n", expected_sha256=sha256(original)
    )
    assert result["created"] is False
    assert target.read_text() == "module new; endmodule\n"


def test_independent_mcp_writers_are_serialized_and_only_one_hash_wins(tmp_path: Path) -> None:
    original = b"module old; endmodule\n"
    (tmp_path / "top.v").write_bytes(original)
    expected = sha256(original)
    context = multiprocessing.get_context("spawn")
    entered_write = context.Event()
    release_write = context.Event()
    results = context.Queue()
    first = context.Process(
        target=_concurrent_writer,
        args=(
            str(tmp_path),
            expected,
            "module first; endmodule\n",
            True,
            entered_write,
            release_write,
            results,
        ),
    )
    second = context.Process(
        target=_concurrent_writer,
        args=(
            str(tmp_path),
            expected,
            "module second; endmodule\n",
            False,
            entered_write,
            release_write,
            results,
        ),
    )

    first.start()
    assert entered_write.wait(5)
    second.start()
    with pytest.raises(queue.Empty):
        results.get(timeout=0.5)
    release_write.set()
    first.join(5)
    second.join(5)

    assert first.exitcode == 0
    assert second.exitcode == 0
    assert sorted([results.get(timeout=1), results.get(timeout=1)]) == ["stale", "written"]


@pytest.mark.parametrize("value", ["", "abc", "g" * 64, "0" * 63, "0" * 65])
def test_write_rejects_malformed_expected_hash(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError, match="64 hexadecimal"):
        FPGAWorkspace(tmp_path, allow_write=True).write_source("top.v", "x", value)


def test_new_file_rejects_an_expected_hash(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="does not exist"):
        FPGAWorkspace(tmp_path, allow_write=True).write_source("top.v", "x", "0" * 64)


def test_write_rejects_oversize_symlink_and_hardlink_targets(tmp_path: Path) -> None:
    workspace = FPGAWorkspace(tmp_path, allow_write=True)
    with pytest.raises(ValueError, match="256 KiB"):
        workspace.write_source("large.v", "x" * (256 * 1024 + 1))

    outside = tmp_path.parent / f"{tmp_path.name}-outside-write.v"
    outside.write_text("secret")
    (tmp_path / "link.v").symlink_to(outside)
    os.link(outside, tmp_path / "hard.v")
    for name in ("link.v", "hard.v"):
        with pytest.raises(PermissionError, match="link"):
            workspace.write_source(name, "replacement", sha256(b"secret"))
    assert outside.read_text() == "secret"


def test_inspect_artifact_streams_hash_and_does_not_claim_hardware_validation(
    tmp_path: Path,
) -> None:
    payload = b"opaque-bitstream-payload"
    (tmp_path / "top.bit").write_bytes(payload)

    result = FPGAWorkspace(tmp_path).inspect_artifact("top.bit", expected_sha256="f" * 64)

    assert result == {
        "path": "top.bit",
        "sha256": sha256(payload),
        "size": len(payload),
        "format": "Xilinx bitstream",
        "format_verified": False,
        "format_source": "filename_extension_only",
        "expected_sha256": "f" * 64,
        "hash_matches": False,
        "fpga_loaded": False,
        "board_compatibility_verified": False,
    }


def test_inspect_device_tree_overlay_uses_extension_label_only(tmp_path: Path) -> None:
    payload = b"opaque-device-tree-overlay"
    (tmp_path / "redpitaya.dtbo").write_bytes(payload)

    result = FPGAWorkspace(tmp_path).inspect_artifact("redpitaya.dtbo")

    assert result["format"] == "device-tree overlay"
    assert result["format_verified"] is False
    assert result["format_source"] == "filename_extension_only"
    assert result["sha256"] == sha256(payload)


@pytest.mark.parametrize("path", ["top.zip", "top.v", "top.BIT"])
def test_inspect_artifact_rejects_unapproved_extensions(tmp_path: Path, path: str) -> None:
    (tmp_path / path).write_bytes(b"x")
    with pytest.raises(ValueError, match="artifact extension"):
        FPGAWorkspace(tmp_path).inspect_artifact(path)


def test_inspect_artifact_rejects_oversize_without_reading_it_all(tmp_path: Path) -> None:
    artifact = tmp_path / "large.bit"
    with artifact.open("wb") as handle:
        handle.truncate(64 * 1024 * 1024 + 1)

    with pytest.raises(ValueError, match="64 MiB"):
        FPGAWorkspace(tmp_path).inspect_artifact("large.bit")


def test_read_report_returns_bounded_utf8_snapshot(tmp_path: Path) -> None:
    content = "Slack (MET) : 0.125ns\n"
    (tmp_path / "timing.rpt").write_text(content)

    result = FPGAWorkspace(tmp_path).read_report("timing.rpt")

    assert result == {
        "path": "timing.rpt",
        "content": content,
        "sha256": sha256(content.encode()),
        "size": len(content),
    }
    assert "timing_passed" not in result


@pytest.mark.parametrize("path", ["top.bit", "top.dcp", "top.txt"])
def test_read_report_allows_only_report_extensions(tmp_path: Path, path: str) -> None:
    (tmp_path / path).write_text("report")
    with pytest.raises(ValueError, match="report extension"):
        FPGAWorkspace(tmp_path).read_report(path)


def test_file_descriptors_are_closed_after_read_errors(tmp_path: Path) -> None:
    if not Path("/dev/fd").exists():
        pytest.skip("descriptor accounting requires /dev/fd")
    (tmp_path / "bad.v").write_bytes(b"\xff")
    workspace = FPGAWorkspace(tmp_path)
    before = len(list(Path("/dev/fd").iterdir()))

    for _ in range(20):
        with pytest.raises(ValueError, match="UTF-8"):
            workspace.read_source("bad.v")

    after = len(list(Path("/dev/fd").iterdir()))
    assert after <= before + 1
