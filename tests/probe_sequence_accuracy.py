"""Verify that a generated sequence reproduces the intended camera path.

    blender -b -P tests/probe_sequence_accuracy.py -- "<sequence.blend>" [sample.json]

Loads a generated sequence file and compares, frame by frame:

* the camera's **evaluated world position** in the sequence file, against
* the world position recorded in the generator's own trajectory JSON.

They must agree: the generator validates world poses and the renderer draws the
evaluated camera, so any divergence is a baking bug (a parent, a constraint, or a
rotation-mode mismatch).
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

import bpy  # noqa: E402

from blender_motion_pipeline.core.scene_loader import SceneEntry, open_scene_for_generation  # noqa: E402


def world_from_row(row, focal):
    """Recover the camera centre from a W2C 3x4 block (R^T * t)."""
    rotation = [[row[i][j] for j in range(3)] for i in range(3)]
    translation = [row[i][3] for i in range(3)]
    return [
        -sum(rotation[k][j] * translation[k] for k in range(3))
        for j in range(3)
    ]


def main() -> int:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if not argv:
        print("usage: blender -b -P tests/probe_sequence_accuracy.py -- <sequence.blend> [json]")
        return 2
    sequence_blend = os.path.abspath(argv[0])
    sidecar = os.path.abspath(argv[1]) if len(argv) > 1 else os.path.splitext(sequence_blend)[0] + ".json"

    load = open_scene_for_generation(SceneEntry(path=sequence_blend))
    if not load.ok:
        print("open failed:", load.error)
        return 1
    scene = bpy.context.scene

    payload = {}
    if os.path.isfile(sidecar):
        with open(sidecar, encoding="utf-8") as handle:
            payload = json.load(handle)
    recorded = {entry["frame"]: entry for entry in (payload.get("camera_trajectory") or [])}
    print(f"sequence blend : {sequence_blend}")
    print(f"sidecar        : {sidecar} ({len(recorded)} recorded frame(s))")
    print(f"scene          : {scene.name!r} frames {scene.frame_start}..{scene.frame_end} "
          f"engine={scene.render.engine}")

    camera = scene.camera
    if camera is None:
        print("no active camera in the sequence file")
        return 1
    parent = getattr(camera, "parent", None)
    print(f"camera         : {camera.name}")
    print(f"  parent       : {getattr(parent, 'name', None)} "
          f"(type={camera.parent_type}, inverse set={tuple(round(v, 3) for v in camera.matrix_parent_inverse.translation)})")
    print(f"  rotation_mode: {camera.rotation_mode}")
    print(f"  constraints  : {[(c.type, round(c.influence, 2), getattr(c.target, 'name', None)) for c in camera.constraints]}")
    print(f"  local loc    : {tuple(round(float(v), 3) for v in camera.location)}")

    frames = sorted(recorded) or list(range(scene.frame_start, min(scene.frame_end, 80) + 1))
    worst = 0.0
    worst_frame = None
    print("\n frame |        sequence world pos        |        recorded world pos        |   delta")
    for frame in frames:
        scene.frame_set(int(frame))
        bpy.context.view_layer.update()
        evaluated = camera.evaluated_get(bpy.context.evaluated_depsgraph_get())
        actual = evaluated.matrix_world.translation
        actual = (float(actual[0]), float(actual[1]), float(actual[2]))
        entry = recorded.get(frame)
        if entry is None:
            print(f" {frame:5d} | {tuple(round(v,3) for v in actual)} | (not recorded)")
            continue
        expected = world_from_row(entry["matrix"], entry.get("focal_length"))
        delta = math.dist(actual, expected)
        if delta > worst:
            worst, worst_frame = delta, frame
        if frame in frames[:3] or frame in frames[-3:] or delta > 0.5:
            print(f" {frame:5d} | {tuple(round(v,3) for v in expected)} | "
                  f"{tuple(round(v,3) for v in actual)} | {delta:8.3f} m")

    print(f"\nworst deviation: {worst:.3f} m at frame {worst_frame}")
    verdict = "OK (baked path matches the validated path)" if worst < 0.01 else (
        "MISMATCH (the sequence does not render what was validated)")
    print("verdict:", verdict)
    return 0 if worst < 0.01 else 1


if __name__ == "__main__":
    sys.exit(main())
