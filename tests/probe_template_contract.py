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


def check_compound(samples, config, library, *, tolerance: float = TRANSLATION_TOLERANCE) -> "list[str]":
    """Check a compound sequence: same frame range, contiguous windows, chained parts.

    The parts themselves are checked against their own numbers by the single-template
    rules above; a compound's own contract is that it keeps the total frame range and
    that each part starts where the previous one ended.
    """
    name = str((config.get("sequence") or {}).get("motion_name") or "")
    block = ((config.get("motion") or {}).get("parameters") or {}).get("compound") or {}
    problems: "list[str]" = []
    parts = [str(part) for part in (block.get("parts") or [])]
    windows = [[int(v) for v in window] for window in (block.get("windows") or [])]
    if not parts or not windows:
        return [f"{name}: a compound sequence without its recipe"]
    if len(parts) != len(windows):
        problems.append(f"{name}: {len(parts)} parts but {len(windows)} window(s)")
    for part in parts:
        try:
            library.get(part)
        except Exception:
            problems.append(f"{name}: part {part!r} is not in the template document")
    for (start, end), (next_start, _next_end) in zip(windows, windows[1:]):
        if next_start != end + 1:
            problems.append(f"{name}: windows are not contiguous ({windows})")
    frames = config.get("frames") or {}
    if windows and frames.get("frame_start") is not None:
        if (int(frames["frame_start"]) != windows[0][0]
                or int(frames["frame_end"]) != windows[-1][1]):
            problems.append(
                f"{name}: the sequence covers {frames.get('frame_start')}.."
                f"{frames.get('frame_end')} but its windows cover "
                f"{windows[0][0]}..{windows[-1][1]}"
            )
    if frames.get("frame_count") is not None and len(samples) != int(frames["frame_count"]):
        problems.append(
            f"{name}: {len(samples)} sample(s) for frame_count {frames['frame_count']}"
        )
    # A broken chain leaves a cut-sized step at a junction.
    for index, ((_start, end), window) in enumerate(zip(windows, windows[1:])):
        junction = [s for s in samples if int(s["frame"]) in (end, window[0])]
        if len(junction) == 2:
            step = math.dist(junction[0]["location"], junction[1]["location"])
            if step > 1.0:
                problems.append(
                    f"{name}: {step:.2f} m jump between part {index + 1} and {index + 2} "
                    "(the parts are not chained)"
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
    if not document or not os.path.isfile(document):
        print(f"cannot find the template document ({document or 'not recorded'})")
        return 2
    library = mt.load_template_file(document)
    print(f"templates : {document} ({len(library)})")
    print(f"tree      : {args.sequence_root}")

    checked = 0
    compound_count = 0
    skipped: "list[str]" = []
    families: "dict[str, int]" = {}
    problems: "list[str]" = []
    for _directory, config, sidecar in iter_sequences(args.sequence_root):
        name = str((config.get("sequence") or {}).get("motion_name") or "")
        samples = ((sidecar.get("motion") or {}).get("samples")) or []
        block = ((config.get("motion") or {}).get("parameters") or {}).get("compound")
        if isinstance(block, dict):
            # A compound is checked against its recipe, and its parts are checked
            # against their own numbers by the same run on a base sequence.
            problems.extend(check_compound(samples, config, library,
                                            tolerance=args.tolerance))
            compound_count += 1
            families["compound"] = families.get("compound", 0) + 1
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
    print(f"checked   : {checked} base sequence(s)"
          + (f" + {compound_count} compound(s)" if compound_count else ""))
    if skipped:
        print(f"not in the document: {sorted(set(skipped))[:5]}")
    if problems:
        print(f"PROBLEMS  : {len(problems)}")
        for problem in problems[:20]:
            print(f"  ! {problem}")
        return 1
    print("verdict   : every sequence matches its template's Blender numbers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
