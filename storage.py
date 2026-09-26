"""
JSON storage helpers — the ONE place any JSON file in this app is written.
==========================================================================
Every write goes through `write_json_atomic` / `update_json`:

  * A per-file lock serialises read-modify-write cycles inside this single
    process (app threads + the 7 marking workers), so two workers finishing
    at the same moment can never overwrite each other's result.
  * The new content is written to a temp file in the SAME folder, flushed,
    fsync'd, then swapped in with os.replace() — which is atomic on Windows,
    macOS and Linux. A crash or power cut mid-write leaves either the old
    file or the new file on disk, never a half-written one.

`load_metadata_cached` keeps parsed students_metadata.json files in memory,
keyed by path + modified time + size, so the dashboard / results polling
never re-reads a file that hasn't changed. Callers get a deep copy, so they
may mutate what they receive freely.
"""

import copy
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

_locks_guard = threading.Lock()
_locks: dict = {}


def _lock_for(path: Path) -> threading.RLock:
    key = str(Path(path).resolve())
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.RLock()
        return lock


def read_json(path: Path, default: Any = None) -> Any:
    """Reads a JSON file; returns `default` (deep-copied) if missing or unreadable."""
    path = Path(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return copy.deepcopy(default)
    except (OSError, ValueError):
        return copy.deepcopy(default)


def _replace_with_retry(src: str, dst: Path, attempts: int = 10) -> None:
    """os.replace, retried briefly: on Windows it fails while another program
    (antivirus, backup, an editor) has the target open for a moment."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.05 * (i + 1))


def write_json_atomic(path: Path, data: Any, indent: int = 2) -> None:
    """Atomically replaces `path` with `data` serialised as JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock_for(path):
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=indent, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            _replace_with_retry(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    _invalidate_cache(path)


def update_json(path: Path, mutate: Callable[[Any], Any], default: Any = None, indent: int = 2) -> Any:
    """
    Locked read-modify-write. `mutate(data)` edits `data` in place (or returns
    a replacement); the result is written atomically and returned. The lock
    is held for the whole cycle, so concurrent updates are applied one after
    the other instead of one silently discarding the other.
    """
    path = Path(path)
    with _lock_for(path):
        data = read_json(path, default)
        result = mutate(data)
        if result is not None:
            data = result
        write_json_atomic(path, data, indent=indent)
        return data


# ─── Cached metadata reads (D2.6) ─────────────────────────────────────────────
_cache_lock = threading.Lock()
_meta_cache: dict = {}   # resolved path -> (mtime_ns, size, parsed data)


def _invalidate_cache(path: Path) -> None:
    with _cache_lock:
        _meta_cache.pop(str(Path(path).resolve()), None)


def load_metadata_cached(path: Path, default: Any = None, copy_result: bool = True) -> Any:
    """
    Parsed JSON for `path`, re-read only when the file's mtime or size has
    changed since the last read. Returns a deep copy (callers may mutate);
    read-only callers pass copy_result=False to skip the copy.
    """
    path = Path(path)
    try:
        st = path.stat()
    except OSError:
        return copy.deepcopy(default)
    key = str(path.resolve())
    with _cache_lock:
        hit = _meta_cache.get(key)
        if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
            return copy.deepcopy(hit[2]) if copy_result else hit[2]
    data = read_json(path, None)
    if data is None:
        return copy.deepcopy(default)
    with _cache_lock:
        _meta_cache[key] = (st.st_mtime_ns, st.st_size, data)
    return copy.deepcopy(data) if copy_result else data
