"""A/B probe: how long does the camera-trajectory pass take, old path vs new?

    blender.exe -b -noaudio --factory-startup -P probe_trajectory_speed.py -- \
        "<sequence_config.json>" [frames]

``sample_camera_trajectory`` used to run ``scene.frame_set()`` + a depsgraph update
per frame, which re-evaluates a heavy scene once per trajectory row.  The analytic
F-curve path replaced that; this probe proves the rows are identical and prints how
much time it saves on a real generated sequence.
"""
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(_HERE))
for path in (_PACKAGE_PARENT, _HERE):
    if path not in sys.path:
        sys.path.insert(0, path)
import _boot  # noqa: E402,F401  (the add-on folder may be called anything)

import bpy  # noqa: E402

from blender_motion_pipeline.core.camera_animation import (  # noqa: E402
    PAYLOAD_KEY,
    apply_payload,
    find_camera,
)
from blender_motion_pipeline.io.json_io import load_json_file  # noqa: E402
from blender_motion_pipeline.render import metadata_exporter as mx  # noqa: E402

argv = sys.argv
args = argv[argv.index("--") + 1:] if "--" in argv else []
config_path = args[0] if args else ""
frames_wanted = int(args[1]) if len(args) > 1 else 12
if not config_path or not os.path.isfile(config_path):
    print("TRAJSPEED need a sequence_config.json path")
    raise SystemExit(2)

config = load_json_file(config_path)
scene = bpy.context.scene
animation = config.get("camera_animation") or {}
payload_path = os.path.join(os.path.dirname(config_path), animation.get("file") or "")
payload = {}
if os.path.isfile(payload_path):
    payload = load_json_file(payload_path) or {}
    payload = payload.get(animation.get("key") or PAYLOAD_KEY) or {}
summary = apply_payload(payload, config=config)
if summary.get("applied"):
    print("TRAJSPEED replayed %s (%s key(s))"
          % (os.path.basename(payload_path), summary.get("key_count")))
else:
    print("TRAJSPEED replay failed: %s" % (summary.get("warnings") or summary))

camera = find_camera(payload, config)
if camera is None:
    print("TRAJSPEED no camera in this scene")
    raise SystemExit(2)

start = int((config.get("frames") or {}).get("frame_start") or 0)
end = min(start + frames_wanted - 1, int((config.get("frames") or {}).get("frame_end") or start))
print("TRAJSPEED frames %d..%d (%d)" % (start, end, end - start + 1))

depsgraph = bpy.context.evaluated_depsgraph_get()
reason = mx.analytic_camera_reason(camera)
print("TRAJSPEED analytic-supported=%s%s" % (not reason, (" reason=%s" % reason) if reason else ""))
action = getattr(getattr(camera, "animation_data", None), "action", None)
if action is not None:
    from blender_motion_pipeline.utils.animation import action_fcurves

    curves = action_fcurves(action)
    seen = {}
    for curve in curves:
        key = (curve.data_path, curve.array_index)
        seen[key] = seen.get(key, 0) + 1
    slot = getattr(camera.animation_data, "action_slot", None)
    print("TRAJSPEED action=%r curves=%d slots=%d slot=%r dup=%s"
          % (action.name, len(curves), len(getattr(action, "slots", ()) or ()),
             getattr(slot, "name_display", getattr(slot, "name", None)),
             [key for key, count in seen.items() if count > 1][:4]))
    print("TRAJSPEED curve paths=%s" % sorted({curve.data_path for curve in curves})[:8])
    for layer in getattr(action, "layers", []) or []:
        for strip in getattr(layer, "strips", []) or []:
            bags = list(getattr(strip, "channelbags", []) or [])
            print("TRAJSPEED layer=%r strip=%r bags=%d" % (layer.name, strip.type, len(bags)))
            for bag in bags:
                bag_slot = getattr(bag, "slot", None)
                print("TRAJSPEED   bag slot=%r curves=%d"
                      % (getattr(bag_slot, "name_display", getattr(bag_slot, "name", None)),
                         len(list(getattr(bag, "fcurves", []) or []))))
            lookup = getattr(strip, "channelbag", None)
            if callable(lookup) and slot is not None:
                try:
                    found = lookup(slot)
                    print("TRAJSPEED   strip.channelbag(slot) -> %s"
                          % ("found" if found is not None else "None"))
                except Exception as exc:
                    print("TRAJSPEED   strip.channelbag(slot) raised %s" % exc)

# -- new path: analytic F-curve poses (what sample_camera_trajectory uses now) ----
t0 = time.time()
poses = mx.analytic_camera_poses(camera, list(range(start, end + 1)))
t_analytic = time.time() - t0
verified = bool(poses) and mx.verify_camera_poses(camera, scene, depsgraph,
                                                 list(range(start, end + 1)), poses)
print("TRAJSPEED analytic %.3fs poses=%s verified=%s" % (t_analytic, bool(poses), verified))

t0 = time.time()
fast_rows = mx.sample_camera_trajectory(camera, frame_start=start, frame_end=end,
                                        scene=scene, depsgraph=depsgraph)
t_fast = time.time() - t0
print("TRAJSPEED sample_camera_trajectory %.3fs rows=%d" % (t_fast, len(fast_rows)))

# -- old path: force the depsgraph loop ------------------------------------------
original = mx.analytic_camera_poses
mx.analytic_camera_poses = lambda *a, **k: None
try:
    t0 = time.time()
    slow_rows = mx.sample_camera_trajectory(camera, frame_start=start, frame_end=end,
                                           scene=scene, depsgraph=depsgraph)
    t_slow = time.time() - t0
finally:
    mx.analytic_camera_poses = original
print("TRAJSPEED depsgraph-loop %.3fs rows=%d" % (t_slow, len(slow_rows)))

same = len(fast_rows) == len(slow_rows) and all(
    abs(a.focal_length - b.focal_length) < 1e-6 and all(
        abs(getattr(a, name) - getattr(b, name)) < 1e-5
        for name in ("r00", "r01", "r02", "tx", "r10", "r11", "r12", "ty",
                     "r20", "r21", "r22", "tz"))
    for a, b in zip(fast_rows, slow_rows)
)
print("TRAJSPEED identical=%s  speedup=%.0fx  (%.2fs -> %.2fs)"
      % (same, (t_slow / t_fast) if t_fast > 0 else 0.0, t_slow, t_fast))
print("TRAJSPEED done")
