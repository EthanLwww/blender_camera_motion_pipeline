"""Generate ``templates/atomic_motion_templates.json`` -- the combinable vocabulary.

    python tests/make_atomic_templates.py            # rewrite the document
    python tests/make_atomic_templates.py --check     # report, write nothing

Every entry is an ordinary motion template (so the parser, the library, the panel
and the probes treat it like any other document) that additionally carries the
metadata the spatio-temporal compound planner needs:

* ``type``     -- the atom's family, spelled the way the shot report spells it:
                  ``Pan``, ``Tilt``, ``Roll``, ``Truck``, ``Dolly In``, ``Dolly Out``,
                  ``Pedestal``, ``Arc``, ``Zoom In``, ``Zoom Out``, ``Static``.
* ``direction``-- ``left``/``right``/``up``/``down``/``clockwise``/``counterclockwise``,
                  or ``null`` for the directionless atoms.
* ``speed``    -- ``slow``/``medium``/``fast``.
* ``channels`` -- the axes the atom drives.  Two atoms may run at the same time
                  **iff their channel sets are disjoint**, which is exactly what the
                  user's rule says: ``zoom_in`` + ``zoom_out`` or ``pedestal_up`` +
                  ``pedestal_down`` share a channel and conflict, while ``pan_right`` +
                  ``tilt_down`` do not.

Each atom is authored as a **one-second** ramp: the keys run from frame 0 to frame
24 (24 fps) and their delta is therefore the *rate per second* at that speed.  The
planner integrates the rate over a segment's real duration, so a "medium pan left"
turns 18 deg per second whether the segment is 0.5 s or 6 s long -- the speed
attribute means a rate, not a fixed distance.

Rates (per second, in the camera's own frame: ``+X`` right, ``+Y`` up, ``-Z``
forward, degrees about those axes) --:

===========  ==================  ======  ======  ======
atom         channel             slow    medium  fast
===========  ==================  ======  ======  ======
Pan          yaw (``ry``)        8 deg   18 deg  40 deg
Tilt         pitch (``rx``)      5 deg   12 deg  26 deg
Roll         roll (``rz``)       4 deg   10 deg  22 deg
Truck        lateral (``x``)     0.25 m  0.6 m   1.3 m
Dolly        depth (``z``)       0.3 m   0.7 m   1.5 m
Pedestal     vertical (``y``)    0.15 m  0.35 m  0.75 m
Arc          lateral + yaw       0.25 m  0.6 m   1.3 m
Zoom         focal               4 mm    10 mm   22 mm
===========  ==================  ======  ======  ======

An arc travels laterally at the truck rate and yaws by ``degrees(v / 4 m)`` per
second, i.e. it keeps a subject standing ~4 m away centred; that assumed radius is
the only place where the vocabulary commits to a scene scale, and it is recorded in
each arc entry's description.

Deliberately **not** in the vocabulary: shot-specific families such as ``hitchcock``
(a simultaneous dolly + zoom), ``fixed``, or the reference set's numbered variants.
They are whole shots, not atoms -- a compound builds them out of the atoms instead.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PACKAGE = os.path.dirname(HERE)
DOCUMENT = os.path.join(PACKAGE, "templates", "atomic_motion_templates.json")

#: The fps the one-second ramps are authored at; the planner reads the span from
#: the document itself, this is only what the generator writes.
UNIT_FPS = 24
#: Reference focal length the zoom rates start from in the document's keys.
REFERENCE_FOCAL = 35.0
#: Subject radius the arc rates assume when converting lateral speed to yaw.
ARC_SUBJECT_RADIUS = 4.0

#: ``stem, type, direction, channels, components, (slow, medium, fast)``.
#:
#: ``components`` maps a key of the one-second delta to a sign: ``rx``/``ry``/``rz``
#: are camera-local Euler degrees, ``x``/``y``/``z`` camera-local metres, ``focal``
#: millimetres.  The sign gives the direction of the motion; the magnitude comes
#: from the speed column.
ATOMS = (
    ("pan_left", "Pan", "left", ("yaw",), {"ry": +1}, (8.0, 18.0, 40.0)),
    ("pan_right", "Pan", "right", ("yaw",), {"ry": -1}, (8.0, 18.0, 40.0)),
    ("tilt_up", "Tilt", "up", ("pitch",), {"rx": +1}, (5.0, 12.0, 26.0)),
    ("tilt_down", "Tilt", "down", ("pitch",), {"rx": -1}, (5.0, 12.0, 26.0)),
    ("roll_clockwise", "Roll", "clockwise", ("roll",), {"rz": -1}, (4.0, 10.0, 22.0)),
    ("roll_counterclockwise", "Roll", "counterclockwise", ("roll",), {"rz": +1}, (4.0, 10.0, 22.0)),
    ("truck_left", "Truck", "left", ("lateral",), {"x": -1}, (0.25, 0.6, 1.3)),
    ("truck_right", "Truck", "right", ("lateral",), {"x": +1}, (0.25, 0.6, 1.3)),
    ("dolly_in", "Dolly In", None, ("depth",), {"z": -1}, (0.3, 0.7, 1.5)),
    ("dolly_out", "Dolly Out", None, ("depth",), {"z": +1}, (0.3, 0.7, 1.5)),
    ("pedestal_up", "Pedestal", "up", ("vertical",), {"y": +1}, (0.15, 0.35, 0.75)),
    ("pedestal_down", "Pedestal", "down", ("vertical",), {"y": -1}, (0.15, 0.35, 0.75)),
    # An arc is a lateral track plus the yaw that keeps the subject centred; the
    # yaw magnitude is derived from the lateral one, so only the lateral rate is
    # listed and ``ry`` reuses the same row.
    ("arc_clockwise", "Arc", "clockwise", ("lateral", "yaw"), {"x": +1, "ry": +1},
     (0.25, 0.6, 1.3)),
    ("arc_counterclockwise", "Arc", "counterclockwise", ("lateral", "yaw"), {"x": -1, "ry": -1},
     (0.25, 0.6, 1.3)),
    ("zoom_in", "Zoom In", None, ("focal",), {"focal": +1}, (4.0, 10.0, 22.0)),
    ("zoom_out", "Zoom Out", None, ("focal",), {"focal": -1}, (4.0, 10.0, 22.0)),
)

SPEEDS = ("slow", "medium", "fast")

#: Channel -> what it drives, for the document's own header and the docs.
CHANNELS = {
    "yaw": "rotation about the camera's own up axis (ry)",
    "pitch": "rotation about the camera's own right axis (rx)",
    "roll": "rotation about the camera's own view axis (rz)",
    "lateral": "translation along the camera's own right axis (x)",
    "vertical": "translation along the camera's own up axis (y)",
    "depth": "translation along the camera's own view axis (z)",
    "focal": "focal length (mm)",
}


def atom_keys(stem: str, components: dict, magnitude: float) -> "list[dict]":
    """The two keys of one atom, as its one-second delta from a neutral pose."""
    location = [0.0, 0.0, 0.0]
    rotation = [0.0, 0.0, 0.0]
    focal = 0.0
    is_arc = stem.startswith("arc_")
    axes = {"x": 0, "y": 1, "z": 2}
    for key, sign in components.items():
        if key in axes:
            location[axes[key]] = sign * magnitude
        elif key in ("rx", "ry", "rz"):
            rotation["xyz".index(key[1])] = sign * magnitude
        elif key == "focal":
            focal = sign * magnitude
    if is_arc:
        # Lateral speed -> yaw rate that keeps a subject ARC_SUBJECT_RADIUS away centred.
        rotation[1] = components["ry"] * math.degrees(magnitude / ARC_SUBJECT_RADIUS)
    return [
        {"frame": 0, "location": [0.0, 0.0, 0.0], "rotation": [0.0, 0.0, 0.0],
         "focal": REFERENCE_FOCAL},
        {"frame": UNIT_FPS, "location": [round(v, 6) or 0.0 for v in location],
         "rotation": [round(v, 6) or 0.0 for v in rotation],
         "focal": round(REFERENCE_FOCAL + focal, 6)},
    ]


def build_document() -> dict:
    templates = []
    for stem, kind, direction, channels, components, rates in ATOMS:
        for speed, magnitude in zip(SPEEDS, rates):
            unit = "deg/s" if any(k.startswith("r") for k in components) else (
                "mm/s" if "focal" in components else "m/s")
            templates.append({
                "id": f"{stem}_{speed}",
                "type": kind,
                "direction": direction,
                "speed": speed,
                "channels": list(channels),
                "description": (
                    f"{kind}{' ' + direction if direction else ''} at {speed} speed: "
                    f"{magnitude:g} {'deg/s' if kind in ('Pan', 'Tilt', 'Roll') else unit}"
                    + (f" with a yaw that keeps a subject ~{ARC_SUBJECT_RADIUS:g} m away centred"
                       if stem.startswith("arc_") else "")
                ),
                "keys": atom_keys(stem, components, magnitude),
            })
    # Static: no channel, no direction, no rate -- a segment that holds still.
    templates.append({
        "id": "static",
        "type": "Static",
        "direction": None,
        "speed": None,
        "channels": [],
        "description": "Hold the camera still (a segment with no motion).",
        "keys": [
            {"frame": 0, "location": [0.0, 0.0, 0.0], "rotation": [0.0, 0.0, 0.0],
             "focal": REFERENCE_FOCAL},
            {"frame": UNIT_FPS, "location": [0.0, 0.0, 0.0], "rotation": [0.0, 0.0, 0.0],
             "focal": REFERENCE_FOCAL},
        ],
    })
    return {
        "schema_version": 1,
        "name": "atomic camera motions",
        "unit_scale": {"fps": UNIT_FPS, "rotation_order": "XYZ"},
        "coordinate_system": (
            "camera-local: location [right, up, back] metres, rotation [rx, ry, rz] "
            "degrees about the camera's own axes; every entry is a one-second ramp, "
            "so its delta is a rate per second"
        ),
        "channels": CHANNELS,
        "templates": templates,
    }


def serialise(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", default=DOCUMENT)
    parser.add_argument("--check", action="store_true", help="report, write nothing")
    args = parser.parse_args(argv)

    text = serialise(build_document())
    target = os.path.abspath(args.output)
    existing = ""
    if os.path.isfile(target):
        with open(target, encoding="utf-8") as handle:
            existing = handle.read()
    payload = json.loads(text)
    print(f"document : {target}")
    print(f"  templates : {len(payload['templates'])} "
          f"({len(ATOMS)} atoms x {len(SPEEDS)} speeds + static)")
    print(f"  channels  : {', '.join(sorted(CHANNELS))}")
    if args.check:
        print(f"  up to date: {existing == text}")
        return 0 if existing == text else 1
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    print(f"  wrote     : {target} ({len(text)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
