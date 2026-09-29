"""Crash-safe filesystem primitives shared by every stateful component.

WHY THIS MODULE EXISTS
----------------------
Every ledger, queue and result file in this project used the same pattern::

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)

``open(path, "w")`` **truncates the file before a single byte is written**.
A GitHub runner can disappear between the truncate and the final flush —
job timeout, the 6-hour ceiling, an OOM kill, a cancelled workflow, a full
disk. When that happens the file is left empty or half-written:

  * ``campaign/failed_urls.txt``   → every URL's strike count reset to zero
  * ``campaign/upload_queue.json`` → the entire pending upload queue vanishes
  * ``campaign/upload_ledger.json``→ daily quota counter resets, so the next
    run can blow straight through the free 10,000-unit YouTube budget
  * ``result_NNN.json``            → truncated JSON that no tool can read

The fix is the standard atomic-replace dance: write the full payload to a
temporary file **in the same directory** (same filesystem, so ``rename`` is
atomic), ``fsync`` it so the bytes are really on the platter, then
``os.replace`` it over the target. ``os.replace`` is atomic on POSIX and on
Windows, so a reader either sees the complete old file or the complete new
file — never a half-written one.

Nothing here raises on a missing parent directory; it is created on demand,
exactly like the call sites used to do by hand.
"""
import json
import os
import tempfile
from typing import Any, Optional

__all__ = [
    "atomic_write_text",
    "atomic_write_bytes",
    "atomic_write_json",
    "append_line_durable",
    "read_json_safe",
    "fsync_dir",
]


def _ensure_parent(path: str) -> str:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    return parent


def fsync_dir(directory: str) -> None:
    """Flush a directory entry so a rename survives a power/VM loss.

    Best-effort: Windows cannot open a directory as a file descriptor, and
    some container filesystems refuse it. A failure here only costs us the
    very last durability guarantee, never correctness of the file content.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except (OSError, AttributeError):
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path: str, payload: bytes) -> None:
    """Replace ``path`` with ``payload`` atomically (write-temp + rename)."""
    parent = _ensure_parent(path)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=parent
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())      # bytes are on disk BEFORE the rename
        os.replace(tmp, path)          # atomic on POSIX and Windows
        tmp = ""                       # ownership transferred; nothing to clean
    finally:
        if tmp and os.path.exists(tmp):
            # Never leave a .tmp turd behind when the write itself failed.
            try:
                os.remove(tmp)
            except OSError:
                pass
    fsync_dir(parent)


def atomic_write_text(path: str, text: str, encoding: str = "utf-8") -> None:
    """Atomically replace ``path`` with ``text``."""
    atomic_write_bytes(path, text.encode(encoding, errors="replace"))


def atomic_write_json(path: str, obj: Any, *, indent: Optional[int] = 1,
                      ensure_ascii: bool = False) -> None:
    """Atomically replace ``path`` with the JSON encoding of ``obj``.

    ``ensure_ascii=False`` by default: this project processes Hindi/Devanagari
    content, and \\uXXXX-escaping every character triples the file size and
    makes the artifacts unreadable to a human reviewer.

    The payload is fully serialised **in memory first** — deliberately. If
    ``obj`` contains something unserialisable, the TypeError is raised before
    the target file has been touched, so a bad value can never destroy the
    previous good copy.
    """
    payload = json.dumps(obj, indent=indent, ensure_ascii=ensure_ascii,
                         default=str)
    atomic_write_bytes(path, payload.encode("utf-8"))


def append_line_durable(path: str, line: str, encoding: str = "utf-8") -> None:
    """Append one line and fsync it.

    Append-only ledgers (``processed_urls.txt``) do not need the temp-rename
    dance — a single small ``O_APPEND`` write does not interleave — but they
    DO need the fsync, otherwise the line sits in the page cache and is lost
    if the runner is killed before writeback.
    """
    _ensure_parent(path)
    if not line.endswith("\n"):
        line += "\n"
    with open(path, "a", encoding=encoding) as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def read_json_safe(path: str, default: Any = None) -> Any:
    """Read JSON, returning ``default`` for missing/corrupt/unreadable files.

    A ledger truncated by an older build of this code must degrade to "start
    fresh", never crash the run.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default
