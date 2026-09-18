"""Inspect the working scene without generating anything.

    blender -b -P tests/probe_scene.py -- "<blend path>"

Uses the plugin's own scene loader so the report matches what generation will
see (same camera discovery, same geometry harvest, same ray caster).
"""
from __future__ import annotations

import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(_HERE))
for path in (_PACKAGE_PARENT, _HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

import bpy  # noqa: E402

from blender_motion_pipeline.core import blender_context as bctx  # noqa: E402
from blender_motion_pipeline.core.scene_loader import SceneEntry, open_scene_for_generation  # noqa: E402
from blender_motion_pipeline.io.resource_check import (  # noqa: E402
    blend_resources_from_bpy, check_blend_resources,
)


def main() -> int:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if not argv:
        print("usage: blender -b -P tests/probe_scene.py -- <blend path>")
        return 2
    blend = os.path.abspath(argv[0])
    print("opening:", blend, flush=True)
    started = time.time()
    load = open_scene_for_generation(SceneEntry(path=blend))
    print(f"open ok={load.ok} in {time.time() - started:.1f}s error={load.error or '-'}",
          flush=True)
    if not load.ok:
        return 1

    scene = bpy.context.scene
    report = bctx.scene_report()
    context = bctx.build_scene_context()

    summary = {
        "blend": blend,
        "open_seconds": round(time.time() - started, 2),
        "scene_name": report["scene_name"],
        "scene_count": report["scene_count"],
        "other_scenes": report["other_scenes"],
        "frame_range": report["frame_range"],
        "frame_count": report["frame_count"],
        "fps": report["fps"],
        "resolution": report["resolution"],
        "resolution_percentage": report["resolution_percentage"],
        "engine": report["engine"],
        "camera_count": report["camera_count"],
        "camera_names": report["camera_names"],
        "mesh_count": report["mesh_count"],
        "world_bbox": report["world_bbox"],
        "world_diagonal": report["world_diagonal"],
        "object_count": report["object_count"],
        "has_armature": report["has_character_rig"],
        "active_camera": scene.camera.name if scene.camera else None,
        "ray_caster": getattr(context.ray_caster, "description", "none"),
    }

    # Per-camera detail, because every camera multiplies the sequence count.
    cameras = []
    for obj in bctx.list_camera_objects(scene):
        snapshot = bctx.camera_snapshot(obj, scene)
        cameras.append({
            "name": obj.name,
            "location": [round(float(v), 3) for v in snapshot.location],
            "rotation_mode": snapshot.rotation_mode,
            "lens_mm": round(snapshot.lens, 2),
            "sensor_width": snapshot.sensor_width,
            "sensor_fit": snapshot.sensor_fit,
            "clip": [snapshot.clip_start, snapshot.clip_end],
            "resolution": list(snapshot.effective_resolution),
            "had_animation": snapshot.had_animation,
            "animated_properties": snapshot.animated_properties,
            "data_users": snapshot.data_users,
        })
    summary["cameras"] = cameras

    # Missing external assets matter on a 265 MB production file.
    resources = blend_resources_from_bpy(blend)
    result = check_blend_resources(resources)
    summary["resource_check"] = result.as_dict()

    print("\n================ SCENE REPORT ================")
    for key, value in summary.items():
        if key in ("cameras", "resource_check", "world_bbox"):
            continue
        print(f"  {key}: {value}")
    print("  world_bbox:", summary["world_bbox"])
    print("\n  cameras:")
    for camera in cameras:
        print(f"    {camera['name']}: loc={camera['location']} lens={camera['lens_mm']}mm "
              f"clip={camera['clip']} anim={camera['had_animation']} "
              f"{camera['animated_properties']}")
    print("\n  external resources:")
    check = summary["resource_check"]
    print(f"    checked={check['checked']} missing={check['missing_count']} ok={check['ok']}")
    for item in (check.get("missing") or [])[:10]:
        print(f"      MISSING [{item['kind']}] {item['path']}")
    print("=============================================")

    target = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scene_report.json")
    with open(os.path.abspath(target), "w", encoding="utf-8", newline="\n") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    print("report written to", os.path.abspath(target))
    return 0


if __name__ == "__main__":
    sys.exit(main())
