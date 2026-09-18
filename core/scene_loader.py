"""Scene discovery, list management and safe ``.blend`` loading.

Two guarantees matter here:

1. **Original files are never modified.**  Generation normally works in the
   live session, and ``.blend`` saving is always a *Save As* into the output
   tree.  When ``safe_copy`` is requested the source file is copied to a
   scratch folder first and the copy is what gets opened, which also protects
   against Blender writing a recovery file next to the original.
2. **A broken scene never aborts a batch.**  Loading problems are captured in a
   structured :class:`SceneLoadResult` so the batch runner can log, record a
   manifest failure and continue with the next scene.
"""

from __future__ import annotations

import glob
import os
import shutil
import tempfile
from dataclasses import dataclass, field

from ..io.json_io import JsonError, load_json_file, save_json_file
from ..io.path_utils import ensure_dir, normalize_path, to_forward_slashes, unique_path

BLEND_PATTERN = "*.blend"
DEFAULT_LIST_VERSION = 1


@dataclass
class SceneEntry:
    """One ``.blend`` file queued for processing."""

    path: str
    name: str = ""
    enabled: bool = True
    status: str = "pending"
    note: str = ""
    camera_count: int = 0
    scene_names: "list[str]" = field(default_factory=list)

    def __post_init__(self):
        self.path = normalize_path(self.path)
        if not self.name:
            self.name = os.path.splitext(os.path.basename(self.path))[0]

    @property
    def exists(self) -> bool:
        return os.path.isfile(self.path)

    def to_dict(self) -> dict:
        return {
            "path": to_forward_slashes(self.path),
            "name": self.name,
            "enabled": bool(self.enabled),
            "status": self.status,
            "note": self.note,
            "camera_count": int(self.camera_count),
            "scene_names": list(self.scene_names),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "SceneEntry":
        path = str(raw.get("path") or raw.get("filepath") or "")
        return cls(
            path=path,
            name=str(raw.get("name") or ""),
            enabled=bool(raw.get("enabled", True)),
            status=str(raw.get("status") or "pending"),
            note=str(raw.get("note") or ""),
        )


@dataclass
class SceneLoadResult:
    """Outcome of opening one ``.blend`` file."""

    path: str
    ok: bool = False
    opened_path: str = ""
    scene_names: "list[str]" = field(default_factory=list)
    error: str = ""
    warnings: "list[str]" = field(default_factory=list)
    missing_resources: "list[dict]" = field(default_factory=list)
    elapsed_seconds: float = 0.0
    used_copy: bool = False

    def to_dict(self) -> dict:
        return {
            "path": to_forward_slashes(self.path),
            "opened_path": to_forward_slashes(self.opened_path),
            "ok": bool(self.ok),
            "scene_names": list(self.scene_names),
            "error": self.error,
            "warnings": list(self.warnings),
            "missing_resource_count": len(self.missing_resources),
            "missing_resources": list(self.missing_resources),
            "elapsed_seconds": round(float(self.elapsed_seconds), 4),
            "used_copy": bool(self.used_copy),
        }


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------
def is_blend_file(path: str) -> bool:
    return bool(path) and os.path.isfile(path) and path.lower().endswith(".blend")


def scan_directory(
    directory: str,
    *,
    recursive: bool = False,
    pattern: str = BLEND_PATTERN,
    skip_hidden: bool = True,
    skip_names: "tuple[str, ...]" = (),
) -> "list[str]":
    """Return the sorted ``.blend`` files inside ``directory``."""
    root = normalize_path(directory)
    if not os.path.isdir(root):
        raise NotADirectoryError(f"not a directory: {directory}")
    found: "list[str]" = []
    if recursive:
        for current, dirnames, filenames in os.walk(root):
            if skip_hidden:
                dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__pycache__"]
            for filename in filenames:
                if filename.startswith(".") and skip_hidden:
                    continue
                if not filename.lower().endswith(".blend"):
                    continue
                found.append(normalize_path(os.path.join(current, filename)))
    else:
        found = [normalize_path(p) for p in glob.glob(os.path.join(root, pattern))]
    if skip_names:
        found = [p for p in found if os.path.basename(p) not in skip_names]
    return sorted(set(found))


def merge_scene_entries(existing: "list[SceneEntry]", new_paths, *, logger=None) -> "tuple[list[SceneEntry], list[str]]":
    """Append ``new_paths``, skipping duplicates and reporting missing files.

    Returns ``(entries, problems)``.  Duplicate detection is case-insensitive
    and normalised, so ``E:\\A\\x.blend`` and ``e:/a/x.blend`` count as one.
    """
    entries = list(existing)
    known = {os.path.normcase(entry.path) for entry in entries}
    problems: "list[str]" = []
    for raw in new_paths:
        path = normalize_path(raw)
        if not path:
            continue
        key = os.path.normcase(path)
        if key in known:
            problems.append(f"already in the list: {path}")
            continue
        if not os.path.isfile(path):
            problems.append(f"file does not exist: {path}")
            continue
        if not path.lower().endswith(".blend"):
            problems.append(f"not a .blend file: {path}")
            continue
        entries.append(SceneEntry(path=path))
        known.add(key)
        if logger is not None:
            logger.debug("queued scene %s", path)
    entries.sort(key=lambda entry: os.path.normcase(entry.path))
    return entries, problems


def missing_scene_entries(entries) -> "list[SceneEntry]":
    return [entry for entry in entries if not entry.exists]


def scene_name_for(entry: SceneEntry, mode: str = "stem") -> str:
    """Folder-safe scene folder name used in the output tree."""
    from ..io.path_utils import safe_filename

    if mode == "filename":
        return safe_filename(os.path.basename(entry.path), fallback="scene")
    return safe_filename(entry.name or os.path.splitext(os.path.basename(entry.path))[0], fallback="scene")


# --------------------------------------------------------------------------
# list persistence
# --------------------------------------------------------------------------
def save_scene_list(path: str, entries, *, extra: dict | None = None) -> str:
    payload = {
        "list_version": DEFAULT_LIST_VERSION,
        "entry_count": len(entries),
        "scenes": [entry.to_dict() if isinstance(entry, SceneEntry) else dict(entry) for entry in entries],
    }
    if extra:
        payload.update(extra)
    return save_json_file(path, payload)


def load_scene_list(path: str) -> "tuple[list[SceneEntry], list[str]]":
    """Load a scene list; returns ``(entries, warnings)``."""
    warnings: "list[str]" = []
    try:
        payload = load_json_file(path)
    except JsonError as exc:
        return [], [str(exc)]
    if isinstance(payload, list):
        raw_entries = payload
    elif isinstance(payload, dict):
        raw_entries = payload.get("scenes") or payload.get("entries") or []
    else:
        return [], [f"{path}: expected an array or an object with a 'scenes' array"]
    entries = []
    for index, raw in enumerate(raw_entries):
        if isinstance(raw, str):
            raw = {"path": raw}
        if not isinstance(raw, dict):
            warnings.append(f"scenes[{index}] is not an object; ignored")
            continue
        entry = SceneEntry.from_dict(raw)
        if not entry.path:
            warnings.append(f"scenes[{index}] has no path; ignored")
            continue
        if not entry.exists:
            warnings.append(f"scene file is missing: {entry.path}")
        entries.append(entry)
    return entries, warnings


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------
def load_blend_file(path: str) -> str:
    """Open ``path`` in the current Blender session.

    Raises :class:`RuntimeError` with the Blender error text on failure.
    """
    import bpy  # local import: keeps this module importable outside Blender

    target = normalize_path(path)
    if not os.path.isfile(target):
        raise RuntimeError(f"blend file not found: {target}")
    # load_ui=False is what makes headless loading behave like the UI's
    # "Open" without depending on window state.
    try:
        bpy.ops.wm.open_mainfile(filepath=target, load_ui=False)
    except TypeError:  # older/newer signature differences
        bpy.ops.wm.open_mainfile(filepath=target)
    return target


def copy_blend_file(path: str, *, scratch_dir: str = "", suffix: str = "_pipeline_copy") -> str:
    """Copy ``path`` into ``scratch_dir`` (default: system temp) and return it."""
    source = normalize_path(path)
    if not os.path.isfile(source):
        raise RuntimeError(f"blend file not found: {source}")
    directory = ensure_dir(scratch_dir) if scratch_dir else ensure_dir(
        os.path.join(tempfile.gettempdir(), "blender_motion_pipeline")
    )
    stem = os.path.splitext(os.path.basename(source))[0]
    destination = os.path.join(directory, f"{stem}{suffix}.blend")
    if os.path.normcase(source) == os.path.normcase(destination):
        destination = unique_path(destination)
    shutil.copy2(source, destination)
    return normalize_path(destination)


def open_scene_for_generation(
    entry: SceneEntry,
    *,
    safe_copy: bool = False,
    scratch_dir: str = "",
) -> SceneLoadResult:
    """Open a queued scene, optionally through a protective copy."""
    import time

    started = time.time()
    result = SceneLoadResult(path=entry.path, error="")
    if not entry.exists:
        result.error = f"blend file does not exist: {entry.path}"
        entry.status = "missing"
        entry.note = result.error
        return result

    opened = entry.path
    try:
        if safe_copy:
            opened = copy_blend_file(entry.path, scratch_dir=scratch_dir)
            result.used_copy = True
        load_blend_file(opened)
    except RuntimeError as exc:
        result.error = str(exc)
        entry.status = "load_failed"
        entry.note = result.error
        return result
    except Exception as exc:  # pragma: no cover - defensive
        result.error = f"{type(exc).__name__}: {exc}"
        entry.status = "load_failed"
        entry.note = result.error
        return result

    result.ok = True
    result.opened_path = opened
    result.elapsed_seconds = time.time() - started
    entry.status = "loaded"
    entry.note = "loaded from a protective copy" if result.used_copy else ""
    return result


def active_scene_names() -> "list[str]":
    import bpy

    return [scene.name for scene in bpy.data.scenes]


def current_blend_path() -> str:
    import bpy

    return normalize_path(bpy.data.filepath) if bpy.data.filepath else ""


def blend_file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def describe_entries(entries) -> str:
    """One-line summary for logs and the UI."""
    if not entries:
        return "no scenes queued"
    existing = sum(1 for entry in entries if entry.exists)
    enabled = sum(1 for entry in entries if entry.enabled)
    return f"{len(entries)} scene(s): {existing} present, {len(entries) - existing} missing, {enabled} enabled"
