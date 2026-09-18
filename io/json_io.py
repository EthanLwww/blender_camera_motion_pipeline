"""UTF-8 JSON read/write with Blender-friendly diagnostics.

Every artifact this pipeline writes is UTF-8 with ``ensure_ascii=False`` so a
scene named in Chinese stays readable, and stable ``sort_keys`` output makes
diffs between two runs meaningful.
"""

from __future__ import annotations

import json
import os
import tempfile

from .path_utils import ensure_dir, normalize_path


class JsonError(RuntimeError):
    """Raised for a missing, unreadable or malformed JSON document."""


def load_json_file(path: str, *, default=None, required: bool = True):
    """Load UTF-8 JSON.

    ``utf-8-sig`` transparently eats the BOM that Windows editors add, which is
    a very common cause of ``json.load`` failures on hand-edited config files.
    """
    target = normalize_path(path)
    if not target or not os.path.isfile(target):
        if required:
            raise JsonError(f"JSON file not found: {path}")
        return default
    try:
        with open(target, "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise JsonError(
            f"Malformed JSON in {target}: {exc.msg} (line {exc.lineno}, column {exc.colno})"
        ) from exc
    except OSError as exc:
        raise JsonError(f"Cannot read {target}: {exc}") from exc


def dump_json_file(path: str, payload, *, indent: int = 2) -> str:
    """Atomically write ``payload`` as UTF-8 JSON.  Returns the written path."""
    target = normalize_path(path)
    if not target:
        raise JsonError("dump_json_file() received an empty path")
    parent = os.path.dirname(target)
    if parent:
        ensure_dir(parent)
    text = json.dumps(payload, ensure_ascii=False, indent=indent, sort_keys=True)
    # Write to a sibling temp file first so a crash cannot leave a half-written
    # manifest behind (the render farm polls these files).
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n",
        dir=parent or ".", prefix=".tmp_", suffix=".json", delete=False,
    )
    try:
        handle.write(text)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, target)
    return target


def save_json_file(path: str, payload, *, indent: int = 2) -> str:
    """Alias kept for readability at call sites."""
    return dump_json_file(path, payload, indent=indent)


def dumps(payload, *, indent: int = 2) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=indent, sort_keys=True)
