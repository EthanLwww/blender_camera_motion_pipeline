"""Cross-platform path helpers.

Windows is the primary authoring platform for this pipeline while the render
farm is usually Linux, so every path that ends up inside a JSON/TXT artifact is
written with forward slashes and (where possible) relative to the artifact
itself.  Absolute Windows paths are kept verbatim in ``source_blend`` style
fields, because that is what the reference tooling did and what a human needs
to trace a bad render back to its source.
"""

from __future__ import annotations

import os
import re
import unicodedata

# Characters that Windows forbids in a file name, plus control characters.
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# Applied when a stored path cannot be resolved on the current machine.  The
# render farm fills this in through --path-map / config "batch.path_mappings".
DEFAULT_PATH_MAPPINGS: "list[tuple[str, str]]" = []


def looks_absolute(path: str) -> bool:
    """Return True for POSIX (``/x``), Windows drive (``C:\\x``) or UNC paths."""
    if not path:
        return False
    if path.startswith(("/", "\\")):
        return True
    return bool(re.match(r"^[A-Za-z]:[\\/]", path))


def to_forward_slashes(path: str) -> str:
    """``E:\\a\\b`` -> ``E:/a/b``.  Keeps UNC prefixes intact."""
    if not path:
        return path
    return path.replace("\\", "/")


def normalize_path(path: str, *, make_absolute: bool = True, base: str | None = None) -> str:
    """Expand ``~``/env vars, collapse ``..`` and normalise separators.

    The result always uses the *native* separator of the running platform so it
    can be handed straight back to ``open()``/``bpy.ops``.  Use
    :func:`to_forward_slashes` before writing a path into an artifact.
    """
    if path is None:
        return ""
    text = str(path).strip().strip('"')
    if not text:
        return ""
    text = os.path.expandvars(os.path.expanduser(text))
    if base and not looks_absolute(text):
        text = os.path.join(base, text)
    if make_absolute:
        text = os.path.abspath(text)
    else:
        text = os.path.normpath(text)
    return os.path.normpath(text)


def apply_path_mappings(path: str, mappings) -> str:
    """Rewrite ``path`` using ``{"from": .., "to": ..}`` mapping rules.

    The first matching rule wins.  Matching is case-insensitive and
    separator-insensitive, so a Windows-authored project file can be replayed on
    Linux.  Resolution is *prefix based*: the longest matching ``from`` prefix
    is used, never a substring match.
    """
    if not path or not mappings:
        return path
    raw = to_forward_slashes(str(path))
    lowered = raw.lower()
    best = None  # (prefix_length, source, destination)
    for item in mappings:
        src = to_forward_slashes(str(_mapping_field(item, "from"))).rstrip("/")
        dst = str(_mapping_field(item, "to"))
        if not src:
            continue
        if lowered == src.lower() or lowered.startswith(src.lower() + "/"):
            if best is None or len(src) > best[0]:
                best = (len(src), src, dst)
    if best is None:
        return path
    _length, src, dst = best
    tail = raw[len(src):]
    return _join_mapped(dst, tail)


def _join_mapped(destination: str, tail: str) -> str:
    """Join a mapping destination with the leftover path tail.

    The join has to be separator-agnostic: a mapping target may be a POSIX path
    (``/mnt/e/scenes``) even when the pipeline runs on Windows, and vice versa.
    Collapsing ``..`` here is only safe because the tail is taken verbatim from
    an already-normalised path.
    """
    tail = tail.lstrip("/")
    head = to_forward_slashes(str(destination)).rstrip("/")
    if not tail:
        combined = head
    elif not head:
        combined = "/" + tail
    else:
        combined = f"{head}/{tail}"
    return _posix_normpath(combined)


def _posix_normpath(path: str) -> str:
    """Collapse ``.``/``..`` and duplicate separators without touching the root."""
    absolute = path.startswith("/")
    parts: "list[str]" = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts and parts[-1] != "..":
                parts.pop()
            elif not absolute:
                parts.append(part)
            continue
        parts.append(part)
    joined = "/".join(parts)
    if absolute:
        return "/" + joined
    return joined or "."

def _mapping_field(item, name: str):
    if isinstance(item, dict):
        return item.get(name, "")
    return getattr(item, name, "")


def parse_path_mappings(pairs) -> "list[tuple[str, str]]":
    """Accept ``["E:/a=D:/b", ...]`` or ``[{"from":..,"to":..}, ...]``."""
    result: "list[tuple[str, str]]" = []
    if not pairs:
        return result
    for item in pairs:
        if isinstance(item, dict):
            src, dst = item.get("from", ""), item.get("to", "")
        elif isinstance(item, str) and "=" in item:
            src, dst = item.split("=", 1)
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            src, dst = item[0], item[1]
        else:
            continue
        src, dst = str(src).strip(), str(dst).strip()
        if src and dst:
            result.append((src, dst))
    return result


def safe_filename(name: str, *, fallback: str = "unnamed") -> str:
    """Turn arbitrary text into a single path component safe on Windows+Linux."""
    text = unicodedata.normalize("NFKC", str(name or ""))
    text = _ILLEGAL_CHARS.sub("_", text).strip().strip(".")
    text = re.sub(r"\s+", "_", text)
    text = text.strip("_")
    if not text:
        return fallback
    if text.split(".")[0].upper() in _WINDOWS_RESERVED:
        text = "_" + text
    # Leave headroom for the sequence suffix and extension.
    return text[:120]


def slugify(text: str, *, fallback: str = "unnamed", max_length: int = 80) -> str:
    """Lowercase, ASCII-only, dash-separated identifier."""
    normalised = unicodedata.normalize("NFKD", str(text or ""))
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii")
    ascii_only = re.sub(r"[^A-Za-z0-9]+", "-", ascii_only).strip("-").lower()
    ascii_only = re.sub(r"-{2,}", "-", ascii_only)
    if not ascii_only:
        return fallback
    return ascii_only[:max_length].strip("-") or fallback


def sanitize_relpath(path: str) -> str:
    """Sanitise every component of a relative path, keeping the separators."""
    parts = [p for p in to_forward_slashes(str(path or "")).split("/") if p not in ("", ".")]
    if not parts:
        return ""
    cleaned = []
    for part in parts:
        if part == "..":
            cleaned.append("..")
        else:
            cleaned.append(safe_filename(part))
    return "/".join(cleaned)


def ensure_dir(path: str) -> str:
    """Create ``path`` (and parents) and return it.  Raises OSError on failure."""
    target = normalize_path(path)
    if not target:
        raise ValueError("ensure_dir() received an empty path")
    os.makedirs(target, exist_ok=True)
    return target


def is_subpath(child: str, parent: str) -> bool:
    """True when ``child`` lives inside ``parent`` (case-insensitive on Win)."""
    if not child or not parent:
        return False
    c = os.path.normcase(os.path.abspath(child))
    p = os.path.normcase(os.path.abspath(parent))
    if c == p:
        return True
    return c.startswith(p.rstrip(os.sep) + os.sep)


def relative_to(path: str, base: str) -> str:
    """``os.path.relpath`` that degrades gracefully across drives.

    Returns a forward-slash relative path, or the original absolute path when no
    relative form exists (e.g. ``E:`` vs ``/mnt/e``).
    """
    if not path or not base:
        return to_forward_slashes(path or "")
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(base))
    except ValueError:
        return to_forward_slashes(path)
    return to_forward_slashes(rel)


def unique_path(path: str) -> str:
    """``a.blend`` -> ``a_001.blend`` when the target already exists."""
    target = normalize_path(path)
    if not os.path.exists(target):
        return target
    stem, ext = os.path.splitext(target)
    index = 1
    while True:
        candidate = f"{stem}_{index:03d}{ext}"
        if not os.path.exists(candidate):
            return candidate
        index += 1


def sequence_folder_name(index: int) -> str:
    """``1`` -> ``sequence_000001`` (matches the documented output layout)."""
    return f"sequence_{int(index):06d}"
