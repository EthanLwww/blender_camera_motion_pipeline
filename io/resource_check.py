"""Pre-flight checks for external assets and path mappings.

A ``.blend`` produced on an artist workstation routinely references textures,
caches or linked libraries that simply do not exist on the render node.  These
helpers deliberately do **not** import ``bpy``: :func:`check_blend_resources`
is handed a plain ``(kind, path, owner)`` iterable that the caller harvested
from ``bpy.data.*``, which keeps the logic testable and lets the render script
reuse it on a background-loaded file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .path_utils import apply_path_mappings, is_subpath, normalize_path, to_forward_slashes


@dataclass
class ResourceCheckResult:
    """Outcome of a missing-asset scan."""

    checked: int = 0
    missing: "list[dict]" = field(default_factory=list)
    remapped: "list[dict]" = field(default_factory=list)
    resolved_mappings: "list[tuple[str, str]]" = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing

    def add_missing(self, kind: str, path: str, owner: str = "") -> None:
        self.missing.append(
            {"kind": kind, "path": to_forward_slashes(path), "owner": owner}
        )

    def summary(self) -> str:
        if self.ok:
            return f"{self.checked} external resource(s) resolved"
        return (
            f"{len(self.missing)} of {self.checked} external resource(s) missing: "
            + ", ".join(sorted({m["kind"] for m in self.missing}))
        )

    def as_dict(self) -> dict:
        return {
            "checked": self.checked,
            "ok": self.ok,
            "missing_count": len(self.missing),
            "missing": self.missing,
            "remapped_count": len(self.remapped),
            "remapped": self.remapped,
            "applied_mappings": [
                {"from": src, "to": dst} for src, dst in self.resolved_mappings
            ],
        }


def check_blend_resources(resources, *, mappings=(), probe=None) -> ResourceCheckResult:
    """Scan harvested external resources.

    ``resources`` is an iterable of ``(kind, raw_path, owner)`` triples.
    ``probe(path) -> bool`` overrides the existence test (used for library
    paths, datablocks, or anything that is not a plain file).
    """
    result = ResourceCheckResult()
    resolved: "list[tuple[str, str]]" = []
    seen_pairs = set()

    for kind, raw_path, owner in resources:
        if not raw_path:
            continue
        result.checked += 1
        mapped = apply_path_mappings(raw_path, mappings) if mappings else raw_path
        candidate = normalize_path(mapped, make_absolute=True)
        if mapped != raw_path and (raw_path, mapped) not in seen_pairs:
            seen_pairs.add((raw_path, mapped))
            result.remapped.append(
                {"kind": kind, "owner": owner, "from": to_forward_slashes(raw_path),
                 "to": to_forward_slashes(mapped)}
            )
            resolved.append((raw_path, mapped))

        exists = bool(probe(candidate)) if probe else os.path.exists(candidate)
        if not exists:
            result.add_missing(kind, candidate, owner)

    result.resolved_mappings = resolved
    return result


def check_path_mappings(mappings, *, sample_paths=()) -> dict:
    """Report which mappings actually resolve, and how they would rewrite paths.

    A mapping whose ``to`` directory does not exist is almost always a typo on a
    fresh render node, so surface it before a multi-hour render starts.
    """
    report = {"mappings": [], "unresolved_targets": [], "samples": []}
    for src, dst in mappings:
        target_ok = os.path.isdir(normalize_path(dst))
        report["mappings"].append(
            {
                "from": to_forward_slashes(src),
                "to": to_forward_slashes(dst),
                "target_exists": target_ok,
            }
        )
        if not target_ok:
            report["unresolved_targets"].append(to_forward_slashes(dst))
    for sample in sample_paths:
        mapped = apply_path_mappings(sample, [{"from": s, "to": d} for s, d in mappings])
        report["samples"].append(
            {
                "input": to_forward_slashes(sample),
                "output": to_forward_slashes(mapped),
                "changed": mapped != sample,
                "exists": os.path.exists(normalize_path(mapped)),
            }
        )
    report["ok"] = not report["unresolved_targets"]
    return report


def blend_resources_from_bpy(blend_file: str | None = None, *, include_libraries: bool = True):
    """Harvest ``(kind, path, owner)`` triples from the currently loaded file.

    Must be called with Blender running.  Only paths that are *not* packed and
    *not* generated are reported, since packed data survives the copy to the
    render node by definition.
    """
    import bpy  # local import keeps this module importable outside Blender

    resources = []
    for image in bpy.data.images:
        if image.packed_file or image.source == "GENERATED":
            continue
        if image.filepath:
            resources.append(("image", bpy.path.abspath(image.filepath, library=image.library), image.name))
    for movie in getattr(bpy.data, "movieclips", []):
        if getattr(movie, "packed_file", None):
            continue
        if movie.filepath:
            resources.append(("movieclip", bpy.path.abspath(movie.filepath, library=movie.library), movie.name))
    for cache in getattr(bpy.data, "cache_files", []):
        if cache.filepath:
            resources.append(("cache", bpy.path.abspath(cache.filepath), cache.name))
    if include_libraries:
        for library in bpy.data.libraries:
            if library.filepath:
                resources.append(("library", bpy.path.abspath(library.filepath), library.name or "library"))
    for sound in getattr(bpy.data, "sounds", []):
        if getattr(sound, "packed_file", None):
            continue
        if sound.filepath:
            resources.append(("sound", bpy.path.abspath(sound.filepath, library=sound.library), sound.name))
    for font in bpy.data.fonts:
        if getattr(font, "packed_file", None):
            continue
        if getattr(font, "filepath", ""):
            resources.append(("font", bpy.path.abspath(font.filepath, library=font.library), font.name))
    if blend_file:
        resources.append(("blend", blend_file, "current_file"))
    return resources


def missing_within(paths, root: str) -> "list[str]":
    """Return the subset of ``paths`` that fall outside ``root``.

    Used to warn when an output folder is nested inside an input scene folder,
    which would make a batch run chew on its own output.
    """
    return [p for p in paths if p and not is_subpath(p, root)]
