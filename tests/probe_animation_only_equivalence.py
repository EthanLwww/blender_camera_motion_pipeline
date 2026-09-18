"""Prove that both storage modes render the same camera path.

    blender -b -P tests/probe_animation_only_equivalence.py -- \
        "<sequence.blend>" "<animation-only sequence dir>" [--frames N]

A sequence can be stored two ways:

* **blend** — ``sequence_<id>.blend`` is a copy of the scene with the animation
  baked in;
* **animation** — no scene copy; ``sequence_<id>.json`` carries the keyed values
  and the renderer replays them onto the source scene.

They must agree frame for frame, otherwise switching modes would silently change
every video.  This opens both, evaluates the camera's world position on every
frame of the recorded trajectory, and reports the largest disagreement -- against
the renderer's own code path (``core.camera_animation.apply_payload``).
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

from blender_motion_pipeline.core.camera_animation import PAYLOAD_KEY, apply_payload  # noqa: E402
from blender_motion_pipeline.core.scene_loader import SceneEntry, open_scene_for_generation  # noqa: E402


def world_path(camera_name: str, frames) -> "dict[int, tuple]":
    import bpy

    scene = bpy.context.scene
    camera = bpy.data.objects.get(camera_name)
    if camera is None or camera.type != "CAMERA":
        camera = scene.camera
    path = {}
    for frame in frames:
        scene.frame_set(int(frame))
        bpy.context.view_layer.update()
        evaluated = camera.evaluated_get(bpy.context.evaluated_depsgraph_get())
        translation = evaluated.matrix_world.translation
        path[int(frame)] = (
            float(translation[0]), float(translation[1]), float(translation[2]),
            float(evaluated.data.lens if hasattr(evaluated, "data") else camera.data.lens),
        )
    return path


def main() -> int:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if len(argv) < 2:
        print("usage: blender -b -P tests/probe_animation_only_equivalence.py -- "
              "<sequence.blend> <animation-only sequence dir>")
        return 2
    blend_sequence = os.path.abspath(argv[0])
    sequence_dir = os.path.abspath(argv[1])
    limit = int(argv[2]) if len(argv) > 2 else 0

    config_path = os.path.join(sequence_dir, "sequence_config.json")
    with open(config_path, encoding="utf-8") as handle:
        config = json.load(handle)
    sidecar = os.path.join(sequence_dir, (config.get("camera_animation") or {}).get("file") or "")
    with open(sidecar, encoding="utf-8") as handle:
        payload = (json.load(handle) or {}).get(PAYLOAD_KEY) or {}
    frames = [sample["frame"] for sample in payload.get("samples") or []]
    if limit:
        frames = frames[:limit]
    if not frames:
        print("the animation-only sequence carries no samples")
        return 1

    camera_name = (config.get("sequence") or {}).get("camera_name") or payload.get("object_name") or ""

    print(f"blend sequence   : {blend_sequence}")
    print(f"animation-only   : {sequence_dir}")
    print(f"source scene     : {(config.get('sequence') or {}).get('source_blend')}")
    print(f"camera           : {camera_name}   frames: {frames[0]}..{frames[-1]} ({len(frames)})")
    print(f"payload          : {payload.get('key_count')} key(s), "
          f"interpolation={payload.get('interpolation')}, "
          f"muted={payload.get('muted_constraints')}\n")

    load = open_scene_for_generation(SceneEntry(path=blend_sequence))
    if not load.ok:
        print("cannot open the blend sequence:", load.error)
        return 1
    from_blend = world_path(camera_name, frames)

    source = (config.get("sequence") or {}).get("source_blend") or ""
    load = open_scene_for_generation(SceneEntry(path=source))
    if not load.ok:
        print("cannot open the source scene:", load.error)
        return 1
    summary = apply_payload(payload, config=config)
    print(f"apply_payload    : {summary}")
    if not summary.get("applied"):
        print("the payload could not be applied")
        return 1
    from_payload = world_path(summary.get("object_name") or camera_name, frames)

    worst = 0.0
    worst_frame = None
    worst_lens = 0.0
    for frame in frames:
        left = from_blend[frame]
        right = from_payload[frame]
        delta = math.dist(left[:3], right[:3])
        if delta > worst:
            worst, worst_frame = delta, frame
        worst_lens = max(worst_lens, abs(left[3] - right[3]))

    print(f"\nworst camera-position difference : {worst:.6f} m at frame {worst_frame}")
    print(f"worst focal-length difference    : {worst_lens:.6f} mm")
    for frame in (frames[0], frames[len(frames) // 2], frames[-1]):
        print(f"  frame {frame:4d}  blend={tuple(round(v, 4) for v in from_blend[frame][:3])}  "
              f"payload={tuple(round(v, 4) for v in from_payload[frame][:3])}")
    ok = worst < 0.001 and worst_lens < 0.001
    print("\nverdict:", "OK - both storage modes give the same camera path"
          if ok else "MISMATCH - the two storage modes disagree")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
