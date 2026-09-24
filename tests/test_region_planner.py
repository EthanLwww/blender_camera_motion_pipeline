"""Graded region re-draw tests (pure Python, no bpy).

The region is a *feasibility* rule: a plan that leaves the box is re-drawn, cheapest
change first, and reported honestly when nothing fits.  These cases pin the ladder
(L0/L1/L2/L3/L4/L5), the "only position-changing motions are tested" rule, the safety
margin, and the deterministic per-attempt seeding -- all against real
:class:`AtomicMotion` / :class:`MotionPlan` objects, never mocks of them.
"""

from __future__ import annotations

import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import _boot  # noqa: E402,F401  (the add-on folder may be called anything)

from blender_motion_pipeline.camera import motion_composite as mc  # noqa: E402
from blender_motion_pipeline.camera import region_planner as planner  # noqa: E402
from blender_motion_pipeline.core import region as region_mod  # noqa: E402
from blender_motion_pipeline.tests.harness import Suite, equal, ok  # noqa: E402


def atom(name, kind, direction, speed, channels, location, rotation=(0.0, 0.0, 0.0)):
    return mc.AtomicMotion(
        name=name, type=kind, direction=direction, speed=speed, channels=channels,
        rate_location=location, rate_rotation=rotation, rate_focal=0.0, description="",
    )


def library():
    """A dolly, a truck and a pan, each at two speeds plus a hold."""
    return [
        atom("dolly_in", "dolly", "in", "fast", ("depth",), (0.0, 0.0, 0.35)),
        atom("dolly_in_slow", "dolly", "in", "slow", ("depth",), (0.0, 0.0, 0.15)),
        atom("dolly_out_slow", "dolly", "out", "slow", ("depth",), (0.0, 0.0, -0.15)),
        atom("truck_left", "truck", "left", "fast", ("lateral",), (0.6, 0.0, 0.0)),
        atom("truck_left_slow", "truck", "left", "slow", ("lateral",), (0.25, 0.0, 0.0)),
        atom("truck_right_slow", "truck", "right", "slow", ("lateral",), (-0.25, 0.0, 0.0)),
        atom("pan_left", "pan", "left", "medium", ("yaw",), (0.0, 0.0, 0.0), (0.0, 12.0, 0.0)),
    ]


def box(half, center=(0.0, 0.0, 0.0), mode=region_mod.MODE_OBJECT, inset=0.0):
    return region_mod.RegionSpec(
        center=center, half_size=(half, half, half), basis=region_mod.IDENTITY_BASIS,
        inset=inset, mode=mode, source="test",
    )


def builder(duration=4.0, fps=24.0, max_segments=4, max_simultaneous=2):
    def build(seed, atoms):
        return mc.plan_compound(
            atoms, duration_seconds=duration, fps=fps, max_simultaneous=max_simultaneous,
            max_segments=max_segments, randomize=True, rng=random.Random(int(seed)),
            seed=int(seed), frame_start=0, source="test",
        )
    return build


def draw(atoms, region, *, seed=7, margin=0.0, slower=None, build=None, limits=None):
    return planner.draw_feasible_plan(
        build or builder(), atoms, region=region, base_position=(0.0, 0.0, 0.0), seed=seed,
        max_simultaneous=2, margin=margin, slower=slower,
        limits=limits if limits is not None else planner.DEFAULT_LIMITS, logger=None,
    )


def build_suite() -> Suite:
    suite = Suite("test_region_planner")

    @suite.case("only position-changing atoms are region-relevant")
    def _():
        atoms = library()
        by_name = {item.name: item for item in atoms}
        ok(planner.changes_position(by_name["dolly_in"]), "a dolly translates")
        ok(planner.changes_position(by_name["truck_left"]), "a truck translates")
        ok(not planner.changes_position(by_name["pan_left"]), "a pan must not be re-drawn")
        # A rotation-only shot can never leave a box, so L0 accepts it untouched.
        plan, record = draw([by_name["pan_left"]], box(0.01))
        equal(record["stage"], "L0")
        ok(record["ok"], record)
        equal(record["report"]["exit_frames"], 0)

    @suite.case("a plan that already fits is kept as drawn (L0, one attempt)")
    def _():
        plan, record = draw(library(), box(500.0))
        equal(record["stage"], "L0")
        ok(record["ok"], record)
        equal(record["attempts"], 1)
        equal(record["segment_redraws"], 0)
        equal(record["plan_redraws"], 0)

    @suite.case("region=None disables the feature entirely")
    def _():
        plan, record = draw(library(), None)
        ok(record["ok"], record)
        equal(record["attempts"], 1)
        equal(record["report"].get("available"), False)

    @suite.case("a hopeless box escalates through the ladder and fails honestly")
    def _():
        atoms = [item for item in library() if item.name in ("dolly_in", "truck_left")]
        plan, record = draw(atoms, box(0.005))
        equal(record["stage"], "L5")
        ok(not record["ok"], record)
        ok(record["segment_redraws"] > 0, record)
        ok(record["plan_redraws"] > 0, record)
        # Honest failure still reports what went wrong, and never claims feasibility.
        ok(record["report"]["exit_frames"] > 0, record["report"])
        equal(planner.plan_is_feasible(plan, box(0.005))[0], False)

    @suite.case("L4 splits a too-long segment, because a small box needs short ones")
    def _():
        atoms = [item for item in library() if item.name == "dolly_in"]
        # A builder that can only ever produce one 4 s segment: L1 and L2 cannot shorten
        # it, so the last resort is the split stage.  A 0.4 m box is reachable once the
        # drift is walked in 0.5 s steps instead of one 4 s push; 0.2 m honestly is not.
        one_segment = builder(max_segments=1)
        plan, record = draw(atoms, box(0.4), slower=library(), build=one_segment)
        equal(record["stage"], "L4")
        ok(record["ok"], record)
        ok(record["split_rounds"] > 0, record)
        ok(len(plan.segments) > 1, [segment.index for segment in plan.segments])
        equal(record["report"]["exit_frames"], 0)
        # Splitting keeps the plan's own time span and never leaves a stub segment.
        equal(plan.frame_start, 0)
        equal(plan.duration_seconds, 4.0)
        for segment in plan.segments:
            ok(segment.end_time - segment.start_time >= planner.MIN_SPLIT_SECONDS - 1e-9,
               (segment.start_time, segment.end_time))
        equal([segment.index for segment in plan.segments],
              list(range(len(plan.segments))))
        # The same shot in a box it cannot walk out of stays an honest failure.
        _plan, tight = draw(atoms, box(0.2), slower=library(), build=one_segment)
        equal(tight["stage"], "L5")
        ok(not tight["ok"], tight)

    @suite.case("L3 draws in the slower variant of the same family")
    def _():
        fast = [item for item in library() if item.name in ("dolly_in", "truck_left")]
        ok(planner._smaller_pool(fast, library()), "a pool needs a wider library")
        pool = planner._smaller_pool(fast, library())
        names = {item.name for item in pool}
        ok("dolly_in_slow" in names, names)
        ok("dolly_out_slow" in names, "the opposite direction is what cancels drift")
        ok("pan_left" not in names, "a pan is not a position candidate")
        # Smallest first, so drawing from the front of the pool means "prefer slow".
        equal(pool[0].name, "dolly_in_slow")
        plan, record = draw(fast, box(0.6), slower=library())
        ok(record["ok"], record)
        ok(record["stage"] in ("L1", "L2", "L3", "L4"), record["stage"])
        equal(record["report"]["exit_frames"], 0)

    @suite.case("the safety margin can reject a path that is geometrically inside")
    def _():
        atoms = [item for item in library() if item.name == "dolly_in_slow"]
        plan, relaxed = draw(atoms, box(50.0), margin=0.0)
        ok(relaxed["ok"], relaxed)
        equal(relaxed["stage"], "L0")
        plan, strict = draw(atoms, box(50.0), margin=5000.0)
        ok(not strict["ok"], strict)
        equal(strict["stage"], "L5")
        # The report is about the path, not the margin: no frame actually leaves the box.
        equal(strict["report"]["exit_frames"], 0)

    @suite.case("offending segments are the ones that move the camera out")
    def _():
        atoms = [item for item in library() if item.name == "truck_left"]
        plan = builder(max_segments=1)(seed=3, atoms=atoms)
        ok(planner.offending_segments(plan, box(0.01)), "a 4 s truck leaves a 1 cm box")
        equal(planner.offending_segments(plan, box(500.0)), [])
        equal(planner.offending_segments(plan, None), [])

    @suite.case("a given seed reproduces the same accepted plan")
    def _():
        atoms = [item for item in library() if item.name in ("dolly_in", "truck_left")]
        first, first_record = draw(atoms, box(0.6), seed=11, slower=library())
        second, second_record = draw(atoms, box(0.6), seed=11, slower=library())
        equal(first_record["stage"], second_record["stage"])
        equal(first_record["attempts"], second_record["attempts"])
        equal([segment.motions for segment in first.segments],
              [segment.motions for segment in second.segments])

    @suite.case("plans are reported with their world path and worst frame")
    def _():
        atoms = [item for item in library() if item.name == "truck_left"]
        plan = builder(max_segments=1)(seed=1, atoms=atoms)
        report = planner.plan_report(plan, box(0.01))
        equal(report["available"], True)
        ok(report["frames"] > 0, report)
        # Frames are counted from the plan's first frame, so the last one is frames - 1.
        equal(report["worst_frame"], report["frames"] - 1)
        ok(report["max_excess_m"] > 0.0, report)
        # The same plan under a box big enough to hold it reports no exit at all.
        roomy = planner.plan_report(plan, box(500.0))
        equal(roomy["exit_frames"], 0)
        equal(roomy["first_exit_frame"], None)

    return suite


if __name__ == "__main__":
    raise SystemExit(build_suite().run())
