"""Inspect the working scene's cameras, constraints and drivers in detail.

    blender -b -P tests/probe_camera_constraints.py -- "<blend path>"

Answers: what actually drives each camera, whether it is pure f-curve animation,
and what pose it holds at the template's first frame.
"""
from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(_HERE))
for path in (_PACKAGE_PARENT, _HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

import bpy  # noqa: E402

from blender_motion_pipeline.core import blender_context as bctx  # noqa: E402
from blender_motion_pipeline.core.scene_loader import SceneEntry, open_scene_for_generation  # noqa: E402
from blender_motion_pipeline.utils.animation import action_data_paths, action_fcurves  # noqa: E402


def describe_constraint(constraint) -> dict:
    info = {
        "name": constraint.name,
        "type": constraint.type,
        "mute": bool(getattr(constraint, "mute", False)),
        "influence": round(float(getattr(constraint, "influence", 1.0)), 3),
    }
    target = getattr(constraint, "target", None)
    info["target"] = getattr(target, "name", None)
    if constraint.type == "COPY_LOCATION":
        info["subtarget"] = getattr(constraint, "subtarget", "")
        info["use_offset"] = bool(getattr(constraint, "use_offset", False))
    if constraint.type == "TRACK_TO":
        info["track_axis"] = str(getattr(constraint, "track_axis", ""))
        info["up_axis"] = str(getattr(constraint, "up_axis", ""))
    return info


def main() -> int:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    blend = os.path.abspath(argv[0]) if argv else ""
    load = open_scene_for_generation(SceneEntry(path=blend))
    if not load.ok:
        print("open failed:", load.error)
        return 1

    scene = bpy.context.scene
    print(f"scene={scene.name!r} frame_range={scene.frame_start}..{scene.frame_end} "
          f"fps={scene.render.fps} engine={scene.render.engine}")
    print(f"active camera: {scene.camera.name if scene.camera else None}")

    report = {}
    for obj in bctx.list_camera_objects(scene):
        entry = {"name": obj.name, "constraints": [], "drivers": [], "action": None,
                 "nla_tracks": 0, "parent": getattr(obj.parent, "name", None),
                 "parent_type": obj.parent_type}
        for constraint in obj.constraints:
            entry["constraints"].append(describe_constraint(constraint))
        animation_data = obj.animation_data
        if animation_data is not None:
            action = animation_data.action
            if action is not None:
                entry["action"] = {
                    "name": action.name,
                    "frame_range": [round(float(v), 1) for v in action.frame_range],
                    "paths": action_data_paths(action),
                    "curve_count": len(action_fcurves(action)),
                    "key_count": sum(len(c.keyframe_points) for c in action_fcurves(action)),
                }
            entry["nla_tracks"] = len(getattr(animation_data, "nla_tracks", []) or [])
        # Sampled pose at the template's frame window and at the scene's end.
        poses = {}
        for frame in (scene.frame_start, 1, 40, 81, scene.frame_end):
            scene.frame_set(int(frame))
            bpy.context.view_layer.update()
            evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
            location = evaluated.matrix_world.translation
            poses[int(frame)] = [round(float(v), 4) for v in location]
        entry["sampled_locations"] = poses
        report[obj.name] = entry
        scene.frame_set(scene.frame_start)

    print("\n================ CAMERA DRIVERS ================")
    for name, entry in report.items():
        print(f"\n{name}")
        print(f"  parent        : {entry['parent']} ({entry['parent_type']})")
        if entry["action"]:
            action = entry["action"]
            print(f"  action        : {action['name']} frames="
                  f"{action['frame_range']} curves={action['curve_count']} "
                  f"keys={action['key_count']}")
            print(f"  animated paths: {action['paths']}")
        else:
            print("  action        : none")
        print(f"  nla tracks    : {entry['nla_tracks']}")
        print(f"  constraints   : {len(entry['constraints'])}")
        for constraint in entry["constraints"]:
            print(f"    - {constraint['type']:14s} target={constraint['target']} "
                  f"mute={constraint['mute']} influence={constraint['influence']} "
                  f"{constraint.get('subtarget', '')}")
        print(f"  sampled loc   : {entry['sampled_locations']}")
    print("===============================================")

    target = os.path.abspath(os.path.join(_HERE, "..", "camera_drivers.json"))
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print("written to", target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
