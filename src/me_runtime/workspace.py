"""Workspace jail: every file/code/artifact path stays inside the run workspace.

Reserved broker state (`.me_runtime/`: SqliteSaver `graph.db` + trusted
`pending.json`) is denied to ALL public file tools. Internal runtime paths
use `resolve()` / `ensure_dirs()` / `root` directly and are unaffected;
public tools must go through `resolve_public()` / `read_text` / `list_dir`
/ `write_bytes`, which deny the reserved namespace AFTER symlink
resolution (so same-workspace symlink aliases and normalized `..`/`.`
paths cannot bypass it) and hide reserved entries from listings.
"""
from __future__ import annotations

import stat
from pathlib import Path


class WorkspaceDenied(Exception):
    pass


# Reserved internal namespace: checkpoint + trusted approval state.
RESERVED_NAME = ".me_runtime"
MAX_READ_BYTES = 1_000_000
MAX_WRITE_BYTES = 10_000_000
MAX_LIST_ENTRIES = 200


class Workspace:
    def __init__(self, root: str | Path) -> None:
        p = Path(root)
        if not p.is_absolute():
            raise WorkspaceDenied(f"workspace must be absolute: {root!r}")
        if not p.is_dir():
            raise WorkspaceDenied(f"workspace does not exist: {root!r}")
        self.root = p.resolve()

    def resolve(self, rel: str) -> Path:
        """Internal resolve: jail only, reserved namespace allowed.

        Used by the runtime itself for checkpoint/pending/sandbox state.
        Public model tools must use `resolve_public()` instead.
        """
        if not rel or not isinstance(rel, str):
            raise WorkspaceDenied("path must be a non-empty string")
        candidate = (self.root / rel).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            raise WorkspaceDenied(f"path escapes workspace: {rel!r}")
        return candidate

    def _is_reserved(self, resolved: Path) -> bool:
        """True if a jail-resolved absolute path is the reserved dir or inside it."""
        reserved = self.root / RESERVED_NAME
        return resolved == reserved or reserved in resolved.parents

    def resolve_public(self, rel: str) -> Path:
        """Public resolve: jail + reserved-namespace denial (post-symlink)."""
        target = self.resolve(rel)
        if self._is_reserved(target):
            raise WorkspaceDenied(
                f"reserved broker state is not visible to tools: {rel!r}")
        return target

    def read_text(self, rel: str, max_bytes: int = MAX_READ_BYTES) -> str:
        """Public bounded read: reserved denied, regular files only, streamed."""
        target = self.resolve_public(rel)
        try:
            st = target.stat()
        except OSError:
            raise WorkspaceDenied(f"not a file: {rel!r}")
        if not stat.S_ISREG(st.st_mode):
            raise WorkspaceDenied(
                f"not a regular file (fifo/device/socket/dir rejected): {rel!r}")
        try:
            cap = min(int(max_bytes), MAX_READ_BYTES)
        except (TypeError, ValueError):
            raise WorkspaceDenied(f"invalid max_bytes: {max_bytes!r}")
        cap = max(cap, 0)
        chunks: list[bytes] = []
        remaining = cap
        try:
            with open(target, "rb") as fh:
                while remaining > 0:
                    data = fh.read(min(65536, remaining))
                    if not data:
                        break
                    chunks.append(data)
                    remaining -= len(data)
        except OSError as exc:
            raise WorkspaceDenied(f"not readable: {rel!r}: {exc}") from exc
        return b"".join(chunks).decode("utf-8", errors="replace")

    def list_dir(self, rel: str = ".", limit: int = MAX_LIST_ENTRIES) -> list[str]:
        """Public listing: reserved target denied, reserved entries hidden, bounded."""
        target = self.resolve_public(rel)
        if not target.is_dir():
            raise WorkspaceDenied(f"not a directory: {rel!r}")
        try:
            cap = min(int(limit), MAX_LIST_ENTRIES)
        except (TypeError, ValueError):
            raise WorkspaceDenied(f"invalid limit: {limit!r}")
        cap = max(cap, 0)
        out: list[str] = []
        try:
            it = target.iterdir()
        except OSError as exc:
            raise WorkspaceDenied(f"not listable: {rel!r}: {exc}") from exc
        try:
            for _ in range(cap):
                try:
                    entry = next(it)
                except StopIteration:
                    break
                # Hide reserved namespace and symlink aliases into it.
                try:
                    if self._is_reserved(entry.resolve()):
                        continue
                except OSError:
                    continue
                try:
                    out.append(entry.relative_to(self.root).as_posix())
                except ValueError:
                    continue
        finally:
            try:
                it.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        return sorted(out)

    def write_bytes(self, rel: str, data: bytes) -> Path:
        """Public write: reserved denied, individual cap enforced (no truncate)."""
        if not isinstance(data, (bytes, bytearray)):
            raise WorkspaceDenied("data must be bytes")
        if len(data) > MAX_WRITE_BYTES:
            raise WorkspaceDenied(
                f"file too large: {len(data)} bytes exceeds "
                f"{MAX_WRITE_BYTES} byte per-file cap")
        # resolve_public() already resolves symlinks, so a same-workspace
        # `link -> .me_runtime` alias lands inside reserved and is denied.
        target = self.resolve_public(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        if self._is_reserved(target.resolve()):
            raise WorkspaceDenied(
                f"reserved broker state is not writable: {rel!r}")
        target.write_bytes(bytes(data))
        return target

    def ensure_dirs(self, *names: str) -> None:
        """Internal helper: jail only, reserved allowed (runtime state setup)."""
        for name in names:
            d = self.resolve(name)
            d.mkdir(parents=True, exist_ok=True)

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()
