"""Sweep every generated sequence and verify its baked camera path.

    blender -b -P tests/probe_all_sequences.py -- "<sequence root>"

For each ``sequence_*.blend`` found under the root this loads the file and
compares the camera's **evaluated world position** frame by frame against the
world position recorded in the sidecar JSON that the generator wrote.  They must
agree, otherwise what the renderer draws is not what the validator approved.

A one-line result per sequence keeps a whole batch readable, and any sequence
whose worst deviation exceeds 1 cm is printed with its offending frames.
"""
from __future__ import annotations

import json
import math
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

from blender_motion_pipeline.core.scene_loader import (  # noqa: E402
    SceneEntry,
    open_scene_for_generation,
)

TOLERANCE = 0.01


def world_from_row(row):
    """Recover the camera centre from a W2C 3x4 block (R^T * t)."""
    rotation = [[row[i][j] for j in range(3)] for i in range(3)]
    translation = [row[i][3] for i in range(3)]
    return [-sum(rotation[k][j] * translation[k] for k in range(3)) for j in range(3)]


def probe(sequence_blend: str) -> "tuple[bool, str]":
    sidecar = os.path.splitext(sequence_blend)[0] + ".json"
    load = open_scene_for_generation(SceneEntry(path=sequence_blend))
    if not load.ok:
        return False, f"open failed: {load.error}"
    scene = bpy.context.scene
    camera = scene.camera
    if camera is None:
        return False, "no active camera"
    with open(sidecar, encoding="utf-8") as handle:
        payload = json.load(handle)
    recorded = {entry["frame"]: entry for entry in (payload.get("camera_trajectory") or [])}
    if not recorded:
        return False, "sidecar has no camera_trajectory"

    worst = 0.0
    worst_frame = None
    travelled = 0.0
    previous = None
    focal_delta = 0.0
    for frame in sorted(recorded):
        scene.frame_set(int(frame))
        bpy.context.view_layer.update()
        evaluated = camera.evaluated_get(bpy.context.evaluated_depsgraph_get())
        actual = evaluated.matrix_world.translation
        actual = (float(actual[0]), float(actual[1]), float(actual[2]))
        expected = world_from_row(recorded[frame]["matrix"])
        delta = math.dist(actual, expected)
        if delta > worst:
            worst, worst_frame = delta, frame
        if previous is not None:
            travelled += math.dist(actual, previous)
        previous = actual
        wanted_focal = recorded[frame].get("focal_length")
        if wanted_focal:
            focal_delta = max(focal_delta, abs(float(camera.data.lens) - float(wanted_focal)))

    parent = getattr(camera, "parent", None)
    muted = [c.name for c in camera.constraints if getattr(c, "mute", False)]
    note = (
        f"parent={getattr(parent, 'name', None)!r} "
        f"constraints_muted={muted} "
        f"travelled={travelled:9.3f} m focal_delta={focal_delta:.4f} mm"
    )
    ok = worst < TOLERANCE
    return ok, f"worst={worst:8.4f} m @f{worst_frame} | {note}"


def main() -> int:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if not argv:
        print("usage: blender -b -P tests/probe_all_sequences.py -- <sequence root>")
        return 2
    root = os.path.abspath(argv[0])
    blends = []
    for folder, _dirs, files in os.walk(root):
        for name in sorted(files):
            if name.startswith("sequence_") and name.endswith(".blend"):
                blends.append(os.path.join(folder, name))
    blends.sort()
    if not blends:
        print(f"no sequence blends found under {root}")
        return 2

    print(f"probing {len(blends)} sequence(s) under {root}\n")
    failures = 0
    for index, blend in enumerate(blends, start=1):
        label = os.path.relpath(blend, root).replace("\\", "/")
        ok, detail = probe(blend)
        if not ok:
            failures += 1
        print(f"[{'ok  ' if ok else 'FAIL'}] {index:2d}/{len(blends)} {label}\n           {detail}")
    print()
    if failures:
        print(f"verdict: {failures} of {len(blends)} sequence(s) MISMATCH the validated path")
        return 1
    print(f"verdict: OK - all {len(blends)} sequence(s) reproduce the validated camera path")
    return 0


if __name__ == "__main__":
    sys.exit(main())
