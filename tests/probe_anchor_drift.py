"""Measure whether one generated sequence leaks state into the next.

    blender -b -P tests/probe_anchor_drift.py -- "<scene.blend>" [template.json [n]]

Each sequence is supposed to start from *the artist's* camera state: the generator
snapshots the camera, keys its own animation, then restores the snapshot so the
next combination starts clean.  If that round trip is not exact, the anchor pose
walks away from the original -- and because the anchor is the base pose every
template offset is applied to, the same template then produces a different shot
depending on where it sits in the batch.

This probe runs a few sequences in one process and prints, before and after each,
the scene frame and the camera's local/world position plus the parent's world
position, so the leak is visible instead of inferred.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(_HERE))
for path in (_PACKAGE_PARENT, _HERE):
    if path not in sys.path:
        sys.path.insert(0, path)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _boot  # noqa: E402,F401  (the add-on folder may be called anything)

import bpy  # noqa: E402
from mathutils import Vector  # noqa: E402

from blender_motion_pipeline.camera.motion_templates import MotionTemplateLibrary  # noqa: E402
from blender_motion_pipeline.config.defaults import default_config  # noqa: E402
from blender_motion_pipeline.core.scene_loader import (  # noqa: E402
    SceneEntry,
    open_scene_for_generation,
)
from blender_motion_pipeline.core.sequence_generator import SequenceGenerator  # noqa: E402


def state(label: str) -> None:
    scene = bpy.context.scene
    camera = scene.camera
    bpy.context.view_layer.update()
    world = camera.matrix_world.translation
    parent = getattr(camera, "parent", None)
    parent_world = parent.matrix_world.translation if parent is not None else Vector((0, 0, 0))
    print(
        f"{label:26s} frame={scene.frame_current:4d} "
        f"local={tuple(round(float(v), 3) for v in camera.location)} "
        f"world={tuple(round(float(v), 3) for v in world)} "
        f"parent_world={tuple(round(float(v), 3) for v in parent_world)} "
        f"action={'yes' if (camera.animation_data and camera.animation_data.action) else 'no'}"
    )


def main() -> int:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if not argv:
        print("usage: blender -b -P tests/probe_anchor_drift.py -- <scene.blend> [templates.json [count]]")
        return 2
    scene_path = os.path.abspath(argv[0])
    templates = os.path.abspath(argv[1]) if len(argv) > 1 else ""
    count = int(argv[2]) if len(argv) > 2 else 4

    load = open_scene_for_generation(SceneEntry(path=scene_path))
    if not load.ok:
        print("open failed:", load.error)
        return 1

    config = default_config()
    config.motion.template_path = templates or config.motion.template_path
    config.motion.frame_start = 0
    config.motion.frame_end = 80
    config.validation.enabled = True
    config.search.enabled = True
    # Always regenerate: reusing an earlier run's artifacts would test nothing,
    # because a skipped sequence never touches the camera at all.
    config.batch.overwrite = True
    config.batch.resume = False
    library = MotionTemplateLibrary.from_config(config)
    names = library.names[:count]

    print(f"scene     : {scene_path}")
    print(f"templates : {names}\n")
    state("artist (as opened)")
    anchors = []
    for index, name in enumerate(names, start=1):
        template = library.by_name(name) if hasattr(library, "by_name") else None
        if template is None:
            template = next(item for item in library if item.name == name)
        generator = SequenceGenerator(
            config, output_root=os.path.join(os.path.dirname(scene_path), "_probe_drift_out")
        )
        requests = generator.build_requests(
            SceneEntry(path=scene_path),
            library=[template],
            cameras=[bpy.context.scene.camera.name],
            character_variants=[(False, None, None, "")],
        )
        before = bpy.context.scene.camera.matrix_world.translation.copy()
        result = generator.generate(requests[0])
        after = bpy.context.scene.camera.matrix_world.translation.copy()
        anchors.append((name, tuple(before), tuple(after), result.ok, result.error))
        print(f"\n--- sequence {index}: {name} (ok={result.ok} {result.error})")
        state("after generate")
    print("\n== anchor drift per sequence ==")
    previous = None
    for name, before, after, ok, error in anchors:
        drift = 0.0 if previous is None else (before[0] - previous[0])
        print(
            f"{name:34s} anchor_before={tuple(round(v, 3) for v in before)} "
            f"anchor_after={tuple(round(v, 3) for v in after)} drift_x={drift:+.3f}"
        )
        previous = before
    return 0


if __name__ == "__main__":
    sys.exit(main())
