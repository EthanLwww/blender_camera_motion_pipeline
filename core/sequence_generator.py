"""Per-(camera, motion, character) sequence generation.

For every combination the generator:

1. rebuilds a clean scene context (original camera state + geometry);
2. builds the candidate animation from the motion template;
3. validates it -- including the spherical camera search when validation fails;
4. bakes the winner into the scene as per-frame keyframes on a *copy* of the
   camera data block, so the artist's original camera animation survives;
5. writes ``sequence.blend``, ``sequence_config.json``, the JSON/TXT camera
   artifacts, a validation report and a generation log;
6. restores the scene so the next combination starts from a pristine state.

A failure in any step is recorded and the loop continues: one bad template must
never cost the rest of the batch.
"""

from __future__ import annotations

import copy
import math
import os
import random
import time
import zlib
import traceback
from dataclasses import dataclass, field
from typing import Sequence

from ..camera.camera_export import (
    SequenceMetadata,
    build_trajectory_rows,
    camera_intrinsics,
    write_trajectory_txt,
)
from ..camera.camera_search import CameraSearch, SearchResult, apply_candidate
from ..camera.camera_validator import CameraValidator, ValidationReport, validate_camera_static
from ..camera.motion_composite import (
    COMPOUND_MOTION_NAME,
    consecutive_seed,
    flatten_plan,
    load_atomic_library,
    plan_compound,
    plan_duration,
    plan_single,
)
from ..camera.motion_templates import (
    MotionAnimation,
    MotionTemplate,
    MotionTemplateGenerator,
    TemplateUnitScale,
    matrix_to_quaternion,
    quaternion_to_matrix,
)
from ..camera.scene_context import CameraSnapshot, CharacterBox
from ..character.base_provider import (
    CharacterDescriptor,
    CharacterPlacement,
    CharacterProvider,
)
from ..config.models import (
    BatchConfig,
)
from ..io.json_io import save_json_file
from ..io.manifest import ManifestWriter, utc_now_iso
from ..io.path_utils import (
    ensure_dir,
    normalize_path,
    relative_to,
    safe_filename,
    sanitize_relpath,
    to_forward_slashes,
)
from ..utils.logging_utils import RunLogger, get_logger
from ..utils.animation import set_interpolation
from ..utils.task_control import TaskController
from ..utils.version import GENERATOR_VERSION, generator_stamp
from . import blender_context as bctx
from . import focus as focus_objects
from .camera_animation import PAYLOAD_KEY, build_payload, payload_summary, sample_to_dict
from .scene_loader import SceneEntry, scene_name_for


# --------------------------------------------------------------------------
# small row-major 4x4 helpers (pure python: usable without ``bpy``, testable)
# --------------------------------------------------------------------------

#: How many keys the compact ``motion.keyframes`` summary keeps.
MOTION_SUMMARY_KEYS = 13


def _compact_keyframes(keyframes, limit: int = MOTION_SUMMARY_KEYS) -> "list[dict]":
    """First/last plus evenly spaced keys: enough to eyeball a motion, not to replay it."""
    rows = [kf.to_dict() for kf in keyframes]
    if len(rows) <= limit:
        return rows
    step = (len(rows) - 1) / float(limit - 1)
    picked = sorted({int(round(i * step)) for i in range(limit)})
    return [rows[index] for index in picked]

def _identity_4x4() -> "list[list[float]]":
    return [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _matmul(a: Sequence[Sequence[float]], b: Sequence[Sequence[float]]) -> "list[list[float]]":
    return [
        [sum(float(a[i][k]) * float(b[k][j]) for k in range(4)) for j in range(4)]
        for i in range(4)
    ]


def _invert_4x4(matrix: Sequence[Sequence[float]]):
    """Gauss-Jordan inverse of a row-major 4x4; ``None`` when singular."""
    rows = [[float(value) for value in row] for row in matrix]
    inverse = _identity_4x4()
    for column in range(4):
        pivot_row = max(range(column, 4), key=lambda r: abs(rows[r][column]))
        if abs(rows[pivot_row][column]) < 1e-12:
            return None
        if pivot_row != column:
            rows[column], rows[pivot_row] = rows[pivot_row], rows[column]
            inverse[column], inverse[pivot_row] = inverse[pivot_row], inverse[column]
        pivot = rows[column][column]
        rows[column] = [value / pivot for value in rows[column]]
        inverse[column] = [value / pivot for value in inverse[column]]
        for row in range(4):
            if row == column:
                continue
            factor = rows[row][column]
            if factor == 0.0:
                continue
            rows[row] = [value - factor * other for value, other in zip(rows[row], rows[column])]
            inverse[row] = [
                value - factor * other for value, other in zip(inverse[row], inverse[column])
            ]
    return inverse


def _matrix_from_pose(position: Sequence[float], quaternion: Sequence[float]) -> "list[list[float]]":
    """World-space homogeneous transform from a position and a (w, x, y, z) quaternion."""
    rotation = quaternion_to_matrix(quaternion)
    return [
        [float(rotation[0][0]), float(rotation[0][1]), float(rotation[0][2]), float(position[0])],
        [float(rotation[1][0]), float(rotation[1][1]), float(rotation[1][2]), float(position[1])],
        [float(rotation[2][0]), float(rotation[2][1]), float(rotation[2][2]), float(position[2])],
        [0.0, 0.0, 0.0, 1.0],
    ]


def assert_object_parenting(camera_obj) -> None:
    """Reject parenting modes the world-to-local bake cannot reproduce.

    ``local_basis = inverse(matrix_parent_inverse) @ inverse(parent_world) @
    world`` holds for object parenting.  Bone and vertex parenting insert an extra
    space matrix (the bone's pose matrix, the vertex weights) that this bake does
    not model, so they fail loudly rather than producing a path that ends up in
    the video, the trajectory and a validator PASS while being wrong.
    """
    parent = camera_obj.parent
    if parent is None or str(camera_obj.parent_type) == "OBJECT":
        return
    raise RuntimeError(
        f"camera {camera_obj.name!r} is {camera_obj.parent_type}-parented to "
        f"{parent.name!r}; sequence generation supports object parenting only. "
        "Clear the parent, or parent the camera to an empty that the rig drives."
    )


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------
def _is_compound(template) -> bool:
    """True when *template* was flattened from a compound plan."""
    parameters = getattr(template, "parameters", None) or {}
    block = parameters.get("compound")
    return isinstance(block, dict) and bool(block.get("compound", True))


def _is_compound_plan(plan) -> bool:
    """True when *plan* is a spatio-temporal compound (not a single-atom shot)."""
    return bool(getattr(plan, "compound", False))


def compound_parts(template) -> "list[str]":
    """The distinct atom names behind a compound plan ([] otherwise)."""
    parameters = getattr(template, "parameters", None) or {}
    block = parameters.get("compound")
    if not isinstance(block, dict):
        return []
    names: "list[str]" = []
    for segment in block.get("segments") or []:
        for name in segment.get("motions") or []:
            if name not in names:
                names.append(str(name))
    return names


def _matrix_basis(matrix) -> "tuple[float, ...]":
    """Row-major 3x3 of a 4x4 world matrix, i.e. the camera's own axes."""
    return tuple(float(matrix[row][column]) for row in range(3) for column in range(3))


def _region_line(info: dict) -> str:
    """One log line saying whether the path stayed inside the box."""
    return (
        f"{info.get('mode', '?')}: {'inside' if info.get('ok') else 'OUTSIDE'} the box "
        f"(stage {info.get('stage')}, {info.get('attempts', 0)} attempt(s), "
        f"{info.get('exit_frames', 0)}/{info.get('frames', 0)} frame(s) out, "
        f"worst excess {float(info.get('max_excess_m') or 0.0):.2f} m)"
    )


def _focus_block(result) -> dict:
    """The part of the focus record a render node needs, or ``{}`` when there is none.

    Numbers and names only, the same way the region block works: the renderer switches
    the staged copy's focus objects on from ``objects`` alone, and a sequence *without*
    a subject carries ``{}`` so it hides every focus object instead of leaving the
    previous sequence's subject standing there.
    """
    record = getattr(result, "focus", None) or {}
    placement = record.get("placement") or {}
    objects = list(placement.get("objects") or [])
    if not objects:
        return {}
    block = {
        "object": record.get("object") or placement.get("id") or "",
        "objects": objects,
        "anchor": [float(v) for v in (placement.get("anchor") or [])],
        "center": [float(v) for v in (placement.get("center") or [])],
        "model_path": placement.get("model_path") or "",
        "label": placement.get("label") or "",
    }
    visibility = record.get("visibility") or {}
    if visibility:
        block["visibility"] = {
            "ok": bool(visibility.get("ok")),
            "visible_ratio": visibility.get("visible_ratio"),
            "visible_frames": visibility.get("visible_frames"),
            "frames": visibility.get("frames"),
        }
    orbit = record.get("orbit") or {}
    if orbit.get("ok"):
        block["orbit"] = {
            "radius_m": orbit.get("radius_m"),
            "sweep_deg": orbit.get("sweep_deg"),
            "direction": orbit.get("direction"),
        }
        # When the circle had to be replayed at another distance the record says so:
        # ``radius_m`` alone cannot be read as "this is the distance the camera was at",
        # because the room may not have allowed it.
        if orbit.get("radius_natural_m") is not None:
            block["orbit"]["radius_natural_m"] = orbit.get("radius_natural_m")
        if orbit.get("radius_source"):
            block["orbit"]["radius_source"] = orbit.get("radius_source")
        attempts = orbit.get("radius_attempts") or []
        if len(attempts) > 1:
            block["orbit"]["radius_attempts"] = [
                {"radius_m": item.get("radius_m"), "passed": bool(item.get("passed")),
                 "inside_box": bool(item.get("inside_box")),
                 "reasons": list(item.get("reasons") or [])}
                for item in attempts
            ]
    return block


@dataclass
class SequenceRequest:
    """Everything that identifies one output sequence."""

    scene_entry: SceneEntry
    scene_name: str
    motion_name: str
    template: "MotionTemplate | None"
    camera_name: str
    has_character: bool
    character: "CharacterDescriptor | None" = None
    animation: object | None = None
    character_note: str = ""
    index: int = 0
    #: Set for plan-driven shots (atomic single moves and spatio-temporal compounds):
    #: the segment layout, flattened into ``template`` once the camera's lens is
    #: known.  ``None`` for the classic "one template in, one sequence out" flow.
    plan: object | None = None
    #: Camera-movement-region outcome for this shot, filled in once the camera's world
    #: pose is known: the box that was used, whether the path stayed inside it and what
    #: the re-draw cost.  Empty when the feature is off.
    region: dict = field(default_factory=dict)
    #: Id of the focus object this sequence belongs to (``""`` when the feature is off).
    #: The object itself is already placed in the staged scene copy; the id is what
    #: makes one sequence differ from the same shot with a different subject.
    focus: str = ""

    @property
    def motion_folder(self) -> str:
        return sanitize_relpath(self.motion_name) or "motion"

    @property
    def sequence_folder(self) -> str:
        return f"sequence_{self.index:06d}"

    def output_dir(self, output_root: str) -> str:
        return os.path.join(
            normalize_path(output_root), self.scene_name, self.motion_folder, self.sequence_folder
        )

    def plain_id(self) -> str:
        return self.sequence_folder

    def key(self) -> str:
        return "|".join([
            self.scene_name, self.motion_name, self.camera_name,
            (self.character.id if self.character else "-"),
            (getattr(self.animation, "id", "") or "-"),
            "char" if self.has_character else "nochar",
        ])


@dataclass
class SequenceResult:
    """Outcome of generating one sequence."""

    request: SequenceRequest
    ok: bool = False
    sequence_id: str = ""
    output_dir: str = ""
    files: "dict[str, str]" = field(default_factory=dict)
    validation: "ValidationReport | None" = None
    search: "SearchResult | None" = None
    animation: "MotionAnimation | None" = None
    error: str = ""
    messages: "list[str]" = field(default_factory=list)
    skipped: bool = False
    elapsed_seconds: float = 0.0
    camera_used: str = ""
    character_status: str = ""
    #: What the focus-object feature did for this sequence: the object, where it stands,
    #: how the orbit was re-centred and how much of the shot it stayed in frame for.
    #: Empty when the feature is off.
    focus: dict = field(default_factory=dict)

    def to_manifest_entry(self) -> dict:
        from ..io.manifest import ManifestWriter  # noqa: F401  (typing only)

        entry = {
            "sequence_id": self.sequence_id,
            "motion_name": self.request.motion_name,
            "scene_name": self.request.scene_name,
            "camera_name": self.camera_used or self.request.camera_name,
            "has_character": bool(self.request.has_character),
            "character_name": self.request.character.id if self.request.character else "",
            "character_animation": getattr(self.request.animation, "id", "") or "",
            "character_status": self.character_status,
            "focus_object": str(self.request.focus or ""),
            "source_blend": to_forward_slashes(self.request.scene_entry.path),
            "status": "ok" if self.ok else ("skipped" if self.skipped else "failed"),
            "error": self.error,
            "validation_passed": bool(self.validation.passed) if self.validation else False,
            "validation_score": round(float(self.validation.score), 6) if self.validation else 0.0,
            "search_attempts": self.search.attempts if self.search else 0,
            "search_used": bool(self.search and self.search.passed),
            "frame_start": self.animation.frame_start if self.animation else None,
            "frame_end": self.animation.frame_end if self.animation else None,
            "frame_count": self.animation.frame_count if self.animation else 0,
            "elapsed_seconds": round(float(self.elapsed_seconds), 4),
            "files": {key: to_forward_slashes(value) for key, value in self.files.items()},
        }
        if self.validation:
            entry["validation_reasons"] = self.validation.failures
        return entry


# --------------------------------------------------------------------------
# generator
# --------------------------------------------------------------------------
class SequenceGenerator:
    """Generate sequence artifacts for one loaded scene."""

    def __init__(
        self,
        config: BatchConfig,
        *,
        output_root: str,
        character_provider: "CharacterProvider | None" = None,
        logger=None,
        task: "TaskController | None" = None,
        project_layout=None,
    ):
        self.config = config
        self.output_root = normalize_path(output_root)
        self.provider = character_provider
        self.logger = logger or get_logger("sequence_generator")
        self.task = task
        #: The project folder the sequences belong to, when generation runs
        #: through one.  It lets a sequence record *where inside the shipped
        #: project* its scene lives, so the folder can be moved to a render node.
        self.project_layout = project_layout
        self.motion = config.motion
        self.validation_config = config.validation
        self.search_config = config.search
        self.unit_scale: TemplateUnitScale = config.motion.unit_scale
        self.results: "list[SequenceResult]" = []
        self.notes: "list[str]" = []

    # -- helpers ---------------------------------------------------------
    def _tick(self, *, stage: str = "", step: int = 1) -> None:
        if self.task is not None:
            if stage:
                self.task.set_stage(stage)
            self.task.tick(step)
            self.task.check()

    def _generator(self) -> MotionTemplateGenerator:
        return MotionTemplateGenerator(
            unit_scale=self.unit_scale,
            frame_start=self.motion.frame_start,
            frame_scale=self.motion.frame_scale,
            interpolation=self.motion.interpolation,
        )

    def _scene_reference(self, entry) -> dict:
        """How a sequence records the scene its animation must be replayed onto.

        ``source_blend`` is the file the renderer opens.  Inside a project folder
        that is the copy in ``scene/``, and the *relative* form is recorded too so
        the folder keeps working after it is moved to a render node; the path the
        scene originally came from is kept for traceability.
        """
        path = normalize_path(entry.path)
        reference = {"source_blend": to_forward_slashes(path)}
        original = str(getattr(entry, "original_path", "") or "")
        if original and os.path.normcase(normalize_path(original)) != os.path.normcase(path):
            reference["source_blend_original"] = to_forward_slashes(original)
        layout = self.project_layout
        if layout is not None:
            relative = layout.relative_scene(path)
            if relative:
                reference["source_scene_rel"] = relative
                reference["project_root"] = to_forward_slashes(layout.root)
        return reference

    @staticmethod
    def camera_object(name: str):
        import bpy

        obj = bpy.data.objects.get(name)
        if obj is None or obj.type != "CAMERA":
            raise KeyError(f"camera object {name!r} is not present in the loaded file")
        return obj

    # -- public API ------------------------------------------------------
    def build_requests(
        self,
        scene_entry: SceneEntry,
        *,
        library,
        cameras: Sequence[str],
        character_variants: Sequence[tuple],
        start_index: int = 1,
        focus_variants: Sequence = (),
    ) -> "list[SequenceRequest]":
        """Expand the scene x motion x camera x character x focus-object matrix.

        Sequence numbers restart at 1 for **each motion folder**, so a
        ``scene/motion/`` directory is self-contained: its manifest, its
        sequence ids and its numbering all agree, and a partial re-run of one
        motion never renumbers another motion's sequences.

        Three shapes are possible:

        * **Plan-driven** (``composite.enabled`` and an atomic document is
          loadable): every sequence is a :class:`MotionPlan` -- one atom for a
          single-move shot, several per segment for a compound.  The plan is
          flattened into an ordinary template once the camera's lens is known.
        * **Classic**: every template in the library becomes one sequence, with the
          template's own frame range.
        * Either of the above **times the focus objects**: ``focus_models`` adds one
          axis, and the numbered folders of one motion keep counting across it, so a
          motion folder holds ``motion x camera x character x focus`` sequences with
          no per-object sub-folder.  Every sequence records which object it used.
        """
        scene_name = scene_name_for(scene_entry, self.config.batch.scene_name_mode)
        del start_index  # numbering is per motion folder, not global
        variants = list(focus_variants or ())
        if not variants:
            variants = [(None, scene_entry)]
        focus_ids = [str(getattr(model, "id", "")) if model is not None else ""
                     for model, _entry in variants]
        composite = getattr(self.config, "composite", None)
        if composite is not None and composite.enabled:
            atoms, atomic_source = load_atomic_library(
                template_path=composite.template_path,
                fps=float(self.motion.unit_scale.fps),
                logger=self.logger,
            )
            if atoms:
                return self._plan_requests(
                    scene_entry, scene_name, atoms, atomic_source,
                    cameras=cameras, character_variants=character_variants,
                    focus_variants=variants,
                )
            self.notes.append(
                "composite is enabled but no atomic motions could be loaded; "
                "falling back to the classic one-template-per-sequence flow"
            )
            if self.logger is not None:
                self.logger.warning("composite: %s", self.notes[-1])

        requests: "list[SequenceRequest]" = []
        for template in list(library):
            motion_name = safe_filename(template.name, fallback="motion")
            index = 1
            for focus_id, variant_entry in zip(focus_ids, [item[1] for item in variants]):
                for camera_name in cameras:
                    for has_character, character, animation, note in character_variants:
                        requests.append(SequenceRequest(
                            scene_entry=variant_entry,
                            scene_name=scene_name,
                            motion_name=motion_name,
                            template=template,
                            camera_name=camera_name,
                            has_character=bool(has_character),
                            character=character,
                            animation=animation,
                            character_note=note,
                            index=index,
                            focus=focus_id,
                        ))
                        index += 1
        return requests

    def _plan_requests(
        self,
        scene_entry: SceneEntry,
        scene_name: str,
        atoms,
        atomic_source: str,
        *,
        cameras: Sequence[str],
        character_variants: Sequence[tuple],
        focus_variants: Sequence = (),
    ) -> "list[SequenceRequest]":
        """Build the plan-driven requests: single-atom shots and compounds.

        Each sequence draws its own duration (fixed or from the configured range)
        and, for compounds, its own segment layout -- so a batch is a varied set of
        shots that is still reproducible from ``composite.seed``.
        """
        composite = self.config.composite
        fps = float(self.motion.unit_scale.fps)
        self._atoms = list(atoms)
        self._atomic_source = atomic_source
        requests: "list[SequenceRequest]" = []
        seed = int(composite.seed)
        variants = list(focus_variants or ())
        if not variants:
            variants = [(None, scene_entry)]
        focus_ids = [str(getattr(model, "id", "")) if model is not None else ""
                     for model, _entry in variants]

        def plan_seed(kind: str, motion_name: str, camera_name: str, slot: int,
                      attempt: int = 0) -> int:
            """A seed that depends only on *what* is being planned, never on order.

            Crunching the sequence counter (the obvious choice) coupled every plan to
            how many requests happened to be queued before it, so adding single-move
            shots silently rewrote the compounds.  Keying on
            ``(kind, motion, camera, slot)`` keeps every plan an independent random
            draw that a re-run reproduces exactly (``attempt`` only moves on when a
            duplicate plan has to be re-drawn).
            """
            key = f"{kind}|{motion_name}|{camera_name}|{slot}|{attempt}".encode("utf-8")
            return consecutive_seed(seed, zlib.crc32(key))

        def fingerprint(plan) -> str:
            return "|".join(
                f"{segment.index}:{segment.start_frame}-{segment.end_frame}:"
                + ",".join(motion.name for motion in segment.motions)
                for segment in plan.segments
            )

        #: Plans already drawn per camera, so a run never repeats a compound.
        seen_plans: "dict[str, set]" = {}

        def draw_duration(rng) -> float:
            return plan_duration(
                mode=composite.duration_mode,
                duration=composite.duration,
                minimum=composite.duration_min,
                maximum=composite.duration_max,
                rng=rng,
            )

        def add(kind: str, motion_name: str, atom=None, *, per_camera: int = 0) -> None:
            """Queue ``per_camera`` sequences for every camera.

            ``per_camera`` defaults to one sequence per character/animation variant
            (the classic matrix).  For compounds it is the configured **per-camera
            total** instead, and the variants are then *spread over* the sequences
            rather than multiplying them -- a camera with four variants and a total
            of two yields two compounds, not eight.
            """
            slots = max(1, int(per_camera) or len(character_variants))
            index = 1

            def queue(focus_id: str, camera_name: str, variant_entry) -> None:
                """Queue this camera's ``slots`` sequences for one focus object."""
                nonlocal index
                for slot in range(slots):
                    if character_variants:
                        has_character, character, animation, note = (
                            character_variants[slot % len(character_variants)]
                        )
                    else:
                        has_character, character, animation, note = (False, None, None, "")
                    # Purely random, but never a repeat: a compound is re-drawn (with
                    # the next sub-seed) while its plan collides with one this camera
                    # already has, so N sequences per camera are N *different* shots
                    # instead of a walk through the combination list.  The key carries
                    # the focus object so that the same draw is reused for every object:
                    # the subject is then the only thing that differs between otherwise
                    # identical sequences.
                    drawn: "set[str]" = seen_plans.setdefault(f"{focus_id}|{camera_name}", set())
                    for attempt in range(8):
                        sequence_seed = plan_seed(kind, motion_name, camera_name, slot, attempt)
                        rng = random.Random(sequence_seed)
                        duration = draw_duration(rng)
                        if kind == "compound":
                            plan = plan_compound(
                                atoms,
                                duration_seconds=duration,
                                fps=fps,
                                max_simultaneous=composite.max_simultaneous,
                                max_segments=composite.max_segments,
                                randomize=bool(composite.random),
                                rng=rng,
                                seed=sequence_seed,
                                frame_start=0,
                                source=atomic_source,
                            )
                        else:
                            plan = plan_single(
                                atom,
                                duration_seconds=duration,
                                fps=fps,
                                frame_start=0,
                                source=atomic_source,
                                seed=sequence_seed,
                            )
                        if kind != "compound":
                            break
                        mark = fingerprint(plan)
                        if mark not in drawn:
                            break
                    if kind == "compound":
                        # Only compounds take part in the duplicate check: a single-move
                        # shot has a plan of its own and must not shadow a compound.
                        drawn.add(mark)
                    for note_text in plan.notes:
                        if note_text not in self.notes:
                            self.notes.append(note_text)
                    requests.append(SequenceRequest(
                        scene_entry=variant_entry,
                        scene_name=scene_name,
                        motion_name=motion_name,
                        template=None,
                        camera_name=camera_name,
                        has_character=bool(has_character),
                        character=character,
                        animation=animation,
                        character_note=note,
                        index=index,
                        plan=plan,
                        focus=focus_id,
                    ))
                    index += 1

            for focus_id, variant_entry in zip(focus_ids, [item[1] for item in variants]):
                for camera_name in cameras:
                    queue(focus_id, camera_name, variant_entry)

        if composite.want_base():
            for atom in atoms:
                add("single", safe_filename(atom.name, fallback="atom"), atom=atom)
        if composite.want_compound():
            add("compound", COMPOUND_MOTION_NAME,
                per_camera=int(composite.sequences_per_camera))
        if self.logger is not None:
            compounds = sum(1 for request in requests if _is_compound_plan(request.plan))
            self.logger.info(
                "composite: %d sequence plan(s) ready (%d atom(s), %d compound(s), "
                "max_simultaneous=%d, max_segments=%d, per_camera=%d, %s, seed=%s)",
                len(requests), len(atoms), compounds, composite.max_simultaneous,
                composite.max_segments, composite.sequences_per_camera,
                "random" if composite.random else "fixed", composite.seed,
            )
        return requests

    def _region_for_scene(self, scene):
        """The camera-movement region for this scene (``None`` when the feature is off).

        Resolved once per scene and cached: the box is a property of the *set*, not of one
        shot, and re-fitting it per sequence would cost time and let two sequences of the
        same scene disagree about where the camera may go.
        """
        section = getattr(self.config, "region", None)
        if section is None or not section.enabled:
            return None
        # Imported here on purpose: ``core`` is imported by ``camera``, so a module-level
        # import would close the cycle core.region -> core.__init__ -> sequence_generator.
        from ..camera.region_source import region_spec_from_section
        cache = getattr(self, "_region_cache", None)
        if cache is None:
            cache = self._region_cache = {}
        key = str(getattr(scene, "name", "") or id(scene))
        if key not in cache:
            cache[key] = region_spec_from_section(section, scene=scene, logger=self.logger)
        return cache[key]

    @staticmethod
    def _region_info(region, report, *, ok, stage, record, margin) -> dict:
        """The ``region`` block written into ``sequence_config.json`` (plain numbers)."""
        if region is None:
            return {}
        report = report or {}
        clearance = report.get("min_clearance_m")
        return {
            "mode": str(getattr(region, "mode", "")),
            "source": str(getattr(region, "source", "")),
            "box": region.to_dict(),
            "margin": float(margin),
            "ok": bool(ok),
            "stage": str(stage),
            "attempts": int(record.get("attempts", 0) or 0),
            "segment_redraws": int(record.get("segment_redraws", 0) or 0),
            "plan_redraws": int(record.get("plan_redraws", 0) or 0),
            "speed_preferred": int(record.get("speed_preferred", 0) or 0),
            "split_rounds": int(record.get("split_rounds", 0) or 0),
            "frames": int(report.get("frames", 0) or 0),
            "exit_frames": int(report.get("exit_frames", 0) or 0),
            "max_excess_m": round(float(report.get("max_excess_m") or 0.0), 6),
            "min_clearance_m": None if clearance is None else round(float(clearance), 6),
            "worst_frame": report.get("worst_frame"),
        }

    def _fit_template_to_region(self, template, region, base_matrix, camera, *,
                                run_log=None, measure_only=False):
        """Keep a *fixed template* inside the region by scaling its amplitude.

        The counterpart of :meth:`_fit_plan_to_region` for the classic flow, where the
        template is a whole shot rather than a plan of atoms: nothing is re-drawn or
        re-ordered, the camera's offsets from its first frame are multiplied by one
        factor and the shot plays out smaller -- "follow the template, but adjust the
        amplitude to the size of the scene".  Angles and focal length are never scaled
        (they cannot leave the scene), and a shot that already fits is returned
        untouched with ``scale == 1.0``.

        ``measure_only`` is for a re-centred focus orbit: its circle is centred on the
        subject, so shrinking the offsets would slide the camera off it.  The path is
        measured and reported, and ``region.strict`` decides whether to keep it.
        """
        from ..camera.motion_templates import axis_basis, vec_add, vec_scale
        from ..core.region import (clearance_of, fit_translation_scale, region_report,
                                   with_inset)

        if region is None:
            return template, {}
        section = self.config.region
        margin = float(getattr(section, "margin", 0.0) or 0.0)
        # One box for the decision *and* for the record: the run's margin is part of
        # the rule, so a record can never say "ok: false" next to "exit_frames: 0".
        checked = with_inset(region, margin)
        position = tuple(float(v) for v in camera.location)
        basis = _matrix_basis(base_matrix)
        generator = self._generator()
        right, up, forward = axis_basis(base_matrix)
        back = vec_scale(forward, -1.0)
        offsets = []
        for key in template.keyframes:
            local = generator.local_offset(key.location)
            offsets.append(vec_add(
                vec_add(vec_scale(right, local[0]), vec_scale(up, local[1])),
                vec_scale(back, local[2]),
            ))

        start_clearance = float(clearance_of(region, position))
        if start_clearance < margin:
            info = self._region_info(
                region, region_report(checked, [position]), ok=False,
                stage="start-outside", record={"attempts": 1}, margin=margin,
            )
            info["start_clearance_m"] = round(start_clearance, 6)
            info["scale"] = 1.0
            if run_log is not None:
                run_log.log(
                    "camera region: the camera starts "
                    f"{abs(start_clearance):.2f} m outside the box, so no amount of "
                    "scaling can help (move the box, or the camera)",
                    level="WARNING",
                )
            return template, info

        if measure_only:
            scaled = [tuple(position[axis] + offset[axis] for axis in range(3))
                      for offset in offsets]
            report = region_report(checked, scaled)
            info = self._region_info(
                region, report, ok=int(report["exit_frames"]) == 0, stage="focus-orbit",
                record={"attempts": 1}, margin=margin,
            )
            info["scale"] = 1.0
            if int(report["exit_frames"]) and run_log is not None:
                run_log.log(
                    "camera region: this orbit leaves the box on "
                    f"{report['exit_frames']} frame(s) (worst "
                    f"{float(report['max_excess_m']):.2f} m).  An orbit's radius is the "
                    "camera's distance to the focus object, so the amplitude is left "
                    "alone -- move the camera closer, or widen the box",
                    level="WARNING",
                )
            return template, info

        fit = fit_translation_scale(region, position, offsets, margin=margin)
        scale = float(fit.get("scale") or 0.0)
        scaled = [tuple(position[axis] + scale * offset[axis] for axis in range(3))
                  for offset in offsets]
        # Measure against the box the fit had to respect, with the same tolerance it
        # used for its own boundary: the two numbers in the record must agree, and a
        # frame parked on the wall by the optimiser is not a frame outside the box.
        report = region_report(checked, scaled,
                               tolerance=float(fit.get("tolerance_m") or 0.0))
        # The stage says what actually happened, so a reader can tell "this shot was
        # already inside the box" from "this shot had to be shrunk to get inside it".
        if not fit.get("ok"):
            stage = "fit-failed"
        elif scale >= 0.999999:
            stage = "inside"
        else:
            stage = "fit"
        info = self._region_info(region, report, ok=bool(fit.get("ok")), stage=stage,
                                 record={"attempts": 1}, margin=margin)
        info["scale"] = round(scale, 6)
        if not fit.get("ok"):
            info["reason"] = str(fit.get("reason") or "")
            if run_log is not None:
                run_log.log(f"camera region: {info['reason']}", level="WARNING")
            return template, info
        if scale >= 0.999999:
            if run_log is not None:
                run_log.log("camera region: the template already fits the box unchanged")
            return template, info
        if run_log is not None:
            run_log.log(
                f"camera region: scaled the template's amplitude to {scale * 100:.1f}% "
                "so it stays inside the box (shape, timing, angles and focal unchanged)"
            )
        keyframes = [
            type(key)(frame=key.frame,
                      location=tuple(scale * float(value) for value in key.location),
                      rotation=key.rotation, focal=key.focal)
            for key in template.keyframes
        ]
        parameters = dict(getattr(template, "parameters", None) or {})
        parameters["region_fit"] = {"scale": round(scale, 6), "margin": margin}
        keyframes = [
            type(key)(frame=key.frame,
                      location=tuple(scale * float(value) for value in key.location),
                      rotation=key.rotation, focal=key.focal)
            for key in template.keyframes
        ]
        from dataclasses import replace

        return replace(template, keyframes=keyframes, parameters=parameters), info

    def _fit_plan_to_region(self, plan, region, base_matrix, camera, *, run_log=None):
        """Keep a plan inside *region*: re-draw it, never bend an atom.

        The camera path is evaluated analytically from the plan (no scene evaluation, no
        baking), so trying a hundred candidates costs microseconds.  A compound shot is
        re-drawn through the graded ladder; a single-atom shot has nothing to re-draw and
        is only measured.  Returns the plan to use (the original when it already fits) and
        the report to record.
        """
        if region is None:                    # feature off: the plan is used as drawn
            return plan, {}
        position = tuple(float(v) for v in camera.location)
        basis = _matrix_basis(base_matrix)
        focal = float(getattr(camera, "lens", 0.0) or 0.0)
        section = self.config.region
        margin = float(getattr(section, "margin", 0.0) or 0.0)
        from ..camera.region_planner import (  # see _region_for_scene for the cycle
            DEFAULT_LIMITS,
            draw_feasible_plan,
            plan_is_feasible,
        )
        from ..core.region import clearance_of
        limits = tuple(int(v) for v in (getattr(section, "attempts", None) or DEFAULT_LIMITS))

        # Where the camera *starts* cannot be re-drawn: if the box does not contain it, no
        # plan can ever be feasible and burning the whole ladder would only hide that.
        start_clearance = float(clearance_of(region, position))
        if start_clearance < margin:
            _ok, report = plan_is_feasible(plan, region, base_position=position,
                                           base_basis=basis, base_focal=focal, margin=margin)
            info = self._region_info(region, report, ok=False, stage="start-outside",
                                     record={"attempts": 1}, margin=margin)
            info["start_clearance_m"] = round(start_clearance, 6)
            if run_log is not None:
                run_log.log(
                    "camera region: the camera starts "
                    f"{abs(start_clearance):.2f} m outside the box, so no re-draw can help "
                    "(move the box, or the camera)",
                    level="WARNING",
                )
            return plan, info
        composite = self.config.composite
        atoms = list(getattr(self, "_atoms", []) or [])

        if not atoms or not _is_compound_plan(plan):
            ok, report = plan_is_feasible(plan, region, base_position=position,
                                          base_basis=basis, base_focal=focal, margin=margin)
            info = self._region_info(region, report, ok=ok, stage="single-atom",
                                     record={}, margin=margin)
        else:
            # The plan the batch already drew is tested *first*: switching the region on
            # must not silently re-roll shots that were fine, and it keeps the
            # duplicate-avoidance fingerprint taken at request time meaningful.
            already_ok, report = plan_is_feasible(
                plan, region, base_position=position, base_basis=basis, base_focal=focal,
                margin=margin,
            )
            if already_ok:
                info = self._region_info(region, report, ok=True, stage="L0",
                                         record={"attempts": 1}, margin=margin)
            else:
                def build_plan(seed, atoms):
                    return plan_compound(
                        atoms,
                        duration_seconds=float(plan.duration_seconds),
                        fps=float(plan.fps),
                        max_simultaneous=int(composite.max_simultaneous),
                        max_segments=int(composite.max_segments),
                        randomize=bool(composite.random),
                        rng=random.Random(int(seed)),
                        seed=int(seed),
                        frame_start=int(plan.frame_start),
                        source=str(getattr(self, "_atomic_source", "") or plan.source),
                    )

                plan, record = draw_feasible_plan(
                    build_plan, atoms, region=region, base_position=position, base_basis=basis,
                    base_focal=focal, seed=int(getattr(plan, "seed", 0) or 0),
                    max_simultaneous=int(composite.max_simultaneous), margin=margin,
                    limits=limits, slower=atoms, logger=None,
                )
                info = self._region_info(region, record.get("report"), ok=bool(record.get("ok")),
                                         stage=record.get("stage", "?"), record=record,
                                         margin=margin)

        if run_log is not None:
            run_log.log(f"camera region: {_region_line(info)}",
                        level="INFO" if info["ok"] else "WARNING")
        return plan, info

    def generate(self, request: SequenceRequest) -> SequenceResult:
        """Generate (or skip) one sequence."""
        started = time.time()
        result = SequenceResult(request=request)
        output_dir = request.output_dir(self.output_root)
        result.output_dir = output_dir
        result.sequence_id = request.plain_id()

        run_log = RunLogger(echo=self.logger)
        run_log.log(f"sequence {request.sequence_folder} | scene={request.scene_name} "
                    f"motion={request.motion_name} camera={request.camera_name} "
                    f"character={request.character.id if request.character else '-'}")

        try:
            self._generate_inner(request, result, output_dir, run_log)
        except Exception as exc:  # one bad combination must not stop the batch
            result.ok = False
            result.error = f"{type(exc).__name__}: {exc}"
            run_log.log(result.error, level="ERROR")
            run_log.log(traceback.format_exc(), level="ERROR")
            self.logger.error(
                "sequence %s failed: %s", request.sequence_folder, result.error, exc_info=True
            )
            if self.task is not None:
                self.task.note_error()
            try:
                self._write_log(run_log, output_dir)
            except Exception:
                pass
        finally:
            result.elapsed_seconds = time.time() - started
            result.messages = list(run_log.lines)
            self.results.append(result)
        return result

    # -- implementation --------------------------------------------------
    def _generate_inner(self, request, result, output_dir, run_log) -> None:
        import bpy

        # -------- incremental re-run support -----------------------------
        planned_files = self._planned_files(output_dir, request)
        if self._can_skip(planned_files, run_log):
            result.ok = True
            result.skipped = True
            result.files = planned_files
            result.camera_used = request.camera_name
            run_log.log("existing artifacts found and batch.overwrite/resume allow reuse: skipped", level="WARNING")
            self._write_log(run_log, output_dir)
            return

        ensure_dir(output_dir)
        scene = bpy.context.scene
        self._tick(stage=f"{request.motion_name}/{request.camera_name}")

        # -------- 0. deterministic anchor frame --------------------------
        # The template motion is applied to the camera's pose at the sequence's
        # *first* frame, so that frame has to be the same for every sequence in a
        # batch.  Left alone it is whatever the artist saved (frame 1 in the
        # reference scene) for the first sequence and the previous sequence's last
        # frame for the rest, which makes a template's result depend on batch
        # position -- measured on the reference scene as a 0.2 m anchor shift.
        anchor_frame = self.motion.frame_start
        if anchor_frame is None:
            anchor_frame = scene.frame_start
        try:
            scene.frame_set(int(anchor_frame))
        except Exception as exc:
            run_log.log(f"could not set the anchor frame to {anchor_frame}: {exc}", level="WARNING")

        # -------- 1. character (optional, isolated) ----------------------
        character_box: "CharacterBox | None" = None
        character_placement: "CharacterPlacement | None" = None
        if request.has_character and request.character is not None:
            character_box, character_placement, status = self._place_character(request, run_log)
            result.character_status = status
        elif request.has_character:
            result.character_status = "requested but no character descriptor was resolved"
            run_log.log(result.character_status, level="WARNING")
        else:
            result.character_status = "no character in this sequence"

        # -------- 2. camera + original state ----------------------------
        camera_obj = self.camera_object(request.camera_name)
        original = bctx.camera_snapshot(camera_obj, scene)
        static_problems = validate_camera_static(original, self.validation_config)
        for problem in static_problems:
            run_log.log(f"camera static check: {problem}", level="WARNING")

        # -------- 1b. focus object (the subject an Arc orbits) -----------
        focus_placement: "focus_objects.FocusPlacement | None" = None
        focus_visibility: dict = {}
        keep_focus_visible = bool(getattr(self.config.focus, "keep_visible", True))
        focus_visible_ratio = float(getattr(self.config.focus, "visible_ratio", 0.95))
        if getattr(request, "focus", ""):
            focus_placement = focus_objects.placement_for(scene, request.focus)
            if focus_placement is None:
                run_log.log(
                    f"focus object {request.focus!r} is not registered in this scene; the "
                    "sequence is generated with no subject in it",
                    level="WARNING",
                )
            else:
                focus_visibility = focus_objects.apply_visibility(
                    scene, focus_placement.objects
                )
                run_log.log(
                    f"focus object {focus_placement.id}: {len(focus_visibility.get('shown') or [])} "
                    f"object(s) shown at "
                    f"{[round(float(v), 3) for v in focus_placement.center]}"
                )

        exclude = list(character_placement.imported_objects) if character_placement else []
        if focus_placement is not None:
            # The subject is what the shot is *of*: it must not count as an obstacle the
            # camera has to keep clear of, or an Arc could never come near it.
            exclude.extend(focus_placement.objects)
        context = bctx.build_scene_context(
            scene=scene,
            exclude_objects=exclude,
            characters=[character_box] if character_box else [],
            cameras=[original],
            blend_path=request.scene_entry.path,
            logger=self.logger,
        )

        # -------- 3. animation + validation + search --------------------
        generator = self._generator()
        base_matrix = original.matrix_world
        base_position = original.location
        from ..camera.motion_templates import matrix_to_quaternion, quat_angle_between, quat_multiply

        base_quaternion = matrix_to_quaternion(base_matrix)

        # A plan-driven shot becomes an ordinary per-frame template here, where the
        # camera's lens is known (a zoom plan is relative to it).  Its frame range
        # is the plan's -- the video duration, not a template's own span.
        plan = getattr(request, "plan", None)
        if plan is not None:
            region = self._region_for_scene(scene)
            if region is not None:
                plan, request.region = self._fit_plan_to_region(
                    plan, region, base_matrix, original, run_log=run_log,
                )
                request.plan = plan
                if not request.region.get("ok") and bool(
                    getattr(self.config.region, "strict", False)
                ):
                    # Strict mode: reject the shot instead of shipping a camera path that
                    # leaves the box.  Nothing is written, so this is a skip, not a failure.
                    result.ok = True
                    result.skipped = True
                    result.camera_used = request.camera_name
                    run_log.log(
                        "camera region: no plan fits the box and region.strict is on; "
                        "the sequence was skipped",
                        level="WARNING",
                    )
                    self._write_log(run_log, output_dir)
                    return
            request.template = flatten_plan(plan, base_focal=original.lens)
            range_start, range_end = int(plan.frame_start), int(plan.frame_end)
            run_log.log(
                f"plan: {plan.duration_seconds:.2f} s, {len(plan.segments)} segment(s), "
                f"up to {max((len(s.motions) for s in plan.segments), default=0)} motion(s) "
                f"at once -> frames {range_start}..{range_end}"
            )
        else:
            range_start = self.motion.frame_start if self.motion.frame_end is not None else None
            range_end = self.motion.frame_end

        # -------- 3b. an Arc orbits the focus object ---------------------
        # The camera's own distance to the subject is the shot the author framed, so it
        # is tried first.  A room can be too small or too cluttered for it, though -- a
        # 7 m circle inside a 5 m bedroom drives the camera through a wall -- and an arc
        # that is *about* the subject is worth more than the exact distance it is filmed
        # from.  The same circle is therefore replayed at the nearest distance the scene
        # accepts, validated at each step, and the authored one is kept when it works.
        focus_orbit: dict = {}
        validator = None
        if self.validation_config.enabled and context.ray_caster is not None:
            validator = CameraValidator(context, self.validation_config, logger=self.logger)
        if focus_placement is not None and plan is None and request.template is not None:
            authored_template = request.template
            authored_matrix = base_matrix

            def orbit_at(radius):
                """``(template, info, base matrix, base quaternion)`` at one radius."""
                retargeted, orbit_info = focus_objects.orbit_template(
                    authored_template,
                    anchor=focus_placement.center,
                    base_position=base_position,
                    base_quaternion=base_quaternion,
                    fps=float(self.motion.unit_scale.fps),
                    rotation_order=str(self.motion.unit_scale.rotation_order or "XYZ"),
                    radius=radius,
                )
                if not orbit_info.get("ok"):
                    return retargeted, orbit_info, None, None
                # Re-centring the circle is only half of it: the camera also has to look
                # at the subject from the first frame, so the aim is folded into the base
                # pose here (the same mechanism the camera search uses for its nudge).
                adjust = tuple(orbit_info.get("rotation_adjust") or (1.0, 0.0, 0.0, 0.0))
                matrix = _matmul(_matrix_from_pose((0.0, 0.0, 0.0), adjust),
                                 [[float(v) for v in row] for row in authored_matrix])
                # The camera keeps its position: a bare matrix product would rotate it
                # about the world origin (the animation builder overwrites the
                # translation for exactly this reason, and a candidate built without
                # that correction is aimed at nothing -- measured: subject in frame
                # 0/145 for a radius whose final sequence reports 145/145).
                matrix[0][3] = float(base_position[0])
                matrix[1][3] = float(base_position[1])
                matrix[2][3] = float(base_position[2])
                return (
                    retargeted,
                    orbit_info,
                    matrix,
                    quat_multiply(adjust, base_quaternion),
                )

            radius_attempts: "list[dict]" = []
            box = self._region_for_scene(scene)
            margin = float(getattr(self.config.region, "margin", 0.0) or 0.0)
            chosen = orbit_at(None)
            if chosen[1].get("ok"):
                natural = float(chosen[1].get("radius_natural_m")
                                or chosen[1].get("radius_m") or 0.0)
                radii = focus_objects.orbit_radius_candidates(
                    natural, minimum=float(focus_objects.MIN_ORBIT_RADIUS)) or [round(natural, 6)]
                for index, radius in enumerate(radii):
                    if index == 0:
                        template_i, info_i, matrix_i, quaternion_i = chosen
                    else:
                        template_i, info_i, matrix_i, quaternion_i = orbit_at(radius)
                        if not info_i.get("ok"):
                            continue
                    if validator is None:
                        chosen = (template_i, info_i, matrix_i, quaternion_i)
                        break
                    candidate = generator.generate(
                        template_i, base_matrix=matrix_i, base_focal=original.lens,
                        base_quaternion=quaternion_i, frame_start=range_start,
                        frame_end=range_end,
                    )
                    inside_box = True
                    if box is not None:
                        from ..core.region import region_report, with_inset
                        inside_box = int(region_report(
                            with_inset(box, margin),
                            [sample.position for sample in candidate.samples],
                        )["exit_frames"]) == 0
                    candidate_report = validator.validate(
                        original, candidate, character=character_box,
                        base_matrix=matrix_i, base_focal=original.lens,
                    )
                    visible = True
                    seen = None
                    if keep_focus_visible:
                        seen = focus_objects.visibility_report(
                            candidate, focus_placement, original,
                            threshold=focus_visible_ratio,
                        )
                        visible = bool(seen.get("ok"))
                    radius_attempts.append({
                        "radius_m": round(float(radius), 4),
                        "passed": bool(candidate_report.passed and visible and inside_box),
                        "subject_visible": bool(visible),
                        "inside_box": bool(inside_box),
                        "reasons": list(candidate_report.failures),
                    })
                    run_log.log(
                        f"focus orbit: radius {radius:.2f} m -- validation "
                        + ("passed" if candidate_report.passed
                           else "failed: " + ", ".join(candidate_report.failures))
                        + (f", subject in frame {seen['visible_frames']}/{seen['frames']}"
                           if seen else "")
                        + (", inside the box" if inside_box else ", outside the box")
                    )
                    if candidate_report.passed and visible and inside_box:
                        chosen = (template_i, info_i, matrix_i, quaternion_i)
                        if index:
                            run_log.log(
                                f"focus orbit: the authored {natural:.2f} m distance does "
                                f"not survive the scene; using {radius:.2f} m instead"
                            )
                        break
                else:
                    run_log.log(
                        "focus orbit: no distance around the subject passes validation; "
                        "keeping the authored one and recording the failure",
                        level="WARNING",
                    )

            if chosen[1].get("ok"):
                # Re-centring the circle is only half of it: the camera also has to look
                # at the subject from the first frame, so the aim is folded into the base
                # pose here (the same mechanism the camera search uses for its nudge).
                request.template = chosen[0]
                base_matrix = chosen[2]
                base_quaternion = chosen[3]
                focus_orbit = dict(chosen[1])
                if len(radius_attempts) > 1:
                    focus_orbit["radius_attempts"] = radius_attempts
                run_log.log(
                    f"focus orbit: {focus_orbit['direction']} "
                    f"{float(focus_orbit['sweep_deg']):.1f} deg around {focus_placement.id!r} "
                    f"at {float(focus_orbit['radius_m']):.2f} m "
                    f"({focus_orbit.get('keys', 0)} keys; the camera was aimed "
                    f"{float(focus_orbit['base_aim_deg']):.1f} deg off its authored orientation)"
                )
            else:
                focus_orbit = dict(chosen[1])
                run_log.log(
                    f"focus object {focus_placement.id!r} does not change this motion: "
                    f"{focus_orbit.get('reason') or 'not an orbit'}",
                    level="WARNING" if focus_orbit.get("reason") else "INFO",
                )

        # -------- 3c. keep a fixed template inside the scene ------------
        # A template is a whole shot, so the region is honoured here by *scaling* its
        # amplitude -- never by re-drawing it, which is what the atomic ladder does.
        # The path keeps its shape, its timing and its angles and plays out smaller;
        # rotation-only and zoom-only shots cannot leave the scene and are untouched.
        # A re-centred focus orbit is the exception: its circle is centred on the
        # subject, so scaling would slide the camera off the subject.  There the box is
        # *measured* instead, and `region.strict` decides what to do about it.
        if plan is None and request.template is not None:
            region = self._region_for_scene(scene)
            if region is not None:
                request.template, request.region = self._fit_template_to_region(
                    request.template, region, base_matrix, original, run_log=run_log,
                    measure_only=bool(focus_orbit.get("ok")),
                )
                info = request.region or {}
                if info and not info.get("ok") and bool(
                    getattr(self.config.region, "strict", False)
                ):
                    result.ok = True
                    result.skipped = True
                    result.camera_used = request.camera_name
                    run_log.log(
                        "camera region: the template cannot be made to fit and "
                        "region.strict is on; the sequence was skipped",
                        level="WARNING",
                    )
                    self._write_log(run_log, output_dir)
                    return

        def make_animation_for(position, quaternion=None, rotation_adjust=None):
            """Animation anchored at ``position``, in the frame ``rotation_adjust`` gives.

            The search may turn the camera to keep its subject framed.  Folding that
            rotation into the **base matrix** is what keeps a template's notion of
            "forward" pointing where the camera now looks: rotating only the keyed
            quaternions afterwards left the path running along the *old* view axis,
            so an accepted candidate moved the camera sideways (measured: 76-99 deg
            off its own view axis on the user's scene).
            """
            matrix = copy.deepcopy(base_matrix)
            quaternion_result = quaternion
            if rotation_adjust is not None and quat_angle_between(
                rotation_adjust, (1.0, 0.0, 0.0, 0.0)
            ) > 1e-9:
                adjustment = _matrix_from_pose((0.0, 0.0, 0.0), rotation_adjust)
                matrix = _matmul(adjustment, [[float(v) for v in row] for row in matrix])
                if quaternion_result is None:
                    quaternion_result = quat_multiply(rotation_adjust, base_quaternion)
            matrix[0][3] = float(position[0])
            matrix[1][3] = float(position[1])
            matrix[2][3] = float(position[2])
            return generator.generate(
                request.template,
                base_matrix=matrix,
                base_focal=original.lens,
                base_quaternion=quaternion_result if quaternion_result is not None else base_quaternion,
                frame_start=range_start,
                frame_end=range_end,
            )

        animation = make_animation_for(base_position)
        report: "ValidationReport | None" = None
        search_result: "SearchResult | None" = None

        if self.validation_config.enabled and context.ray_caster is not None:
            validator = CameraValidator(context, self.validation_config, logger=self.logger)
            report = validator.validate(
                original,
                animation,
                character=character_box,
                base_matrix=base_matrix,
                base_focal=original.lens,
            )
            run_log.log(f"validation (original camera): {report.summary_line()}")
            for message in report.messages:
                run_log.log(f"  {message}", level="WARNING")

            if not report.passed and self.search_config.enabled:
                run_log.log(
                    f"original camera failed; searching {self.search_config.candidate_count} "
                    f"candidate position(s) in radius "
                    f"[{self.search_config.min_radius}, {self.search_config.max_radius}]"
                )
                search = CameraSearch(context, self.search_config, validator, logger=self.logger)

                def make_animation(candidate):
                    return make_animation_for(candidate.position, rotation_adjust=candidate.rotation_adjust)

                def subject_veto(candidate, candidate_animation):
                    """Keep the focus object in frame when the search moves the camera.

                    An Arc is *about* its subject: a candidate that fixes a grazing
                    wall by turning away from the subject would replace one bad shot
                    with a worse one, so it is refused and the original pose (and its
                    recorded failure) stands instead.
                    """
                    if focus_placement is None or not keep_focus_visible:
                        return None
                    check = focus_objects.visibility_report(
                        candidate_animation, focus_placement, original,
                        threshold=focus_visible_ratio, step=4,
                    )
                    if check.get("ok"):
                        return None
                    return (
                        f"focus_object_lost: {check['visible_frames']}/{check['frames']} "
                        f"frame(s) show {focus_placement.id!r}"
                    )

                search_result = search.search(
                    original,
                    make_animation,
                    base_position=base_position,
                    base_quaternion=base_quaternion,
                    base_focal=original.lens,
                    character=character_box,
                    base_matrix=base_matrix,
                    original_report=report,
                    # Only an Arc is *about* the subject: in every other motion the focus
                    # object merely exists in the scene ("the object only exists, it does
                    # not take part"), so a search there must not be held to keeping it in
                    # frame -- that would fail shots the user never asked to be of it.
                    veto=subject_veto if (focus_placement is not None
                                          and focus_orbit.get("ok")) else None,
                )
                for message in search_result.messages:
                    run_log.log(f"  search: {message}")
                if search_result.passed and search_result.accepted:
                    winner = search_result.accepted[0]
                    best = next(
                        (e for e in search_result.evaluations if e.candidate is winner), None
                    )
                    animation = apply_candidate(
                        make_animation_for(winner.position, rotation_adjust=winner.rotation_adjust),
                        winner,
                    )
                    if best is not None:
                        report = best.report
                    run_log.log(f"camera search accepted: {winner.describe()}")
                else:
                    vetoed = sum(1 for e in search_result.evaluations if e.rejected_reason)
                    if vetoed:
                        run_log.log(
                            f"camera search: {vetoed} candidate(s) passed the geometry checks "
                            "but lost the focus object and were refused",
                            level="WARNING",
                        )
                    run_log.log(
                        "camera search found no acceptable candidate; recording the failure "
                        "instead of emitting a knowingly bad sequence",
                        level="ERROR",
                    )
            elif not report.passed:
                run_log.log(
                    "validation failed and the camera search is disabled "
                    "(search.enabled = false); recording the failure rather than emitting a "
                    "knowingly bad sequence",
                    level="ERROR",
                )

            # ---------------------------------------------------------------
            # Emission gate: a failing validation must never produce a
            # "successful" sequence.  This is deliberately *outside* the search
            # branch -- an earlier version nested it inside, which silently let
            # an unvalidated sequence through whenever the search was disabled.
            # ---------------------------------------------------------------
            if not report.passed:
                result.search = search_result
                result.validation = report
                result.animation = animation
                result.error = "validation_failed: " + ", ".join(report.failures)
                result.ok = False
                if self.task is not None:
                    self.task.note_error()
                self._write_failure_artifacts(
                    request, result, output_dir, run_log, report, animation, search_result
                )
                return
        elif self.validation_config.enabled:
            run_log.log(
                "geometry validation skipped: no ray caster is available for this scene",
                level="WARNING",
            )
        else:
            run_log.log("geometry validation disabled by configuration")

        # -------- 3c. did the subject stay in frame? ---------------------
        focus_record: dict = {}
        if focus_placement is not None:
            focus_record = {
                "object": focus_placement.id,
                "placement": focus_placement.to_dict(),
                "orbit": dict(focus_orbit or {}),
                "shown": list(focus_visibility.get("shown") or []),
            }
            if bool(getattr(self.config.focus, "keep_visible", True)):
                visibility = focus_objects.visibility_report(
                    animation, focus_placement, original,
                    threshold=float(getattr(self.config.focus, "visible_ratio", 0.95)),
                )
                focus_record["visibility"] = visibility
                run_log.log(
                    f"focus object {focus_placement.id!r}: in frame for "
                    f"{visibility['visible_frames']}/{visibility['frames']} frame(s) "
                    f"({float(visibility['visible_ratio']) * 100:.1f}%), nearest approach "
                    f"{float(visibility['min_distance_m']):.2f} m"
                )
                if not visibility["ok"] and focus_orbit.get("ok"):
                    reason = (
                        f"the focus object {focus_placement.id!r} is out of frame: "
                        f"{visibility['visible_frames']}/{visibility['frames']} frame(s) "
                        f"visible, needing "
                        f"{float(visibility['threshold']) * 100:.0f}%"
                    )
                    run_log.log(reason, level="WARNING")
                    if bool(getattr(self.config.focus, "strict", False)):
                        # Same contract as region.strict: a shot whose subject is not in
                        # frame is not written at all, and that is a skip, not a failure.
                        result.ok = True
                        result.skipped = True
                        result.camera_used = request.camera_name
                        result.focus = focus_record
                        run_log.log("focus.strict is on; the sequence was skipped",
                                    level="WARNING")
                        self._write_log(run_log, output_dir)
                        return
                    focus_record["message"] = reason
        result.focus = focus_record

        result.validation = report
        result.search = search_result
        result.animation = animation

        # -------- 4. bake keyframes -------------------------------------
        camera_data_copy, animation_payload = self._apply_animation(camera_obj, animation, run_log)
        result.camera_used = camera_obj.name

        # -------- 5. persist artifacts ----------------------------------
        sequence_id = request.sequence_folder
        files = self._write_artifacts(
            request, result, output_dir, run_log,
            camera=original, animation=animation, report=report,
            search_result=search_result, scene=scene,
            character_placement=character_placement, character_status=result.character_status,
            animation_payload=animation_payload,
        )
        result.files = files

        # -------- 6. restore --------------------------------------------
        self._restore(camera_obj, original, camera_data_copy, run_log)
        result.ok = True
        run_log.log(f"sequence {sequence_id} completed in {result.elapsed_seconds:.2f}s")
        self._write_log(run_log, output_dir)

    # -- step helpers ----------------------------------------------------
    def _planned_files(self, output_dir: str, request: SequenceRequest) -> "dict[str, str]":
        sequence_id = request.sequence_folder
        files = {
            "sequence_config": os.path.join(output_dir, "sequence_config.json"),
            "metadata": os.path.join(output_dir, f"{sequence_id}.json"),
            "camera_trajectory": os.path.join(output_dir, f"{sequence_id}_camera.txt"),
            "generation_log": os.path.join(output_dir, "generation_log.txt"),
        }
        if self.config.batch.save_validation_report:
            files["validation_report"] = os.path.join(output_dir, "validation_report.json")
        return files

    def _can_skip(self, planned: "dict[str, str]", run_log: RunLogger) -> bool:
        """Reuse an existing sequence only when every artifact it needs is present.

        The sidecar carries the animation the renderer replays, so it and the
        trajectory are the deliverables -- their absence always means "regenerate".
        """
        if self.config.batch.overwrite or not self.config.batch.resume:
            return False
        required = ["sequence_config", "metadata", "camera_trajectory"]
        return all(os.path.isfile(planned[key]) for key in required if key in planned)

    def _place_character(self, request: SequenceRequest, run_log: RunLogger):
        """Import/place/animate the character, returning a box when successful."""
        assert self.provider is not None
        provider = self.provider
        if not provider.is_usable():
            message = (
                f"character sequence requested but provider {provider.name!r} is "
                f"{provider.status()}; generating the character-free variant instead"
            )
            run_log.log(message, level="WARNING")
            return None, None, f"unavailable: {provider.status()}"

        scene_context = bctx.build_scene_context(
            scene=None, ray_caster="bpy", logger=self.logger
        )
        placement = provider.import_character(scene_context, request.character)
        if not placement.ok:
            run_log.log(
                f"character import failed: {'; '.join(placement.errors or placement.messages) or 'unknown reason'}",
                level="ERROR",
            )
            return None, placement, f"import_failed: {placement.status}"
        for message in placement.messages:
            run_log.log(f"character: {message}")
        placement = provider.place_character(placement, scene_context)
        if request.animation is not None:
            placement = provider.apply_animation(placement, request.animation)
            for message in placement.messages[-2:]:
                run_log.log(f"character animation: {message}")
        validation = provider.validate_character_placement(placement, scene_context)
        for message in validation.messages:
            run_log.log(
                f"character placement: {message}",
                level="WARNING" if validation.valid else "ERROR",
            )
        run_log.log(
            f"character {placement.descriptor_id} placed as {placement.object_name!r} "
            f"(status={placement.status}, method={placement.placement_method or 'n/a'})"
        )
        if not placement.ok:
            return None, placement, f"placement_failed: {placement.status}"

        # Rebuild the box from the *current* geometry so validation sees the
        # character where it actually ended up.
        box = placement.to_character_box()
        if box is not None:
            refreshed = self._measure_character(placement)
            if refreshed is not None:
                box = refreshed
        return box, placement, placement.status

    def _measure_character(self, placement: CharacterPlacement) -> "CharacterBox | None":
        import bpy

        root = bpy.data.objects.get(placement.object_name)
        if root is None:
            return None
        lo = [math.inf] * 3
        hi = [-math.inf] * 3
        found = False
        from ..character.blender_provider import _descendants

        for obj in [root, *_descendants(root)]:
            if obj.type != "MESH":
                continue
            snapshot = bctx.mesh_snapshot_safe(obj)
            if snapshot is None:
                continue
            for axis in range(3):
                lo[axis] = min(lo[axis], snapshot.bbox_min[axis])
                hi[axis] = max(hi[axis], snapshot.bbox_max[axis])
            found = True
        if not found:
            return placement.to_character_box()
        return CharacterBox(
            name=placement.descriptor_id or placement.object_name,
            bbox_min=tuple(lo),  # type: ignore[arg-type]
            bbox_max=tuple(hi),  # type: ignore[arg-type]
            object_name=placement.object_name,
            animation=placement.animation,
        )

    def _apply_animation(self, camera_obj, animation: MotionAnimation, run_log: RunLogger):
        """Key every validated frame onto the camera; returns the data block copy.

        The generator produces **world-space** poses (that is what the validator
        checked, and what the trajectory records).  ``obj.location`` however is
        expressed in the object's *parent* space, so a parented camera needs the
        pose converted before it is keyed.  Writing world coordinates straight
        into ``location`` double-counts the parent's transform -- on the reference
        scene (camera parented to an animated train) that displaced the rendered
        camera by 26 m at frame 0 and 110 m by frame 80, and it silently discarded
        the camera's own ride.

        Constraints with a non-zero influence are muted for the bake and left
        muted, because they would otherwise overwrite the keyed rotation at
        evaluation time and the rendered motion would not match the validated one.
        """
        import bpy

        assert_object_parenting(camera_obj)

        scene = bpy.context.scene
        # Keep the artist's original animation intact: work on a copy of the
        # camera *data* block and clear animation on the new one only.
        data_copy = camera_obj.data.copy()
        data_copy.name = f"{camera_obj.data.name}_seq{animation.template_name}"[:63]
        camera_obj.data = data_copy
        bctx.clear_camera_animation(camera_obj)

        run_log.log(
            f"applying {animation.template_name}: frames {animation.frame_start}..{animation.frame_end} "
            f"({animation.frame_count} samples), interpolation={animation.interpolation}"
        )
        run_log.log(f"camera data block replaced with independent copy {data_copy.name!r}")

        muted: "list[str]" = []
        for constraint in camera_obj.constraints:
            if getattr(constraint, "mute", False):
                continue
            try:
                influence = float(getattr(constraint, "influence", 0.0))
            except Exception:
                influence = 1.0
            if influence > 1e-6:
                constraint.mute = True
                muted.append(f"{constraint.name}({constraint.type})")
        if muted:
            run_log.log(
                "muted camera constraint(s) for the bake, because they would "
                "override the generated rotation: " + ", ".join(muted),
                level="WARNING",
            )

        parent = camera_obj.parent
        conversion = self._local_bake_helper(camera_obj, parent)
        run_log.log(
            "camera is unparented: baking world poses directly"
            if parent is None else
            f"camera is parented to {parent.name!r} (type={camera_obj.parent_type}): "
            "converting each world pose into the parent's space before keying"
        )

        # Every keyed value is recorded as it is written, so a sequence can be
        # replayed onto the source scene without re-deriving anything (see
        # ``core/camera_animation.py``).
        keyed: "list[dict]" = []
        for sample in animation.samples:
            location, quaternion = conversion(sample)
            camera_obj.location = location
            camera_obj.rotation_mode = "QUATERNION"
            camera_obj.rotation_quaternion = quaternion
            data_copy.lens = max(1.0, float(sample.focal))
            camera_obj.keyframe_insert(data_path="location", frame=sample.frame, group="motion_pipeline")
            camera_obj.keyframe_insert(data_path="rotation_quaternion", frame=sample.frame, group="motion_pipeline")
            camera_obj.keyframe_insert(data_path="scale", frame=sample.frame, group="motion_pipeline")
            data_copy.keyframe_insert(data_path="lens", frame=sample.frame, group="motion_pipeline")
            keyed.append(sample_to_dict(
                sample.frame,
                camera_obj.location,
                camera_obj.rotation_quaternion,
                camera_obj.scale,
                data_copy.lens,
            ))

        for owner in (camera_obj, data_copy):
            animation_data = getattr(owner, "animation_data", None)
            if animation_data is None or animation_data.action is None:
                continue
            action = animation_data.action
            keys = set_interpolation(action, self._blender_interpolation())
            run_log.log(f"set {keys} keyframe(s) to {self._blender_interpolation()} in {action.name!r}")
            try:
                action.name = f"MP_{animation.template_name}_{camera_obj.name}"[:63]
            except Exception:
                pass

        scene.frame_start = int(animation.frame_start)
        scene.frame_end = int(animation.frame_end)
        scene.render.fps = int(round(animation.fps)) or scene.render.fps
        scene.render.fps_base = 1.0
        scene.frame_set(int(animation.frame_start))
        bpy.context.view_layer.update()
        return data_copy, build_payload(
            object_name=camera_obj.name,
            data_name=data_copy.name,
            rotation_mode=str(camera_obj.rotation_mode),
            interpolation=self._blender_interpolation(),
            samples=keyed,
            scene_name=bpy.context.scene.name,
            parent_name=getattr(parent, "name", "") or "",
            parent_type=str(camera_obj.parent_type),
            muted_constraints=[entry.split("(")[0] for entry in muted],
        )

    def _local_bake_helper(self, camera_obj, parent):
        """Return ``sample -> (local_location, local_quaternion)`` for keying.

        Blender evaluates a parented object as::

            world = parent_world @ matrix_parent_inverse @ local_basis

        so the local basis that reproduces a desired world pose is::

            local_basis = inverse(matrix_parent_inverse)
                        @ inverse(parent_world)
                        @ world_pose

        The parent's evaluated world matrix is read fresh per frame, because the
        parent's own animation is exactly what carries the camera through the
        scene.  Without a parent the pose is keyed as-is.
        """
        import bpy

        scene = bpy.context.scene
        parent_inverse_inverse = None
        if parent is not None:
            stored = [[float(v) for v in row] for row in camera_obj.matrix_parent_inverse]
            parent_inverse_inverse = _invert_4x4(stored)

        def convert(sample):
            if parent is None:
                return tuple(sample.position), tuple(sample.quaternion)
            scene.frame_set(int(sample.frame))
            depsgraph = bpy.context.evaluated_depsgraph_get()
            parent_world = [
                [float(v) for v in row]
                for row in parent.evaluated_get(depsgraph).matrix_world
            ]
            parent_world_inverse = _invert_4x4(parent_world)
            if parent_world_inverse is None:
                raise RuntimeError(
                    f"parent {parent.name!r} has a singular world matrix at frame {sample.frame}"
                )
            prefix = (
                parent_world_inverse
                if parent_inverse_inverse is None
                else _matmul(parent_inverse_inverse, parent_world_inverse)
            )
            local_matrix = _matmul(prefix, _matrix_from_pose(sample.position, sample.quaternion))
            return (
                (local_matrix[0][3], local_matrix[1][3], local_matrix[2][3]),
                matrix_to_quaternion(local_matrix),
            )

        return convert

    def _sequence_resolution(self, camera: CameraSnapshot) -> "tuple[int, int]":
        """The size this sequence is meant to be rendered at.

        ``render.resolution_explicit`` decides: when the user set a sequence
        resolution before generating, that wins and the renderer obeys it; otherwise
        the sequence follows the source scene (the historical behaviour, where a
        2000x2000 scene produced 2000x2000 videos no matter what the panel said).
        """
        render = self.config.render
        if render.resolution_explicit:
            factor = max(1, int(render.resolution_percentage)) / 100.0
            return (
                int(round(int(render.resolution_x) * factor)),
                int(round(int(render.resolution_y) * factor)),
            )
        return camera.effective_resolution

    def _blender_interpolation(self) -> str:
        mapping = {"LINEAR": "LINEAR", "BEZIER": "BEZIER", "CONSTANT": "CONSTANT"}
        return mapping.get(str(self.motion.interpolation).upper(), "BEZIER")

    def _restore(self, camera_obj, original: CameraSnapshot, data_copy, run_log: RunLogger) -> None:
        """Put the scene back so the next combination starts clean."""
        import bpy

        previous_data = camera_obj.data
        try:
            bctx.clear_camera_animation(camera_obj)
            bctx.restore_camera(camera_obj, original)
            if previous_data is not None and previous_data.users == 0:
                bpy.data.cameras.remove(previous_data)
        except Exception as exc:
            run_log.log(f"camera restore warning: {exc}", level="WARNING")
        try:
            bpy.context.view_layer.update()
        except Exception:
            pass
        del data_copy

    def _write_failure_artifacts(
        self, request, result, output_dir, run_log, report, animation, search_result
    ) -> None:
        """Persist whatever we learned about a failing combination."""
        ensure_dir(output_dir)
        payload = {
            "sequence_id": request.sequence_folder,
            "status": "failed",
            "error": result.error,
            "scene_name": request.scene_name,
            "motion_name": request.motion_name,
            "camera_name": request.camera_name,
            "has_character": bool(request.has_character),
            "character_name": request.character.id if request.character else "",
            "character_animation": getattr(request.animation, "id", "") or "",
            **self._scene_reference(request.scene_entry),
            "generator_version": GENERATOR_VERSION,
            "created_utc": utc_now_iso(),
            "validation": report.to_dict(config=self.validation_config, include_frames=True) if report else None,
            "search": search_result.to_dict(include_all=True) if search_result else None,
            "motion": animation.to_dict(include_samples=False) if animation else None,
            "random_seed": int(self.search_config.random_seed),
        }
        result.files["failure_report"] = save_json_file(
            os.path.join(output_dir, "failure_report.json"), payload
        )
        result.files["generation_log"] = self._write_log(run_log, output_dir)

    def _write_log(self, run_log: RunLogger, output_dir: str) -> str:
        ensure_dir(output_dir)
        path = os.path.join(output_dir, "generation_log.txt")
        run_log.write(path)
        return path

    def _write_artifacts(
        self, request, result, output_dir, run_log, *, camera, animation, report,
        search_result, scene, character_placement, character_status,
        animation_payload=None,
    ) -> "dict[str, str]":
        import bpy

        ensure_dir(output_dir)
        sequence_id = request.sequence_folder
        files: "dict[str, str]" = {}
        reason = ", ".join(report.failures) if report else ""

        # No scene copy is ever written: the animation travels in the sidecar and the
        # renderer replays it onto the scene shipped beside the sequence tree.  See
        # core/camera_animation.py for the payload and why it is the keyed values.
        run_log.log(
            "writing an animation-only sequence (the renderer replays it onto the "
            "scene recorded in sequence.source_blend)"
        )

        # -- trajectory + metadata ----------------------------------------
        rows = build_trajectory_rows(
            animation.samples,
            mode=self.config.render.trajectory_mode,
            step=self.config.render.trajectory_step,
        )
        video_ext = f".{self.config.render.video_format}"
        video_path = os.path.join(output_dir, f"{sequence_id}{video_ext}")
        metadata = SequenceMetadata(
            sequence_id=sequence_id,
            scene_name=request.scene_name,
            motion_name=request.motion_name,
            camera_name=camera.name,
            source_blend=request.scene_entry.path,
            frame_start=animation.frame_start,
            frame_end=animation.frame_end,
            fps=animation.fps,
            video_path=video_path,
            camera=camera,
            camera_trajectory=rows,
            has_character=bool(request.has_character and character_placement is not None and character_placement.ok),
            character_name=request.character.id if request.character else "",
            character_animation=getattr(request.animation, "id", "") or "",
            character_status=character_status,
            sequence_dir=output_dir,
            sequence_blend=files.get("sequence_blend", ""),
            validation=(report.to_dict(config=self.validation_config, include_frames=False) if report else {"passed": None, "skipped": True}),
            motion=animation.to_dict(include_samples=True),
            search=(search_result.to_dict(include_all=False) if search_result else {"attempted": False}),
            camera_original=camera.to_dict(),
            random_seed=int(self.search_config.random_seed),
            render={
                "resolution": list(camera.effective_resolution),
                "engine": str(scene.render.engine),
                "fps": int(round(animation.fps)),
                "planned_video_path": to_forward_slashes(video_path),
                "video_format": self.config.render.video_format,
            },
            extra={
                "scene_frame_range": [int(scene.frame_start), int(scene.frame_end)],
                "camera_data_block": getattr(getattr(bpy.data.objects.get(camera.name), "data", None), "name", ""),
                "character_placement": character_placement.to_dict() if character_placement else None,
                "focus": dict(getattr(result, "focus", None) or {}) or None,
                **generator_stamp(),
            },
        )
        if not (report is None or report.passed):
            metadata.status = "validation_failed"
            metadata.error = reason
        metadata.extra["motion_pipeline"] = {
            "output_dir": to_forward_slashes(output_dir),
            "relative_sequence_dir": relative_to(output_dir, self.output_root),
            "template_source": request.template.source,
            "template_parameters": dict(request.template.parameters),
        }
        plan = getattr(request, "plan", None)
        if plan is not None:
            metadata.extra["motion_plan"] = plan.to_dict()
        if animation_payload:
            # Recorded whether or not a blend was written: it is what makes the
            # sequence replayable, and it costs ~15 KB next to a 265 MB scene copy.
            metadata.extra[PAYLOAD_KEY] = animation_payload
        metadata_path = os.path.join(output_dir, f"{sequence_id}.json")
        files["metadata"] = metadata.write(metadata_path)

        if plan is not None:
            # The shot report: what moved, when, and how fast.  Written next to the
            # sequence and copied beside the rendered video (see render/metadata_exporter).
            plan_path = os.path.join(output_dir, f"{sequence_id}_motion_plan.json")
            # ``sort_keys=False``: the shot report has a documented field order.
            files["motion_plan"] = save_json_file(plan_path, plan.report(), sort_keys=False)

        trajectory_path = os.path.join(output_dir, f"{sequence_id}_camera.txt")
        files["camera_trajectory"] = write_trajectory_txt(
            trajectory_path, rows,
            extra_header=[
                f"sequence_id={sequence_id}",
                f"scene={request.scene_name} motion={request.motion_name} camera={camera.name}",
                f"frames={animation.frame_start}..{animation.frame_end} fps={animation.fps:g}",
                f"coordinate_system=blender_world_to_camera units=blender_world_units "
                f"rotation=3x3_rotation_matrix rows_are_camera_axes=+X_right,+Y_up,+Z_back "
                f"view_axis=-Z",
                f"generator=blender_motion_pipeline {GENERATOR_VERSION}",
            ],
        )

        # -- sequence config -------------------------------------------------
        config_path = os.path.join(output_dir, "sequence_config.json")
        config_payload = self._sequence_config_payload(request, camera, animation, result, report)
        # The renderer discovers sequences by reading only this file, so it says
        # where the animation lives without touching the (larger) sidecar.
        config_payload["camera_animation"] = payload_summary(
            animation_payload or {}, filename=os.path.basename(metadata_path)
        )
        if plan is not None:
            # The renderer re-emits this beside the video, which is where a dataset
            # consumer expects to find "what the camera did, and when".
            config_payload["motion_plan"] = plan.to_dict()
        files["sequence_config"] = save_json_file(config_path, config_payload)

        # -- validation report ----------------------------------------------
        # Only worth shipping when something went wrong (or when the run was asked
        # to keep everything): a 30 KB report per successful sequence is 10% of the
        # package and tells nobody anything.
        keep_reports = bool(getattr(self.config.batch, "keep_reports", False))
        sequence_failed = report is not None and not report.passed
        if self.config.batch.save_validation_report and (keep_reports or sequence_failed):
            report_path = os.path.join(output_dir, "validation_report.json")
            files["validation_report"] = save_json_file(report_path, {
                "sequence_id": sequence_id,
                "generated_utc": utc_now_iso(),
                "validation": report.to_dict(config=self.validation_config, include_frames=True) if report else None,
                "search": search_result.to_dict(include_all=True) if search_result else None,
                "configuration": self.validation_config.to_dict(),
                "search_configuration": self.search_config.to_dict(),
                "camera_original": camera.to_dict(),
                "camera_final": {
                    "location": [round(float(v), 6) for v in animation.samples[0].position],
                    "lens_mm": round(float(animation.samples[0].focal), 6),
                    "intrinsics": camera_intrinsics(camera, focal_length=animation.samples[0].focal),
                },
                "motion": animation.to_dict(include_samples=False),
                **generator_stamp(),
            })

        # -- motion-level manifest -------------------------------------------
        manifest_path = os.path.join(
            normalize_path(self.output_root), request.scene_name, request.motion_folder, "manifest.json"
        )
        writer = ManifestWriter(
            manifest_path,
            kind="motion",
            scope={"scene_name": request.scene_name, "motion_name": request.motion_name,
                   "source_blend": to_forward_slashes(request.scene_entry.path)},
        )
        writer.add_sequence(result.to_manifest_entry())
        writer.flush()
        files["manifest"] = writer.path

        # -- log -------------------------------------------------------------
        if keep_reports or sequence_failed:
            files["generation_log"] = self._write_log(run_log, output_dir)
        return files

    def _sequence_config_payload(self, request, camera, animation, result, report) -> dict:
        payload = {
            "schema_version": self.config.schema_version,
            "sequence": {
                "sequence_id": request.sequence_folder,
                "scene_name": request.scene_name,
                "motion_name": request.motion_name,
                "camera_name": camera.name,
                "has_character": bool(request.has_character),
                "character_name": request.character.id if request.character else "",
                "character_animation": getattr(request.animation, "id", "") or "",
                "character_status": result.character_status,
                **self._scene_reference(request.scene_entry),
                "output_dir": to_forward_slashes(result.output_dir),
                "random_seed": int(self.search_config.random_seed),
            },
            "frames": {
                "frame_start": int(animation.frame_start),
                "frame_end": int(animation.frame_end),
                "frame_count": int(animation.frame_count),
                "fps": float(animation.fps),
            },
            "camera": {
                "original": camera.to_dict(),
                "final_first_frame": {
                    "location": [round(float(v), 6) for v in animation.samples[0].position],
                    "focal_length_mm": round(float(animation.samples[0].focal), 6),
                },
            },
            "focus": _focus_block(result),
            "motion": {
                "template_name": animation.template_name,
                "template_source": request.template.source,
                "interpolation": animation.interpolation,
                "parameters": dict(animation.template_parameters),
                # A baked per-frame motion is 216 keys (~20 KB) and made up 84% of
                # this file; the renderer never reads it (it replays the payload
                # below), and the full list is reproducible from template_name +
                # parameters (or from motion_plan).  Keep a compact summary only.
                "keyframes": _compact_keyframes(request.template.keyframes),
                "keyframe_count": len(request.template.keyframes),
                "keyframes_note": (
                    "summary only -- rebuild the full list from template_name + "
                    "parameters, or from motion_plan for compound sequences"
                ),
                "unit_scale": dict(animation.unit_scale),
            },
            "render": {
                **self.config.render.to_dict(),
                # What the scene itself would have produced, so a mismatch between
                # the requested size and the source scene is visible in the file the
                # renderer reads (this is what the 2000x2000 report came from).
                "scene_resolution": list(camera.effective_resolution),
                "effective_resolution": list(
                    self._sequence_resolution(camera)
                ),
            },
            "validation": {
                "passed": bool(report.passed) if report else None,
                "score": round(float(report.score), 6) if report else None,
                "reasons": report.failures if report else [],
            },
            "region": dict(getattr(request, "region", None) or {}),
            "generator_version": GENERATOR_VERSION,
            "created_utc": utc_now_iso(),
        }
        payload.update(generator_stamp())
        return payload


def load_sequence_config(sequence_dir: str) -> "dict | None":
    """Read ``sequence_config.json`` beside a sequence (used by the renderer)."""
    from ..io.json_io import load_json_file

    path = os.path.join(normalize_path(sequence_dir), "sequence_config.json")
    if not os.path.isfile(path):
        return None
    try:
        payload = load_json_file(path)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def find_sequence_files(root: str, *, recursive: bool = True) -> "list[dict]":
    """Discover generated sequences under ``root``.

    A "sequence" is a folder containing a ``sequence_config.json``; the scene and
    motion are taken from that config when available so the renderer can rebuild
    the documented ``scene/motion/sequence`` output layout.
    """
    root = normalize_path(root)
    found: "list[dict]" = []
    if not os.path.isdir(root):
        return found
    if recursive:
        for current, _dirnames, filenames in os.walk(root):
            if "sequence_config.json" in filenames:
                found.append(_describe_sequence_dir(current, root))
    else:
        for name in sorted(os.listdir(root)):
            candidate = os.path.join(root, name)
            if os.path.isdir(candidate) and os.path.isfile(os.path.join(candidate, "sequence_config.json")):
                found.append(_describe_sequence_dir(candidate, root))
    return sorted(found, key=lambda item: item["sequence_dir"])


def _describe_sequence_dir(directory: str, root: str) -> dict:
    config = load_sequence_config(directory) or {}
    sequence = config.get("sequence") or {}
    frames = config.get("frames") or {}
    scene_name = sequence.get("scene_name") or os.path.basename(os.path.dirname(os.path.dirname(directory)))
    motion_name = sequence.get("motion_name") or os.path.basename(os.path.dirname(directory))
    sequence_id = sequence.get("sequence_id") or os.path.basename(directory)
    blends = sorted(
        name for name in os.listdir(directory) if name.lower().endswith(".blend")
    )
    return {
        "sequence_dir": normalize_path(directory),
        "relative_dir": relative_to(directory, root),
        "scene_name": scene_name,
        "motion_name": motion_name,
        "sequence_id": sequence_id,
        "has_character": bool(sequence.get("has_character")),
        "character_name": sequence.get("character_name", ""),
        "camera_name": sequence.get("camera_name", ""),
        "frame_start": frames.get("frame_start"),
        "frame_end": frames.get("frame_end"),
        "fps": frames.get("fps"),
        "blend_files": blends,
        "config": config,
    }
