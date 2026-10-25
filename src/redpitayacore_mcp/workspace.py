"""Local, operator-configured FPGA workspace access with strict path boundaries."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

if os.name == "posix":
    import fcntl

SOURCE_EXTENSIONS: Final = frozenset({".v", ".sv", ".vhd", ".vhdl", ".xdc"})
ARTIFACT_EXTENSIONS: Final = frozenset({".bit", ".bin", ".ltx", ".rpt", ".dcp", ".dtbo"})
REPORT_EXTENSIONS: Final = frozenset({".rpt", ".ltx"})
SOURCE_MAX_BYTES: Final = 256 * 1024
REPORT_MAX_BYTES: Final = 256 * 1024
ARTIFACT_MAX_BYTES: Final = 64 * 1024 * 1024
FILE_LIST_LIMIT: Final = 500
INFO_SCAN_LIMIT: Final = 2_000
_IGNORED_DIRECTORIES: Final = frozenset(
    {".git", ".venv", "venv", "build", "dist", "__pycache__", ".mypy_cache", ".pytest_cache"}
)
_HASH_PATTERN: Final = re.compile(r"[0-9a-fA-F]{64}")
_NOFOLLOW: Final = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY: Final = getattr(os, "O_DIRECTORY", 0)
_CLOEXEC: Final = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK: Final = getattr(os, "O_NONBLOCK", 0)
_WRITE_LOCK_NAME: Final = ".redpitayacore-workspace.lock"
_WRITE_CONSISTENCY: Final = "serialized_cooperating_writers_only"


class FPGAWorkspace:
    """Provide bounded access to one explicitly configured local FPGA project."""

    def __init__(self, root: Path | None, allow_write: bool = False) -> None:
        self._allow_write = allow_write
        if root is None:
            self._root: Path | None = None
            return
        if os.name != "posix":
            raise NotImplementedError("Configured local FPGA workspaces currently require POSIX")
        try:
            canonical = root.expanduser().resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise ValueError("FPGA workspace root must be an existing directory") from exc
        if not canonical.is_dir():
            raise ValueError("FPGA workspace root must be an existing directory")
        self._root = canonical

    @property
    def root(self) -> Path | None:
        """Return the canonical operator-configured root, when available."""
        return self._root

    @property
    def allow_write(self) -> bool:
        """Return whether the operator enabled bounded source writes."""
        return self._allow_write

    def info(self) -> dict[str, Any]:
        """Return capabilities and a bounded inventory without reading file contents."""
        common: dict[str, Any] = {
            "available": self._root is not None,
            "root": str(self._root) if self._root is not None else None,
            "allow_write": self._allow_write,
            "source_extensions": sorted(SOURCE_EXTENSIONS),
            "artifact_extensions": sorted(ARTIFACT_EXTENSIONS),
            "report_extensions": sorted(REPORT_EXTENSIONS),
            "source_max_bytes": SOURCE_MAX_BYTES,
            "report_max_bytes": REPORT_MAX_BYTES,
            "artifact_max_bytes": ARTIFACT_MAX_BYTES,
            "files": [],
            "files_truncated": False,
            "file_limit": FILE_LIST_LIMIT,
            "write_consistency": _WRITE_CONSISTENCY,
            "requires_quiescent_external_editors": True,
        }
        if self._root is None:
            return common

        files: list[str] = []
        truncated = False
        scanned = 0
        allowed = SOURCE_EXTENSIONS | ARTIFACT_EXTENSIONS
        directories: list[tuple[str, ...]] = [()]
        while directories and scanned < INFO_SCAN_LIMIT and len(files) < FILE_LIST_LIMIT:
            directory = directories.pop()
            try:
                with self._open_parent(directory, create=False) as directory_fd:
                    with os.scandir(directory_fd) as entries:
                        while scanned < INFO_SCAN_LIMIT and len(files) < FILE_LIST_LIMIT:
                            try:
                                entry = next(entries)
                            except StopIteration:
                                break
                            scanned += 1
                            name = entry.name
                            if name.startswith("."):
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                if name not in _IGNORED_DIRECTORIES:
                                    directories.append((*directory, name))
                                continue
                            if Path(name).suffix not in allowed or not entry.is_file(
                                follow_symlinks=False
                            ):
                                continue
                            metadata = entry.stat(follow_symlinks=False)
                            if metadata.st_nlink != 1:
                                continue
                            files.append("/".join((*directory, name)))
            except (OSError, PermissionError):
                continue
        if directories or scanned == INFO_SCAN_LIMIT or len(files) == FILE_LIST_LIMIT:
            truncated = True
        common["files"] = sorted(files)
        common["files_truncated"] = truncated
        return common

    def resolve_source(self, path: str) -> Path:
        """Validate a source path and return its root-relative location.

        The returned path is for planning and display. Call ``read_source`` to obtain a
        descriptor-backed snapshot because filesystem state can change after this method returns.
        """
        root = self._require_root()
        parts = self._validate_path(path, SOURCE_EXTENSIONS, "source")
        return root.joinpath(*parts)

    def read_source(self, path: str) -> dict[str, Any]:
        """Read one bounded UTF-8 HDL or constraint source snapshot."""
        parts = self._validated_parts(path, SOURCE_EXTENSIONS, "source")
        data = self._read_regular(parts, SOURCE_MAX_BYTES, "256 KiB")
        return self._text_result(path, data)

    def read_report(self, path: str) -> dict[str, Any]:
        """Read one bounded UTF-8 report snapshot without interpreting its result."""
        parts = self._validated_parts(path, REPORT_EXTENSIONS, "report")
        data = self._read_regular(parts, REPORT_MAX_BYTES, "256 KiB")
        return self._text_result(path, data)

    def write_source(
        self, path: str, content: str, expected_sha256: str | None = None
    ) -> dict[str, Any]:
        """Write under the cooperative lock after a best-effort stale-hash check.

        The lock serializes RedPitayaCore-MCP writers. External editors do not honor it, so
        they must be quiescent; ``expected_sha256`` is not a filesystem-wide compare-and-swap.
        """
        self._require_root()
        if not self._allow_write:
            raise PermissionError("FPGA workspace writes are disabled by operator configuration")
        parts = self._validate_path(path, SOURCE_EXTENSIONS, "source")
        expected = self._validate_expected_hash(expected_sha256)
        if not isinstance(content, str):
            raise TypeError("content must be a string")
        data = content.encode("utf-8")
        if len(data) > SOURCE_MAX_BYTES:
            raise ValueError("source exceeds the 256 KiB limit")

        with self._writer_lock():
            return self._write_source_serialized(path, parts, data, expected)

    def _write_source_serialized(
        self, path: str, parts: tuple[str, ...], data: bytes, expected: str | None
    ) -> dict[str, Any]:
        with self._open_parent(parts[:-1], create=True) as parent_fd:
            name = parts[-1]
            existing = self._read_existing_for_write(parent_fd, name)
            created = existing is None
            if existing is None:
                if expected is not None:
                    raise FileNotFoundError(
                        "source does not exist; expected_sha256 is only valid for replacement"
                    )
                mode = 0o644
            else:
                current_data, current_stat = existing
                if expected is None:
                    raise PermissionError(
                        "expected_sha256 is required to replace an existing source"
                    )
                if not secrets.compare_digest(hashlib.sha256(current_data).hexdigest(), expected):
                    raise PermissionError(
                        "source hash is stale; read the source again before replacing it"
                    )
                mode = stat.S_IMODE(current_stat.st_mode)

            temp_name = f".{name}.tmp-{secrets.token_hex(8)}"
            temp_fd = -1
            try:
                temp_fd = os.open(
                    temp_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                    mode,
                    dir_fd=parent_fd,
                )
                self._write_all(temp_fd, data)
                os.fsync(temp_fd)
                os.close(temp_fd)
                temp_fd = -1

                if created:
                    os.link(
                        temp_name,
                        name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    os.unlink(temp_name, dir_fd=parent_fd)
                else:
                    if expected is None:
                        raise PermissionError("A current source hash is required for replacement")
                    latest = self._read_existing_for_write(parent_fd, name)
                    if latest is None or not secrets.compare_digest(
                        hashlib.sha256(latest[0]).hexdigest(), expected
                    ):
                        raise PermissionError(
                            "source changed during replacement; read it again before retrying"
                        )
                    os.replace(temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                os.fsync(parent_fd)
            finally:
                if temp_fd >= 0:
                    os.close(temp_fd)
                try:
                    os.unlink(temp_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass

        return {
            "path": path,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "created": created,
            "write_consistency": _WRITE_CONSISTENCY,
            "requires_quiescent_external_editors": True,
        }

    def inspect_artifact(self, path: str, expected_sha256: str | None = None) -> dict[str, Any]:
        """Hash a bounded build artifact without making hardware compatibility claims."""
        parts = self._validated_parts(path, ARTIFACT_EXTENSIONS, "artifact")
        expected = self._validate_expected_hash(expected_sha256)
        digest, size = self._hash_regular(parts, ARTIFACT_MAX_BYTES, "64 MiB")
        formats = {
            ".bit": "Xilinx bitstream",
            ".bin": "binary image",
            ".ltx": "Xilinx debug probes",
            ".rpt": "tool report",
            ".dcp": "Xilinx design checkpoint",
            ".dtbo": "device-tree overlay",
        }
        return {
            "path": path,
            "sha256": digest,
            "size": size,
            "format": formats[Path(path).suffix],
            "format_verified": False,
            "format_source": "filename_extension_only",
            "expected_sha256": expected,
            "hash_matches": None if expected is None else secrets.compare_digest(digest, expected),
            "fpga_loaded": False,
            "board_compatibility_verified": False,
        }

    def _require_root(self) -> Path:
        if self._root is None:
            raise PermissionError(
                "FPGA workspace root is not configured; set it in operator configuration"
            )
        return self._root

    def _validated_parts(self, path: str, extensions: frozenset[str], kind: str) -> tuple[str, ...]:
        self._require_root()
        return self._validate_path(path, extensions, kind)

    @staticmethod
    def _validate_path(path: str, extensions: frozenset[str], kind: str) -> tuple[str, ...]:
        if not isinstance(path, str):
            raise TypeError("path must be a string")
        if not path or "\x00" in path or "\\" in path or path.startswith("/"):
            raise ValueError("path must be a non-empty relative POSIX path")
        parts = tuple(path.split("/"))
        if any(not part or part in {".", ".."} or part.startswith(".") for part in parts):
            raise ValueError("path contains a forbidden component")
        if Path(parts[-1]).suffix not in extensions:
            raise ValueError(f"unsupported {kind} extension")
        return parts

    @staticmethod
    def _validate_expected_hash(expected: str | None) -> str | None:
        if expected is None:
            return None
        if not isinstance(expected, str) or _HASH_PATTERN.fullmatch(expected) is None:
            raise ValueError("expected_sha256 must contain exactly 64 hexadecimal characters")
        return expected.lower()

    @contextmanager
    def _writer_lock(self) -> Iterator[None]:
        """Serialize cooperating writers; external processes must remain quiescent."""
        root = self._require_root()
        root_fd = os.open(root, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC)
        lock_fd = -1
        try:
            try:
                lock_fd = os.open(
                    _WRITE_LOCK_NAME,
                    os.O_RDWR | os.O_CREAT | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
                    0o600,
                    dir_fd=root_fd,
                )
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise PermissionError("workspace writer lock must not be a link") from exc
                raise
            metadata = os.fstat(lock_fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise PermissionError("workspace writer lock must be a regular unlinked file")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield
        finally:
            if lock_fd >= 0:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
            os.close(root_fd)

    @contextmanager
    def _open_parent(self, parts: tuple[str, ...], *, create: bool) -> Iterator[int]:
        root = self._require_root()
        descriptors: list[int] = []
        try:
            current = os.open(root, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC)
            descriptors.append(current)
            for part in parts:
                try:
                    child = os.open(
                        part,
                        os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
                        dir_fd=current,
                    )
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(part, mode=0o755, dir_fd=current)
                    child = os.open(
                        part,
                        os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
                        dir_fd=current,
                    )
                except OSError as exc:
                    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                        raise PermissionError("workspace path contains a link") from exc
                    raise
                descriptors.append(child)
                current = child
            yield current
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _read_regular(self, parts: tuple[str, ...], maximum: int, label: str) -> bytes:
        with self._open_parent(parts[:-1], create=False) as parent_fd:
            descriptor = self._open_regular(parent_fd, parts[-1])
            try:
                return self._read_bounded(descriptor, maximum, label)
            finally:
                os.close(descriptor)

    def _hash_regular(self, parts: tuple[str, ...], maximum: int, label: str) -> tuple[str, int]:
        with self._open_parent(parts[:-1], create=False) as parent_fd:
            descriptor = self._open_regular(parent_fd, parts[-1])
            try:
                metadata = os.fstat(descriptor)
                if metadata.st_size > maximum:
                    raise ValueError(f"file exceeds the {label} limit")
                digest = hashlib.sha256()
                size = 0
                while True:
                    chunk = os.read(descriptor, min(1024 * 1024, maximum - size + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > maximum:
                        raise ValueError(f"file exceeds the {label} limit")
                    digest.update(chunk)
                return digest.hexdigest(), size
            finally:
                os.close(descriptor)

    @staticmethod
    def _open_regular(parent_fd: int, name: str) -> int:
        try:
            descriptor = os.open(name, os.O_RDONLY | _NOFOLLOW | _CLOEXEC, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise PermissionError("workspace path contains a link") from exc
            raise
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            os.close(descriptor)
            raise PermissionError("workspace files must be regular files with no links")
        return descriptor

    @staticmethod
    def _read_bounded(descriptor: int, maximum: int, label: str) -> bytes:
        metadata = os.fstat(descriptor)
        if metadata.st_size > maximum:
            raise ValueError(f"file exceeds the {label} limit")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, maximum - size + 1))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > maximum:
                raise ValueError(f"file exceeds the {label} limit")
        return b"".join(chunks)

    @staticmethod
    def _text_result(path: str, data: bytes) -> dict[str, Any]:
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("file must contain valid UTF-8 text") from exc
        return {
            "path": path,
            "content": content,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }

    def _read_existing_for_write(
        self, parent_fd: int, name: str
    ) -> tuple[bytes, os.stat_result] | None:
        try:
            descriptor = self._open_regular(parent_fd, name)
        except FileNotFoundError:
            return None
        try:
            metadata = os.fstat(descriptor)
            data = self._read_bounded(descriptor, SOURCE_MAX_BYTES, "256 KiB")
            return data, metadata
        finally:
            os.close(descriptor)

    @staticmethod
    def _write_all(descriptor: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written == 0:
                raise OSError("short write while saving source")
            view = view[written:]
