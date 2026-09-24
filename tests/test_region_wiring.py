"""Region wiring tests (pure Python, no bpy).

The planner itself is covered by ``test_region_planner``; these cases pin the *wiring*
between it and the generator: the region block that reaches ``sequence_config.json``, the
re-draw of an infeasible compound, the single-atom path that may only be measured, and the
"feature off means nothing changes" rule.  The generator is instantiated without running
its constructor so no bpy is needed -- the only scene objects it touches are the camera's
location and lens.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import _boot  # noqa: E402,F401  (the add-on folder may be called anything)

from blender_motion_pipeline.camera import motion_composite as mc  # noqa: E402
from blender_motion_pipeline.camera import region_planner as planner  # noqa: E402
from blender_motion_pipeline.config.defaults import default_config  # noqa: E402
from blender_motion_pipeline.core import region as region_mod  # noqa: E402
from blender_motion_pipeline.core.sequence_generator import (  # noqa: E402
    SequenceGenerator,
)
from blender_motion_pipeline.tests.harness import Suite, equal, ok  # noqa: E402

IDENTITY = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0]]


def atom(name, kind, direction, speed, channels, location):
    return mc.AtomicMotion(
        name=name, type=kind, direction=direction, speed=speed, channels=channels,
        rate_location=location, rate_rotation=(0.0, 0.0, 0.0), rate_focal=0.0,
        description="",
    )


def generator(*, slow=False):
    """A generator with just enough state for the region wiring (no bpy)."""
    instance = object.__new__(SequenceGenerator)
    config = default_config()
    config.composite.enabled = True
    config.region.mode = "numbers"
    config.region.size = [0.0, 0.0, 0.0]     # replaced per case
    instance.config = config
    instance.logger = None
    rate = 0.15 if slow else 0.35
    # A realistic atomic library: both directions of a dolly and a truck, at two speeds.
    # The region ladder needs the slower/opposite variants to be able to shrink a path.
    instance._atoms = [
        atom("dolly_in", "dolly", "in", "fast", ("depth",), (0.0, 0.0, rate)),
        atom("dolly_in_slow", "dolly", "in", "slow", ("depth",), (0.0, 0.0, 0.15)),
        atom("dolly_out_slow", "dolly", "out", "slow", ("depth",), (0.0, 0.0, -0.15)),
        atom("truck_left_slow", "truck", "left", "slow", ("lateral",), (0.25, 0.0, 0.0)),
        atom("truck_right_slow", "truck", "right", "slow", ("lateral",), (-0.25, 0.0, 0.0)),
    ]
    instance._atomic_source = "test"
    return instance


def camera(lens=50.0):
    return SimpleNamespace(location=(0.0, 0.0, 0.0), lens=lens)


def plan_for(atoms, *, duration=4.0, fps=24.0, segments=1, seed=5):
    return mc.plan_compound(
        atoms, duration_seconds=duration, fps=fps, max_simultaneous=2, max_segments=segments,
        randomize=True, rng=__import__("random").Random(seed), seed=seed, frame_start=0,
        source="test",
    )


def build_suite() -> Suite:
    suite = Suite("test_region_wiring")

    @suite.case("region off means the plan is untouched and nothing is reported")
    def _():
        gen = generator()
        gen.config.region.mode = "off"
        equal(gen._region_for_scene(None), None)
        plan = plan_for(gen._atoms)
        same, info = gen._fit_plan_to_region(plan, None, IDENTITY, camera())
        ok(same is plan, "a plan with no region must come back unchanged")
        equal(info, {})

    @suite.case("a compound is re-drawn until it fits, and the report says so")
    def _():
        gen = generator()
        gen.config.region.mode = "numbers"
        gen.config.region.size = [1.4, 1.4, 1.4]
        gen.config.region.margin = 0.0
        region = gen._region_for_scene(None)
        equal(region.mode, region_mod.MODE_NUMBERS)
        plan = plan_for(gen._atoms, seed=6)   # seed 6 draws a path that overruns 0.7 m
        fitted, info = gen._fit_plan_to_region(plan, region, IDENTITY, camera())
        ok(info["ok"], info)
        equal(info["exit_frames"], 0)
        ok(info["stage"] in ("L1", "L2", "L3", "L4"), info["stage"])
        ok(info["attempts"] > 1, info)
        ok(info["segment_redraws"] + info["plan_redraws"] + info["split_rounds"] > 0, info)
        equal(info["box"]["half_size"], [0.7, 0.7, 0.7])
        equal(info["mode"], region_mod.MODE_NUMBERS)
        # The returned plan is the one that must be rendered, so it has to be feasible.
        equal(planner.plan_is_feasible(fitted, region, margin=0.0)[0], True)

    @suite.case("a re-drawn plan is still built from the atomic library")
    def _():
        gen = generator()
        gen.config.region.mode = "numbers"
        gen.config.region.size = [1.4, 1.4, 1.4]
        gen.config.region.margin = 0.0
        region = gen._region_for_scene(None)
        fitted, info = gen._fit_plan_to_region(plan_for(gen._atoms, seed=6), region,
                                               IDENTITY, camera())
        ok(info["ok"], info)
        known = {item.name for item in gen._atoms}
        used = {motion.name for segment in fitted.segments for motion in segment.motions}
        ok(used, "a re-drawn plan must still play motions")
        ok(used <= known, f"{sorted(used - known)} are not in the library")
        # The plan keeps the shot's own length: only the moves inside it were re-drawn.
        equal(fitted.duration_seconds, 4.0)
        equal(fitted.frame_start, 0)

    @suite.case("the safety margin makes the same box stricter")
    def _():
        gen = generator()
        gen.config.region.mode = "numbers"
        gen.config.region.size = [2.0, 2.0, 2.0]
        gen.config.region.margin = 0.0
        region = gen._region_for_scene(None)
        _plan, relaxed = gen._fit_plan_to_region(plan_for(gen._atoms), region, IDENTITY, camera())
        ok(relaxed["ok"], relaxed)
        # A metre of required clearance cannot fit inside a 1 m half-extent box at all.
        gen.config.region.margin = 1.0
        region = gen._region_for_scene(None)
        _plan, strict = gen._fit_plan_to_region(plan_for(gen._atoms), region, IDENTITY, camera())
        equal(strict["ok"], False)
        equal(strict["exit_frames"], 0)

    @suite.case("a hopeless box is reported honestly instead of being clamped")
    def _():
        gen = generator()
        gen.config.region.mode = "numbers"
        gen.config.region.size = [0.02, 0.02, 0.02]
        gen.config.region.margin = 0.0
        region = gen._region_for_scene(None)
        plan = plan_for(gen._atoms)
        fitted, info = gen._fit_plan_to_region(plan, region, IDENTITY, camera())
        equal(info["ok"], False)
        equal(info["stage"], "L5")
        ok(info["exit_frames"] > 0, info)
        ok(info["max_excess_m"] > 0.0, info)
        # Even a rejected shot keeps a renderable plan: never a bent motion.
        equal([segment.motions for segment in fitted.segments],
              [segment.motions for segment in fitted.segments])

    @suite.case("the report carries the numbers a render node needs, and no objects")
    def _():
        gen = generator()
        gen.config.region.mode = "numbers"
        gen.config.region.size = [2.0, 4.0, 6.0]
        gen.config.region.inset = 0.1
        gen.config.region.margin = 0.2
        region = gen._region_for_scene(None)
        _plan, info = gen._fit_plan_to_region(plan_for(gen._atoms), region, IDENTITY, camera())
        for key in ("mode", "source", "box", "margin", "ok", "stage", "attempts",
                    "segment_redraws", "plan_redraws", "speed_preferred", "split_rounds",
                    "frames", "exit_frames", "max_excess_m", "min_clearance_m",
                    "worst_frame"):
            ok(key in info, f"{key} is missing from {sorted(info)}")
        equal(info["box"]["half_size"], [1.0, 2.0, 3.0])
        equal(info["box"]["inset"], 0.1)
        equal(info["margin"], 0.2)
        # Numbers only: a render node must never need the helper object or the scene.
        for value in info["box"]["center"] + info["box"]["half_size"] + info["box"]["basis"]:
            ok(isinstance(value, float), value)
        equal(len(info["box"]["basis"]), 9)

    @suite.case("a plan that already fits is left exactly as drawn")
    def _():
        gen = generator()
        gen.config.region.mode = "numbers"
        gen.config.region.size = [400.0, 400.0, 400.0]
        gen.config.region.margin = 0.0
        region = gen._region_for_scene(None)
        plan = plan_for(gen._atoms, seed=5)
        fitted, info = gen._fit_plan_to_region(plan, region, IDENTITY, camera())
        ok(info["ok"], info)
        equal(info["stage"], "L0")
        equal(info["attempts"], 1)
        ok(fitted is plan, "a shot that already fits must not be re-drawn at all")
        equal([segment.motions for segment in fitted.segments],
              [segment.motions for segment in plan.segments])

    @suite.case("a camera that starts outside the box is reported, not re-drawn forever")
    def _():
        gen = generator()
        gen.config.region.mode = "numbers"
        gen.config.region.size = [1.0, 1.0, 1.0]
        gen.config.region.center = [0.0, 0.0, 0.0]
        gen.config.region.margin = 0.0
        region = gen._region_for_scene(None)
        far = SimpleNamespace(location=(9.0, 0.0, 0.0), lens=50.0)
        plan = plan_for(gen._atoms, seed=6)
        same, info = gen._fit_plan_to_region(plan, region, IDENTITY, far)
        # A re-draw can shorten a path but never move its first frame, so the ladder is
        # skipped entirely instead of burning every attempt on an impossible box.
        ok(same is plan, "the plan must come back untouched")
        equal(info["stage"], "start-outside")
        equal(info["ok"], False)
        equal(info["attempts"], 1)
        ok(info["start_clearance_m"] < 0.0, info)

    @suite.case("a single-atom shot is measured, never re-drawn")
    def _():
        gen = generator()
        gen.config.region.mode = "numbers"
        gen.config.region.size = [0.01, 0.01, 0.01]
        gen.config.region.margin = 0.0
        region = gen._region_for_scene(None)
        single = mc.plan_single(gen._atoms[0], duration_seconds=4.0, fps=24.0,
                                frame_start=0, source="test", seed=3)
        same, info = gen._fit_plan_to_region(single, region, IDENTITY, camera())
        ok(same is single, "a single atomic move has nothing to re-draw")
        equal(info["stage"], "single-atom")
        equal(info["ok"], False)
        ok(info["exit_frames"] > 0, info)

    return suite


if __name__ == "__main__":
    raise SystemExit(build_suite().run())
