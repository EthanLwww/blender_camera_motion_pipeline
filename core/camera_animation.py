"""Store a sequence's camera animation as data, and put it back on a scene.

A generated sequence has two possible shapes:

* **blend** — ``sequence_<id>.blend`` is a complete copy of the scene with the
  animation baked in.  Self-contained: it renders anywhere, with no original file.
* **animation** — no scene copy at all.  The keyed camera animation travels in
  ``sequence_<id>.json`` and the renderer re-applies it to the *source* scene
  recorded in ``sequence_config.json``.  A sequence folder drops from hundreds of
  megabytes to a couple of hundred kilobytes, at the cost of needing that scene.

Both shapes must render the **same frames**: the payload therefore records the
values that were actually keyed (``location`` in parent space, ``rotation_quaternion``,
``scale``, and the camera data's ``lens``), not the world-space poses.  Re-deriving
them at render time would re-run the parent-space conversion and could drift;
replaying them cannot.

The payload also records which constraints were muted for the bake.  A keyed
rotation cannot survive an active ``TRACK_TO``/``COPY_ROTATION``, so the renderer
has to mute exactly the same ones or it would draw a different path than the
generator validated.
"""

from __future__ import annotations

from typing import Sequence

#: Bumped when the payload's meaning changes; the renderer refuses newer payloads.
ANIMATION_PAYLOAD_SCHEMA = 1

#: Key of the payload inside the sequence sidecar JSON.
PAYLOAD_KEY = "camera_animation"


def sample_to_dict(frame: int, location: Sequence[float], quaternion: Sequence[float],
                   scale: Sequence[float], lens: float) -> dict:
    """One keyframe of a camera animation, rounded for a readable JSON file."""
    return {
        "frame": int(frame),
        "location": [round(float(v), 6) for v in location],
        "quaternion": [round(float(v), 8) for v in quaternion],
        "scale": [round(float(v), 6) for v in scale],
        "lens": round(float(lens), 6),
    }


def build_payload(
    *,
    object_name: str,
    data_name: str,
    rotation_mode: str,
    interpolation: str,
    samples: "list[dict]",
    scene_name: str = "",
    sequence_id: str = "",
    parent_name: str = "",
    parent_type: str = "OBJECT",
    muted_constraints: Sequence[str] = (),
) -> dict:
    """Assemble the payload written beside a sequence (and replayed by the renderer)."""
    frames = [int(sample["frame"]) for sample in samples]
    return {
        "schema": ANIMATION_PAYLOAD_SCHEMA,
        "object_name": object_name,
        "data_name": data_name,
        "parent_name": parent_name,
        "parent_type": parent_type,
        "rotation_mode": rotation_mode,
        "interpolation": interpolation,
        "muted_constraints": sorted(str(name) for name in muted_constraints),
        "frame_start": min(frames) if frames else None,
        "frame_end": max(frames) if frames else None,
        "key_count": len(samples),
        "scene_name": scene_name,
        "sequence_id": sequence_id,
        "samples": samples,
    }


def payload_summary(payload: dict, *, filename: str = "") -> dict:
    """The small block that goes into ``sequence_config.json``.

    Discovery reads only ``sequence_config.json``, so the bulky per-frame samples
    stay in the sidecar and this says whether (and where) they are.
    """
    if not payload:
        return {"available": False}
    return {
        "available": True,
        "file": filename,
        "key": PAYLOAD_KEY,
        "schema": int(payload.get("schema") or 0),
        "object_name": payload.get("object_name", ""),
        "data_name": payload.get("data_name", ""),
        "parent_name": payload.get("parent_name", ""),
        "rotation_mode": payload.get("rotation_mode", "QUATERNION"),
        "interpolation": payload.get("interpolation", "BEZIER"),
        "muted_constraints": list(payload.get("muted_constraints") or []),
        "key_count": int(payload.get("key_count") or 0),
        "frame_start": payload.get("frame_start"),
        "frame_end": payload.get("frame_end"),
    }


def config_animation_block(config: dict) -> dict:
    """The payload block out of a ``sequence_config.json`` payload."""
    block = (config or {}).get("camera_animation") or {}
    return block if isinstance(block, dict) else {}


def find_camera(payload: dict, config: dict = None):
    """Locate the camera a payload belongs to, in the currently open scene."""
    import bpy

    config = config or {}
    wanted = [
        (config.get("sequence") or {}).get("camera_name") or "",
        payload.get("object_name") or "",
    ]
    for name in wanted:
        if not name:
            continue
        obj = bpy.data.objects.get(name)
        if obj is not None and obj.type == "CAMERA":
            return obj
    scene = bpy.context.scene
    if scene.camera is not None:
        return scene.camera
    for obj in scene.objects:
        if obj.type == "CAMERA":
            return obj
    return None


def apply_payload(payload: dict, config: dict = None) -> dict:
    """Re-key ``payload`` onto the camera of the currently open scene.

    Mirrors what the generator did: clear the artist's animation, mute the
    constraints that would override the keys, then key location/quaternion/scale on
    the object and lens on its data.  Returns a summary (never raises for a
    recoverable problem -- it reports instead).
    """
    import bpy

    from ..utils.animation import clear_animation, set_interpolation

    summary = {
        "applied": False,
        "object_name": "",
        "key_count": 0,
        "muted_constraints": [],
        "missing_constraints": [],
        "warnings": [],
    }
    if not payload:
        summary["warnings"].append("no camera animation payload")
        return summary
    schema = int(payload.get("schema") or 0)
    if schema > ANIMATION_PAYLOAD_SCHEMA:
        summary["warnings"].append(
            f"payload schema {schema} is newer than this build understands "
            f"({ANIMATION_PAYLOAD_SCHEMA})"
        )
        return summary
    samples = payload.get("samples") or []
    if not samples:
        summary["warnings"].append("payload contains no keyframes")
        return summary

    camera = find_camera(payload, config)
    if camera is None:
        summary["warnings"].append("no camera found in the source scene")
        return summary
    camera_data = camera.data
    scene = bpy.context.scene
    scene.camera = camera

    # The artist's animation would fight the keys we are about to write.
    clear_animation(camera)
    if camera_data is not None:
        clear_animation(camera_data)

    wanted = [str(name) for name in (payload.get("muted_constraints") or [])]
    for constraint in camera.constraints:
        if constraint.name in wanted and not getattr(constraint, "mute", False):
            constraint.mute = True
            summary["muted_constraints"].append(constraint.name)
    present = {constraint.name for constraint in camera.constraints}
    summary["missing_constraints"] = [name for name in wanted if name not in present]
    if summary["missing_constraints"]:
        summary["warnings"].append(
            "constraint(s) muted during generation are not on this camera any more: "
            + ", ".join(summary["missing_constraints"])
        )

    rotation_mode = str(payload.get("rotation_mode") or "QUATERNION")
    camera.rotation_mode = rotation_mode
    written = 0
    for sample in samples:
        frame = int(sample["frame"])
        location = sample.get("location") or (0.0, 0.0, 0.0)
        quaternion = sample.get("quaternion") or (1.0, 0.0, 0.0, 0.0)
        scale = sample.get("scale") or (1.0, 1.0, 1.0)
        camera.location = (float(location[0]), float(location[1]), float(location[2]))
        if rotation_mode == "QUATERNION":
            camera.rotation_quaternion = tuple(float(v) for v in quaternion)
        else:
            camera.rotation_quaternion = tuple(float(v) for v in quaternion)
            camera.rotation_mode = "QUATERNION"
        camera.scale = (float(scale[0]), float(scale[1]), float(scale[2]))
        camera.keyframe_insert(data_path="location", frame=frame, group="motion_pipeline")
        camera.keyframe_insert(data_path="rotation_quaternion", frame=frame, group="motion_pipeline")
        camera.keyframe_insert(data_path="scale", frame=frame, group="motion_pipeline")
        if camera_data is not None and sample.get("lens") is not None:
            camera_data.lens = max(1.0, float(sample["lens"]))
            camera_data.keyframe_insert(data_path="lens", frame=frame, group="motion_pipeline")
        written += 1

    interpolation = str(payload.get("interpolation") or "BEZIER")
    for owner in (camera, camera_data):
        animation_data = getattr(owner, "animation_data", None) if owner is not None else None
        action = getattr(animation_data, "action", None) if animation_data is not None else None
        if action is not None:
            set_interpolation(action, interpolation)

    frame_start = payload.get("frame_start")
    frame_end = payload.get("frame_end")
    frames_cfg = (config or {}).get("frames") or {}
    if frames_cfg.get("frame_start") is not None:
        frame_start = frames_cfg["frame_start"]
    if frames_cfg.get("frame_end") is not None:
        frame_end = frames_cfg["frame_end"]
    if frame_start is not None:
        scene.frame_start = int(frame_start)
    if frame_end is not None:
        scene.frame_end = int(frame_end)
    fps = frames_cfg.get("fps")
    if fps:
        scene.render.fps = int(round(float(fps))) or scene.render.fps
        scene.render.fps_base = 1.0
    if frame_start is not None:
        scene.frame_set(int(frame_start))
    bpy.context.view_layer.update()

    summary["applied"] = True
    summary["object_name"] = camera.name
    summary["key_count"] = written
    return summary
