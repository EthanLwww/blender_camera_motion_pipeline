"""Dump what a generated sequence file actually contains for its camera.

    blender -b -P tests/probe_baked_curves.py -- "<sequence.blend>"

Shows every f-curve, the keyed values at a few frames, the parent's animation,
and the resulting evaluated world position -- so a baking bug is visible directly
instead of being inferred from a trajectory file.
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

from blender_motion_pipeline.core.scene_loader import SceneEntry, open_scene_for_generation  # noqa: E402
from blender_motion_pipeline.utils.animation import action_fcurves, action_data_paths  # noqa: E402


def curve_samples(action, paths=("location", "rotation_quaternion", "rotation_euler"), frames=(0, 1, 40, 80)):
    by_path = {}
    for curve in action_fcurves(action):
        if curve.data_path not in paths:
            continue
        by_path[(curve.data_path, curve.array_index)] = [
            round(float(curve.evaluate(frame)), 4) for frame in frames
        ]
    return by_path


def main() -> int:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    blend = os.path.abspath(argv[0])
    load = open_scene_for_generation(SceneEntry(path=blend))
    if not load.ok:
        print("open failed:", load.error)
        return 1
    scene = bpy.context.scene
    frames = (0, 1, 40, 80)

    print(f"file        : {blend}")
    print(f"scene       : {scene.name!r} frames {scene.frame_start}..{scene.frame_end} "
          f"engine={scene.render.engine} fps={scene.render.fps}")
    print(f"active cam  : {getattr(scene.camera, 'name', None)}")
    print(f"all cameras : {[o.name for o in scene.objects if o.type == 'CAMERA']}")

    for obj in [o for o in scene.objects if o.type == "CAMERA"]:
        print(f"\n=== {obj.name} ===")
        print(f"  parent        : {getattr(obj.parent, 'name', None)} ({obj.parent_type})")
        print(f"  matrix_parent_inverse translation: "
              f"{tuple(round(float(v), 4) for v in obj.matrix_parent_inverse.translation)}")
        print(f"  rotation_mode : {obj.rotation_mode}")
        print(f"  constraints   : "
              f"{[(c.type, round(c.influence, 2), getattr(c.target, 'name', None)) for c in obj.constraints]}")
        animation_data = obj.animation_data
        if animation_data and animation_data.action:
            action = animation_data.action
            print(f"  action        : {action.name} paths={action_data_paths(action)}")
            for (path, index), values in sorted(curve_samples(action).items()):
                print(f"    {path}[{index}] @{frames} = {values}")
        else:
            print("  action        : none")

        parent = obj.parent
        if parent is not None and parent.animation_data and parent.animation_data.action:
            action = parent.animation_data.action
            print(f"  PARENT action : {action.name} paths={action_data_paths(action)}")
            for (path, index), values in sorted(curve_samples(action).items()):
                print(f"    parent {path}[{index}] @{frames} = {values}")

        print("  evaluated world positions:")
        for frame in frames:
            scene.frame_set(int(frame))
            bpy.context.view_layer.update()
            evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
            translation = evaluated.matrix_world.translation
            parent_world = parent.matrix_world.translation if parent else None
            print(f"    frame {frame:3d}: world={tuple(round(float(v), 4) for v in translation)}"
                  + (f" parent_world={tuple(round(float(v), 4) for v in parent_world)}"
                     if parent_world else "")
                  + f" local={tuple(round(float(v), 4) for v in obj.location)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
