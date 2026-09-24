"""Sidecar metadata writers shared by the generator and the renderer.

The renderer must produce a JSON sidecar and a camera-trajectory TXT *per
rendered video*, so it reuses the generator's exporters and only overrides the
fields that describe the finished video (path, frame count, render settings).
"""

from __future__ import annotations

import math
import os

from ..camera.camera_export import (
    TRAJECTORY_HEADER,
    TrajectoryRow,
    world_to_camera_row,
)
from ..io.json_io import save_json_file
from ..io.path_utils import ensure_dir, normalize_path, relative_to, to_forward_slashes
from ..utils.version import GENERATOR_VERSION, generator_stamp


def _slot_fcurves(action, slot=None):
    """The F-curves of one action *slot* (Blender 4.4+ layered actions).

    One action can drive several IDs -- the generator ends up with a single
    ``CameraAction`` holding a *Camera* slot for the object and another for the
    camera data (``lens``) -- so the curves have to be taken per slot, or the object's
    set would look like it animates ``lens``.  Falls back to every curve when this
    Blender has no layers (the historical flat ``Action.fcurves`` layout).
    """
    from ..utils.animation import action_fcurves

    layers = getattr(action, "layers", None)
    if not layers:
        return action_fcurves(action)
    curves = []
    for layer in layers or []:
        for strip in getattr(layer, "strips", []) or []:
            bag = None
            lookup = getattr(strip, "channelbag", None)
            if slot is not None and callable(lookup):
                try:
                    bag = lookup(slot)
                except Exception:
                    bag = None
            if bag is not None:
                curves.extend(list(getattr(bag, "fcurves", []) or []))
                continue
            for candidate in getattr(strip, "channelbags", []) or []:
                if slot is None or getattr(candidate, "slot", None) is slot:
                    curves.extend(list(getattr(candidate, "fcurves", []) or []))
    return curves


def _fcurve_index(action, slot=None):
    """``{(data_path, array_index): fcurve}`` for *action*, or None if it is unusable.

    ``Action.fcurves`` no longer exists in Blender 5 (layered actions), so the walk
    goes through the slot's channelbag.  Duplicate keys or an unreadable action return
    None so the caller falls back to the dependency graph.
    """
    curves = {}
    try:
        flat = _slot_fcurves(action, slot)
    except Exception:
        return None
    if not flat:
        return None
    for curve in flat:
        try:
            key = (str(curve.data_path), int(curve.array_index))
        except Exception:
            return None
        if key in curves:
            return None
        curves[key] = curve
    return curves


def _slot_of(animation_data):
    """The action slot an ID uses, or None when this Blender has no slots."""
    return getattr(animation_data, "action_slot", None)


def _curve_value(curves, path, index, default, evaluate):
    curve = curves.get((path, index))
    if curve is None:
        return float(default)
    try:
        value = float(evaluate(curve))
    except Exception:
        return float(default)
    return value if math.isfinite(value) else float(default)


def _analytic_plan(camera_object):
    """``(plan, reason)``: how to evaluate this camera from F-curves, or why not.

    The plan is a dict with ``curves``/``data_curves`` (F-curve maps), ``mode`` and
    ``rotation_path``; ``reason`` is "" when the plan is usable.
    """
    obj = camera_object
    if obj is None:
        return None, "no camera"
    if getattr(obj, "parent", None) is not None:
        return None, "camera is parented"
    if len(getattr(obj, "constraints", ()) or ()):
        return None, "camera has constraints"
    anim = getattr(obj, "animation_data", None)
    if anim is None or anim.action is None:
        return None, "camera has no action"
    if len(getattr(anim, "drivers", ()) or ()):
        return None, "camera has drivers"
    if len(getattr(anim, "nla_tracks", ()) or ()):
        return None, "camera has NLA tracks"
    if str(getattr(anim, "action_blend_type", "REPLACE") or "REPLACE") != "REPLACE":
        return None, "action blend type is %s" % anim.action_blend_type

    mode = str(getattr(obj, "rotation_mode", "XYZ") or "XYZ").upper()
    if mode == "AXIS_ANGLE":
        return None, "rotation mode AXIS_ANGLE"
    rotation_path = {"QUATERNION": "rotation_quaternion"}.get(
        mode, "rotation_%s" % mode.lower())

    curves = _fcurve_index(anim.action, _slot_of(anim))
    if curves is None:
        return None, "camera action is not a plain F-curve action"
    allowed = {"location", "scale", rotation_path}
    unexpected = sorted({path for path, _index in curves if path not in allowed})
    if unexpected:
        return None, "camera action animates %s" % ", ".join(unexpected)

    data = getattr(obj, "data", None)
    data_curves = {}
    lens_animated = False
    if data is not None and getattr(data, "animation_data", None) is not None:
        data_anim = data.animation_data
        if len(getattr(data_anim, "drivers", ()) or ()):
            return None, "camera data has drivers"
        if len(getattr(data_anim, "nla_tracks", ()) or ()):
            return None, "camera data has NLA tracks"
        if data_anim.action is not None:
            data_curves = _fcurve_index(data_anim.action, _slot_of(data_anim))
            if data_curves is None:
                return None, "camera data action is not a plain F-curve action"
            odd = sorted({path for path, _index in data_curves if path != "lens"})
            if odd:
                return None, "camera data animates %s" % ", ".join(odd)
            lens_animated = any(path == "lens" for path, _index in data_curves)
    if not curves and not data_curves:
        return None, "camera has no usable curves"

    return {
        "curves": curves,
        "data_curves": data_curves,
        "mode": mode,
        "rotation_path": rotation_path,
        "lens_animated": lens_animated,
        "data": data,
    }, ""


def analytic_camera_reason(camera_object) -> str:
    """``""`` when the camera can be evaluated from F-curves, else why it cannot.

    Kept separate from :func:`analytic_camera_poses` so a probe (or a log line) can
    explain *why* a scene fell back to the dependency graph.
    """
    return _analytic_plan(camera_object)[1]


def analytic_camera_poses(camera_object, frames) -> "list[tuple[object, float]] | None":
    """Camera ``(matrix_world, lens)`` per frame, evaluated from F-curves only.

    The trajectory export needs the camera pose of every frame, and the honest way to
    get it -- ``scene.frame_set()`` plus a depsgraph update -- re-evaluates the whole
    scene.  On a scene with geometry-node scattering that is seconds *per frame*, so a
    216-frame sequence spent minutes producing a trajectory before the first frame was
    even rendered.

    When the camera is a plain F-curve camera (no parent, no constraints, no drivers,
    no NLA, only ``location`` / ``rotation_*`` / ``scale`` animated, plus an optional
    animated ``lens``), the pose can be evaluated straight from the curves, exactly
    and without touching the dependency graph.

    Returns None whenever the camera does not fit that shape, so the caller can fall
    back to the dependency graph instead of guessing.
    """
    import bpy  # noqa: F401  (kept local: the module is imported outside Blender too)

    plan, reason = _analytic_plan(camera_object)
    if plan is None:
        return None
    base = camera_object
    curves = plan["curves"]
    data_curves = plan["data_curves"]
    mode = plan["mode"]
    rotation_path = plan["rotation_path"]
    data = plan["data"]

    from mathutils import Euler, Matrix, Quaternion, Vector

    poses = []
    for frame in frames:
        def value(path, index, default, _frame=frame):
            return _curve_value(curves, path, index, default,
                                lambda curve: curve.evaluate(_frame))

        location = Vector([value("location", i, base.location[i]) for i in range(3)])
        scale = Vector([value("scale", i, base.scale[i]) for i in range(3)])
        if mode == "QUATERNION":
            components = [value("rotation_quaternion", i, base.rotation_quaternion[i])
                          for i in range(4)]
            quaternion = Quaternion(components)
        else:
            order = mode if len(mode) == 3 else "XYZ"
            angles = [value(rotation_path, i, base.rotation_euler[i]) for i in range(3)]
            quaternion = Euler(angles, order).to_quaternion()
        try:
            matrix = Matrix.LocRotScale(location, quaternion, scale)
        except Exception:
            return None
        lens = float(getattr(data, "lens", 35.0) or 35.0)
        if plan["lens_animated"]:
            lens = _curve_value(data_curves, "lens", 0, lens,
                                lambda curve: curve.evaluate(frame))
        poses.append((matrix, lens))
    return poses


def verify_camera_poses(
    camera_object,
    scene,
    depsgraph,
    frames,
    poses,
    *,
    tolerance: float = 1e-4,
    samples: int = 5,
) -> bool:
    """True when the analytic poses match the dependency graph on sampled frames.

    Cheap insurance: a handful of depsgraph evaluations instead of one per frame.
    Any mismatch (a property we did not model) sends the caller back to the slow,
    always-correct path.
    """
    if scene is None or depsgraph is None or not frames:
        return False
    count = max(1, min(int(samples), len(frames)))
    if count == 1:
        picks = [0]
    else:
        picks = sorted({round(i * (len(frames) - 1) / (count - 1)) for i in range(count)})
    for index in picks:
        frame = frames[index]
        try:
            scene.frame_set(int(frame))
            depsgraph.update()
            reference = camera_object.evaluated_get(depsgraph).matrix_world
        except Exception:
            return False
        candidate = poses[index][0]
        for row in range(4):
            for column in range(4):
                if abs(float(reference[row][column]) - float(candidate[row][column])) > tolerance:
                    return False
    return True


def sample_camera_trajectory(
    camera_object,
    *,
    frame_start: int,
    frame_end: int,
    step: int = 1,
    scene=None,
    depsgraph=None,
    mode: str = "all_frames",
) -> "list[TrajectoryRow]":
    """Read the camera pose per frame and build trajectory rows.

    Poses normally come from the dependency graph rather than from
    ``object.location`` so that drivers, constraints and parented cameras are all
    honoured -- and so the exported trajectory is by construction the same camera
    the renderer used.  ``scene.frame_set()`` however re-evaluates the *whole*
    scene, which on a heavy scene costs seconds per frame; when the camera is a
    plain F-curve camera :func:`analytic_camera_poses` reproduces the same poses
    without touching the graph, and :func:`verify_camera_poses` checks a few frames
    against the graph before that shortcut is trusted.
    """
    import bpy

    scene = scene or bpy.context.scene
    depsgraph = depsgraph or bpy.context.evaluated_depsgraph_get()
    step = max(1, int(step))
    frames = list(range(int(frame_start), int(frame_end) + 1))
    if str(mode) == "sampled" and step > 1:
        wanted = set(frames[::step])
        wanted.add(frames[0])
        wanted.add(frames[-1])
        frames = [frame for frame in frames if frame in wanted]

    lens_fallback = float(getattr(camera_object.data, "lens", 35.0))

    poses = analytic_camera_poses(camera_object, frames)
    if poses is not None and not verify_camera_poses(camera_object, scene, depsgraph,
                                                     frames, poses):
        poses = None
    if poses is not None:
        rows = []
        for frame, (matrix, lens) in zip(frames, poses):
            values = world_to_camera_row(matrix)
            if not math.isfinite(lens) or lens <= 0:
                lens = lens_fallback
            rows.append(TrajectoryRow(
                frame=int(frame),
                focal_length=float(lens),
                r00=values[0], r01=values[1], r02=values[2], tx=values[3],
                r10=values[4], r11=values[5], r12=values[6], ty=values[7],
                r20=values[8], r21=values[9], r22=values[10], tz=values[11],
            ))
        return rows

    rows = []
    for frame in frames:
        scene.frame_set(frame)
        depsgraph.update()
        evaluated = camera_object.evaluated_get(depsgraph)
        matrix = [[float(v) for v in row] for row in evaluated.matrix_world]
        values = world_to_camera_row(matrix)
        # Local (constraint-aware) lens if it is animated, else the base value.
        data = getattr(evaluated, "data", None) or camera_object.data
        try:
            lens = float(data.lens)
        except Exception:
            lens = lens_fallback
        if not math.isfinite(lens) or lens <= 0:
            lens = lens_fallback
        rows.append(TrajectoryRow(
            frame=int(frame),
            focal_length=lens,
            r00=values[0], r01=values[1], r02=values[2], tx=values[3],
            r10=values[4], r11=values[5], r12=values[6], ty=values[7],
            r20=values[8], r21=values[9], r22=values[10], tz=values[11],
        ))
    return rows


def write_camera_trajectory(
    path: str,
    rows,
    *,
    sequence_id: str = "",
    scene_name: str = "",
    motion_name: str = "",
    camera_name: str = "",
    frame_start: int | None = None,
    frame_end: int | None = None,
    fps: float | None = None,
    header_notes=(),
) -> str:
    """Write the ``*_camera.txt`` trajectory file.

    Same column layout as ``movie_render.py`` (frame, focal length, five reserved
    distortion slots, then the 12 numbers of the world-to-camera matrix) with a
    leading ``#`` comment block that documents the coordinate system, rotation
    representation and units.
    """
    target = normalize_path(path)
    ensure_dir(os.path.dirname(target) or ".")
    lines = [
        f"# sequence_id={sequence_id}",
        f"# scene={scene_name} motion={motion_name} camera={camera_name}",
        f"# frames={frame_start}..{frame_end} fps={fps:g}" if fps else
        f"# frames={frame_start}..{frame_end}",
        "# coordinate_system=blender_world_to_camera (row0=+X right, row1=+Y up, row2=+Z back)",
        "# rotation_representation=3x3 rotation matrix, rows r00..r22, column-vector convention",
        "# view_axis=the camera looks down its local -Z, i.e. -(r20 r21 r22)",
        "# inverse=inv([R|t]) is the camera-to-world matrix (Blender matrix_world)",
        "# opencv_equivalent=flip rows 1 and 2 of R and t for +Y down / +Z forward",
        "# translation=tx ty tz from W2C = [R^T | -R^T * camera_position]",
        "# units=blender_world_units (metres by default); distortion d1..d5 reserved, always 0",
        f"# generator=blender_motion_pipeline {GENERATOR_VERSION}",
    ]
    lines.extend(f"# {note}" for note in header_notes)
    lines.append(TRAJECTORY_HEADER)
    for row in rows:
        lines.append(row.to_line())
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    return target


def build_render_metadata(
    *,
    sequence_id: str,
    scene_name: str,
    motion_name: str,
    camera_name: str,
    source_blend: str,
    sequence_dir: str,
    video_path: str,
    rows,
    frame_start: int,
    frame_end: int,
    fps: float,
    resolution: "tuple[int, int]",
    engine: str,
    samples: int | None = None,
    has_character: bool = False,
    character_name: str = "",
    character_animation: str = "",
    status: str = "rendered",
    error: str = "",
    trajectory_mode: str = "all_frames",
    trajectory_step: int = 1,
    sequence_config: "dict | None" = None,
    extra: "dict | None" = None,
    sensor_width_mm: float = 36.0,
    elapsed_seconds: float | None = None,
) -> dict:
    """Assemble the per-video JSON document.

    The first block of keys mirrors ``E:\\VSCode\\CameraCtrl\\movie_render.py``
    exactly (``level_name``, ``sequence_name``, ``video_id``, ``video_path``,
    ``frame_count``, ``camera_trajectory``, ``text_prompt``) so existing
    downstream readers keep working; the second block adds the video-render
    details.
    """
    video_path = to_forward_slashes(video_path)
    trajectory = [
        {
            "frame": row.frame,
            "fov": math.degrees(2.0 * math.atan((sensor_width_mm * 0.5) / max(1e-9, row.focal_length))),
            "focal_length": round(float(row.focal_length), 6),
            "matrix": [
                [row.r00, row.r01, row.r02, row.tx],
                [row.r10, row.r11, row.r12, row.ty],
                [row.r20, row.r21, row.r22, row.tz],
                [0.0, 0.0, 0.0, 1.0],
            ],
        }
        for row in rows
    ]
    payload = {
        # -- reference-compatible keys ------------------------------------
        "level_name": scene_name,
        "sequence_name": sequence_id,
        "video_id": sequence_id,
        "video_path": video_path,
        "frame_count": len(trajectory),
        "camera_trajectory": trajectory,
        "text_prompt": "",
        # -- pipeline / render extensions ---------------------------------
        "sequence_id": sequence_id,
        "scene_name": scene_name,
        "motion_name": motion_name,
        "camera_name": camera_name,
        "has_character": bool(has_character),
        "character_name": character_name,
        "character_animation": character_animation,
        "source_blend": to_forward_slashes(source_blend),
        "sequence_dir": to_forward_slashes(sequence_dir),
        "video_file": to_forward_slashes(os.path.basename(video_path)),
        "relative_sequence_dir": relative_to(sequence_dir, os.path.dirname(os.path.dirname(os.path.dirname(sequence_dir)))),
        "status": status,
        "error": error,
        "generator_version": GENERATOR_VERSION,
        "frames": {
            "frame_start": int(frame_start),
            "frame_end": int(frame_end),
            "frame_count": int(frame_end) - int(frame_start) + 1,
            "exported_frame_count": len(trajectory),
            "fps": round(float(fps), 6),
        },
        "render": {
            "engine": engine,
            "samples": samples,
            "resolution": [int(resolution[0]), int(resolution[1])],
            "fps": round(float(fps), 6),
            "duration_seconds": (
                (int(frame_end) - int(frame_start) + 1) / float(fps) if fps else None
            ),
            "elapsed_seconds": round(float(elapsed_seconds), 4) if elapsed_seconds is not None else None,
        },
        "trajectory_export": {
            "mode": str(trajectory_mode),
            "step": int(trajectory_step),
            "row_count": len(trajectory),
            "coordinate_system": "blender_world_to_camera",
            "rotation_representation": "3x3 rotation matrix rows r00..r22 (camera axes: +X right, +Y up, +Z back)",
            "view_axis": "the camera looks down local -Z, i.e. -(r20 r21 r22)",
            "inverse": "inv([R|t]) is the camera-to-world matrix (Blender matrix_world)",
            "opencv_equivalent": "flip rows 1 and 2 of the rotation and the translation for +Y down / +Z forward",
            "units": "blender_world_units (metres by default)",
            "distortion_slots": "d1..d5 are reserved and always 0",
            "matches_reference": "E:\\VSCode\\CameraCtrl\\movie_render.py",
        },
    }
    if sequence_config:
        payload["source_sequence_config"] = {
            "generator_version": sequence_config.get("generator_version", ""),
            "created_utc": sequence_config.get("created_utc", ""),
            "motion_template": (sequence_config.get("motion") or {}).get("template_name", ""),
            "validation": sequence_config.get("validation", {}),
        }
        # The shot report travels with the video: one entry per segment, naming the
        # atomic moves that were running and how fast.
        plan = sequence_config.get("motion_plan")
        if isinstance(plan, dict):
            payload["motion_plan"] = plan
            payload["shot_report"] = plan.get("shot_report")
    payload.update(generator_stamp())
    if extra:
        payload.update(extra)
    return payload


def write_render_metadata(path: str, payload: dict) -> str:
    return save_json_file(path, payload)


def video_extension(payload_format: str) -> str:
    """Blender's FFmpeg ``format`` identifier -> file extension."""
    mapping = {
        "MPEG4": ".mp4",
        "MKV": ".mkv",
        "WEBM": ".webm",
        "AVI": ".avi",
        "QUICKTIME": ".mov",
    }
    return mapping.get(str(payload_format).upper(), ".mp4")
