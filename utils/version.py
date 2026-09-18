"""Single source of truth for the generator version string.

The version is stamped into every ``sequence_config.json`` and manifest so a
render farm can tell which generator produced a folder it is about to render.
"""

from __future__ import annotations

import os
import sys

GENERATOR_VERSION = "1.0.0"
SCHEMA_VERSION = 1


def generator_stamp() -> dict:
    """Version + interpreter metadata for artifact provenance."""
    stamp = {
        "generator": "blender_motion_pipeline",
        "generator_version": GENERATOR_VERSION,
        "schema_version": SCHEMA_VERSION,
        "python_version": sys.version.split()[0],
        "platform": os.name,
    }
    try:  # pragma: no cover - only meaningful inside Blender
        import bpy  # type: ignore

        stamp["blender_version"] = bpy.app.version_string
        stamp["blender_version_tuple"] = list(bpy.app.version)
    except Exception:
        stamp["blender_version"] = ""
    return stamp
