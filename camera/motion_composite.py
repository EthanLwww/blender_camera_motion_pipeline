"""Spatio-temporal compounds: several atomic camera moves at once, in segments.

A compound shot divides the video into **segments** (时段) and plays one or more
*atomic* moves inside each one.  Two atoms may share a segment **iff their channel
sets are disjoint**:

========  ==============================================
channel   what it drives
========  ==============================================
yaw       rotation about the camera's own up axis (``ry``)
pitch     rotation about the camera's own right axis (``rx``)
roll      rotation about the camera's own view axis (``rz``)
lateral   translation along the camera's own right axis (``x``)
vertical  translation along the camera's own up axis (``y``)
depth     translation along the camera's own view axis (``z``)
focal     focal length (mm)
========  ==============================================

So ``pan_right`` + ``tilt_down`` + ``truck_left`` is a legal three-way segment,
while ``zoom_in`` + ``zoom_out`` or ``pedestal_up`` + ``pedestal_down`` are not --
exactly the rule a shot list needs.  An ``Arc`` drives *two* channels (lateral and
yaw), which is why it cannot be combined with a pan or a truck.

Design:

* The vocabulary is a template document (``templates/atomic_motion_templates.json``
  by default, or ``composite.template_path``).  Every atom is an ordinary template
  whose keys are a **one-second ramp**, so its delta is a *rate per second*; the
  extra metadata (``type``/``direction``/``speed``/``channels``) is what makes it
  combinable.
* A :class:`MotionPlan` is the segment layout: which atoms run when, at which
  speed, for how long.  The plan is what gets recorded in the sidecar and written
  next to the rendered video.
* :func:`flatten_plan` turns the plan into one ordinary per-frame template
  (camera-local offsets and Euler degrees -- what the rest of the pipeline already
  consumes), so validation, the camera search, the bake, the metadata and the
  renderer need to know nothing about compounds.

Because the atoms are rates, a segment's length changes how far a move travels but
not how fast it looks: "Pan left, medium" is 18 deg/s whether the segment is 0.5 s
or 6 s long.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass, field
from typing import Sequence

from ..config.models import ConfigError
from .motion_templates import (
    MotionTemplate,
    MotionTemplateLibrary,
    TemplateKeyframe,
    Vec3,
)

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------
#: Shortest segment a plan may contain, in seconds (the 0.5 s floor).  It is what
#: bounds how many segments a video of a given length can have.
MIN_SEGMENT_SECONDS = 0.5
#: Hard limit on how many atoms may run at the same moment.
MAX_SIMULTANEOUS_LIMIT = 5
#: The speeds the vocabulary is authored in.
SPEEDS = ("slow", "medium", "fast")

#: ``composite.output_mode`` values.
OUTPUT_WITH_BASE = "with_base"          # single-atom shots and compounds
OUTPUT_ONLY_COMPOUND = "only_compound"  # compounds only
OUTPUT_ONLY_BASE = "only_base"          # single-atom shots only
OUTPUT_MODES = (OUTPUT_WITH_BASE, OUTPUT_ONLY_COMPOUND, OUTPUT_ONLY_BASE)

#: ``composite.duration_mode`` values.
DURATION_FIXED = "fixed"
DURATION_RANDOM = "random"
DURATION_MODES = (DURATION_FIXED, DURATION_RANDOM)

#: Folder every compound sequence lands in.  Deliberately short and constant: the
#: interesting part of a compound is its *plan*, and that lives in the sidecar
#: (``*_motion_plan.json``) rather than in a folder name nobody can read.
COMPOUND_MOTION_NAME = "combo"

#: Fallback duration when the configuration does not say (seconds).
DEFAULT_DURATION_SECONDS = 4.0

#: Lens range a zoom plan may drive the camera through (millimetres).  A zoom that
#: runs past an end simply holds there -- a real lens cannot go further -- and the
#: shot report keeps describing what was asked for.
FOCAL_MIN_MM = 8.0
FOCAL_MAX_MM = 300.0


# --------------------------------------------------------------------------
# atomic vocabulary
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class AtomicMotion:
    """One combinable atomic camera move at one speed."""

    name: str
    type: str
    direction: "str | None"
    speed: "str | None"
    channels: "tuple[str, ...]"
    rate_location: Vec3
    rate_rotation: Vec3
    rate_focal: float
    description: str = ""

    @property
    def is_static(self) -> bool:
        return not self.channels

    @property
    def moves(self) -> bool:
        return (
            any(abs(v) > 1e-12 for v in (*self.rate_location, *self.rate_rotation))
            or abs(self.rate_focal) > 1e-12
        )

    def to_dict(self) -> dict:
        """The shot-report shape: ``{"type": .., "direction": .., "speed": ..}``."""
        return {"type": self.type, "direction": self.direction, "speed": self.speed}

    def summary(self) -> dict:
        return {
            **self.to_dict(),
            "name": self.name,
            "channels": list(self.channels),
            "rate_location": [round(float(v), 6) for v in self.rate_location],
            "rate_rotation": [round(float(v), 6) for v in self.rate_rotation],
            "rate_focal": round(float(self.rate_focal), 6),
            "description": self.description,
        }


def _rate_from_keys(template: MotionTemplate, fps: float) -> "tuple[Vec3, Vec3, float]":
    """``(location, rotation, focal)`` per second for a template's own ramp."""
    keys = template.keyframes
    first, last = keys[0], keys[-1]
    span = (last.frame - first.frame) / max(fps, 1e-6)
    span_seconds = max(span, 1.0 / max(fps, 1e-6))
    location = tuple(
        (float(b) - float(a)) / span_seconds for a, b in zip(first.location, last.location)
    )
    rotation = tuple(
        (float(b) - float(a)) / span_seconds for a, b in zip(first.rotation, last.rotation)
    )
    focal = 0.0
    if last.focal is not None and first.focal is not None:
        focal = (float(last.focal) - float(first.focal)) / span_seconds
    return location, rotation, focal  # type: ignore[return-value]


def atomic_from_template(template: MotionTemplate, *, fps: float = 24.0) -> "AtomicMotion | None":
    """Read one atom out of a template, or ``None`` when it carries no metadata.

    An atom needs ``type``; a template without it is an ordinary shot and is simply
    not combinable.
    """
    parameters = template.parameters or {}
    kind = str(parameters.get("type") or "").strip()
    if not kind:
        return None
    channels = parameters.get("channels") or []
    if isinstance(channels, str):
        channels = [channels]
    channels = tuple(str(name).strip() for name in channels if str(name).strip())
    speed = parameters.get("speed")
    speed = str(speed).strip() if speed not in (None, "") else None
    direction = parameters.get("direction")
    direction = str(direction).strip() if direction not in (None, "") else None
    location, rotation, focal = _rate_from_keys(template, fps)
    return AtomicMotion(
        name=template.name,
        type=kind,
        direction=direction,
        speed=speed,
        channels=channels,
        rate_location=location,
        rate_rotation=rotation,
        rate_focal=focal,
        description=template.description or "",
    )


def load_atomic_motions(library: MotionTemplateLibrary, *, fps: float = 24.0) -> "list[AtomicMotion]":
    """Every combinable atom in *library*, in document order."""
    atoms = []
    for template in library:
        atom = atomic_from_template(template, fps=fps)
        if atom is not None:
            atoms.append(atom)
    return atoms


def atomic_document_path() -> str:
    """The bundled vocabulary (``<package>/templates/atomic_motion_templates.json``)."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "templates", "atomic_motion_templates.json")


def load_atomic_library(*, template_path: str = "", fps: float = 24.0, logger=None
                        ) -> "tuple[list[AtomicMotion], str]":
    """Atoms for a run: an explicit document when configured, else the bundled one."""
    from ..io.path_utils import normalize_path

    path = (template_path or "").strip() or atomic_document_path()
    target = normalize_path(path)
    if not os.path.isfile(target):
        if logger is not None:
            logger.warning("composite: atomic template document not found: %s", target)
        return [], target
    library = MotionTemplateLibrary.from_file(target)
    atoms = load_atomic_motions(library, fps=fps)
    if not atoms and logger is not None:
        logger.warning(
            "composite: %s has no combinable atoms (entries need a 'type' field)", target
        )
    for warning in library.warnings:
        if logger is not None:
            logger.warning("composite: %s", warning)
    return atoms, target


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class PlanSegment:
    """One time window of a compound, with the atoms that run inside it."""

    index: int
    start_time: float
    end_time: float
    start_frame: int
    end_frame: int
    motions: "tuple[AtomicMotion, ...]"

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.end_time - self.start_time)

    @property
    def frame_count(self) -> int:
        return max(1, self.end_frame - self.start_frame + 1)

    def to_dict(self) -> dict:
        return {
            "start_time": round(self.start_time, 6),
            "end_time": round(self.end_time, 6),
            "basic_movement": [motion.to_dict() for motion in self.motions]
            or [{"type": "Static", "direction": None, "speed": None}],
        }


@dataclass
class MotionPlan:
    """The segment layout of one sequence."""

    duration_seconds: float
    fps: float
    frame_start: int
    frame_end: int
    segments: "tuple[PlanSegment, ...]"
    compound: bool = True
    seed: int = 0
    source: str = ""
    notes: "list[str]" = field(default_factory=list)

    @property
    def frame_count(self) -> int:
        return self.frame_end - self.frame_start + 1

    @property
    def atom_names(self) -> "list[str]":
        seen: "list[str]" = []
        for segment in self.segments:
            for motion in segment.motions:
                if motion.name not in seen:
                    seen.append(motion.name)
        return seen

    def report(self) -> "list[dict]":
        """The shot report: ``[{start_time, end_time, basic_movement: [...]}, ...]``."""
        return ordered_report([segment.to_dict() for segment in self.segments])

    def to_dict(self) -> dict:
        return {
            "compound": bool(self.compound),
            "duration_seconds": round(self.duration_seconds, 6),
            "fps": self.fps,
            "frame_start": self.frame_start,
            "frame_end": self.frame_end,
            "frame_count": self.frame_count,
            "segment_count": len(self.segments),
            "max_simultaneous": max((len(s.motions) for s in self.segments), default=0),
            "seed": int(self.seed),
            "template_source": self.source,
            "segments": [
                {
                    "index": segment.index,
                    "start_time": round(segment.start_time, 6),
                    "end_time": round(segment.end_time, 6),
                    "start_frame": segment.start_frame,
                    "end_frame": segment.end_frame,
                    "motions": [motion.name for motion in segment.motions],
                }
                for segment in self.segments
            ],
            "shot_report": self.report(),
            "notes": list(self.notes),
        }


def frame_bounds(duration_seconds: float, fps: float, *, frame_start: int = 0) -> "tuple[int, int]":
    """The frame range a plan of *duration_seconds* occupies."""
    frames = max(1, int(round(float(duration_seconds) * float(fps))))
    return int(frame_start), int(frame_start) + frames - 1


def segment_boundaries(duration_seconds: float, count: int, fps: float,
                       *, minimum: float = MIN_SEGMENT_SECONDS) -> "list[tuple[float, float]]":
    """Split *duration_seconds* into contiguous, near-equal windows.

    The count is capped by the ``minimum`` (0.5 s) floor, so this is the one place
    that decides how many segments a video of a given length can have.  Boundaries
    are snapped to whole frames, so the times in the shot report are exactly what
    the video shows; every window is at least one frame long.
    """
    count = max_segments_for(duration_seconds, minimum=minimum, requested=count)
    total_frames = max(count, int(round(float(duration_seconds) * float(fps))))
    base, remainder = divmod(total_frames, count)
    windows: "list[tuple[float, float]]" = []
    cursor = 0
    for index in range(count):
        length = max(1, base + (1 if index < remainder else 0))
        windows.append((cursor / fps, (cursor + length) / fps))
        cursor += length
    return windows


def max_segments_for(duration_seconds: float, *, minimum: float = MIN_SEGMENT_SECONDS,
                     requested: int = 1) -> int:
    """How many segments fit: ``requested``, capped by the 0.5 s floor."""
    fits = max(1, int(float(duration_seconds) // float(minimum)))
    return max(1, min(int(requested), fits))


def choose_motions(atoms: Sequence[AtomicMotion], count: int,
                   rng: random.Random) -> "tuple[AtomicMotion, ...]":
    """Pick up to *count* atoms whose channels do not collide."""
    if count <= 0:
        return ()
    pool = [atom for atom in atoms if not atom.is_static and atom.moves]
    rng.shuffle(pool)
    chosen: "list[AtomicMotion]" = []
    used: "set[str]" = set()
    for atom in pool:
        if len(chosen) >= count:
            break
        if used.isdisjoint(atom.channels):
            chosen.append(atom)
            used.update(atom.channels)
    return tuple(chosen)


def plan_duration(*, mode: str = DURATION_FIXED, duration: float | None = None,
                  minimum: float | None = None, maximum: float | None = None,
                  rng: random.Random | None = None) -> float:
    """The length of one sequence: fixed, or drawn from ``[minimum, maximum]``."""
    mode = (mode or DURATION_FIXED).strip().lower()
    if mode not in DURATION_MODES:
        raise ConfigError(
            f"composite duration mode must be one of {DURATION_MODES}, got {mode!r}"
        )
    if mode == DURATION_FIXED:
        seconds = float(duration if duration is not None else DEFAULT_DURATION_SECONDS)
    else:
        low = float(minimum if minimum is not None else 2.0)
        high = float(maximum if maximum is not None else 6.0)
        if high < low:
            low, high = high, low
        seconds = (rng or random.Random(0)).uniform(low, high)
    return max(MIN_SEGMENT_SECONDS, round(seconds, 6))


def consecutive_seed(seed: int, index: int) -> int:
    """A stable per-sequence seed: the same run reproduces the same plans."""
    return (int(seed) * 1_000_003 + int(index) * 7919) % (2 ** 31 - 1)


def plan_compound(
    atoms: Sequence[AtomicMotion],
    *,
    duration_seconds: float,
    fps: float,
    max_simultaneous: int = 3,
    max_segments: int = 4,
    randomize: bool = True,
    rng: random.Random | None = None,
    seed: int = 0,
    frame_start: int = 0,
    source: str = "",
) -> MotionPlan:
    """Lay out one compound shot.

    ``max_simultaneous`` and ``max_segments`` are the configured values: with
    ``randomize`` they are the *upper bounds* of the per-segment / per-video draws,
    without it every segment holds exactly ``max_simultaneous`` atoms and the video
    holds exactly ``max_segments`` segments (as many as the 0.5 s floor allows).

    Which atoms and which speeds are used is always drawn from the seeded RNG --
    a fixed layout would otherwise generate the same shot over and over.
    """
    rng = rng or random.Random(int(seed))
    fps = max(1.0, float(fps))
    if not atoms:
        raise ConfigError(
            "no atomic motions are loaded; point composite.template_path at the atomic "
            "template document (templates/atomic_motion_templates.json)"
        )
    movable = [atom for atom in atoms if not atom.is_static and atom.moves]
    if not movable:
        raise ConfigError("the atomic document has no moving atoms")

    max_simultaneous = max(1, min(int(max_simultaneous), MAX_SIMULTANEOUS_LIMIT))
    notes: "list[str]" = []
    requested_segments = max(1, int(max_segments))
    segments_fit = max_segments_for(duration_seconds, requested=requested_segments)
    if segments_fit < requested_segments:
        notes.append(
            f"{float(duration_seconds):.2f} s only fits {segments_fit} segment(s) of at "
            f"least {MIN_SEGMENT_SECONDS:g} s; {requested_segments} were requested"
        )
    count = rng.randint(1, segments_fit) if randomize else segments_fit
    windows = segment_boundaries(duration_seconds, count, fps)
    plan_start, plan_end = frame_bounds(duration_seconds, fps, frame_start=frame_start)

    segments: "list[PlanSegment]" = []
    for index, (start, end) in enumerate(windows):
        wanted = rng.randint(1, max_simultaneous) if randomize else max_simultaneous
        motions = choose_motions(movable, wanted, rng)
        start_frame = int(round(start * fps))
        end_frame = max(start_frame, int(round(end * fps)) - 1)
        segments.append(PlanSegment(
            index=index,
            start_time=start,
            end_time=end,
            start_frame=start_frame,
            end_frame=end_frame,
            motions=motions,
        ))
    return MotionPlan(
        duration_seconds=float(duration_seconds),
        fps=fps,
        frame_start=plan_start,
        frame_end=plan_end,
        segments=tuple(segments),
        compound=True,
        seed=int(seed),
        source=source,
        notes=notes,
    )


def plan_single(atom: AtomicMotion, *, duration_seconds: float, fps: float,
                frame_start: int = 0, source: str = "", seed: int = 0) -> MotionPlan:
    """One atom running for the whole video (the non-compound shot of a run)."""
    fps = max(1.0, float(fps))
    start, end = frame_bounds(duration_seconds, fps, frame_start=frame_start)
    segment = PlanSegment(
        index=0,
        start_time=start / fps,
        end_time=(end + 1) / fps,
        start_frame=start,
        end_frame=end,
        motions=(atom,) if atom.moves else (),
    )
    return MotionPlan(
        duration_seconds=float(duration_seconds),
        fps=fps,
        frame_start=start,
        frame_end=end,
        segments=(segment,),
        compound=False,
        seed=int(seed),
        source=source,
    )


# --------------------------------------------------------------------------
# flattening
# --------------------------------------------------------------------------
def flatten_plan(plan: MotionPlan, *, base_focal: float | None = None) -> MotionTemplate:
    """Turn a plan into one ordinary per-frame template.

    Offsets and rotations are camera-local (``[right, up, back]`` metres and
    ``[rx, ry, rz]`` degrees), accumulated across segments: a motion that has
    finished keeps the pose it reached, and the next segment's atoms add to it.
    Focals are only written when the plan zooms -- otherwise the camera's own lens
    (including whatever the camera search settled on) is kept.
    """
    fps = max(1.0, float(plan.fps))
    keys: "list[TemplateKeyframe]" = []
    has_zoom = any(
        abs(motion.rate_focal) > 1e-12
        for segment in plan.segments for motion in segment.motions
    )
    for frame in range(plan.frame_start, plan.frame_end + 1):
        # Sampled at the *end* of each frame's exposure, so a segment's whole
        # ``rate x duration`` lands on the video: a 3 s "pan left, medium" ends at
        # exactly 54 deg (18 deg/s), which is what the shot report claims.
        time = (frame - plan.frame_start + 1) / fps
        location = [0.0, 0.0, 0.0]
        rotation = [0.0, 0.0, 0.0]
        focal_delta = 0.0
        for segment in plan.segments:
            active = min(time, segment.end_time) - segment.start_time
            if active <= 0.0:
                continue
            for motion in segment.motions:
                for axis in range(3):
                    location[axis] += motion.rate_location[axis] * active
                    rotation[axis] += motion.rate_rotation[axis] * active
                focal_delta += motion.rate_focal * active
        focal = None
        if has_zoom:
            focal = min(FOCAL_MAX_MM, max(FOCAL_MIN_MM, float(base_focal or 35.0) + focal_delta))
        keys.append(TemplateKeyframe(
            frame=frame,
            location=tuple(round(v, 9) for v in location),
            rotation=tuple(round(v, 9) for v in rotation),
            focal=focal,
        ))
    if plan.compound:
        name = COMPOUND_MOTION_NAME
    elif plan.segments and plan.segments[0].motions:
        name = plan.segments[0].motions[0].name
    else:
        name = "static"
    template = MotionTemplate(
        name=name,
        keyframes=keys,
        description=describe_plan(plan),
        source=plan.source,
        parameters={"compound": plan.to_dict()},
    )
    template.validate()
    return template


#: Field order of a shot-report entry and of a movement inside it.  Documented
#: because consumers key on it; :func:`ordered_report` rebuilds a report that came
#: back through JSON (where the order may have been sorted away) into exactly this.
REPORT_ENTRY_KEYS = ("start_time", "end_time", "basic_movement")
REPORT_MOVEMENT_KEYS = ("type", "direction", "speed")


def ordered_report(entries) -> "list[dict]":
    """Rebuild a shot report with the documented field order.

    ``[{"start_time": .., "end_time": ..,
        "basic_movement": [{"type": .., "direction": .., "speed": ..}, ...]}, ...]``
    """
    report: "list[dict]" = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        movements = []
        for movement in entry.get("basic_movement") or []:
            if not isinstance(movement, dict):
                continue
            movements.append({key: movement.get(key) for key in REPORT_MOVEMENT_KEYS})
        report.append({
            "start_time": entry.get("start_time"),
            "end_time": entry.get("end_time"),
            "basic_movement": movements,
        })
    return report


def describe_plan(plan: MotionPlan) -> str:
    """One line for logs, the panel and the manifest."""
    if not plan.compound:
        motion = plan.segments[0].motions[0] if plan.segments and plan.segments[0].motions else None
        return f"{motion.name if motion else 'static'} for {plan.duration_seconds:.2f} s"
    parts = []
    for segment in plan.segments:
        names = "+".join(motion.name for motion in segment.motions) or "static"
        parts.append(f"{segment.start_time:.2f}-{segment.end_time:.2f}s {names}")
    widest = max((len(s.motions) for s in plan.segments), default=0)
    return (f"{len(plan.segments)} segment(s), up to {widest} at once, "
            f"{plan.duration_seconds:.2f} s: " + " | ".join(parts))


def summarize_plan(plan: MotionPlan) -> dict:
    """Compact plan summary for the panel / dry run."""
    return {
        "duration_seconds": round(plan.duration_seconds, 3),
        "segments": len(plan.segments),
        "max_simultaneous": max((len(s.motions) for s in plan.segments), default=0),
        "atoms": plan.atom_names,
        "line": describe_plan(plan),
    }
