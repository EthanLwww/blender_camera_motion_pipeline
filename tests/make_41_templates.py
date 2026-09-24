"""Generate ``templates/camera_motion_templates_41.json`` -- the 41 dataset moves.

    python tests/make_41_templates.py                        # rewrite the document
    python tests/make_41_templates.py --check                 # report, write nothing
    python tests/make_41_templates.py --verify-jsonl FILE     # cross-check the source list

The document is an ordinary motion-template document (the parser, the library, the
panel and the probes treat it like any other), so it is loaded exactly like the
reference 80-template set:

    motion_pipeline_cli.py --templates templates/camera_motion_templates_41.json ...

**What it contains.**  The 41 moves of the dataset shot list, in four kinds:

===========  =====  ==========================================================
kind         count  shape
===========  =====  ==========================================================
``single``      17  one move over the whole shot
``sim``          7  several moves **at the same time** (one phase, one delta each)
``seq``         13  two moves **one after the other** (two equal phases)
``tri``          4  three moves in sequence (three equal phases)
===========  =====  ==========================================================

A phase with no components is a *hold* (``static``), so "dolly in, then hold" is a
phase with a component followed by a phase with none.  **Timing:** keys are
absolute frames at 24 fps and the shot is 6.0 s, i.e. frames 0..144; a two-phase
move splits at frame 72 and a three-phase move at 48/96.  The plugin maps template
frames onto the sequence timeline with ``offset + frame * frame_scale``, so leaving
``frame_scale`` at 1.0 (the default) renders exactly these 144 frames.

**Ids.**  ``<kind>_<move>`` -- ``single_pan_left``, ``sim_dolly_in__tilt_up``,
``seq_pan_left__tilt_up``, ``tri_dolly_in__static__dolly_out``.  The kind prefix is
load-bearing: the dataset has both a *simultaneous* and a *sequential* "pan left +
tilt up", and only the prefix tells them apart.  The original dataset key is kept
in ``dataset_key`` (``S01_m_pan_left__tilt_up``), so the document and the shot list
stay traceable to each other; ``cap_zh``/``camera_sentence``/``targets`` are carried
over verbatim for the same reason.

**Coordinates.**  Camera-local, Blender, metres and degrees -- untouched by the
parser: ``location`` is ``[right, up, back]`` (so ``-Z`` is forward) and
``rotation`` is ``[rx, ry, rz]`` about the camera's own axes, applied to the pose
the camera has on the sequence's first frame.  Amplitudes are the *nominal* ones
below; the region box scales translation amplitudes down when a scene is too small
for them (rotation and focal are never scaled -- they cannot leave the scene).

=========================  ==========================================
move                       nominal amplitude over the 6 s shot
=========================  ==========================================
Dolly In / Out             3.0 m forward / backward
Truck Left / Right         2.0 m
Crane Up / Down            2.0 m
Pan Left / Right           45 deg
Tilt Up / Down             20 deg
Roll CW / CCW              25 deg
Zoom In / Out              35 -> 70 mm / 35 -> 18 mm
Arc CW / CCW               90 deg of orbit around a subject 4 m ahead
=========================  ==========================================

**Arc.**  ``Arc`` is a *circular* move, not a straight one: the keys are placed on
the circle that keeps a subject ``ARC_SUBJECT_RADIUS`` in front of the camera
centred, with the yaw following the orbit angle (``ry = +-angle``).  That is the
same convention as ``templates/atomic_motion_templates.json`` (whose arcs derive
the yaw from the lateral rate at the same 4 m radius), authored here as a full
sweep instead of a one-second rate.  With a **focus object** the generator re-bakes
this orbit around the object's real distance instead of the nominal 4 m, which is
what makes the object the actual centre of the shot.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PACKAGE = os.path.dirname(HERE)
DOCUMENT = os.path.join(PACKAGE, "templates", "camera_motion_templates_41.json")

#: The dataset's shot length and the rate its keys are authored at.
UNIT_FPS = 24
DURATION_SECONDS = 6.0
FRAME_END = int(round(UNIT_FPS * DURATION_SECONDS))
#: Focal length every key carries unless the move zooms.
REFERENCE_FOCAL = 35.0

#: Nominal amplitudes.  Translation amplitudes are scalable; angles are not.
DOLLY_M = 3.0
TRUCK_M = 2.0
CRANE_M = 2.0
PAN_DEG = 45.0
TILT_DEG = 20.0
ROLL_DEG = 25.0
ZOOM_IN_MM = 35.0
ZOOM_OUT_MM = -17.0
ARC_SWEEP_DEG = 90.0
#: Radius a subject is assumed to stand at when the arc is written; the same 4 m
#: the atomic vocabulary uses.  A focus object overrides it at bake time.
ARC_SUBJECT_RADIUS = 4.0
#: Keys every this many degrees along an arc.  15 deg keeps the chord error under
#: 1% of the radius (0.034 m at 4 m) for a linear per-frame sampler.
ARC_KEY_STEP_DEG = 15.0

#: Keyframe vector keys, in the order the parser reads them.
_AXES = ("x", "y", "z")
_ROTATIONS = ("rx", "ry", "rz")

#: Short name -> ``(type, direction, components)``.  ``components`` is the move's
#: total delta over one phase: camera-local metres (``x``/``y``/``z``), camera-local
#: degrees (``rx``/``ry``/``rz``) or millimetres (``focal``).  Signs follow the
#: atomic vocabulary: ``+ry`` is a left pan, ``+rx`` tilts up, ``-z`` pushes in.
STEPS = {
    "arc_ccw": ("Arc", "counterclockwise", {"arc": -1.0}),
    "arc_cw": ("Arc", "clockwise", {"arc": +1.0}),
    "crane_down": ("Crane", "down", {"y": -CRANE_M}),
    "crane_up": ("Crane", "up", {"y": +CRANE_M}),
    "dolly_in": ("Dolly In", None, {"z": -DOLLY_M}),
    "dolly_out": ("Dolly Out", None, {"z": +DOLLY_M}),
    "pan_left": ("Pan", "left", {"ry": +PAN_DEG}),
    "pan_right": ("Pan", "right", {"ry": -PAN_DEG}),
    "roll_ccw": ("Roll", "counterclockwise", {"rz": +ROLL_DEG}),
    "roll_cw": ("Roll", "clockwise", {"rz": -ROLL_DEG}),
    "static": ("Static", None, {}),
    "tilt_down": ("Tilt", "down", {"rx": -TILT_DEG}),
    "tilt_up": ("Tilt", "up", {"rx": +TILT_DEG}),
    "truck_left": ("Truck", "left", {"x": -TRUCK_M}),
    "truck_right": ("Truck", "right", {"x": +TRUCK_M}),
    "zoom_in": ("Zoom In", None, {"focal": +ZOOM_IN_MM}),
    "zoom_out": ("Zoom Out", None, {"focal": ZOOM_OUT_MM}),
}

#: ``(dataset key, step, group, cap_zh, camera sentence)`` -- one phase, 17 moves.
SINGLES = (
    ("S01_b_arc_ccw", "arc_ccw", "单一运动", "逆时针环绕", "镜头逆时针环绕主体。"),
    ("S01_b_arc_cw", "arc_cw", "单一运动", "顺时针环绕", "镜头顺时针环绕主体。"),
    ("S01_b_crane_down", "crane_down", "单一运动", "向下降", "镜头向下降。"),
    ("S01_b_crane_up", "crane_up", "单一运动", "向上升", "镜头向上升。"),
    ("S01_b_dolly_in", "dolly_in", "单一运动", "推近", "镜头推近。"),
    ("S01_b_dolly_out", "dolly_out", "单一运动", "拉远", "镜头拉远。"),
    ("S01_b_pan_left", "pan_left", "单一运动", "向左摇", "镜头向左摇。"),
    ("S01_b_pan_right", "pan_right", "单一运动", "向右摇", "镜头向右摇。"),
    ("S01_b_roll_ccw", "roll_ccw", "单一运动", "逆时针滚转", "镜头逆时针滚转。"),
    ("S01_b_roll_cw", "roll_cw", "单一运动", "顺时针滚转", "镜头顺时针滚转。"),
    ("S01_b_static", "static", "单一运动", "静止", "镜头保持静止。"),
    ("S01_b_tilt_down", "tilt_down", "单一运动", "向下俯", "镜头向下俯。"),
    ("S01_b_tilt_up", "tilt_up", "单一运动", "向上仰", "镜头向上仰。"),
    ("S01_b_truck_left", "truck_left", "单一运动", "向左横移", "镜头向左横移。"),
    ("S01_b_truck_right", "truck_right", "单一运动", "向右横移", "镜头向右横移。"),
    ("S01_b_zoom_in", "zoom_in", "单一运动", "变焦推近", "镜头变焦推近。"),
    ("S01_b_zoom_out", "zoom_out", "单一运动", "变焦拉远", "镜头变焦拉远。"),
)

#: ``(dataset key, steps, group, cap_zh, camera sentence)`` -- simultaneous, 7 moves.
SIMS = (
    ("S01_m_crane_down__tilt_up", ("crane_down", "tilt_up"), "反向耦合",
     "向下降 + 向上仰", "镜头向下降并向上仰。"),
    ("S01_m_crane_up__tilt_down", ("crane_up", "tilt_down"), "反向耦合",
     "向上升 + 向下俯", "镜头向上升并向下俯。"),
    ("S01_m_dolly_in__pan_left", ("dolly_in", "pan_left"), "异轴同时",
     "推近 + 向左摇", "镜头推近并向左摇。"),
    ("S01_m_dolly_in__tilt_up", ("dolly_in", "tilt_up"), "异轴同时",
     "推近 + 向上仰", "镜头推近并向上仰。"),
    ("S01_m_dolly_in__truck_right", ("dolly_in", "truck_right"), "异轴同时",
     "推近 + 向右横移", "镜头推近并向右横移。"),
    ("S01_m_dolly_out__pan_right", ("dolly_out", "pan_right"), "异轴同时",
     "拉远 + 向右摇", "镜头拉远并向右摇。"),
    ("S01_m_pan_left__tilt_up", ("pan_left", "tilt_up"), "异轴同时",
     "向左摇 + 向上仰", "镜头向左摇并向上仰。"),
)

#: ``(dataset key, steps, group, cap_zh, camera sentence)`` -- two phases, 13 moves.
SEQS = (
    ("S01_s_dolly_in__dolly_out", ("dolly_in", "dolly_out"), "同轴反转",
     "先推近 → 拉远", "镜头先推近，随后拉远。"),
    ("S01_s_dolly_in__static", ("dolly_in", "static"), "起止时机",
     "先推近 → 保持静止", "镜头先推近，随后保持静止。"),
    ("S01_s_dolly_out__dolly_in", ("dolly_out", "dolly_in"), "同轴反转",
     "先拉远 → 推近", "镜头先拉远，随后推近。"),
    ("S01_s_pan_left__static", ("pan_left", "static"), "起止时机",
     "先向左摇 → 保持静止", "镜头先向左摇，随后保持静止。"),
    ("S01_s_pan_left__tilt_up", ("pan_left", "tilt_up"), "异轴先后",
     "先向左摇 → 向上仰", "镜头先向左摇，随后向上仰。"),
    ("S01_s_pan_right__pan_left", ("pan_right", "pan_left"), "同轴反转",
     "先向右摇 → 向左摇", "镜头先向右摇，随后向左摇。"),
    ("S01_s_static__dolly_in", ("static", "dolly_in"), "起止时机",
     "先保持静止 → 推近", "镜头先保持静止，随后推近。"),
    ("S01_s_static__pan_left", ("static", "pan_left"), "起止时机",
     "先保持静止 → 向左摇", "镜头先保持静止，随后向左摇。"),
    ("S01_s_static__zoom_in", ("static", "zoom_in"), "起止时机",
     "先保持静止 → 变焦推近", "镜头先保持静止，随后变焦推近。"),
    ("S01_s_tilt_down__pan_right", ("tilt_down", "pan_right"), "异轴先后",
     "先向下俯 → 向右摇", "镜头先向下俯，随后向右摇。"),
    ("S01_s_tilt_up__pan_left", ("tilt_up", "pan_left"), "异轴先后",
     "先向上仰 → 向左摇", "镜头先向上仰，随后向左摇。"),
    ("S01_s_tilt_up__tilt_down", ("tilt_up", "tilt_down"), "同轴反转",
     "先向上仰 → 向下俯", "镜头先向上仰，随后向下俯。"),
    ("S01_s_truck_right__tilt_up", ("truck_right", "tilt_up"), "异轴先后",
     "先向右横移 → 向上仰", "镜头先向右横移，随后向上仰。"),
)

#: ``(dataset key, steps, group, cap_zh, camera sentence)`` -- three phases, 4 moves.
TRIS = (
    ("S01_t_dolly_in__static__dolly_out", ("dolly_in", "static", "dolly_out"), "三段式",
     "先推近 → 保持静止 → 拉远", "镜头先推近，然后保持静止，最后拉远。"),
    ("S01_t_pan_left__static__pan_right", ("pan_left", "static", "pan_right"), "三段式",
     "先向左摇 → 保持静止 → 向右摇", "镜头先向左摇，然后保持静止，最后向右摇。"),
    ("S01_t_static__pan_left__static", ("static", "pan_left", "static"), "三段式",
     "先保持静止 → 向左摇 → 保持静止", "镜头先保持静止，然后向左摇，最后保持静止。"),
    ("S01_t_tilt_up__pan_left__tilt_down", ("tilt_up", "pan_left", "tilt_down"), "三段式",
     "先向上仰 → 向左摇 → 向下俯", "镜头先向上仰，然后向左摇，最后向下俯。"),
)


#: The shot list disagrees with itself for exactly one entry: the tri
#: "static -> pan left -> static" carries ``targets``/``pred_basic`` of only
#: ``Static, Pan left`` (its leading hold is labelled, the trailing one is not) while
#: ``cap_zh`` and ``camera_sentence`` describe all three phases.  The keys follow the
#: caption -- a three-phase shot with a real trailing hold, which is what the video
#: does -- and ``targets`` stays a verbatim copy of the dataset label, so a consumer
#: that scores against the shot list still sees the list's own answer.
TARGET_OVERRIDES = {
    "S01_t_static__pan_left__static": ["Static", "Pan left"],
}


def _short(dataset_key: str) -> str:
    """``S01_m_pan_left__tilt_up`` -> ``pan_left__tilt_up`` (the scene/tier prefix off)."""
    parts = dataset_key.split("_")
    return "_".join(parts[2:]) if len(parts) > 2 else dataset_key


def _neutral(frame: int) -> dict:
    return {"frame": int(frame), "location": [0.0, 0.0, 0.0],
            "rotation": [0.0, 0.0, 0.0], "focal": REFERENCE_FOCAL}


def _key(frame: int, state: dict) -> dict:
    location = [round(state[axis], 6) or 0.0 for axis in _AXES]
    rotation = [round(state[name], 6) or 0.0 for name in _ROTATIONS]
    return {"frame": int(frame), "location": location, "rotation": rotation,
            "focal": round(REFERENCE_FOCAL + state["focal"], 6)}


def _arc_keys(sign: float) -> "list[dict]":
    """The keys of one arc: a circle that keeps a subject ``ARC_SUBJECT_RADIUS`` ahead.

    ``sign`` is +1 for clockwise, -1 for counterclockwise.  At orbit angle ``t`` the
    camera sits at ``(R sin t, 0, -R (1 - cos t))`` in its own start frame and yaws by
    ``t``, which is exactly the orientation that keeps the subject centred.
    """
    sweep = sign * ARC_SWEEP_DEG
    steps = max(1, int(round(abs(sweep) / ARC_KEY_STEP_DEG)))
    keys = []
    for index in range(steps + 1):
        angle = math.radians(sweep * index / steps)
        frame = round(FRAME_END * index / steps)
        keys.append({
            "frame": int(frame),
            "location": [round(ARC_SUBJECT_RADIUS * math.sin(angle), 6) or 0.0, 0.0,
                         round(-ARC_SUBJECT_RADIUS * (1.0 - math.cos(angle)), 6) or 0.0],
            "rotation": [0.0, round(math.degrees(angle), 6) or 0.0, 0.0],
            "focal": REFERENCE_FOCAL,
        })
    return keys


def _phase_keys(phases: tuple) -> "list[dict]":
    """Keys for one shot: a neutral start key, then one key per phase.

    ``phases`` is a tuple of phases, and each phase is a tuple of the steps that run
    **at the same time** inside it -- so ``(("pan_left", "tilt_up"),)`` is one
    simultaneous phase and ``(("pan_left",), ("tilt_up",))`` is two in sequence.
    """
    if len(phases) == 1 and len(phases[0]) == 1 and STEPS[phases[0][0]][2].get("arc"):
        return _arc_keys(STEPS[phases[0][0]][2]["arc"])
    keys = [_neutral(0)]
    state = {name: 0.0 for name in _AXES + _ROTATIONS + ("focal",)}
    for index, phase in enumerate(phases):
        for step in phase:
            for name, delta in STEPS[step][2].items():
                state[name] += delta
        keys.append(_key(round(FRAME_END * (index + 1) / len(phases)), state))
    return keys


def _moves(phases: tuple) -> "list[dict]":
    """The shot report's ``basic_movement`` list, phase by phase."""
    entries = []
    for index, phase in enumerate(phases):
        for step in phase:
            kind, direction, _components = STEPS[step]
            entry = {"type": kind, "direction": direction, "speed": None}
            if len(phases) > 1:
                entry["phase"] = index
            entries.append(entry)
    return entries


def _targets(phases: tuple) -> "list[str]":
    return [f"{STEPS[step][0]}" + (f" {STEPS[step][1]}" if STEPS[step][1] else "")
            for phase in phases for step in phase]


def _template(dataset_key: str, phases: tuple, kind: str, group: str,
              cap_zh: str, sentence: str) -> dict:
    entry = {
        "id": f"{kind}_{_short(dataset_key)}",
        "kind": kind,
        "tier": "基础" if kind == "single" else "组合",
        "group": group,
        "dataset_key": dataset_key,
        "cap_zh": cap_zh,
        "camera_sentence": sentence,
        "targets": TARGET_OVERRIDES.get(dataset_key) or _targets(phases),
        "moves": _moves(phases),
        "duration_seconds": DURATION_SECONDS,
        "fps": UNIT_FPS,
        "keys": _phase_keys(phases),
    }
    if len(phases) == 1 and len(phases[0]) == 1:
        # A single move also carries the flat ``type`` the atom vocabulary uses, so
        # downstream tooling that reads one move per template needs no special case.
        entry["type"] = STEPS[phases[0][0]][0]
        entry["direction"] = STEPS[phases[0][0]][1]
    return entry


def build_document() -> dict:
    templates = []
    for dataset_key, step, group, cap_zh, sentence in SINGLES:
        templates.append(_template(dataset_key, ((step,),), "single", group, cap_zh, sentence))
    for dataset_key, steps, group, cap_zh, sentence in SIMS:
        # One phase: every step runs at the same time.
        templates.append(_template(dataset_key, (steps,), "sim", group, cap_zh, sentence))
    for dataset_key, steps, group, cap_zh, sentence in SEQS:
        templates.append(_template(dataset_key, tuple((step,) for step in steps), "seq",
                                   group, cap_zh, sentence))
    for dataset_key, steps, group, cap_zh, sentence in TRIS:
        templates.append(_template(dataset_key, tuple((step,) for step in steps), "tri",
                                   group, cap_zh, sentence))
    return {
        "schema_version": 1,
        "name": f"{len(templates)} dataset camera moves",
        "unit_scale": {"fps": UNIT_FPS, "rotation_order": "XYZ"},
        "duration_seconds": DURATION_SECONDS,
        "coordinate_system": (
            "camera-local, Blender coordinates used verbatim: location [right, up, back] "
            "metres (-Z is forward), rotation [rx, ry, rz] degrees about the camera's own "
            f"axes, applied to the camera's pose on the sequence's first frame; keys are "
            f"absolute frames at {UNIT_FPS} fps over {DURATION_SECONDS:g} s"
        ),
        "amplitude_note": (
            "Nominal amplitudes.  Translation may be scaled down to fit the scene (the "
            "region box); rotation and focal are never scaled."
        ),
        "source": (
            "41template.jsonl -- each entry keeps its dataset_key, cap_zh, camera_sentence, "
            "targets, tier and group verbatim"
        ),
        "templates": templates,
    }


def serialise(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def verify_jsonl(payload: dict, path: str) -> int:
    """Cross-check the document against the shot list it was transcribed from."""
    with open(path, encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    by_key = {entry["dataset_key"]: entry for entry in payload["templates"]}
    problems = []
    if len(rows) != len(by_key):
        problems.append(f"count: jsonl {len(rows)} vs document {len(by_key)}")
    for row in rows:
        entry = by_key.get(row["key"])
        if entry is None:
            problems.append(f"{row['key']}: missing from the document")
            continue
        targets = [f"{item['type']}" + (f" {item['direction']}" if item["direction"] else "")
                   for item in row["gt"]["pred_basic"]]
        for field, expected, actual in (
            ("kind", row["kind"], entry["kind"]),
            ("tier", row["tier"], entry["tier"]),
            ("group", row["group"], entry["group"]),
            ("cap_zh", row["cap_zh"], entry["cap_zh"]),
            ("camera_sentence", row["camera_sentence"], entry["camera_sentence"]),
            ("targets", targets, entry["targets"]),
            ("duration_seconds", float(row["dur"]), float(entry["duration_seconds"])),
        ):
            if expected != actual:
                problems.append(f"{row['key']}.{field}: jsonl {expected!r} vs document {actual!r}")
    if problems:
        print(f"  MISMATCHES ({len(problems)}):")
        for problem in problems[:20]:
            print(f"    - {problem}")
        return 1
    print(f"  jsonl agrees: {len(rows)} entries, all metadata identical")
    return 0


def _counts(payload: dict) -> dict:
    counts = {}
    for entry in payload["templates"]:
        counts[entry["kind"]] = counts.get(entry["kind"], 0) + 1
    return counts


def _key_shape(payload: dict) -> dict:
    shape = {}
    for entry in payload["templates"]:
        frames = tuple(item["frame"] for item in entry["keys"])
        shape.setdefault(len(frames), 0)
        shape[len(frames)] += 1
    return shape


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", default=DOCUMENT)
    parser.add_argument("--check", action="store_true", help="report, write nothing")
    parser.add_argument("--verify-jsonl", default="",
                        help="cross-check the document against the dataset shot list")
    args = parser.parse_args(argv)

    payload = build_document()
    text = serialise(payload)
    target = os.path.abspath(args.output)
    existing = ""
    if os.path.isfile(target):
        with open(target, encoding="utf-8") as handle:
            existing = handle.read()

    counts = _counts(payload)
    print(f"document : {target}")
    print(f"  templates : {len(payload['templates'])} "
          f"({', '.join(f'{kind} {count}' for kind, count in sorted(counts.items()))})")
    print(f"  timeline  : frames 0..{FRAME_END} at {UNIT_FPS} fps "
          f"({DURATION_SECONDS:g} s)")
    print(f"  keys      : {', '.join(f'{count} keys x {entries}' for count, entries in sorted(_key_shape(payload).items()))}")
    failures = 0
    if args.verify_jsonl:
        failures += verify_jsonl(payload, args.verify_jsonl)
    if args.check:
        same = existing == text
        print(f"  up to date: {same}")
        return failures or (0 if same else 1)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    print(f"  wrote     : {target} ({len(text)} chars)")
    return failures


if __name__ == "__main__":
    sys.exit(main())
