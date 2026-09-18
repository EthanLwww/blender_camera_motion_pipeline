"""Filesystem / serialisation helpers shared by every other module.

Nothing in this package imports ``bpy`` so it stays unit-testable without a
running Blender instance.
"""

from .path_utils import (
    DEFAULT_PATH_MAPPINGS,
    apply_path_mappings,
    ensure_dir,
    is_subpath,
    looks_absolute,
    normalize_path,
    parse_path_mappings,
    relative_to,
    safe_filename,
    sanitize_relpath,
    sequence_folder_name,
    slugify,
    to_forward_slashes,
    unique_path,
)
from .json_io import JsonError, dump_json_file, load_json_file, save_json_file
from .manifest import ManifestWriter, new_manifest, utc_now_iso
from .resource_check import (
    ResourceCheckResult,
    blend_resources_from_bpy,
    check_blend_resources,
    check_path_mappings,
    missing_within,
)

__all__ = [
    "DEFAULT_PATH_MAPPINGS",
    "JsonError",
    "ManifestWriter",
    "ResourceCheckResult",
    "apply_path_mappings",
    "blend_resources_from_bpy",
    "check_blend_resources",
    "check_path_mappings",
    "dump_json_file",
    "ensure_dir",
    "is_subpath",
    "load_json_file",
    "looks_absolute",
    "missing_within",
    "new_manifest",
    "normalize_path",
    "parse_path_mappings",
    "relative_to",
    "safe_filename",
    "sanitize_relpath",
    "save_json_file",
    "sequence_folder_name",
    "slugify",
    "to_forward_slashes",
    "unique_path",
    "utc_now_iso",
]
