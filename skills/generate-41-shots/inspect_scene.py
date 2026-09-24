"""Measure a scene so a human (or an agent) can draw the region box and place the anchor.

    blender -b -noaudio --factory-startup -P skills/generate-41-shots/inspect_scene.py -- \
        --scenes /path/to/scene.blend [--scenes /path/to/folder] --report /tmp/inspect.json

This is step 1 of the ``generate-41-shots`` skill and it **only measures**: it never
writes a scene, and it never decides anything for you.  The camera-region box and the
focus anchor are inputs the operator has to supply, and the point of this report is to
give them the numbers to do that from -- plus the two checks that are easy to get wrong:

* **which objects are actually the room.**  An interior scene usually carries an
  environment dome or a backdrop that is tens or hundreds of metres across; a box fitted
  to "everything" would be useless.  Those objects are reported as excluded so the
  operator can see the decision rather than inherit it.
* **whether a camera looks into the room at all.**  A camera parked in the next room
  still counts as a camera, and the first surface along its view axis says whether its
  shots are of this room or of a wall.

The anchor candidates are ranked by the one thing that decides whether an ``Arc`` shot
works: a re-centred orbit has the radius *the camera's own distance to the subject*, so
the subject has to stand where that circle stays inside the room.  Every candidate is
scored by how many frames of the 90 degree orbit (both directions, for every camera)
would leave the interior or pass through furniture.

Output: JSON on stdout (and to ``--report`` when given).  Nothing else is written.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PACKAGE = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.dirname(PACKAGE))
sys.path.insert(0, HERE)


def _bootstrap():
    """Make the add-on importable whatever its folder is called."""
    import importlib.util

    path = os.path.join(PACKAGE, "_bootstrap.py")
    if not os.path.isfile(path):
        return
    spec = importlib.util.spec_from_file_location("_mpp_bootstrap", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.bootstrap(path)


_bootstrap()

from mathutils import Vector  # noqa: E402

#: Objects whose name suggests a backdrop rather than a room.
DOME_HINTS = ("dome", "sky", "hdri", "backdrop", "environment", "cyclorama")
#: A surface this tall (relative to the room) is furniture, not floor, when looking for
#: somewhere to stand a model.
FLOOR_TOLERANCE = 0.05
#: How close a camera may come to a wall before the orbit counts as leaving the room.
WALL_KEEP = 0.30
ORBIT_SWEEP_DEG = 90.0
ORBIT_STEP_DEG = 5.0


def scene_files(paths, *, recursive=True) -> "list[str]":
    """Every ``.blend`` under the given files and folders."""
    found: "list[str]" = []
    for entry in paths:
        entry = os.path.abspath(entry)
        if os.path.isfile(entry) and entry.lower().endswith(".blend"):
            found.append(entry)
        elif os.path.isdir(entry):
            for root, _dirs, files in os.walk(entry):
                for name in sorted(files):
                    if name.lower().endswith(".blend"):
                        found.append(os.path.join(root, name))
                if not recursive:
                    break
        else:
            raise SystemExit(f"not a .blend file or folder: {entry}")
    return found


def _bounds(obj):
    corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    lo = [min(c[i] for c in corners) for i in range(3)]
    hi = [max(c[i] for c in corners) for i in range(3)]
    return lo, hi


def _split_environment(objects) -> "tuple[list, list]":
    """``(room objects, environment objects)`` -- by name hint and by sheer size."""
    sizes = []
    for obj in objects:
        lo, hi = _bounds(obj)
        sizes.append(max(hi[i] - lo[i] for i in range(3)))
    if not sizes:
        return [], []
    typical = sorted(sizes)[len(sizes) // 2] or 1.0
    room, environment = [], []
    for obj, size in zip(objects, sizes):
        names = obj.name.lower()
        if any(hint in names for hint in DOME_HINTS) or size > max(50.0, typical * 20.0):
            environment.append(obj)
        else:
            room.append(obj)
    return room, environment


def _clearance(scene, depsgraph, point, *, exclude=(), directions=26):
    """Shortest distance from ``point`` to any geometry, along a fibonacci sphere."""
    golden = math.pi * (3.0 - math.sqrt(5.0))
    best = float("inf")
    for index in range(directions):
        z = 1.0 - (2.0 * index + 1.0) / directions
        radius = math.sqrt(max(0.0, 1.0 - z * z))
        theta = golden * index
        direction = Vector((math.cos(theta) * radius, math.sin(theta) * radius, z))
        hit, location, _normal, _index, obj, _matrix = scene.ray_cast(
            depsgraph, Vector(point), direction)
        if not hit or (obj is not None and obj.name in exclude):
            continue
        best = min(best, (Vector(location) - Vector(point)).length)
    return best


def _surface(scene, depsgraph, x, y, *, top=3.0, exclude=()):
    """What a downward ray from ``top`` lands on at ``(x, y)``: ``(z, object name)``."""
    hit, location, _normal, _index, obj, _matrix = scene.ray_cast(
        depsgraph, Vector((x, y, top)), Vector((0.0, 0.0, -1.0)))
    if not hit or (obj is not None and obj.name in exclude):
        return None
    return (float(location[2]), obj.name if obj else "")


def _orbit_score(anchor, camera, interior, obstacles, sweep=ORBIT_SWEEP_DEG,
                 step=ORBIT_STEP_DEG):
    """Frames of a re-centred orbit that leave the interior or cross furniture."""
    start = Vector(camera.matrix_world.translation)
    offset = Vector((start.x - anchor[0], start.y - anchor[1], 0.0))
    radius = offset.length
    if radius < 0.25:
        return None
    bad = 0
    worst = 0.0
    total = 0
    for sign in (-1.0, 1.0):
        for index in range(int(sweep / step) + 1):
            angle = math.radians(sign * index * step)
            cos_a, sin_a = math.cos(angle), math.sin(angle)
            x = anchor[0] + offset.x * cos_a - offset.y * sin_a
            y = anchor[1] + offset.x * sin_a + offset.y * cos_a
            z = start.z
            total += 1
            overflow = 0.0
            if x < interior[0] + WALL_KEEP:
                overflow = max(overflow, interior[0] + WALL_KEEP - x)
            if x > interior[1] - WALL_KEEP:
                overflow = max(overflow, x - (interior[1] - WALL_KEEP))
            if y < interior[2] + WALL_KEEP:
                overflow = max(overflow, interior[2] + WALL_KEEP - y)
            if y > interior[3] - WALL_KEEP:
                overflow = max(overflow, y - (interior[3] - WALL_KEEP))
            for name, lo, hi in obstacles:
                if (lo[0] - 0.15 <= x <= hi[0] + 0.15 and lo[1] - 0.15 <= y <= hi[1] + 0.15
                        and lo[2] <= z <= hi[2] + 0.15):
                    overflow = max(overflow, WALL_KEEP)
            if overflow > 0.0:
                bad += 1
                worst = max(worst, overflow)
    return {"bad_frames": bad, "frames": total, "worst_overflow_m": round(worst, 2),
            "radius_m": round(radius, 2)}


def inspect_scene(path: str, *, grid_step: float = 0.25) -> dict:
    """Everything the report says about one scene."""
    import bpy

    bpy.ops.wm.open_mainfile(filepath=os.path.abspath(path), load_ui=False)
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()

    meshes = [obj for obj in scene.objects if obj.type == "MESH"]
    room, environment = _split_environment(meshes)
    excluded = {obj.name for obj in environment}

    report = {
        "source": os.path.abspath(path),
        "scene_name": scene.name,
        "units": [scene.unit_settings.system, float(scene.unit_settings.scale_length)],
        "fps": float(scene.render.fps),
        "frames": [int(scene.frame_start), int(scene.frame_end)],
        "resolution": [int(scene.render.resolution_x), int(scene.render.resolution_y),
                       int(scene.render.resolution_percentage)],
        "engine": str(scene.render.engine),
    }
    if not room:
        report["error"] = "the scene has no mesh objects to measure"
        return report

    lo = [min(_bounds(obj)[0][i] for obj in room) for i in range(3)]
    hi = [max(_bounds(obj)[1][i] for obj in room) for i in range(3)]
    report["interior"] = {"min": [round(v, 2) for v in lo], "max": [round(v, 2) for v in hi],
                          "size": [round(hi[i] - lo[i], 2) for i in range(3)]}
    report["excluded_objects"] = sorted(excluded)
    if excluded:
        report["excluded_note"] = (
            "these are treated as environment, not room: "
            + ", ".join(f"{name} ({[round(v, 1) for v in _bounds(next(o for o in environment if o.name == name))[1]]})"
                        for name in sorted(excluded)[:4])
        )

    # The floor: the *lowest* surface a downward ray finds across the room.  A single
    # ray under the middle of the bounds lands on whatever furniture is there (a bed,
    # in the reference scene), which then makes every "on the floor" test wrong.
    floor_samples = []
    x = lo[0] + grid_step
    while x <= hi[0] - grid_step:
        y = lo[1] + grid_step
        while y <= hi[1] - grid_step:
            surface = _surface(scene, depsgraph, x, y, exclude=excluded)
            if surface is not None:
                floor_samples.append(surface)
            y += grid_step * 2
        x += grid_step * 2
    floor_z = min((item[0] for item in floor_samples), default=None)
    report["floor_z"] = round(floor_z, 3) if floor_z is not None else None
    report["floor_samples"] = len(floor_samples)
    report["floor_note"] = (
        f"lowest of {len(floor_samples)} downward rays across the room: z={floor_z:.3f}; "
        "anything more than 5 cm above it is furniture, not somewhere to stand a model"
        if floor_z is not None else "no surface found under the room bounds")

    cameras = []
    for obj in [item for item in scene.objects if item.type == "CAMERA"]:
        start = obj.matrix_world.translation
        forward = (obj.matrix_world.to_3x3() @ Vector((0.0, 0.0, -1.0))).normalized()
        hits = []
        origin = Vector(start)
        for _ in range(4):
            hit, location, _normal, _index, hit_obj, _matrix = scene.ray_cast(
                depsgraph, origin, forward)
            if not hit:
                break
            if hit_obj is not None and hit_obj.name in excluded:
                origin = Vector(location) + forward * 0.01
                continue
            hits.append({"object": hit_obj.name if hit_obj else "?",
                         "distance_m": round((Vector(location) - Vector(start)).length, 2),
                         "at": [round(v, 2) for v in location]})
            origin = Vector(location) + forward * 0.01
            if len(hits) >= 3:
                break
        cameras.append({
            "name": obj.name,
            "location": [round(v, 3) for v in start],
            "lens_mm": round(float(obj.data.lens), 2),
            "active": obj == scene.camera,
            "first_hits": hits,
            "sees_geometry_after_m": hits[0]["distance_m"] if hits else None,
        })
    report["cameras"] = cameras

    # Furniture the camera must not fly through, and the surface to stand a model on.
    obstacles = []
    for obj in room:
        flo, fhi = _bounds(obj)
        if (fhi[2] - flo[2]) <= 0.05 and (fhi[0] - flo[0]) > 0.5:
            continue                      # a flat slab: floor or a rug
        obstacles.append((obj.name, flo, fhi))

    candidates = []
    floor_z = report.get("floor_z")
    if floor_z is not None:
        x = lo[0] + grid_step
        while x <= hi[0] - grid_step:
            y = lo[1] + grid_step
            while y <= hi[1] - grid_step:
                on = _surface(scene, depsgraph, x, y, exclude=excluded)
                if on is not None and abs(on[0] - floor_z) <= FLOOR_TOLERANCE:
                    clearance = _clearance(scene, depsgraph, (x, y, floor_z + 1.2),
                                           exclude=excluded)
                    score = 0
                    worst_ratio = 0.0
                    detail = {}
                    for camera in [item for item in scene.objects if item.type == "CAMERA"]:
                        result = _orbit_score((x, y, floor_z), camera, (lo[0], hi[0], lo[1], hi[1]),
                                              obstacles)
                        if result is None:
                            continue
                        score += result["bad_frames"]
                        worst_ratio = max(worst_ratio,
                                          result["bad_frames"] / float(result["frames"]))
                        detail[camera.name] = result
                    candidates.append({
                        "anchor": [round(x, 2), round(y, 2), round(floor_z, 3)],
                        "surface": on[1],
                        "clearance_m": round(clearance, 2) if clearance != float("inf") else None,
                        "orbit_bad_frames": score,
                        "orbit_worst_camera_ratio": round(worst_ratio, 3),
                        "per_camera": detail,
                    })
                y += grid_step
            x += grid_step

    # Sorted by the *worst* camera rather than the total: one distant camera whose orbit
    # cannot fit the room would otherwise dominate the ranking and hide the spots that
    # work for the cameras that matter.
    candidates.sort(key=lambda item: (item["orbit_worst_camera_ratio"],
                                      item["orbit_bad_frames"],
                                      -(item["clearance_m"] or 0.0)))
    report["anchor_candidates"] = candidates[:24]
    report["anchor_note"] = (
        "ranked by the worst camera's share of orbit frames that leave the room (a "
        "re-centred Arc orbits at the camera's own distance to the subject, so a camera "
        "far away always sweeps a big circle).  Pick a candidate whose surface is the "
        "floor you want the subject to stand on, then check per_camera: a camera whose "
        "shots you care about should be near zero."
    )

    # A starting point for the box -- the operator still has to confirm it, because
    # "inside the scene" is a judgement about the room, not a bounding box.
    inset = 0.05
    report["region_suggestion"] = {
        "mode": "numbers",
        "center": [round((lo[i] + hi[i]) / 2.0, 2) for i in range(3)],
        "size": [round(max(0.5, (hi[i] - lo[i]) - 2 * inset), 2) for i in range(3)],
        "rotation": [0.0, 0.0, 0.0],
        "inset": 0.0,
        "margin": 0.25,
        "note": ("fitted to the measured interior minus 5 cm; check that every camera "
                 "start is inside it once the 0.25 m margin is applied, and cut it back "
                 "to the room the shots are of"),
    }
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenes", action="append", default=[], required=True,
                        help="a .blend file or a folder to walk (repeatable)")
    parser.add_argument("--report", default="", help="write the JSON here as well")
    parser.add_argument("--grid", type=float, default=0.25,
                        help="anchor candidate grid step, in metres (default 0.25)")
    parser.add_argument("--non-recursive", action="store_true",
                        help="only look at the top level of a folder")
    args = parser.parse_args(argv if argv is not None else
                             (sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []))

    files = scene_files(args.scenes, recursive=not args.non_recursive)
    if not files:
        print("no .blend files found under: " + ", ".join(args.scenes))
        return 2
    payload = {"scenes": []}
    for path in files:
        print(f"--- inspecting {path}", file=sys.stderr)
        payload["scenes"].append(inspect_scene(path, grid_step=args.grid))
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    if args.report:
        with open(args.report, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text + "\n")
        print(f"report written to {args.report}", file=sys.stderr)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
