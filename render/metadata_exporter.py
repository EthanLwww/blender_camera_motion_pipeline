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
    """Read the *evaluated* camera pose per frame and build trajectory rows.

    Poses are taken from the dependency graph rather than from ``object.location``
    so that drivers, constraints and parented cameras are all honoured -- and so
    the exported trajectory is by construction the same camera the renderer used.
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

    rows: "list[TrajectoryRow]" = []
    lens_fallback = float(getattr(camera_object.data, "lens", 35.0))
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
        "# coordinate_system=opencv_world_to_camera (row0=+X right, row1=+Y down, row2=+Z)",
        "# rotation_representation=3x3 rotation matrix, rows r00..r22, column-vector convention",
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
            "coordinate_system": "opencv_world_to_camera",
            "rotation_representation": "3x3 rotation matrix rows r00..r22",
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
