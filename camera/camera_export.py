"""Camera trajectory (TXT) and per-sequence JSON metadata export.

Artifact formats follow ``E:\\VSCode\\CameraCtrl\\movie_render.py`` so the same
downstream consumers (notably the ``CameraPoseVisualizer`` script) keep working:

TXT
    Header line::

        frame focal_length d1 d2 d3 d4 d5 r00 r01 r02 tx r10 r11 r12 ty r20 r21 r22 tz

    then one line per frame with 12 numbers: the first three rows of the
    **world-to-camera** matrix in the OpenCV convention (``+X`` right, ``+Y``
    down, ``+Z`` forward).  ``d1..d5`` are reserved distortion slots and stay 0,
    exactly as in the reference script, which skips the header when parsing.

JSON
    The reference writes a JSONL record per sequence with the keys
    ``level_name``, ``sequence_name``, ``video_id``, ``video_path``,
    ``frame_count``, ``camera_trajectory`` and ``text_prompt``.  Those keys are
    reproduced verbatim (as a ``.json`` document, per the project brief) and
    augmented with pipeline-specific fields such as ``has_character`` and
    ``sequence_id``.  Field names are ASCII-only and stable.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Sequence

from ..io.json_io import save_json_file
from ..io.path_utils import ensure_dir, normalize_path, to_forward_slashes
from ..utils.version import GENERATOR_VERSION
from .motion_templates import CameraSample, quaternion_to_matrix, vec_normalized
from .scene_context import CameraSnapshot

#: Reserved distortion slots kept for byte-compatibility with the reference.
_RESERVED_SLOTS = 5

TRAJECTORY_HEADER = (
    "frame focal_length d1 d2 d3 d4 d5 "
    "r00 r01 r02 tx r10 r11 r12 ty r20 r21 r22 tz"
)

#: Axis flip that converts Blender's camera matrix columns into the OpenCV
#: camera basis, in the same row layout ``movie_render.py`` emits.
#:
#: Let ``M`` be the camera-to-world matrix with columns ``(R, U, B)`` where
#: ``B`` is Blender's local ``+Z`` (``-B`` is the view direction).  The three
#: emitted rows are the OpenCV axes resolved in world space::
#:
#:   row 0 (OpenCV +X) =  R
#:   row 1 (OpenCV +Y) = -U          (Blender's +Y is up, OpenCV's is down)
#:   row 2 (OpenCV +Z) =  B          (keeps the reference implementation's sign)
#:
#: Together with ``-R^T c`` translations this reproduces the reference exactly,
#: and the defining invariant -- the camera's own world position maps to the
#: camera-space origin -- is asserted in the tests.
_OPENCV_FLIP = (1.0, -1.0, 1.0)


@dataclass
class TrajectoryRow:
    frame: int
    focal_length: float
    r00: float; r01: float; r02: float; tx: float
    r10: float; r11: float; r12: float; ty: float
    r20: float; r21: float; r22: float; tz: float

    def to_line(self, *, decimals: int = 8) -> str:
        values = [
            self.r00, self.r01, self.r02, self.tx,
            self.r10, self.r11, self.r12, self.ty,
            self.r20, self.r21, self.r22, self.tz,
        ]
        matrix = " ".join(f"{value:.{decimals}f}" for value in values)
        reserved = " ".join("0" for _ in range(_RESERVED_SLOTS))
        focal = f"{self.focal_length:.6f}".rstrip("0").rstrip(".") or "0"
        return f"{self.frame} {focal} {reserved} {matrix}"

    def to_dict(self) -> dict:
        return {
            "frame": self.frame,
            "focal_length": round(float(self.focal_length), 6),
            "world_to_camera": {
                "r00": round(self.r00, 10), "r01": round(self.r01, 10), "r02": round(self.r02, 10), "tx": round(self.tx, 10),
                "r10": round(self.r10, 10), "r11": round(self.r11, 10), "r12": round(self.r12, 10), "ty": round(self.ty, 10),
                "r20": round(self.r20, 10), "r21": round(self.r21, 10), "r22": round(self.r22, 10), "tz": round(self.tz, 10),
            },
        }


def camera_pose_to_world_matrix(position: Sequence[float], quaternion: Sequence[float]) -> "list[list[float]]":
    """4x4 camera-to-world matrix (Blender convention, column-major basis)."""
    rotation = quaternion_to_matrix(quaternion)
    return [
        [rotation[0][0], rotation[0][1], rotation[0][2], float(position[0])],
        [rotation[1][0], rotation[1][1], rotation[1][2], float(position[1])],
        [rotation[2][0], rotation[2][1], rotation[2][2], float(position[2])],
        [0.0, 0.0, 0.0, 1.0],
    ]


def world_to_camera_row(matrix: Sequence[Sequence[float]]) -> "tuple[float, ...]":
    """Blender camera-to-world 4x4 -> OpenCV world-to-camera 3x4 (row major).

    The returned rows are the OpenCV camera axes in world space followed by the
    corresponding translation terms of ``W2C = [R^T | -R^T c]``, i.e. exactly the
    layout ``movie_render.py`` writes::

        r00 r01 r02 tx   <- OpenCV +X (right)
        r10 r11 r12 ty   <- OpenCV +Y (down)
        r20 r21 r22 tz   <- OpenCV +Z (back; the view direction is -Z)

    Invariant (asserted in the tests): transforming the camera's own world
    position through this row yields ``(0, 0, 0)``.
    """
    # OpenCV axes expressed in world space.  Blender's camera matrix columns are
    # (right, up, back) with back = -view.
    right = vec_normalized([
        matrix[axis][0] * _OPENCV_FLIP[0] for axis in range(3)
    ])
    down = vec_normalized([
        matrix[axis][1] * _OPENCV_FLIP[1] for axis in range(3)
    ])
    forward = vec_normalized([
        matrix[axis][2] * _OPENCV_FLIP[2] for axis in range(3)
    ])
    translation = (float(matrix[0][3]), float(matrix[1][3]), float(matrix[2][3]))

    def dot(axis, vector):
        return axis[0] * vector[0] + axis[1] * vector[1] + axis[2] * vector[2]

    return (
        right[0], right[1], right[2], -dot(right, translation),
        down[0], down[1], down[2], -dot(down, translation),
        forward[0], forward[1], forward[2], -dot(forward, translation),
    )


def build_trajectory_rows(
    samples: Sequence[CameraSample],
    *,
    mode: str = "all_frames",
    step: int = 1,
    always_include: Sequence[int] = (),
) -> "list[TrajectoryRow]":
    """Convert sampled poses into exportable rows."""
    if not samples:
        return []
    step = max(1, int(step))
    wanted = set()
    if str(mode) == "sampled":
        for index, sample in enumerate(samples):
            if index % step == 0:
                wanted.add(sample.frame)
    else:
        wanted.update(sample.frame for sample in samples)
    wanted.update(int(frame) for frame in always_include)
    wanted.add(samples[0].frame)
    wanted.add(samples[-1].frame)

    rows = []
    for sample in samples:
        if sample.frame not in wanted:
            continue
        matrix = camera_pose_to_world_matrix(sample.position, sample.quaternion)
        values = world_to_camera_row(matrix)
        rows.append(TrajectoryRow(
            frame=int(sample.frame),
            focal_length=float(sample.focal),
            r00=values[0], r01=values[1], r02=values[2], tx=values[3],
            r10=values[4], r11=values[5], r12=values[6], ty=values[7],
            r20=values[8], r21=values[9], r22=values[10], tz=values[11],
        ))
    return rows


def write_trajectory_txt(path: str, rows: Sequence[TrajectoryRow], *, extra_header: Sequence[str] = ()) -> str:
    """Write the CameraPoseVisualizer-compatible trajectory file."""
    target = normalize_path(path)
    ensure_dir(os.path.dirname(target) or ".")
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        for line in extra_header:
            handle.write(f"# {line}\n")
        handle.write(TRAJECTORY_HEADER + "\n")
        for row in rows:
            handle.write(row.to_line() + "\n")
    return target


def sensor_crop_factor(camera: CameraSnapshot) -> float:
    """Extra FOV narrowing Blender applies in ``AUTO`` sensor fit.

    With ``sensor_fit = AUTO`` and a landscape image, Blender keeps the
    horizontal FOV implied by ``sensor_width`` and narrows the vertical FOV to
    match the pixel aspect.  ``world_to_camera_view`` honours that, so a naive
    ``sensor_height/sensor_width`` computation would disagree with the picture.
    """
    if str(camera.sensor_fit).upper() != "AUTO":
        return 1.0
    width, height = camera.effective_resolution
    if width >= height:
        return 1.0
    return width / float(height)


def camera_intrinsics(camera: CameraSnapshot, *, focal_length: float | None = None) -> dict:
    """Intrinsic description for the JSON sidecar (ASCII field names)."""
    width, height = camera.effective_resolution
    focal = float(focal_length if focal_length is not None else camera.lens)
    crop = sensor_crop_factor(camera)
    tan_half_x = (camera.sensor_width * 0.5) / max(1e-9, focal)
    tan_half_y = tan_half_x / (width / float(height)) if height else tan_half_x
    return {
        "focal_length_mm": round(focal, 6),
        "sensor_width_mm": round(float(camera.sensor_width), 6),
        "sensor_height_mm": round(float(camera.sensor_height), 6),
        "sensor_fit": str(camera.sensor_fit),
        "sensor_crop_factor": round(float(crop), 8),
        "image_width": int(width),
        "image_height": int(height),
        "resolution_percentage": int(camera.resolution_percentage),
        "fps": round(float(camera.fps), 6),
        "clip_start": round(float(camera.clip_start), 6),
        "clip_end": round(float(camera.clip_end), 6),
        "shift_x": round(float(camera.shift_x), 6),
        "shift_y": round(float(camera.shift_y), 6),
        "horizontal_fov_deg": round(2.0 * math.degrees(math.atan(tan_half_x)), 6),
        "vertical_fov_deg": round(2.0 * math.degrees(math.atan(tan_half_y)), 6),
    }


@dataclass
class SequenceMetadata:
    """Everything needed to write one sequence's JSON sidecar."""

    sequence_id: str
    scene_name: str
    motion_name: str
    camera_name: str
    source_blend: str
    frame_start: int
    frame_end: int
    fps: float
    video_path: str
    camera: CameraSnapshot
    camera_trajectory: "list[TrajectoryRow]" = field(default_factory=list)
    has_character: bool = False
    character_name: str = ""
    character_animation: str = ""
    character_status: str = ""
    sequence_dir: str = ""
    sequence_blend: str = ""
    validation: dict = field(default_factory=dict)
    motion: dict = field(default_factory=dict)
    search: dict = field(default_factory=dict)
    camera_original: dict = field(default_factory=dict)
    random_seed: int = 0
    generator_version: str = GENERATOR_VERSION
    extra: dict = field(default_factory=dict)
    render: dict = field(default_factory=dict)
    status: str = "generated"
    error: str = ""

    def to_dict(self) -> dict:
        width, height = self.camera.effective_resolution
        trajectory = [
            {
                "frame": row.frame,
                "fov": self._fov_for(row.focal_length),
                "focal_length": round(float(row.focal_length), 6),
                "matrix": [
                    [row.r00, row.r01, row.r02, row.tx],
                    [row.r10, row.r11, row.r12, row.ty],
                    [row.r20, row.r21, row.r22, row.tz],
                    [0.0, 0.0, 0.0, 1.0],
                ],
            }
            for row in self.camera_trajectory
        ]
        sample_step = max(1, int(round(
            (self.frame_end - self.frame_start + 1) / max(1, len(self.camera_trajectory))
        )))
        payload = {
            # -- keys mirroring E:\VSCode\CameraCtrl\movie_render.py ----------
            "level_name": self.scene_name,
            "sequence_name": self.sequence_id,
            "video_id": self.sequence_id,
            "video_path": to_forward_slashes(self.video_path),
            "frame_count": len(trajectory) or (self.frame_end - self.frame_start + 1),
            "camera_trajectory": trajectory,
            "text_prompt": "",
            # -- pipeline extensions -----------------------------------------
            "sequence_id": self.sequence_id,
            "scene_name": self.scene_name,
            "motion_name": self.motion_name,
            "camera_name": self.camera_name,
            "character_name": self.character_name,
            "character_animation": self.character_animation,
            "character_status": self.character_status,
            "has_character": bool(self.has_character),
            "source_blend": to_forward_slashes(self.source_blend),
            "generator_version": self.generator_version,
            "random_seed": int(self.random_seed),
            "status": self.status,
            "error": self.error,
            "frame_start": int(self.frame_start),
            "frame_end": int(self.frame_end),
            "fps": round(float(self.fps), 6),
            "resolution": [int(width), int(height)],
            "trajectory_export": {
                "mode": "sampled" if sample_step > 1 else "all_frames",
                "step": sample_step,
                "row_count": len(trajectory),
                "coordinate_system": "opencv_world_to_camera",
                "rotation_representation": "3x3 rotation matrix, column-major rows r00..r22",
                "units": "blender_world_units (metres by default)",
                "distortion_slots": "d1..d5 are reserved and always 0",
                "matches_reference": "E:\\VSCode\\CameraCtrl\\movie_render.py",
            },
            "camera_intrinsics": camera_intrinsics(self.camera),
            "camera_original": dict(self.camera_original),
            "validation": dict(self.validation),
            "motion": dict(self.motion),
            "search": dict(self.search),
            "render": dict(self.render),
        }
        payload.update(self.extra)
        return payload

    def _fov_for(self, focal_length: float) -> float:
        """Horizontal FOV in degrees at ``focal_length`` (reference parity)."""
        focal = max(1e-9, float(focal_length))
        return math.degrees(2.0 * math.atan((self.camera.sensor_width * 0.5) / focal))

    def write(self, path: str) -> str:
        return save_json_file(path, self.to_dict())


def trajectory_summary_text(rows: Sequence[TrajectoryRow], metadata: Sequence[str] = ()) -> str:
    """Human readable companion text (used by the ``--print-trajectory`` flag)."""
    lines = list(metadata)
    lines.append(TRAJECTORY_HEADER)
    lines.extend(row.to_line(decimals=6) for row in rows)
    return "\n".join(lines) + "\n"


def relative_video_name(sequence_id: str, extension: str = ".mp4") -> str:
    extension = extension if extension.startswith(".") else f".{extension}"
    return f"{sequence_id}{extension}"
