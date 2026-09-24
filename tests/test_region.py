"""Camera movement region tests (pure Python, no bpy).

The region is an oriented box baked into the sequence config; these cases pin the
geometry (auto-detect, rotated boxes, inset), the path report the generator and the
validator both read, and the feasibility gate the planner will use to reject a plan.
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import _boot  # noqa: E402,F401  (the add-on folder may be called anything)

from blender_motion_pipeline.core import region as region_mod  # noqa: E402
from blender_motion_pipeline.tests.harness import Suite, equal, ok  # noqa: E402


def build_suite() -> Suite:
    suite = Suite("test_region")

    @suite.case("auto bounds ignore debris and keep the real set")
    def _():
        boxes = [
            ((-5.0, -5.0, 0.0), (5.0, 5.0, 3.0)),      # the room
            ((-0.2, -0.2, 0.0), (-0.15, -0.15, 0.05)),  # a pebble
            ((1.0, 1.0, 0.0), (1.4, 1.4, 0.4)),         # a crate
        ]
        lo, hi = region_mod.bounds_from_boxes(boxes)
        equal(lo, (-5.0, -5.0, 0.0))
        equal(hi, (5.0, 5.0, 3.0))
        equal(region_mod.bounds_from_boxes([]), None)
        equal(region_mod.bounds_from_boxes(None), None)

    @suite.case("margin grows the box and inset shrinks the usable half")
    def _():
        spec = region_mod.region_from_bounds((-5.0, -5.0, 0.0), (5.0, 5.0, 3.0),
                                             margin_percent=10.0, inset=0.5)
        equal(spec.center, (0.0, 0.0, 1.5))
        ok(abs(spec.half_size[0] - 5.5) < 1e-9, spec.half_size)   # 10 m * 1.1 / 2
        ok(abs(spec.half_size[2] - 1.65) < 1e-9, spec.half_size)  # 3 m * 1.1 / 2
        usable = spec.usable_half()
        ok(abs(usable[0] - 5.0) < 1e-9, usable)
        ok(abs(usable[2] - 1.15) < 1e-9, usable)

    @suite.case("a flat scene still gets a usable height")
    def _():
        spec = region_mod.region_from_bounds((-2.0, -2.0, 0.0), (2.0, 2.0, 0.0))
        equal(spec.half_size[2], region_mod.AUTO_MIN_HALF_EXTENT)

    @suite.case("rotation maps local axes onto world axes")
    def _():
        # 90 degrees about Z sends +X to +Y.
        basis = region_mod.basis_from_euler_deg(0.0, 0.0, 90.0)
        x, y, z = region_mod.apply_basis(basis, (1.0, 0.0, 0.0))
        ok(abs(x) < 1e-9 and abs(y - 1.0) < 1e-9 and abs(z) < 1e-9, (x, y, z))
        # identity stays identity
        equal(region_mod.basis_from_euler_deg(0.0, 0.0, 0.0), region_mod.IDENTITY_BASIS)

    @suite.case("contains/excess/clearance on an axis-aligned box")
    def _():
        spec = region_mod.region_from_bounds((-1.0, -1.0, -1.0), (1.0, 1.0, 1.0), inset=0.25)
        ok(region_mod.contains(spec, (0.0, 0.0, 0.0)), "the centre is inside")
        ok(region_mod.contains(spec, (0.7, 0.0, 0.0)), "inside the usable half (0.75)")
        ok(not region_mod.contains(spec, (0.8, 0.0, 0.0)), "past the inset")
        ok(abs(region_mod.excess_of(spec, (0.8, 0.0, 0.0)) - 0.05) < 1e-9,
           region_mod.excess_of(spec, (0.8, 0.0, 0.0)))
        equal(region_mod.excess_of(spec, (0.5, 0.0, 0.0)), 0.0)
        ok(abs(region_mod.clearance_of(spec, (0.5, 0.0, 0.0)) - 0.25) < 1e-9,
           region_mod.clearance_of(spec, (0.5, 0.0, 0.0)))
        ok(region_mod.clearance_of(spec, (2.0, 0.0, 0.0)) < 0, "outside is negative")

    @suite.case("a rotated region only contains points rotated with it")
    def _():
        basis = region_mod.basis_from_euler_deg(0.0, 0.0, 45.0)
        spec = region_mod.RegionSpec(center=(0.0, 0.0, 0.0), half_size=(2.0, 0.5, 1.0), basis=basis)
        # Rotating the box by 45 degrees about Z means a world point (a, a, 0) has
        # local x = a*sqrt(2): the long axis now runs along the diagonal.
        inside_at = 1.30          # local x = 1.84 < 2.0
        outside_at = 1.55         # local x = 2.19 > 2.0
        ok(region_mod.contains(spec, (inside_at, inside_at, 0.0)),
           "inside along the rotated long axis")
        ok(not region_mod.contains(spec, (outside_at, outside_at, 0.0)),
           "past the rotated half extent")
        # The corner that used to be inside the un-rotated box is now outside.
        ok(not region_mod.contains(spec, (2.0, 0.0, 0.0)), "the old axis-aligned corner is outside")

    @suite.case("path report counts exits and remembers the worst frame")
    def _():
        spec = region_mod.region_from_bounds((-1.0, -1.0, -1.0), (1.0, 1.0, 1.0))
        positions = [(0.0, 0.0, 0.0), (0.5, 0.0, 0.0), (1.4, 0.0, 0.0), (0.2, 0.0, 0.0)]
        report = region_mod.region_report(spec, positions, frame_start=0)
        ok(report["available"], report)
        equal(report["frames"], 4)
        equal(report["exit_frames"], 1)
        equal(report["first_exit_frame"], 2)
        equal(report["worst_frame"], 2)
        ok(abs(report["max_excess_m"] - 0.4) < 1e-9, report)
        ok(abs(report["position_offset"][0] - 0.4) < 1e-9, report)
        equal(region_mod.region_report(None, positions)["available"], False)

    @suite.case("feasible() is the gate the planner uses, with a safety margin")
    def _():
        spec = region_mod.region_from_bounds((-1.0, -1.0, -1.0), (1.0, 1.0, 1.0))
        ok(region_mod.feasible(spec, [(0.0, 0.0, 0.0), (0.9, 0.0, 0.0)]), "inside")
        ok(not region_mod.feasible(spec, [(0.0, 0.0, 0.0), (1.05, 0.0, 0.0)]), "outside")
        ok(not region_mod.feasible(spec, [(0.95, 0.0, 0.0)], margin=0.1), "inside but within the margin")
        ok(region_mod.feasible(None, [(99.0, 0.0, 0.0)]), "no region means no constraint")

    @suite.case("region round-trips through the sequence config")
    def _():
        spec = region_mod.RegionSpec(center=(1.0, 2.0, 3.0), half_size=(4.0, 5.0, 6.0),
                                     basis=region_mod.basis_from_euler_deg(10.0, 20.0, 30.0),
                                     inset=0.25, mode=region_mod.MODE_OBJECT, source="Cube.001")
        payload = spec.to_dict()
        back = region_mod.RegionSpec.from_dict(payload)
        ok(back is not None, payload)
        ok(all(abs(a - b) < 1e-5 for a, b in zip(back.center, spec.center)), back)
        ok(all(abs(a - b) < 1e-5 for a, b in zip(back.half_size, spec.half_size)), back)
        ok(all(abs(a - b) < 1e-6 for a, b in zip(back.basis, spec.basis)), back)
        equal(back.mode, region_mod.MODE_OBJECT)
        equal(back.source, "Cube.001")
        ok("region" in back.describe(), back.describe())
        equal(region_mod.RegionSpec.from_dict(None), None)
        equal(region_mod.RegionSpec.from_dict({"center": [0, 0]}), None)

    @suite.case("a header-less region dict still loads with an identity basis")
    def _():
        back = region_mod.RegionSpec.from_dict({"center": [0, 0, 0], "half_size": [1, 1, 1]})
        ok(back is not None, back)
        equal(back.basis, region_mod.IDENTITY_BASIS)
        equal(back.inset, 0.0)

    @suite.case("a fixed template is scaled to the box, never re-drawn")
    def _():
        spec = region_mod.region_from_bounds((-1.0, -1.0, -1.0), (1.0, 1.0, 1.0))
        base = (0.0, 0.0, 0.0)
        # A 3 m push forward: the box ends at x = 1 m, so 1/3 of the amplitude fits.
        offsets = [(0.0, 0.0, 0.0), (-1.5, 0.0, 0.0), (-3.0, 0.0, 0.0)]
        fit = region_mod.fit_translation_scale(spec, base, offsets)
        ok(fit["ok"], fit)
        # The reported factor is rounded for the JSON, hence 1e-6 rather than 1e-9.
        ok(abs(fit["scale"] - (1.0 / 3.0)) < 1e-6, fit)
        equal(fit["frames"], 3)
        equal(fit["exit_frames"], 0)
        # A path that already fits is left alone.
        short = region_mod.fit_translation_scale(spec, base, [(0.0, 0.0, 0.0), (0.5, 0.0, 0.0)])
        ok(short["ok"] and short["scale"] == 1.0, short)
        # Angles cannot leave the box, so a rotation-only shot is untouched.
        still = region_mod.fit_translation_scale(spec, base, [(0.0, 0.0, 0.0)] * 5)
        equal(still["scale"], 1.0)

    @suite.case("scaling cannot rescue a camera that starts outside the box")
    def _():
        spec = region_mod.region_from_bounds((-1.0, -1.0, -1.0), (1.0, 1.0, 1.0))
        fit = region_mod.fit_translation_scale(spec, (5.0, 0.0, 0.0),
                                               [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0)])
        ok(not fit["ok"], fit)
        equal(fit["scale"], 0.0)
        ok("first frame is outside" in fit["reason"], fit["reason"])
        ok(abs(float(fit["start_clearance_m"]) + 4.0) < 1e-9, fit)

    @suite.case("the fit respects the safety margin and reports what is left")
    def _():
        spec = region_mod.region_from_bounds((-1.0, -1.0, -1.0), (1.0, 1.0, 1.0))
        base = (0.0, 0.0, 0.0)
        offsets = [(0.0, 0.0, 0.0), (-3.0, 0.0, 0.0)]
        loose = region_mod.fit_translation_scale(spec, base, offsets)
        tight = region_mod.fit_translation_scale(spec, base, offsets, margin=0.25)
        ok(tight["ok"], tight)
        ok(tight["scale"] < loose["scale"],
           f"the margin has to cost amplitude: {tight['scale']} < {loose['scale']}")
        ok(abs(tight["scale"] - 0.25) < 1e-9, tight)

    @suite.case("an off-centre, rotated box scales along its own axes")
    def _():
        spec = region_mod.RegionSpec(
            center=(0.0, 0.0, 0.0), half_size=(2.0, 1.0, 1.0),
            basis=region_mod.basis_from_euler_deg(0.0, 0.0, 45.0),
        )
        # Straight along world +X: the rotated box is tighter there (its local axes are
        # diagonal), so the fit has to use the *local* half sizes, not the world ones.
        fit = region_mod.fit_translation_scale(spec, (0.0, 0.0, 0.0), [(0.0, 0.0, 0.0),
                                                                      (4.0, 0.0, 0.0)])
        ok(fit["ok"], fit)
        ok(0.0 < fit["scale"] < 0.5, fit)

    @suite.case("a fit that lands exactly on the wall is still a fit")
    def _():
        # The optimum of fit_translation_scale sits *on* the wall by construction, so
        # the last bit of the arithmetic read as a 1e-16 m overshoot and the shot was
        # labelled "fit-failed" with a worst excess of 0.000 m -- 14 of 246 sequences
        # in a real run.  These three cases are that failure, found by sweeping rotated
        # boxes; each is a fit whose binding frame ends up on the wall.
        cases = [
            ((51.737445, -138.057124, -28.527978), (-0.574269, -0.46041, 0.941858),
             [(0.0, 0.0, 0.0), (-2.314318, -0.845803, 2.835015),
              (1.134685, -3.197338, 3.914414)], 0.354752),
            ((-103.232387, -87.020079, 98.168288), (-0.342089, -0.40735, -0.853203),
             [(0.0, 0.0, 0.0), (0.810271, -1.026368, -0.374335),
              (3.673077, -0.130204, 0.59657)], 0.792753),
            ((131.94924, -114.182022, -124.511285), (0.816847, 0.635604, -0.501003),
             [(0.0, 0.0, 0.0), (-2.427282, 3.601087, 3.057518),
              (0.828274, -0.628342, -3.169283)], 0.697377),
        ]
        for angles, base, offsets, expected in cases:
            spec = region_mod.RegionSpec(
                center=(0.0, 0.0, 0.0), half_size=(2.6, 2.6, 2.6),
                basis=region_mod.basis_from_euler_deg(*angles),
            )
            fit = region_mod.fit_translation_scale(spec, base, offsets)
            ok(fit["ok"], f"{angles}: a path that fits must not be reported as failed: {fit}")
            equal(fit["exit_frames"], 0)
            ok(fit["tolerance_m"] > 0.0, fit)
            ok(abs(fit["scale"] - expected) < 2e-6, f"{fit['scale']} vs {expected}")
            # The scale is rounded down, so the path that gets applied never overshoots.
            scaled = [tuple(base[axis] + fit["scale"] * offset[axis] for axis in range(3))
                      for offset in offsets]
            worst = max(region_mod.excess_of(spec, point) for point in scaled)
            ok(worst <= 0.0, f"{angles}: the applied path overshoots by {worst} m")
            # The tolerance must not swallow a real violation: a millimetre past the
            # wall still counts as leaving the box.
            shrunk = region_mod.with_inset(spec, 0.001)
            strict = region_mod.region_report(shrunk, scaled, tolerance=fit["tolerance_m"])
            ok(int(strict["exit_frames"]) > 0,
               f"{angles}: a millimetre past the wall went unreported: {strict}")

    return suite


if __name__ == "__main__":
    raise SystemExit(build_suite().run())
