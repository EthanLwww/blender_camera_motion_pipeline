"""Ad-hoc diagnostic probe for the template axis conventions.

Reuses the *exact* camera fixture from ``test_motion_templates`` so there is a
single source of truth for the probe camera.  Run with any Python 3.10+:

    <python> tests/probe_axes.py

Prints, for each motion family, the camera move and aim change, so the sign of
every template axis can be eyeballed against physical intuition:

* ``dolly_in``   must move along the view direction.
* ``pedestal_up`` must move along world +Z.
* ``truck_right`` must strafe toward the camera's right axis.
* ``pan_right``  must swing the view toward the right axis.
* ``tilt_up``    must raise the aim.
* ``roll``       must leave the aim alone and only spin the frame.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _boot  # noqa: E402,F401  (the add-on folder may be called anything)

from blender_motion_pipeline.camera import motion_templates as mt  # noqa: E402
from test_motion_templates import _look_neg_y  # noqa: E402


def report(title: str, keys, *, expect: str = "") -> None:
    library = mt.MotionTemplateLibrary.from_entries([{"id": "probe", "keys": keys}], source="probe")
    base = _look_neg_y()
    right, up, forward = mt.axis_basis(base)
    animation = mt.MotionTemplateGenerator(frame_start=0).generate(
        library.get("probe"), base_matrix=base, base_focal=35.0
    )
    first, last = animation.samples[0], animation.samples[-1]
    delta = mt.vec_sub(last.position, first.position)
    aim_start = mt.quat_rotate(first.quaternion, (0, 0, -1))
    aim_end = mt.quat_rotate(last.quaternion, (0, 0, -1))
    grip_start = mt.quat_rotate(first.quaternion, (0, 1, 0))
    grip_end = mt.quat_rotate(last.quaternion, (0, 1, 0))

    print(f"\n=== {title} ===")
    if expect:
        print(f"  expectation : {expect}")
    print(f"  basis       : right={_f(right)} up={_f(up)} view={_f(forward)}")
    print(f"  move        : {_f(delta)}")
    print(f"  aim  start  : {_f(aim_start)}   end: {_f(aim_end)}")
    print(f"  up   start  : {_f(grip_start)}   end: {_f(grip_end)}")
    print(f"  lateral(aim.end . right) : {sum(a * b for a, b in zip(aim_end, right)):+.4f}")
    print(f"  vertical(aim.end . up)   : {sum(a * b for a, b in zip(aim_end, up)):+.4f}")


def _f(vector) -> str:
    return "( " + ", ".join(f"{float(v):+.4f}" for v in vector) + " )"


def main() -> int:
    print(f"template file: {mt.__file__}")
    report("dolly_in  (-Z 3.0)", [{"frame": 0}, {"frame": 80, "location": [0, 0, -3.0]}],
           expect="move 3 m along the view direction")
    report("dolly_out (+Z 3.0)", [{"frame": 0}, {"frame": 80, "location": [0, 0, 3.0]}],
           expect="move 3 m against the view direction")
    report("pedestal_up (+Y 1.2)", [{"frame": 0}, {"frame": 80, "location": [0, 1.2, 0]}],
           expect="rise 1.2 m along the camera's own up axis (local +Y)")
    report("pedestal_down (-Y 0.35)", [{"frame": 0}, {"frame": 80, "location": [0, -0.35, 0]}],
           expect="drop 0.35 m along the camera's own down axis")
    report("truck_right (+X 2.0)", [{"frame": 0}, {"frame": 80, "location": [2.0, 0, 0]}],
           expect="strafe 2 m toward the camera's right axis (local +X)")
    report("truck_left (-X 2.0)", [{"frame": 0}, {"frame": 80, "location": [-2.0, 0, 0]}],
           expect="strafe 2 m toward the camera's left axis")
    report("pan_right (ry -30)", [{"frame": 0}, {"frame": 80, "rotation": [0, -30, 0]}],
           expect="aim swings toward the right axis")
    report("pan_left (ry +30)", [{"frame": 0}, {"frame": 80, "rotation": [0, 30, 0]}],
           expect="aim swings toward the left axis")
    report("tilt_up (rx +20)", [{"frame": 0}, {"frame": 80, "rotation": [20, 0, 0]}],
           expect="vertical component of the aim becomes positive")
    report("tilt_down (rx -20)", [{"frame": 0}, {"frame": 80, "rotation": [-20, 0, 0]}],
           expect="vertical component of the aim becomes negative")
    report("roll (rz -20)", [{"frame": 0}, {"frame": 80, "rotation": [0, 0, -20]}],
           expect="aim unchanged, up vector tilts toward the right axis")
    report("hitchcock push (focal 100 -> 50)",
           [{"frame": 0, "location": [0, 0, 0], "focal": 100.0},
            {"frame": 80, "location": [0, 0, -3.4], "focal": 50.0}],
           expect="move forward while the focal length shortens")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
