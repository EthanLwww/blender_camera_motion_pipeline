"""Check every generated sequence against the template numbers it was made from.

    blender -b -P tests/probe_template_contract.py -- \
        --sequence-root "<generated tree>" --templates "<templates.json>"

Templates are Blender coordinates used verbatim, so this is a *contract* check, not
a taste judgement: for each sequence it re-reads the template's own keyframes and
asserts the recorded camera motion is exactly what those numbers say.

* **Translation**: the world displacement must equal ``right*x + up*y + back*z`` of
  the template's first→last location, measured in the camera's *starting*
  orientation (``-Z`` forward, ``+Y`` up, ``+X`` right, metres).  A template that
  declares no translation must not move the camera at all.
* **Rotation**: the turn must equal the template's ``[rx, ry, rz]`` delta in
  magnitude, and the aim must swing the way the template's family name says
  (``pan_right`` toward the camera's right, ``tilt_up`` upward, ...).  A pure roll
  must leave the aim untouched, and a template that declares no rotation must not
  turn the camera.
* **Focal**: ``zoom_in`` must end shorter than it starts, ``zoom_out`` longer.

Exit code 0 when every sequence agrees, 1 otherwise.  The check is pure Python: it
reads the sidecar JSON (recorded world poses) and the template document, and never
opens a scene, so it runs anywhere -- including on a render node, over a tree that
was generated on another machine.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(_HERE))
if _PACKAGE_PARENT not in sys.path:
    sys.path.insert(0, _PACKAGE_PARENT)
sys.path.insert(0, _HERE)
import _boot  # noqa: E402,F401  (the add-on folder may be called anything)

from blender_motion_pipeline.camera import motion_templates as mt  # noqa: E402

#: Metres: how closely a recorded displacement must match the template's numbers.
TRANSLATION_TOLERANCE = 1e-4
#: Degrees: how closely a recorded turn must match the template's numbers.
ROTATION_TOLERANCE = 0.05
#: An aim that must not move is compared with this slack: the recorded quaternions
#: are rounded to six decimals, which leaves ~1e-6 of single-precision residue.
AIM_TOLERANCE = 1e-4
#: Cosine of the aim swing a pan/tilt must show to count as "the right way".
DIRECTION_MINIMUM = 0.05


def _load(path: str):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _sub(a, b):
    return tuple(float(x) - float(y) for x, y in zip(a, b))


def _dot(a, b) -> float:
    return sum(float(x) * float(y) for x, y in zip(a, b))


def _axes(quaternion) -> "tuple[tuple, tuple, tuple]":
    """``(right, up, back)`` world axes of a camera quaternion."""
    return (
        mt.quat_rotate(quaternion, (1.0, 0.0, 0.0)),
        mt.quat_rotate(quaternion, (0.0, 1.0, 0.0)),
        mt.quat_rotate(quaternion, (0.0, 0.0, 1.0)),
    )


def anchor_quaternion(samples, template, *, rotation_order: str = "XYZ"):
    """The orientation a template's offsets are applied in.

    Not simply the sidecar's ``camera_original``: the camera search may accept a
    candidate that also *turns* the camera (``rotation_adjust``), and the template's
    offsets are then resolved in that rotated frame.  The recorded first sample
    already carries the answer -- it is ``anchor * template_rotation(frame 0)`` -- so
    dividing the template's own frame-0 rotation out of it recovers the anchor
    exactly, whatever the search did.
    """
    generator = mt.MotionTemplateGenerator(
        unit_scale=mt.TemplateUnitScale(rotation_order=rotation_order)
    )
    start = tuple(float(v) for v in samples[0]["rotation_quaternion"])
    first = template.keyframes[0].rotation or (0.0, 0.0, 0.0)
    local = generator.rotation_delta(first)
    return mt.quat_normalize(mt.quat_multiply(start, mt.quat_conjugate(local)))


def check_sequence(samples, template, *, tolerance: float = TRANSLATION_TOLERANCE,
                   rotation_order: str = "XYZ") -> "list[str]":
    """Compare one sequence's recorded samples with its template's numbers.

    The template's offsets are resolved in the camera's *anchor* frame (the pose the
    sequence starts from, before the template's own rotation), which
    :func:`anchor_quaternion` recovers from the recorded samples -- so a sequence
    whose anchor the camera search moved still checks out.
    """
    name = template.name
    if len(samples) < 2:
        return [f"{name}: only {len(samples)} sample(s)"]

    keys = template.keyframes
    template_move = _sub(keys[-1].location or (0, 0, 0), keys[0].location or (0, 0, 0))
    template_turn = _sub(keys[-1].rotation or (0, 0, 0), keys[0].rotation or (0, 0, 0))

    start, end = samples[0], samples[-1]
    start_quaternion = tuple(float(v) for v in start["rotation_quaternion"])
    end_quaternion = tuple(float(v) for v in end["rotation_quaternion"])
    moved = _sub(end["location"], start["location"])
    right, up, back = _axes(anchor_quaternion(samples, template, rotation_order=rotation_order))

    problems: "list[str]" = []

    # -- translation -----------------------------------------------------
    if max(abs(v) for v in template_move) > tolerance:
        wanted = tuple(
            right[i] * template_move[0] + up[i] * template_move[1] + back[i] * template_move[2]
            for i in range(3)
        )
        error = math.dist(moved, wanted)
        if error > tolerance:
            problems.append(
                f"{name}: moved {tuple(round(v, 4) for v in moved)} but the template says "
                f"{tuple(round(v, 4) for v in wanted)} (error {error:.5f} m)"
            )
    elif math.dist(moved, (0.0, 0.0, 0.0)) > tolerance:
        problems.append(
            f"{name}: declares no translation but the camera moved "
            f"{tuple(round(v, 4) for v in moved)}"
        )

    # -- rotation --------------------------------------------------------
    measured_turn = mt.quat_angle_between(start_quaternion, end_quaternion)
    wanted_turn = math.sqrt(sum(float(v) ** 2 for v in template_turn))
    if abs(measured_turn - wanted_turn) > ROTATION_TOLERANCE:
        problems.append(
            f"{name}: turned {measured_turn:.3f} deg, the template asks for {wanted_turn:.3f}"
        )

    if wanted_turn > ROTATION_TOLERANCE:
        aim_start = mt.quat_rotate(start_quaternion, (0.0, 0.0, -1.0))
        aim_end = mt.quat_rotate(end_quaternion, (0.0, 0.0, -1.0))
        lateral = _dot(aim_end, right)
        vertical = _dot(aim_end, up)
        if name.startswith("pan_right") and lateral <= DIRECTION_MINIMUM:
            problems.append(f"{name}: pan_right must swing the aim right ({lateral:+.3f})")
        if name.startswith("pan_left") and lateral >= -DIRECTION_MINIMUM:
            problems.append(f"{name}: pan_left must swing the aim left ({lateral:+.3f})")
        if name.startswith("tilt_up") and vertical <= DIRECTION_MINIMUM:
            problems.append(f"{name}: tilt_up must raise the aim ({vertical:+.3f})")
        if name.startswith("tilt_down") and vertical >= -DIRECTION_MINIMUM:
            problems.append(f"{name}: tilt_down must lower the aim ({vertical:+.3f})")
        if name.startswith("roll_") and math.dist(aim_start, aim_end) > AIM_TOLERANCE:
            problems.append(
                f"{name}: a roll must not change the aim "
                f"(moved {math.dist(aim_start, aim_end):.2e})"
            )

    # -- focal -----------------------------------------------------------
    # ``zoom_in`` tightens the framing, which is a *longer* lens (the reference
    # document's zoom_in goes 24 mm -> 70 mm); ``zoom_out`` widens it again.
    focal_start = float(start.get("focal_length") or 0.0)
    focal_end = float(end.get("focal_length") or 0.0)
    if name.startswith("zoom_in") and not focal_end > focal_start:
        problems.append(f"{name}: zoom_in must lengthen the lens ({focal_start} -> {focal_end})")
    if name.startswith("zoom_out") and not focal_end < focal_start:
        problems.append(f"{name}: zoom_out must shorten the lens ({focal_start} -> {focal_end})")
    return problems


def rebuild_plan(config):
    """Rebuild the :class:`MotionPlan` a sequence recorded, or ``None``.

    Spatio-temporal sequences carry their plan in ``sequence_config.json``
    (``motion_plan``): the segments, the atoms that ran in each one and the frame
    range.  Rebuilding it lets this probe check the recorded camera motion against
    the plan's own numbers without opening a scene.
    """
    from blender_motion_pipeline.camera import motion_composite as mc

    block = config.get("motion_plan")
    if not isinstance(block, dict) or not block.get("segments"):
        return None
    atoms, _source = mc.load_atomic_library(
        template_path=str(block.get("template_source") or ""),
        fps=float(block.get("fps") or 24.0),
    )
    by_name = {atom.name: atom for atom in atoms}
    segments = []
    for raw in block["segments"]:
        motions = tuple(by_name[name] for name in (raw.get("motions") or [])
                        if name in by_name)
        segments.append(mc.PlanSegment(
            index=int(raw.get("index", len(segments))),
            start_time=float(raw["start_time"]),
            end_time=float(raw["end_time"]),
            start_frame=int(raw["start_frame"]),
            end_frame=int(raw["end_frame"]),
            motions=motions,
        ))
    return mc.MotionPlan(
        duration_seconds=float(block.get("duration_seconds") or 0.0),
        fps=float(block.get("fps") or 24.0),
        frame_start=int(block.get("frame_start") or 0),
        frame_end=int(block.get("frame_end") or 0),
        segments=tuple(segments),
        compound=bool(block.get("compound", True)),
        seed=int(block.get("seed") or 0),
        source=str(block.get("template_source") or ""),
    )


def check_plan(samples, config, *, tolerance: float = TRANSLATION_TOLERANCE) -> "list[str]":
    """Check a planned sequence (single atom or compound) against its own plan.

    The plan is flattened again here and compared with the recorded poses frame by
    frame: the displacement must be the atoms' rates integrated over the segments,
    expressed in the camera's starting orientation, and the rotation must be the
    camera-local composition the generator used.
    """
    from blender_motion_pipeline.camera import motion_composite as mc

    name = str((config.get("sequence") or {}).get("motion_name") or "?")
    plan = rebuild_plan(config)
    if plan is None:
        return [f"{name}: no motion plan recorded"]
    problems: "list[str]" = []
    if not samples:
        return [f"{name}: no samples"]

    # -- the time plan itself --------------------------------------------
    frames = config.get("frames") or {}
    if frames.get("frame_start") is not None:
        if (int(frames["frame_start"]) != plan.frame_start
                or int(frames["frame_end"]) != plan.frame_end):
            problems.append(
                f"{name}: the sequence covers {frames.get('frame_start')}.."
                f"{frames.get('frame_end')} but its plan covers "
                f"{plan.frame_start}..{plan.frame_end}"
            )
    if len(samples) != plan.frame_count:
        problems.append(
            f"{name}: {len(samples)} sample(s) for a {plan.frame_count}-frame plan"
        )
    for segment in plan.segments:
        if segment.duration_seconds < mc.MIN_SEGMENT_SECONDS - 1e-9:
            problems.append(
                f"{name}: segment {segment.index} lasts {segment.duration_seconds:.3f} s "
                f"(below the {mc.MIN_SEGMENT_SECONDS:g} s minimum)"
            )
    for previous, following in zip(plan.segments, plan.segments[1:]):
        if abs(previous.end_time - following.start_time) > 1e-6:
            problems.append(f"{name}: segments are not contiguous in time")
        if following.start_frame != previous.end_frame + 1:
            problems.append(f"{name}: segments do not touch in frames")
    for segment in plan.segments:
        channels: "list[str]" = []
        for motion in segment.motions:
            channels.extend(motion.channels)
        if len(channels) != len(set(channels)):
            problems.append(
                f"{name}: segment {segment.index} runs two moves on the same axis "
                f"({[m.name for m in segment.motions]})"
            )

    # -- the motion the plan implies --------------------------------------
    # The plan is flattened again and pushed through the very generator that made
    # the sequence, from the base pose the recording implies: the expected poses must
    # then equal the recorded ones sample for sample.  Re-using the generator (rather
    # than re-deriving the camera basis here) keeps this check free of its own
    # handedness/orthonormalisation assumptions.
    #
    # A zoom plan records the focal it *reached*, not the base lens, so recover the
    # base from the plan's own frame-0 ramp: otherwise the rebuilt plan would sit on
    # the wrong lens and every zoomed frame would look like a mismatch.
    base_focal = float(samples[0].get("focal_length") or 35.0)
    if plan.segments and abs(plan.segments[0].start_time) < 1e-9:
        base_focal -= sum(
            motion.rate_focal for motion in plan.segments[0].motions
        ) / max(plan.fps, 1e-6)
    template = mc.flatten_plan(plan, base_focal=base_focal)
    generator = mt.MotionTemplateGenerator()
    first_key = template.keyframes[0]
    start_quaternion = tuple(float(v) for v in samples[0]["rotation_quaternion"])
    # The generator composes as ``q_base * delta`` (the template turns the camera in
    # its *own* frame), so the frame-0 delta is cancelled on the **right**:
    # quaternions do not commute, and left-cancelling leaves the camera rotated by
    # the frame-0 tap (measured: 0.93 deg, which then grew into a ~1 % path error).
    anchor = mt.quat_normalize(mt.quat_multiply(
        start_quaternion,
        mt.quat_conjugate(generator.rotation_delta(first_key.rotation or (0.0, 0.0, 0.0))),
    ))
    base_axes = mt.orthonormal_axes(mt.quaternion_to_matrix(anchor))
    origin = tuple(
        float(samples[0]["location"][axis])
        - sum(base_axes[column][axis] * float(first_key.location[column])
              for column in range(3))
        for axis in range(3)
    )
    base_matrix = [list(row) + [origin[index]] for index, row in enumerate(mt.quaternion_to_matrix(anchor))]
    base_matrix.append([0.0, 0.0, 0.0, 1.0])
    expected = generator.generate(
        template, base_matrix=base_matrix, base_focal=base_focal,
        frame_start=plan.frame_start, frame_end=plan.frame_end,
    )
    worst_position = 0.0
    worst_rotation = 0.0
    for sample, wanted_sample in zip(samples, expected.samples):
        worst_position = max(
            worst_position, math.dist(sample["location"], wanted_sample.position)
        )
        worst_rotation = max(worst_rotation, mt.quat_angle_between(
            sample["rotation_quaternion"], wanted_sample.quaternion
        ))
        if abs(float(sample.get("focal_length") or 0.0) - float(wanted_sample.focal)) > 1e-3:
            problems.append(
                f"{name}: frame {sample['frame']} zoomed to {sample.get('focal_length')} mm "
                f"but the plan asks for {wanted_sample.focal:.4f} mm"
            )
            break
    if len(expected.samples) != len(samples):
        problems.append(
            f"{name}: the plan produces {len(expected.samples)} frame(s), the sequence "
            f"has {len(samples)}"
        )
    if worst_position > tolerance:
        problems.append(
            f"{name}: the recorded path is {worst_position:.4f} m away from the plan "
            f"(tolerance {tolerance:g} m)"
        )
    if worst_rotation > ROTATION_TOLERANCE:
        problems.append(
            f"{name}: the recorded aim is {worst_rotation:.3f} deg away from the plan "
            f"(tolerance {ROTATION_TOLERANCE:g} deg)"
        )
    return problems


def iter_sequences(root: str):
    """Yield ``(sequence_dir, config, sidecar)`` for a generated tree."""
    for current, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name != "__pycache__"]
        if "sequence_config.json" not in files:
            continue
        config = _load(os.path.join(current, "sequence_config.json"))
        sidecar_name = (config.get("camera_animation") or {}).get("file") or ""
        sidecar_path = os.path.join(current, sidecar_name)
        if sidecar_name and os.path.isfile(sidecar_path):
            yield current, config, _load(sidecar_path)


def recorded_document(root: str) -> str:
    """The template document the tree was generated from, as the sequences recorded it."""
    for _directory, config, _sidecar in iter_sequences(root):
        source = str((config.get("motion_pipeline") or {}).get("template_source") or "")
        if source and os.path.isfile(source):
            return source
    return ""


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    argv = argv[argv.index("--") + 1:] if "--" in argv else argv[1:]
    parser = argparse.ArgumentParser(prog="probe_template_contract.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sequence-root", required=True, help="generated tree to check")
    parser.add_argument("--templates", default="", help="the template document used to generate it")
    parser.add_argument("--tolerance", type=float, default=TRANSLATION_TOLERANCE,
                        help="metres a displacement may differ by")
    args = parser.parse_args(argv)

    document = args.templates or recorded_document(args.sequence_root)
    library = None
    if document and os.path.isfile(document):
        library = mt.load_template_file(document)
        print(f"templates : {document} ({len(library)})")
    else:
        # A plan-driven tree (single atoms / compounds) carries everything it needs
        # in its own sequence_config.json, so a template document is optional there.
        print(f"templates : none ({document or 'not recorded'}) -- plan-driven trees are "
              f"self-describing")
    print(f"tree      : {args.sequence_root}")

    checked = 0
    compound_count = 0
    skipped: "list[str]" = []
    families: "dict[str, int]" = {}
    problems: "list[str]" = []
    for _directory, config, sidecar in iter_sequences(args.sequence_root):
        name = str((config.get("sequence") or {}).get("motion_name") or "")
        samples = ((sidecar.get("motion") or {}).get("samples")) or []
        if config.get("motion_plan"):
            # Spatio-temporal sequence (one atom or a compound): check it against
            # the plan it recorded, which needs no template document at all.
            problems.extend(check_plan(samples, config, tolerance=args.tolerance))
            if bool((config.get("motion_plan") or {}).get("compound")):
                compound_count += 1
                families["compound"] = families.get("compound", 0) + 1
            else:
                checked += 1
                family = name.split("_")[0]
                families[family] = families.get(family, 0) + 1
            continue
        if library is None:
            skipped.append(name)
            continue
        try:
            template = library.get(name)
        except Exception:
            skipped.append(name)
            continue
        order = str(((sidecar.get("motion") or {}).get("unit_scale") or {})
                    .get("rotation_order") or "XYZ")
        problems.extend(check_sequence(
            samples, template, tolerance=args.tolerance, rotation_order=order,
        ))
        checked += 1
        family = name.split("_")[0]
        families[family] = families.get(family, 0) + 1

    for family in sorted(families):
        print(f"  {family:14s} {families[family]:3d} sequence(s)")
    print(f"checked   : {checked} single-move sequence(s)"
          + (f" + {compound_count} compound(s)" if compound_count else ""))
    if skipped:
        print(f"not in the document: {sorted(set(skipped))[:5]}")
    if problems:
        print(f"PROBLEMS  : {len(problems)}")
        for problem in problems[:20]:
            print(f"  ! {problem}")
        return 1
    print("verdict   : every sequence matches the numbers it was generated from")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
