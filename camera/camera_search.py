"""Spherical-range camera search.

When validation rejects the artist's camera for a given motion, the pipeline
retries from a *sphere* of alternative positions around it.  Every candidate is
re-evaluated with the exact same validator, then ranked by a weighted score
that rewards geometric clearance and penalises any drift from the original
camera (position, orientation, focal length).  Nothing is written to the scene
until a winner is chosen, so the search is cheap and side-effect free.

Candidate sources (all deterministic for a given ``random_seed``):

``azimuth``      one ring per elevation band, regular in angle
``fibonacci``    near-uniform spherical distribution
``radial``       several concentric shells
``random``       seeded uniform sampling inside the sphere
``rotation``     orientation nudges (only when there is something to frame)
``focal``        focal-length steps (only when the base framing fails)
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Callable, Sequence

from ..config.models import SearchSection
from .camera_validator import (
    REASON_CHARACTER_INVISIBLE,
    REASON_CHARACTER_UNFRAMED,
    CameraValidator,
    ValidationReport,
)
from .motion_templates import (
    MotionAnimation,
    Quat,
    Vec3,
    quat_from_axis_angle,
    quat_multiply,
    vec_add,
    vec_length,
    vec_normalized,
    vec_sub,
)
from .scene_context import CharacterBox, SceneContext

#: Reasons that a small focal-length change can plausibly fix.
_FOCAL_FIXABLE = {REASON_CHARACTER_INVISIBLE, REASON_CHARACTER_UNFRAMED}
#: Reasons that only a translation can fix.
_TRANSLATION_FIXABLE = {
    "camera_clipping",
    "camera_inside_geometry",
    "camera_obstructed",
    "character_invisible",
    "character_unframed",
    "character_overlap",
}


@dataclass
class SearchCandidate:
    """One point in the search space."""

    index: int
    offset: Vec3
    position: Vec3
    rotation_adjust: Quat = (1.0, 0.0, 0.0, 0.0)
    focal_scale: float = 1.0
    radius: float = 0.0
    azimuth_deg: float = 0.0
    elevation_deg: float = 0.0
    source: str = "azimuth"

    @property
    def rotation_adjust_deg(self) -> float:
        from .motion_templates import quat_angle_between

        return quat_angle_between(self.rotation_adjust, (1.0, 0.0, 0.0, 0.0))

    def describe(self) -> str:
        return (
            f"#{self.index} {self.source} r={self.radius:.3f}m "
            f"az={self.azimuth_deg:.1f}° el={self.elevation_deg:.1f}° "
            f"Δrot={self.rotation_adjust_deg:.1f}° focal×{self.focal_scale:.3f}"
        )

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "source": self.source,
            "offset": [round(float(v), 6) for v in self.offset],
            "position": [round(float(v), 6) for v in self.position],
            "radius": round(float(self.radius), 6),
            "azimuth_deg": round(float(self.azimuth_deg), 4),
            "elevation_deg": round(float(self.elevation_deg), 4),
            "rotation_adjust_deg": round(float(self.rotation_adjust_deg), 4),
            "focal_scale": round(float(self.focal_scale), 6),
        }


@dataclass
class CandidateEvaluation:
    candidate: SearchCandidate
    report: ValidationReport
    #: Set when a candidate passed the geometry checks but an extra veto (the
    #: focus object leaving the frame) rejected it anyway.
    rejected_reason: str = ""

    @property
    def passed(self) -> bool:
        return self.report.passed and not self.rejected_reason

    def to_dict(self, *, include_frames: bool = False) -> dict:
        return {
            "candidate": self.candidate.to_dict(),
            "passed": bool(self.report.passed),
            "accepted": self.passed,
            "rejected_reason": self.rejected_reason,
            "score": round(float(self.report.score), 6),
            "reasons": self.report.failures,
            "metrics": {
                k: (round(v, 6) if isinstance(v, float) else v)
                for k, v in self.report.metrics.items()
            },
            "report": self.report.to_dict(include_frames=include_frames),
        }


@dataclass
class SearchResult:
    """Outcome of one search attempt (which may cover several retry rounds)."""

    passed: bool = False
    best: "CandidateEvaluation | None" = None
    best_valid: "CandidateEvaluation | None" = None
    evaluations: "list[CandidateEvaluation]" = field(default_factory=list)
    rounds: "list[dict]" = field(default_factory=list)
    messages: "list[str]" = field(default_factory=list)
    original_report: "ValidationReport | None" = None
    accepted: "list[SearchCandidate]" = field(default_factory=list)
    seed: int = 0
    radii_used: "list[float]" = field(default_factory=list)

    @property
    def attempts(self) -> int:
        return len(self.evaluations)

    def to_dict(self, *, include_all: bool = False, include_frames: bool = False) -> dict:
        payload = {
            "passed": bool(self.passed),
            "attempt_count": self.attempts,
            "seed": int(self.seed),
            "radii_used": [round(float(r), 6) for r in self.radii_used],
            "rounds": list(self.rounds),
            "messages": list(self.messages),
            "accepted_candidates": [c.to_dict() for c in self.accepted],
            "original_report": self.original_report.to_dict(include_frames=False) if self.original_report else None,
            "best": self.best.to_dict(include_frames=include_frames) if self.best else None,
            "best_valid": self.best_valid.to_dict(include_frames=include_frames) if self.best_valid else None,
        }
        if include_all:
            payload["evaluations"] = [e.to_dict(include_frames=include_frames) for e in self.evaluations]
        return payload


# --------------------------------------------------------------------------
# candidate generation
# --------------------------------------------------------------------------
def _fibonacci_sphere(count: int) -> "list[tuple[float, float]]":
    """``(elevation, azimuth)`` in degrees, near-uniform on the sphere."""
    if count <= 0:
        return []
    points = []
    golden = math.pi * (3.0 - math.sqrt(5.0))
    for index in range(count):
        z = 1.0 - (2.0 * index + 1.0) / count
        z = max(-1.0, min(1.0, z))
        elevation = math.degrees(math.asin(z))
        azimuth = math.degrees((golden * index) % (2.0 * math.pi))
        points.append((elevation, azimuth))
    return points


def generate_candidate_offsets(
    *,
    min_radius: float,
    max_radius: float,
    candidate_count: int,
    azimuth_samples: int,
    elevation_samples: int,
    shell_only: bool,
    seed: int,
    logger=None,
) -> "list[dict]":
    """Build the offset list.  Pure function -> fully unit-testable."""
    min_radius = max(0.0, float(min_radius))
    max_radius = max(min_radius, float(max_radius))
    if candidate_count <= 0:
        return []
    spans = max_radius - min_radius
    rng = random.Random(int(seed))
    offsets: "list[dict]" = []

    def radius_at(u: float) -> float:
        if shell_only or spans <= 0.0:
            return max_radius
        # Volume-uniform so the density does not bunch up near the outer shell.
        return (min_radius ** 3 + (max_radius ** 3 - min_radius ** 3) * u) ** (1.0 / 3.0)

    def emit(radius: float, elevation: float, azimuth: float, source: str) -> None:
        elevation = max(-89.9, min(89.9, elevation))
        el = math.radians(elevation)
        az = math.radians(azimuth)
        offsets.append({
            "offset": (
                radius * math.cos(el) * math.cos(az),
                radius * math.cos(el) * math.sin(az),
                radius * math.sin(el),
            ),
            "radius": radius,
            "azimuth_deg": azimuth % 360.0,
            "elevation_deg": elevation,
            "source": source,
        })

    # 1. regular azimuth/elevation grid -- predictable, good coverage
    az_count = max(1, int(azimuth_samples))
    el_count = max(1, int(elevation_samples))
    grid_total = az_count * el_count
    for el_index in range(el_count):
        if el_count == 1:
            elevation = 0.0
        else:
            elevation = -60.0 + 120.0 * (el_index / float(el_count - 1))
        shell_index = el_index % 3
        radius = radius_at((shell_index + 1) / 4.0)
        for az_index in range(az_count):
            azimuth = 360.0 * (az_index / float(az_count))
            if len(offsets) >= candidate_count:
                break
            emit(radius, elevation, azimuth, "azimuth")
        if len(offsets) >= candidate_count:
            break

    # 2. fibonacci sphere -- even angular coverage when the grid is coarse
    if len(offsets) < candidate_count:
        for index, (elevation, azimuth) in enumerate(_fibonacci_sphere(grid_total * 2)):
            if len(offsets) >= candidate_count:
                break
            radius = radius_at(0.35 + 0.3 * ((index % 3) / 2.0))
            emit(radius, elevation, azimuth, "fibonacci")

    # 3. concentric shells straight out / up / back from the original camera
    if len(offsets) < candidate_count and spans > 0:
        for shell in range(3):
            if len(offsets) >= candidate_count:
                break
            radius = min_radius + spans * (shell / 2.0)
            for elevation, azimuth in ((0.0, 0.0), (0.0, 90.0), (0.0, 180.0), (0.0, 270.0),
                                       (45.0, 0.0), (-45.0, 0.0), (0.0, 45.0), (0.0, 135.0),
                                       (0.0, 225.0), (0.0, 315.0)):
                if len(offsets) >= candidate_count:
                    break
                emit(radius, elevation, azimuth, "radial")

    # 4. seeded random fill -- breaks the symmetry of the grid
    guard = 0
    while len(offsets) < candidate_count and guard < candidate_count * 20:
        guard += 1
        radius = radius_at(rng.random())
        elevation = math.degrees(math.asin(rng.uniform(-1.0, 1.0)))
        azimuth = rng.uniform(0.0, 360.0)
        emit(radius, elevation, azimuth, "random")

    if logger is not None:
        logger.debug(
            "generated %d candidate offset(s) in r=[%.3f, %.3f] shell_only=%s",
            len(offsets), min_radius, max_radius, shell_only,
        )
    return offsets[:candidate_count]


def build_candidates(
    *,
    base_position: Sequence[float],
    base_quaternion: Quat,
    section: SearchSection,
    character: "CharacterBox | None" = None,
    character_center: "Vec3 | None" = None,
    logger=None,
) -> "list[SearchCandidate]":
    """Expand ``SearchSection`` into the full candidate list."""
    offsets = generate_candidate_offsets(
        min_radius=section.min_radius,
        max_radius=section.max_radius,
        candidate_count=section.candidate_count,
        azimuth_samples=section.azimuth_samples,
        elevation_samples=section.elevation_samples,
        shell_only=section.shell_only,
        seed=section.random_seed,
        logger=logger,
    )
    base_position = tuple(float(v) for v in base_position)

    # Orientation nudges: aim at the character when there is one, otherwise a
    # small symmetric ladder so the search can still escape a wall.
    rotation_plan: "list[tuple[Quat, str]]" = [(tuple(base_quaternion), "keep")]
    if section.allow_rotation_adjust:
        max_deg = float(section.max_rotation_adjust_deg)
        if character is not None and character_center is not None:
            aim = aim_rotation(
                base_position, base_quaternion, character_center, max_deg=max_deg
            )
            if aim is not None:
                rotation_plan.append((aim, "aim_at_character"))
        if max_deg > 0:
            for factor, label in ((0.5, "half"), (1.0, "full")):
                angle = max_deg * factor
                for sign in (1.0, -1.0):
                    for axis in ("X", "Z"):
                        rotation_plan.append((
                            quat_multiply(base_quaternion, quat_from_axis_angle(axis, angle * sign)),
                            f"nudge_{axis}{'+' if sign > 0 else '-'}_{label}",
                        ))

    focal_plan = [1.0]
    if section.allow_focal_adjust and float(section.focal_adjust_steps) > 0:
        step = float(section.focal_adjust_steps) / 100.0
        focal_plan = [1.0 - step, 1.0 + step, 1.0 - 2 * step, 1.0 + 2 * step, 1.0]
        focal_plan = [value for value in focal_plan if value > 0.05]

    max_candidates = max(1, int(section.candidate_count)) * len(rotation_plan) * len(focal_plan)
    candidates: "list[SearchCandidate]" = []
    index = 0
    for offset_info in offsets:
        if len(candidates) >= max_candidates:
            break
        offset = offset_info["offset"]
        position = vec_add(base_position, offset)
        for quaternion, _label in rotation_plan:
            if len(candidates) >= max_candidates:
                break
            for focal_scale in focal_plan:
                if len(candidates) >= max_candidates:
                    break
                candidates.append(SearchCandidate(
                    index=index,
                    offset=offset,
                    position=position,
                    rotation_adjust=quaternion,
                    focal_scale=float(focal_scale),
                    radius=float(offset_info["radius"]),
                    azimuth_deg=float(offset_info["azimuth_deg"]),
                    elevation_deg=float(offset_info["elevation_deg"]),
                    source=str(offset_info["source"]),
                ))
                index += 1
    return candidates


def apply_candidate(animation: MotionAnimation, candidate: SearchCandidate) -> MotionAnimation:
    """Return a copy of ``animation`` with the candidate's **focal** step applied.

    Orientation is *not* applied here: the caller's ``make_animation`` callback
    receives the candidate and folds ``candidate.rotation_adjust`` into the base
    matrix, so the template's offsets are built in the rotated frame.  Rotating the
    keyed quaternions here as well (which is what this function used to do) both
    double-applied the turn and left the path pointing along the pre-rotation view
    axis -- an accepted candidate then moved the camera sideways instead of forward.
    """
    samples = []
    for sample in animation.samples:
        samples.append(
            type(sample)(
                frame=sample.frame,
                position=sample.position,
                quaternion=sample.quaternion,
                focal=sample.focal * candidate.focal_scale,
                template_offset=sample.template_offset,
                template_rotation=sample.template_rotation,
            )
        )
    return MotionAnimation(
        template_name=animation.template_name,
        frame_start=animation.frame_start,
        frame_end=animation.frame_end,
        fps=animation.fps,
        interpolation=animation.interpolation,
        samples=samples,
        template_parameters=dict(animation.template_parameters),
        unit_scale=dict(animation.unit_scale),
        notes=list(animation.notes),
        source_unit_focal_range=animation.source_unit_focal_range,
    )


def aim_rotation(
    base_position: Sequence[float],
    base_quaternion: Quat,
    target: Sequence[float],
    *,
    max_deg: float,
) -> "Quat | None":
    """Rotation that points the camera at ``target``, clamped to ``max_deg``.

    Implemented as a minimal-arc correction: we take the quaternion that would
    look at the target and, if the required turn exceeds the budget, scale the
    correction so the result never drifts further than the user allowed.
    """
    direction = vec_sub(target, base_position)
    if vec_length(direction) < 1e-6:
        return None
    desired = look_at_quaternion(direction)
    delta = quat_multiply(desired, _quat_inverse(base_quaternion))
    angle = quat_angle_deg(delta)
    if angle <= 1e-6:
        return None
    if angle > max_deg and max_deg > 0:
        delta = _quat_slerp_from_identity(delta, max_deg / angle)
    return quat_multiply(delta, base_quaternion)


def look_at_quaternion(direction: Sequence[float]) -> Quat:
    """Camera orientation whose view axis (local -Z) follows ``direction``."""
    forward = vec_normalized(direction)
    up_hint = (0.0, 0.0, 1.0)
    if abs(forward[2]) > 0.9999:
        up_hint = (0.0, 1.0, 0.0)
    right = vec_normalized(_cross(forward, up_hint))
    up = _cross(right, forward)
    # Columns are (right, up, back) because Blender cameras look down -Z.
    matrix = [
        [right[0], up[0], -forward[0], 0.0],
        [right[1], up[1], -forward[1], 0.0],
        [right[2], up[2], -forward[2], 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]
    from .motion_templates import matrix_to_quaternion

    return matrix_to_quaternion(matrix)


def _cross(a: Sequence[float], b: Sequence[float]) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _quat_inverse(q: Sequence[float]) -> Quat:
    from .motion_templates import quat_conjugate, quat_normalize

    return quat_conjugate(quat_normalize(q))


def quat_angle_deg(q: Sequence[float]) -> float:
    from .motion_templates import quat_angle_between

    return quat_angle_between(q, (1.0, 0.0, 0.0, 0.0))


def _quat_slerp_from_identity(q: Sequence[float], t: float) -> Quat:
    """Scale a rotation's angle by ``t`` (identity -> q), keeping the axis."""
    from .motion_templates import quat_normalize

    q = quat_normalize(q)
    w = max(-1.0, min(1.0, q[0]))
    angle = 2.0 * math.acos(w)
    s = math.sqrt(max(0.0, 1.0 - w * w))
    if s < 1e-9:
        return (1.0, 0.0, 0.0, 0.0)
    axis = (q[1] / s, q[2] / s, q[3] / s)
    half = angle * max(0.0, min(1.0, t)) * 0.5
    sin_half = math.sin(half)
    return (math.cos(half), axis[0] * sin_half, axis[1] * sin_half, axis[2] * sin_half)


# --------------------------------------------------------------------------
# search driver
# --------------------------------------------------------------------------
class CameraSearch:
    """Drive candidate evaluation for one (camera, motion) pair."""

    def __init__(
        self,
        scene_context: SceneContext,
        section: SearchSection,
        validator: CameraValidator,
        *,
        logger=None,
        max_evaluations: int = 4000,
    ):
        self.context = scene_context
        self.section = section
        self.validator = validator
        self.logger = logger
        self.max_evaluations = int(max_evaluations)
        self._log = logger.debug if logger is not None else (lambda *a, **k: None)

    def search(
        self,
        camera,
        make_animation: Callable[[SearchCandidate], MotionAnimation],
        *,
        base_position: Sequence[float],
        base_quaternion: Quat,
        base_focal: float,
        character: "CharacterBox | None" = None,
        base_matrix: Sequence[Sequence[float]] | None = None,
        project=None,
        original_report: "ValidationReport | None" = None,
        veto: "Callable[[SearchCandidate, MotionAnimation], str | None] | None" = None,
    ) -> SearchResult:
        """Try to find a passing camera position near the original one.

        ``veto`` is an optional last word on a candidate that already passed the
        geometry checks: it returns a reason string to reject it or ``None`` to
        accept.  The generator uses it to keep a focus object in frame.
        """
        result = SearchResult(seed=int(self.section.random_seed), original_report=original_report)
        if not self.section.enabled:
            result.messages.append("camera search is disabled; keeping the original camera position")
            return result

        character_center = character.center() if character is not None else None
        candidates = build_candidates(
            base_position=base_position,
            base_quaternion=base_quaternion,
            section=self.section,
            character=character,
            character_center=character_center,
            logger=self.logger,
        )
        if not candidates:
            result.messages.append("no search candidates were generated (check the radius settings)")
            return result

        # Evaluate the least-drift candidates first so the first success is also
        # the most conservative fix.
        candidates.sort(key=lambda c: (c.radius, c.rotation_adjust_deg, abs(c.focal_scale - 1.0)))
        focus = sorted({reason for reason in (original_report.failures if original_report else [])})
        need_focal = bool(focus) and set(focus) <= _FOCAL_FIXABLE
        if need_focal:
            # A pure framing failure rarely needs a translation; try lens first.
            candidates.sort(key=lambda c: (
                0 if c.radius <= self.section.min_radius + 1e-9 else 1,
                abs(c.focal_scale - 1.0),
                c.rotation_adjust_deg,
            ))
        elif focus and not (set(focus) & _TRANSLATION_FIXABLE):
            result.messages.append(
                f"failure(s) {', '.join(focus)} cannot be fixed by moving the camera; "
                "the search still runs and will report its best attempt"
            )

        best_overall: "CandidateEvaluation | None" = None
        best_valid: "CandidateEvaluation | None" = None
        evaluated = 0
        round_index = 0
        max_rounds = int(self.section.max_retries) + 1

        while round_index < max_rounds and len(result.accepted) < max(1, int(self.section.max_output_candidates)):
            round_index += 1
            found_this_round = 0
            round_summary = {
                "round": round_index,
                "evaluated": 0,
                "passed": 0,
                "best_score": None,
                "best_candidate": None,
                "top_reasons": {},
            }
            for candidate in candidates:
                if evaluated >= self.max_evaluations:
                    result.messages.append(
                        f"stopped after {evaluated} evaluations (safety cap {self.max_evaluations})"
                    )
                    break
                if any(
                    vec_length(vec_sub(candidate.position, accepted.position)) < 1e-6
                    and candidate.focal_scale == accepted.focal_scale
                    and quat_angle_deg(quat_multiply(candidate.rotation_adjust, _quat_inverse(accepted.rotation_adjust))) < 1e-6
                    for accepted in result.accepted
                ):
                    continue
                evaluated += 1
                round_summary["evaluated"] += 1
                full_animation, report = self._evaluate(
                    camera, candidate, make_animation,
                    character=character, base_matrix=base_matrix,
                    base_focal=base_focal, project=project,
                )
                evaluation = CandidateEvaluation(candidate=candidate, report=report)
                if report.passed and veto is not None:
                    # Geometry alone is not enough once the shot has a subject: a
                    # candidate that clears every wall but no longer shows what the
                    # sequence is *of* is not a fix.  Measured on a real 90-degree
                    # arc -- the search walked the camera 1.9 m sideways and 87 deg
                    # off the authored pose (which was grazing a wall) and took the
                    # subject from 145/145 frames in frame down to 0/145.
                    try:
                        rejection = veto(candidate, full_animation)
                    except Exception as exc:  # a broken veto must not kill the run
                        if self.logger is not None:
                            self.logger.warning("search candidate veto failed: %s", exc)
                        rejection = None
                    if rejection:
                        evaluation.rejected_reason = str(rejection)
                del full_animation
                result.evaluations.append(evaluation)
                if best_overall is None or report.score > best_overall.report.score:
                    best_overall = evaluation
                    round_summary["best_score"] = round(float(report.score), 6)
                    round_summary["best_candidate"] = candidate.describe()
                if evaluation.passed:
                    best_valid = evaluation
                    found_this_round += 1
                    result.accepted.append(candidate)
                    round_summary["passed"] += 1
                    result.messages.append(
                        f"round {round_index}: accepted {candidate.describe()} (score {report.score:.4f})"
                    )
                    if len(result.accepted) >= max(1, int(self.section.max_output_candidates)):
                        break
                else:
                    if evaluation.rejected_reason:
                        round_summary["top_reasons"][evaluation.rejected_reason] = (
                            round_summary["top_reasons"].get(evaluation.rejected_reason, 0) + 1
                        )
                        result.messages.append(
                            f"round {round_index}: rejected {candidate.describe()} "
                            f"(score {report.score:.4f}) -- {evaluation.rejected_reason}"
                        )
                    for reason in report.failures:
                        round_summary["top_reasons"][reason] = (
                            round_summary["top_reasons"].get(reason, 0) + 1
                        )
            result.rounds.append(round_summary)
            result.radii_used = sorted({c.radius for c in result.accepted}) or result.radii_used
            if found_this_round == 0:
                break

        result.best = best_overall
        result.best_valid = best_valid
        result.passed = bool(result.accepted)
        if not result.passed:
            reasons = ", ".join(sorted({r for e in result.evaluations for r in e.report.failures}))
            result.messages.append(
                f"all {evaluated} candidate(s) failed"
                + (f"; recurring reasons: {reasons}" if reasons else "")
            )
            if self.logger is not None:
                self.logger.warning(
                    "camera search found no valid position for %s (best score %.4f)",
                    getattr(camera, "name", "camera"),
                    best_overall.report.score if best_overall else 0.0,
                )
        return result

    # -- internals --------------------------------------------------------
    def _evaluate(
        self,
        camera,
        candidate: SearchCandidate,
        make_animation: Callable[[SearchCandidate], MotionAnimation],
        *,
        character,
        base_matrix,
        base_focal,
        project,
    ):
        animation = apply_candidate(make_animation(candidate), candidate)
        report = self.validator.validate(
            camera,
            animation,
            character=character,
            base_matrix=base_matrix,
            base_focal=base_focal,
            project=project,
        )
        return animation, report


def sphere_sample_points(
    *,
    center: Sequence[float],
    min_radius: float,
    max_radius: float,
    count: int,
    seed: int = 0,
) -> "list[Vec3]":
    """Uniformly sample ``count`` points inside a spherical shell.

    Exposed separately because the add-on previews the search volume in the
    viewport and because it is a convenient pure-math helper for tests.
    """
    if count <= 0:
        return []
    rng = random.Random(int(seed))
    low = max(0.0, float(min_radius))
    high = max(low, float(max_radius))
    points = []
    for _ in range(count):
        u = rng.random()
        radius = (low ** 3 + (high ** 3 - low ** 3) * u) ** (1.0 / 3.0)
        z = rng.uniform(-1.0, 1.0)
        theta = rng.uniform(0.0, 2.0 * math.pi)
        r_xy = math.sqrt(max(0.0, 1.0 - z * z))
        points.append(vec_add(center, (
            radius * r_xy * math.cos(theta),
            radius * r_xy * math.sin(theta),
            radius * z,
        )))
    return points
