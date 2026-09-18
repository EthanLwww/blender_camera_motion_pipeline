"""Camera validation: clipping, occlusion, framing, jumps and illegal values.

The validator works on *sampled* camera poses produced by
:class:`~blender_motion_pipeline.camera.motion_templates.MotionTemplateGenerator`
so that a candidate camera position can be evaluated **before** any keyframe is
written into the scene.  That is what makes the spherical auto-search cheap:
hundreds of candidates are scored in memory, and only the winner is baked.

Checks implemented (mirroring the requirement list):

1. the camera body does not intersect scene geometry;
2. the camera does not travel through geometry during the move;
3. the camera is not enclosed / fully obstructed;
4. near/far clip planes stay inside a sane range and bracket the scene;
5. character visibility at the start frame, the end frame and across the move;
6. no abnormal position/rotation jumps;
7. no non-finite or degenerate values;
8. per-frame sampling at a configurable step plus explicit extra frames.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ..config.models import ValidationSection
from .motion_templates import (
    CameraSample,
    Quat,
    Vec3,
    quat_angle_between,
    quat_to_euler_xyz,
    vec_length,
    vec_normalized,
    vec_sub,
)
from .scene_context import (
    CharacterBox,
    RayCaster,
    SceneContext,
    fibonacci_directions,
)

#: Reasons a sequence can be rejected.  Kept as a closed set so downstream
#: tooling can group failures without parsing free text.
REASON_CLIPPING = "camera_clipping"
REASON_INSIDE_GEOMETRY = "camera_inside_geometry"
REASON_OBSTRUCTION = "camera_obstructed"
REASON_CHARACTER_INVISIBLE = "character_invisible"
REASON_CHARACTER_UNFRAMED = "character_unframed"
REASON_CHARACTER_OVERLAP = "character_overlap"
REASON_POSITION_JUMP = "position_jump"
REASON_ROTATION_JUMP = "rotation_jump"
REASON_ILLEGAL_VALUE = "illegal_value"
REASON_CLIP_RANGE = "clip_range_invalid"


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------
@dataclass
class FrameCheck:
    """Per-frame measurement.  ``None`` means "not evaluated"."""

    frame: int
    position: Vec3
    rotation_quaternion: Quat
    focal: float
    inside_geometry: bool = False
    clearance: float | None = None
    obstructed: bool = False
    obstruction_distance: float | None = None
    obstruction_limit: float | None = None
    character_visible_ratio: float | None = None
    character_max_occluded: bool = False
    character_on_screen: bool | None = None
    character_overlap: bool = False
    position_jump: float = 0.0
    rotation_jump_deg: float = 0.0
    #: Frame gap the jump was measured over, plus the raw (un-normalised,
    #: whole-span) deltas that the gap-scaled limits are compared against.
    metrics_span: int = 1
    position_delta: float = 0.0
    rotation_delta_deg: float = 0.0
    illegal_values: "list[str]" = field(default_factory=list)

    def issues(self, config: ValidationSection) -> "list[str]":
        """Every applicable failure for this frame, not just the first.

        Reporting the full set matters because the search's scoring and the
        "why did this fail" log both read these counters; returning early on
        "inside geometry" would hide the fact that the frame was also occluded.
        """
        problems: "list[str]" = []
        if self.illegal_values:
            return [REASON_ILLEGAL_VALUE]
        if self.inside_geometry:
            problems.append(REASON_INSIDE_GEOMETRY)
        elif self.clearance is not None and self.clearance < config.clearance:
            problems.append(REASON_CLIPPING)
        if self.obstructed:
            problems.append(REASON_OBSTRUCTION)
        if self.character_overlap and config.check_character_overlap:
            problems.append(REASON_CHARACTER_OVERLAP)
        if config.check_character_visibility and self.character_visible_ratio is not None:
            if self.character_visible_ratio < config.min_character_visible_ratio:
                problems.append(REASON_CHARACTER_INVISIBLE)
            if self.character_on_screen is False:
                problems.append(REASON_CHARACTER_UNFRAMED)
        if self.position_delta > config.max_position_jump * _jump_scale(self.metrics_span, config.jump_gap_scale):
            problems.append(REASON_POSITION_JUMP)
        if self.rotation_delta_deg > config.max_rotation_jump_deg * _jump_scale(self.metrics_span, config.jump_gap_scale):
            problems.append(REASON_ROTATION_JUMP)
        return problems

    def to_dict(self, *, config: ValidationSection | None = None) -> dict:
        payload = {
            "frame": self.frame,
            "location": [round(float(v), 6) for v in self.position],
            "rotation_quaternion": [round(float(v), 8) for v in self.rotation_quaternion],
            "rotation_euler_deg": [round(math.degrees(v), 4) for v in quat_to_euler_xyz(self.rotation_quaternion)],
            "focal_length": round(float(self.focal), 6),
            "inside_geometry": bool(self.inside_geometry),
            "clearance": None if self.clearance is None else round(float(self.clearance), 6),
            "obstructed": bool(self.obstructed),
            "obstruction_distance": None if self.obstruction_distance is None else round(float(self.obstruction_distance), 6),
            "obstruction_limit": None if self.obstruction_limit is None else round(float(self.obstruction_limit), 6),
            "character_visible_ratio": None if self.character_visible_ratio is None else round(float(self.character_visible_ratio), 6),
            "character_on_screen": self.character_on_screen,
            "character_overlap": bool(self.character_overlap),
            "position_jump": round(float(self.position_jump), 6),
            "position_jump_span_frames": int(self.metrics_span),
            "position_delta": round(float(self.position_delta), 6),
            "rotation_jump_deg": round(float(self.rotation_jump_deg), 4),
            "rotation_delta_deg": round(float(self.rotation_delta_deg), 4),
            "illegal_values": list(self.illegal_values),
        }
        if config is not None:
            payload["issues"] = self.issues(config)
        return payload


@dataclass
class ValidationReport:
    """Aggregated verdict for one candidate camera + motion combination."""

    passed: bool = True
    score: float = 0.0
    sampled_frames: "list[int]" = field(default_factory=list)
    frames: "list[FrameCheck]" = field(default_factory=list)
    reason_counts: dict = field(default_factory=dict)
    messages: "list[str]" = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    char_bbox: "CharacterBox | None" = None
    skipped_checks: "list[str]" = field(default_factory=list)
    offset_from_base: float = 0.0
    rotation_delta_deg: float = 0.0
    focal_delta_ratio: float = 0.0

    @property
    def failures(self) -> "list[str]":
        return sorted(self.reason_counts)

    def add_problem(self, reason: str, count: int = 1) -> None:
        self.reason_counts[reason] = self.reason_counts.get(reason, 0) + int(count)

    def first_failing_frame(self) -> "FrameCheck | None":
        for frame in self.frames:
            if frame.issues(ValidationSection()) or frame.illegal_values:
                return frame
        return None

    def to_dict(self, *, config: ValidationSection | None = None, include_frames: bool = True) -> dict:
        payload = {
            "passed": bool(self.passed),
            "score": round(float(self.score), 6),
            "reason_counts": {k: int(v) for k, v in sorted(self.reason_counts.items())},
            "reasons": self.failures,
            "messages": list(self.messages),
            "sampled_frames": list(self.sampled_frames),
            "frame_sample_count": len(self.frames),
            "metrics": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in self.metrics.items()},
            "candidate_offset_from_base": round(float(self.offset_from_base), 6),
            "candidate_rotation_delta_deg": round(float(self.rotation_delta_deg), 4),
            "candidate_focal_delta_ratio": round(float(self.focal_delta_ratio), 6),
            "skipped_checks": list(self.skipped_checks),
        }
        if include_frames:
            payload["frames"] = [
                frame.to_dict(config=config) if config is not None else frame.to_dict()
                for frame in self.frames
            ]
        return payload

    def summary_line(self) -> str:
        if self.passed:
            return (
                f"PASS score={self.score:.4f} frames={len(self.frames)} "
                f"offset={self.offset_from_base:.3f}m rotΔ={self.rotation_delta_deg:.2f}°"
            )
        detail = ", ".join(f"{reason}x{count}" for reason, count in sorted(self.reason_counts.items()))
        return f"FAIL score={self.score:.4f} frames={len(self.frames)} issues={detail}"


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------
def _effective_sensor(camera) -> "tuple[float, float]":
    """Return ``(sensor_x_mm, sensor_y_mm)`` honouring ``sensor_fit``."""
    width = float(getattr(camera, "sensor_width", 36.0)) or 36.0
    height = float(getattr(camera, "sensor_height", 24.0)) or 24.0
    fit = str(getattr(camera, "sensor_fit", "AUTO")).upper()
    res_x, res_y = camera.effective_resolution
    if fit == "VERTICAL":
        scale = height / width
        return (width * scale, height)
    if fit == "AUTO" and res_y > res_x:
        return (width, width * (res_y / float(res_x)))
    return (width, height)


def camera_tangents(camera) -> "tuple[float, float]":
    """``(tan(hfov/2), tan(vfov/2))`` for a :class:`CameraSnapshot`."""
    sensor_x, _sensor_y = _effective_sensor(camera)
    res_x, res_y = camera.effective_resolution
    focal = max(1e-6, float(camera.lens))
    aspect = (res_x / float(res_y)) if res_y else 1.0
    tan_half_x = (sensor_x * 0.5) / focal
    return (tan_half_x, tan_half_x / aspect if aspect else tan_half_x)


def frustum_contains(camera, position: Sequence[float], direction: Sequence[float], point: Sequence[float]) -> bool:
    """True when ``point`` projects inside the camera frustum (angle test)."""
    tan_x, tan_y = camera_tangents(camera)
    delta = vec_sub(point, position)
    forward = vec_normalized(direction)
    depth = delta[0] * forward[0] + delta[1] * forward[1] + delta[2] * forward[2]
    if depth <= 1e-6:
        return False
    # Build an orthonormal basis around forward to measure lateral extent.
    up_hint = (0.0, 0.0, 1.0)
    if abs(forward[2]) > 0.999:
        up_hint = (0.0, 1.0, 0.0)
    right = vec_normalized((
        forward[1] * up_hint[2] - forward[2] * up_hint[1],
        forward[2] * up_hint[0] - forward[0] * up_hint[2],
        forward[0] * up_hint[1] - forward[1] * up_hint[0],
    ))
    up = vec_normalized((
        right[1] * forward[2] - right[2] * forward[1],
        right[2] * forward[0] - right[0] * forward[2],
        right[0] * forward[1] - right[1] * forward[0],
    ))
    lateral = abs(delta[0] * right[0] + delta[1] * right[1] + delta[2] * right[2])
    vertical = abs(delta[0] * up[0] + delta[1] * up[1] + delta[2] * up[2])
    return lateral <= depth * tan_x and vertical <= depth * tan_y


def character_overlaps_meshes(
    character: CharacterBox,
    ray_caster: RayCaster | None,
    *,
    probe_count: int = 27,
    shrink: float = 0.9,
    exclude: "Iterable[str] | None" = None,
) -> bool:
    """True when a large share of the character's probes sit inside geometry.

    A loose AABB over a humanoid already overlaps the floor, so a single hit is
    not evidence of a problem; we require most probes to be *enclosed* -- i.e.
    geometry within 0.25 m in every direction.  The character's own meshes are
    excluded so the test only reports overlap with the *scene*.
    """
    if ray_caster is None:
        return False
    skip = set(exclude) if exclude else None
    center = character.center()
    probes = [
        tuple(center[i] + (p[i] - center[i]) * shrink for i in range(3))
        for p in character.probe_points(probe_count)
    ]
    directions = fibonacci_directions(8)
    inside = 0
    for point in probes:
        enclosed = True
        for direction in directions:
            hit, distance = ray_caster.cast(point, direction, exclude=skip)
            if not hit or distance > 0.25:
                enclosed = False
                break
        if enclosed:
            inside += 1
    return inside >= max(2, int(round(len(probes) * 0.6)))


def evaluate_frame(
    scene_context: SceneContext,
    camera,
    sample: CameraSample,
    *,
    config: ValidationSection,
    ray_caster: RayCaster | None = None,
    character: "CharacterBox | None" = None,
    probe_directions: "list[Vec3] | None" = None,
    project=None,
) -> FrameCheck:
    """Run every geometric check for one sampled camera pose."""
    caster = ray_caster if ray_caster is not None else scene_context.ray_caster
    check = FrameCheck(
        frame=sample.frame,
        position=sample.position,
        rotation_quaternion=sample.quaternion,
        focal=float(sample.focal),
    )

    # -- 7. illegal values ------------------------------------------------
    for name, value in (
        ("position.x", sample.position[0]), ("position.y", sample.position[1]), ("position.z", sample.position[2]),
        ("quaternion.w", sample.quaternion[0]), ("quaternion.x", sample.quaternion[1]),
        ("quaternion.y", sample.quaternion[2]), ("quaternion.z", sample.quaternion[3]),
        ("focal_length", sample.focal),
    ):
        if value is None or not math.isfinite(float(value)):
            check.illegal_values.append(f"{name} is not finite ({value!r})")
    if not check.illegal_values:
        norm = math.sqrt(sum(component * component for component in sample.quaternion))
        if norm < 1e-6:
            check.illegal_values.append("rotation quaternion is degenerate (norm ~ 0)")
        if not math.isfinite(float(camera.lens)) or float(camera.lens) <= 0:
            check.illegal_values.append(f"camera focal length is invalid ({camera.lens!r})")
    if check.illegal_values:
        return check

    # -- 1/2/3. clipping, enclosure, obstruction --------------------------
    # The character is not scene geometry: the camera may sit right next to it,
    # and the character must not mask the wall behind it.
    scene_exclude = character.self_object_names() if character is not None else None
    if caster is not None:
        directions = probe_directions if probe_directions is not None else fibonacci_directions(26)
        min_distance = math.inf
        for direction in directions:
            hit, distance = caster.cast(sample.position, direction, exclude=scene_exclude)
            if hit and distance < min_distance:
                min_distance = distance
        check.clearance = min_distance if min_distance < math.inf else None
        check.inside_geometry = check.clearance is not None and check.clearance <= config.inside_epsilon

        forward = vec_normalized(_forward_from_quaternion(sample.quaternion))
        hit, distance = caster.cast(sample.position, forward, exclude=scene_exclude)
        limit = view_obstruction_limit(config)
        if hit and distance < limit:
            check.obstructed = True
            check.obstruction_distance = distance
            check.obstruction_limit = limit
    else:
        check.illegal_values.append("no ray caster available; clipping checks were skipped")

    # -- 5. character framing / visibility / overlap -----------------------
    if character is not None and config.check_character_visibility:
        probe_points = character.probe_points(config.character_probe_points)
        visible = 0
        on_screen = 0
        occluded = 0
        forward = vec_normalized(_forward_from_quaternion(sample.quaternion))
        # The character's own mesh must not count as an occluder, otherwise its
        # interior probe points all read as "hidden behind the character".
        self_names = character.self_object_names() if character is not None else None
        for point in probe_points:
            ndc = project(camera, point) if project is not None else None
            if ndc is None:
                inside = frustum_contains(camera, sample.position, forward, point)
            else:
                inside = (
                    -1e-6 <= ndc[0] <= 1.0 + 1e-6
                    and -1e-6 <= ndc[1] <= 1.0 + 1e-6
                    and ndc[2] > 0.0
                )
            if not inside:
                continue
            on_screen += 1
            if caster is not None:
                direction = vec_sub(point, sample.position)
                distance_to_point = vec_length(direction)
                hit, distance = caster.cast(sample.position, direction, exclude=self_names)
                if hit and distance < distance_to_point - 1e-3:
                    occluded += 1
                    continue
            visible += 1
        total = len(probe_points)
        check.character_visible_ratio = (visible / float(total)) if total else 0.0
        check.character_on_screen = on_screen > 0
        check.character_max_occluded = on_screen > 0 and occluded > 0
        if config.check_character_overlap:
            check.character_overlap = character_overlaps_meshes(
                character, caster,
                probe_count=config.character_probe_points,
                exclude=self_names,
            )
    return check


def _forward_from_quaternion(quaternion: Quat) -> Vec3:
    """View direction (world) for a Blender camera orientation.

    A Blender camera looks along its local ``-Z``.
    """
    from .motion_templates import quat_rotate

    return vec_normalized(quat_rotate(quaternion, (0.0, 0.0, -1.0)))


def sampled_frames(start: int, end: int, step: int, extra: Iterable[int] = ()) -> "list[int]":
    """Frame numbers to validate: first, last, every ``step``, plus extras.

    Both endpoints are always included -- a move that is clean in the middle and
    buried in a wall on its final frame is exactly the failure mode this needs
    to catch.
    """
    frames = {int(start), int(end)}
    step = max(1, int(step))
    for frame in range(int(start), int(end) + 1, step):
        frames.add(int(frame))
    for frame in extra:
        try:
            value = int(frame)
        except (TypeError, ValueError):
            continue
        if int(start) <= value <= int(end):
            frames.add(value)
    return sorted(frames)


def build_character_union(characters: Sequence[CharacterBox]) -> "CharacterBox | None":
    if not characters:
        return None
    union = characters[0]
    for character in characters[1:]:
        union = union.union_with(character)
    return union


def _jump_scale(gap: int, mode: str) -> float:
    """Divisor applied to the jump limits for a ``gap``-frame sample interval."""
    gap = max(1, int(gap))
    if gap <= 1:
        return 1.0
    mode = str(mode or "sqrt").lower()
    if mode == "linear":
        return float(gap)
    if mode == "none":
        return 1.0
    return math.sqrt(float(gap))


def view_obstruction_limit(config: ValidationSection) -> float:
    """Distance ahead of the lens within which a surface counts as blocking.

    Kept separate from ``clearance`` so the two concerns stay independent: the
    camera body must clear geometry (``clearance``), and the shot must not be
    staring at a wall (``obstruction_distance``).  ``clearance`` acts as a floor
    so raising it also hardens the obstruction test.
    """
    return max(float(config.obstruction_distance), float(config.clearance))


class CameraValidator:
    """Validate a motion animation against a scene context."""

    def __init__(
        self,
        scene_context: SceneContext,
        config: ValidationSection,
        *,
        logger=None,
        ray_caster: RayCaster | None = None,
    ):
        self.context = scene_context
        self.config = config
        self.logger = logger
        self.ray_caster = ray_caster if ray_caster is not None else scene_context.ray_caster
        self._probe_directions = fibonacci_directions(26)

    # -- public API ------------------------------------------------------
    def validate(
        self,
        camera,
        animation,
        *,
        character: "CharacterBox | None" = None,
        base_matrix: Sequence[Sequence[float]] | None = None,
        base_focal: float | None = None,
        project=None,
    ) -> ValidationReport:
        report = ValidationReport()
        report.char_bbox = character
        animation_samples: "list[CameraSample]" = list(animation.samples)
        if not animation_samples:
            report.passed = False
            report.messages.append("animation produced no frames")
            report.add_problem(REASON_ILLEGAL_VALUE)
            return report

        sample_by_frame = {s.frame: s for s in animation_samples}
        frames = sampled_frames(
            animation.frame_start,
            animation.frame_end,
            self.config.sample_step,
            self.config.extra_sample_frames,
        )
        report.sampled_frames = frames

        if self.config.check_character_visibility and character is None:
            report.skipped_checks.append("character_visibility (no character in this sequence)")
        if self.ray_caster is None:
            report.skipped_checks.append("geometry_clearance (no ray caster available)")

        # -- clip range sanity (checked once, it is a camera-wide property) --
        if camera.clip_start < self.config.min_clip_start:
            report.add_problem(REASON_CLIP_RANGE)
            report.messages.append(
                f"clip_start {camera.clip_start} is below the configured minimum "
                f"{self.config.min_clip_start}"
            )
        if camera.clip_end > self.config.max_clip_end:
            report.add_problem(REASON_CLIP_RANGE)
            report.messages.append(
                f"clip_end {camera.clip_end} exceeds the configured maximum {self.config.max_clip_end}"
            )
        if self.context.world_bbox_min and self.context.world_bbox_max:
            diagonal = math.dist(self.context.world_bbox_min, self.context.world_bbox_max)
            if camera.clip_end < diagonal * 0.5:
                report.add_problem(REASON_CLIP_RANGE)
                report.messages.append(
                    f"clip_end {camera.clip_end:.3f} is shorter than half the scene diagonal "
                    f"({diagonal * 0.5:.3f}); distant geometry would be cut off"
                )
            if camera.clip_start > diagonal * 0.05 and diagonal > 0:
                report.messages.append(
                    f"clip_start {camera.clip_start:.4f} is large relative to the scene "
                    f"(diagonal {diagonal:.3f}); near geometry may pop"
                )

        # -- per-frame checks ---------------------------------------------
        project_fn = project if project is not None else self.context.project
        previous: "CameraSample | None" = None
        position_jumps: "list[float]" = []
        rotation_jumps: "list[float]" = []
        clearances: "list[float]" = []
        visible_ratios: "list[float]" = []
        illegal_frames = 0

        for frame in frames:
            sample = sample_by_frame.get(frame)
            if sample is None:
                continue
            check = evaluate_frame(
                self.context,
                camera,
                sample,
                config=self.config,
                ray_caster=self.ray_caster,
                character=character,
                probe_directions=self._probe_directions,
                project=project_fn,
            )
            if previous is not None:
                # A jump is only meaningful between *consecutive* frames; with a
                # coarse sample step the measured move spans several frames, so
                # the raw delta is stored and the *limit* is relaxed for the
                # span (see ``_jump_scale``).  ``position_jump`` keeps the
                # per-frame rate, which is what a human wants to read.
                gap = max(1, int(sample.frame) - int(previous.frame))
                check.metrics_span = gap
                check.position_delta = vec_length(vec_sub(sample.position, previous.position))
                check.rotation_delta_deg = quat_angle_between(sample.quaternion, previous.quaternion)
                check.position_jump = check.position_delta / gap
                check.rotation_jump_deg = check.rotation_delta_deg / gap
                position_jumps.append(check.position_jump)
                rotation_jumps.append(check.rotation_jump_deg)
            if check.clearance is not None:
                clearances.append(check.clearance)
            if check.character_visible_ratio is not None:
                visible_ratios.append(check.character_visible_ratio)

            problems = check.issues(self.config)
            for reason in problems:
                report.add_problem(reason)
            if check.illegal_values:
                illegal_frames += 1
            if not problems and check.illegal_values:
                report.add_problem(REASON_ILLEGAL_VALUE)
            report.frames.append(check)
            previous = sample

        # -- 6. jump check relative to the animation's own frame spacing ----
        if report.frames:
            report.metrics["max_position_jump"] = max(
                [f.position_jump for f in report.frames] or [0.0]
            )
            report.metrics["max_rotation_jump_deg"] = max(
                [f.rotation_jump_deg for f in report.frames] or [0.0]
            )
        if clearances:
            report.metrics["min_clearance"] = min(clearances)
            report.metrics["mean_clearance"] = sum(clearances) / len(clearances)
        if visible_ratios:
            report.metrics["min_character_visible_ratio"] = min(visible_ratios)
            report.metrics["mean_character_visible_ratio"] = sum(visible_ratios) / len(visible_ratios)
            report.metrics["character_on_screen_frame_ratio"] = (
                sum(1 for value in visible_ratios if value > 0.0) / float(len(visible_ratios))
            )
            on_screen_ratio = report.metrics["character_on_screen_frame_ratio"]
            if (
                self.config.check_character_visibility
                and on_screen_ratio < self.config.min_character_on_screen_frames
            ):
                report.add_problem(REASON_CHARACTER_UNFRAMED)

        # -- candidate deltas vs the original camera ------------------------
        if base_matrix is not None:
            base_position = (
                float(base_matrix[0][3]), float(base_matrix[1][3]), float(base_matrix[2][3]),
            )
            first = animation_samples[0]
            report.offset_from_base = vec_length(vec_sub(first.position, base_position))
        if base_focal:
            first = animation_samples[0]
            report.focal_delta_ratio = abs(float(first.focal) - float(base_focal)) / float(base_focal)

        report.metrics["sampled_frame_count"] = len(report.frames)
        report.metrics["illegal_frame_count"] = illegal_frames
        report.passed = not report.reason_counts
        report.score = self.score(report)
        if not report.passed:
            report.messages.append(
                "failed checks: "
                + ", ".join(f"{reason} ({count} frame(s))" for reason, count in sorted(report.reason_counts.items()))
            )
        return report

    # -- scoring ---------------------------------------------------------
    def score(self, report: ValidationReport, *, weights: dict | None = None) -> float:
        """Penalty-style score in ``[0, 1]``; higher is better.

        Hard failures (geometry intersection, missing character) dominate;
        distance and parameter drift are gentle tie-breakers so the search
        prefers the candidate that stays closest to the artist's camera.
        """
        weights = weights or {
            "distance": 1.0,
            "clipping": 4.0,
            "character_invisible": 2.0,
            "occlusion": 1.5,
            "rotation_delta": 0.5,
            "focal_delta": 0.25,
        }
        frames = max(1, len(report.frames))
        counts = report.reason_counts
        clipping_ratio = counts.get(REASON_CLIPPING, 0) / frames
        inside_ratio = counts.get(REASON_INSIDE_GEOMETRY, 0) / frames
        occluded_ratio = counts.get(REASON_OBSTRUCTION, 0) / frames
        invisible_ratio = (
            counts.get(REASON_CHARACTER_INVISIBLE, 0) + counts.get(REASON_CHARACTER_UNFRAMED, 0)
        ) / frames
        overlap_ratio = counts.get(REASON_CHARACTER_OVERLAP, 0) / frames
        jump_ratio = (
            counts.get(REASON_POSITION_JUMP, 0) + counts.get(REASON_ROTATION_JUMP, 0)
        ) / frames
        illegal_ratio = counts.get(REASON_ILLEGAL_VALUE, 0) / frames
        clip_ratio = counts.get(REASON_CLIP_RANGE, 0) / frames

        distance_penalty = min(1.0, report.offset_from_base / 10.0)
        rotation_penalty = min(1.0, report.rotation_delta_deg / 90.0)
        focal_penalty = min(1.0, report.focal_delta_ratio / 2.0)

        penalty = (
            weights["distance"] * distance_penalty
            + weights["clipping"] * (clipping_ratio + inside_ratio + overlap_ratio + jump_ratio + illegal_ratio + clip_ratio)
            + weights["character_invisible"] * invisible_ratio
            + weights["occlusion"] * occluded_ratio
            + weights["rotation_delta"] * rotation_penalty
            + weights["focal_delta"] * focal_penalty
        )
        worst = 1.0 + weights["distance"] + weights["rotation_delta"] + weights["focal_delta"]
        return max(0.0, 1.0 - (penalty / worst))


# --------------------------------------------------------------------------
# scene level validation (the "Validate scenes" button)
# --------------------------------------------------------------------------
@dataclass
class SceneCheck:
    blend_path: str
    exists: bool = True
    openable: bool = False
    scene_names: "list[str]" = field(default_factory=list)
    camera_count: int = 0
    cameras: "list[dict]" = field(default_factory=list)
    mesh_count: int = 0
    character_count: int = 0
    missing_resources: "list[dict]" = field(default_factory=list)
    problems: "list[str]" = field(default_factory=list)
    warnings: "list[str]" = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exists and self.openable and not self.problems

    def to_dict(self) -> dict:
        return {
            "blend_path": self.blend_path,
            "exists": self.exists,
            "openable": self.openable,
            "ok": self.ok,
            "scene_names": list(self.scene_names),
            "camera_count": self.camera_count,
            "cameras": list(self.cameras),
            "mesh_count": self.mesh_count,
            "character_count": self.character_count,
            "missing_resource_count": len(self.missing_resources),
            "missing_resources": list(self.missing_resources),
            "problems": list(self.problems),
            "warnings": list(self.warnings),
        }


def validate_camera_static(camera, config: ValidationSection) -> "list[str]":
    """Static checks that do not need any motion (used by the scene validator)."""
    problems: "list[str]" = []
    if camera.lens is None or not math.isfinite(float(camera.lens)) or float(camera.lens) <= 0:
        problems.append("focal length is invalid")
    if not math.isfinite(float(camera.clip_start)) or camera.clip_start <= 0:
        problems.append("clip_start must be > 0")
    if not math.isfinite(float(camera.clip_end)) or camera.clip_end <= camera.clip_start:
        problems.append("clip_end must be greater than clip_start")
    if camera.clip_start < config.min_clip_start:
        problems.append(
            f"clip_start {camera.clip_start} is below the configured minimum {config.min_clip_start}"
        )
    if camera.clip_end > config.max_clip_end:
        problems.append(
            f"clip_end {camera.clip_end} exceeds the configured maximum {config.max_clip_end}"
        )
    for name, value in (("location", camera.location), ("scale", camera.scale)):
        if not all(math.isfinite(float(v)) for v in value):
            problems.append(f"{name} contains non-finite values")
    res_x, res_y = camera.effective_resolution
    if res_x < 1 or res_y < 1:
        problems.append("effective render resolution is degenerate")
    return problems
