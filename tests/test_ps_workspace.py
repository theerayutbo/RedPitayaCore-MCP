import pytest

from redpitayacore_mcp.workspace import FPGAWorkspace


def test_ps_source_isolation_and_hash_guard(tmp_path):
    options = {
        "source_extensions": frozenset({".py", ".c", ".h", ".cpp", ".hpp"}),
        "artifact_extensions": frozenset(),
        "report_extensions": frozenset(),
    }
    workspace = FPGAWorkspace(tmp_path, allow_write=True, **options)
    readonly = FPGAWorkspace(tmp_path, **options)
    workspace.write_source("driver.py", "x = 1\n")
    source = workspace.read_source("driver.py")
    assert source["sha256"]
    assert "driver.py" in workspace.info()["files"]
    with pytest.raises(PermissionError):
        readonly.write_source("other.c", "int x;")
    with pytest.raises(PermissionError):
        workspace.write_source("driver.py", "x = 2\n")
    with pytest.raises(PermissionError):
        workspace.write_source("driver.py", "x = 2\n", "0" * 64)
    workspace.write_source("driver.py", "x = 2\n", source["sha256"])
    assert workspace.read_source("driver.py")["sha256"] != source["sha256"]
    with pytest.raises(ValueError):
        workspace.write_source("logic.v", "module logic; endmodule")
    with pytest.raises(ValueError):
        workspace.write_source("../outside.py", "x = 1")
    assert not (tmp_path.parent / "outside.py").exists()
