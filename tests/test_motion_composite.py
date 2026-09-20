"""Compound shot tests (pure Python).

A compound ("复合运镜") plays several base templates one after another inside the
**same total frame range** as a single template, each part anchored on the pose the
previous one ended in.  These cases pin the three things that could silently go
wrong: the frame plan (total preserved, contiguous, no shared frame), the recipe
counts (``n!`` and ``x! * C(n, x)``, deterministic sampling), and the flattening
maths (the chained pose really is part 1 then part 2 in the anchor frame).
"""

from __future__ import annotations

import math
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _boot  # noqa: E402,F401  (the add-on folder may be called anything)

from blender_motion_pipeline.camera import motion_composite as mc  # noqa: E402
from blender_motion_pipeline.camera import motion_templates as mt  # noqa: E402
from blender_motion_pipeline.config.models import (  # noqa: E402
    BatchConfig,
    CompositeSection,
    ConfigError,
)
from blender_motion_pipeline.io.path_utils import safe_filename  # noqa: E402
from blender_motion_pipeline.tests.harness import Suite, close, equal, ok, raises  # noqa: E402


def _library(entries) -> mt.MotionTemplateLibrary:
    return mt.MotionTemplateLibrary.from_entries(entries, source="compound-test")


def _push_in(frames=(0, 80), distance=3.0) -> dict:
    return {"id": "push_in", "keys": [
        {"frame": frames[0], "location": [0.0, 0.0, 0.0], "rotation": [0, 0, 0], "focal": 35.0},
        {"frame": frames[1], "location": [0.0, 0.0, -float(distance)],
         "rotation": [0, 0, 0], "focal": 35.0},
    ]}


def _truck_right(frames=(0, 80), distance=2.0) -> dict:
    return {"id": "truck_right", "keys": [
        {"frame": frames[0], "location": [0.0, 0.0, 0.0], "rotation": [0, 0, 0], "focal": 35.0},
        {"frame": frames[1], "location": [float(distance), 0.0, 0.0],
         "rotation": [0, 0, 0], "focal": 35.0},
    ]}


def _pan_right(frames=(0, 80), yaw=-30.0) -> dict:
    return {"id": "pan_right", "keys": [
        {"frame": frames[0], "location": [0.0, 0.0, 0.0], "rotation": [0, 0, 0], "focal": 35.0},
        {"frame": frames[1], "location": [0.0, 0.0, 0.0], "rotation": [0, float(yaw), 0],
         "focal": 35.0},
    ]}


def _base(position=(0.0, 0.0, 10.0)):
    """Camera-to-world matrix for a camera at ``position`` looking down -Z."""
    matrix = [[1.0, 0.0, 0.0, position[0]],
              [0.0, 1.0, 0.0, position[1]],
              [0.0, 0.0, 1.0, position[2]],
              [0.0, 0.0, 0.0, 1.0]]
    return matrix


def build_suite() -> Suite:
    suite = Suite("test_motion_composite")

    # -- counting --------------------------------------------------------
    @suite.case("compound counts are n! and x! * C(n, x)")
    def _():
        equal(mc.factorial(3), 6)
        equal(mc.factorial(7), 5040)
        equal(mc.ordered_count(4, 2), 12)
        equal(mc.ordered_count(5, 3), 60)
        equal(mc.ordered_count(3, 3), 6)
        equal(mc.ordered_count(2, 3), 0)
        equal(mc.ordered_count(10, 2), 90)
        ok("full compound" in mc.describe_counts(6), mc.describe_counts(6))
        ok("C(6,3)" in mc.describe_counts(6, types_per_sequence=3, mode=mc.MODE_PARTIAL),
           mc.describe_counts(6, types_per_sequence=3, mode=mc.MODE_PARTIAL))

    # -- frame plan ------------------------------------------------------
    @suite.case("the frame plan keeps the total frame count and never shares a frame")
    def _():
        equal(mc.split_windows(0, 80, 1), [(0, 80)])
        equal(mc.split_windows(0, 80, 2), [(0, 40), (41, 80)])
        equal(mc.split_windows(0, 80, 3), [(0, 26), (27, 53), (54, 80)])
        equal(mc.split_windows(0, 80, 4), [(0, 20), (21, 40), (41, 60), (61, 80)])
        for parts in (1, 2, 3, 5, 8, 10):
            windows = mc.split_windows(0, 80, parts)
            equal(len(windows), parts)
            equal(sum(end - start + 1 for start, end in windows), 81,
                  f"{parts} parts must still total 81 frames")
            equal(windows[0][0], 0)
            equal(windows[-1][1], 80)
            for (start, end), (next_start, _next_end) in zip(windows, windows[1:]):
                ok(end < next_start, f"windows must not overlap: {windows}")
                equal(next_start, end + 1, "windows must be contiguous")
        # A range that does not start at zero behaves the same way.
        equal(mc.split_windows(10, 20, 2), [(10, 15), (16, 20)])

    # -- recipes ---------------------------------------------------------
    @suite.case("a full compound is every ordering of every template")
    def _():
        recipes, warnings = mc.build_recipes(["a", "b", "c"], mode=mc.MODE_FULL)
        equal(len(recipes), 6)
        equal(warnings, [])
        equal(sorted(tuple(r.parts) for r in recipes),
              [("a", "b", "c"), ("a", "c", "b"), ("b", "a", "c"),
               ("b", "c", "a"), ("c", "a", "b"), ("c", "b", "a")])
        equal([r.index for r in recipes], [1, 2, 3, 4, 5, 6])
        equal(len({r.name for r in recipes}), 6, "every compound needs its own folder")

    @suite.case("a full compound refuses an n! that cannot be generated")
    def _():
        # 8! = 40320 > the 5040 default: generating it is not a thing anyone wants.
        raises(ConfigError, lambda: mc.build_recipes([f"t{i}" for i in range(8)],
                                                     mode=mc.MODE_FULL))
        recipes, _warnings = mc.build_recipes(
            [f"t{i}" for i in range(8)], mode=mc.MODE_FULL, max_full_sequences=100000
        )
        equal(len(recipes), 40320)
        # ... and a single template cannot compound with itself.
        recipes, warnings = mc.build_recipes(["only"], mode=mc.MODE_FULL)
        equal(len(recipes), 1)
        ok(any("at least two" in w for w in warnings), warnings)

    @suite.case("a partial compound samples x-part orderings deterministically")
    def _():
        names = [f"t{i}" for i in range(6)]
        recipes, warnings = mc.build_recipes(
            names, mode=mc.MODE_PARTIAL, types_per_sequence=3, sequence_count=5, seed=99
        )
        equal(len(recipes), 5)
        equal(warnings, [])
        for recipe in recipes:
            equal(len(recipe.parts), 3, "each sequence holds exactly x templates")
            equal(len(set(recipe.parts)), 3, "and no template twice")
            ok(all(part in names for part in recipe.parts), recipe.parts)
        equal(len({r.parts for r in recipes}), 5, "the sampled combinations must differ")

        again, _w = mc.build_recipes(
            names, mode=mc.MODE_PARTIAL, types_per_sequence=3, sequence_count=5, seed=99
        )
        equal([r.parts for r in again], [r.parts for r in recipes],
              "the same seed must reproduce the same set (so --resume works)")

        other, _w = mc.build_recipes(
            names, mode=mc.MODE_PARTIAL, types_per_sequence=3, sequence_count=5, seed=100
        )
        ok({r.parts for r in other} != {r.parts for r in recipes},
           "a different seed should draw a different set")

    @suite.case("a partial compound cannot ask for more orderings than exist")
    def _():
        recipes, warnings = mc.build_recipes(
            ["a", "b", "c", "d"], mode=mc.MODE_PARTIAL, types_per_sequence=2,
            sequence_count=999, seed=1,
        )
        equal(len(recipes), mc.ordered_count(4, 2))
        ok(any("distinct" in w for w in warnings), warnings)
        # Every ordering exists exactly once when the whole space is requested.
        equal(len({r.parts for r in recipes}), 12)

        recipes, warnings = mc.build_recipes(
            ["a", "b", "c"], mode=mc.MODE_PARTIAL, types_per_sequence=2,
            sequence_count=5000, seed=1, max_partial_sequences=100,
        )
        equal(len(recipes), 6, "x! * C(3, 2) = 6 is the whole space")
        raises(ConfigError, lambda: mc.build_recipes(
            ["a", "b"], mode=mc.MODE_PARTIAL, types_per_sequence=3, sequence_count=2))
        raises(ConfigError, lambda: mc.build_recipes(
            ["a", "b", "c"], mode=mc.MODE_PARTIAL, types_per_sequence=1, sequence_count=2))
        raises(ConfigError, lambda: mc.build_recipes(["a", "b"], mode="nonsense"))

    @suite.case("compound folder names stay readable and bounded")
    def _():
        short = mc.CompoundRecipe(parts=("pan_right_01_standard", "hitchcock_01_base_forward"))
        equal(short.name, "compound_pan_right_01_standard+hitchcock_01_base_forward")
        equal(safe_filename(short.name), short.name, "a compound name must survive sanitising")
        long_names = tuple(f"template_with_a_long_name_{index:02d}" for index in range(10))
        recipe = mc.CompoundRecipe(parts=long_names)
        ok(len(recipe.name) <= mc.MAX_NAME_LENGTH, len(recipe.name))
        ok(recipe.name.startswith("compound_template_with_a_long"), recipe.name)
        ok(recipe.name.split("_")[-1] != "", recipe.name)
        equal(recipe.name, mc.CompoundRecipe(parts=long_names).name, "the hash must be stable")

    # -- flattening ------------------------------------------------------
    @suite.case("Euler XYZ extraction round trips through rotation_delta")
    def _():
        # The flattener stores composed rotations as Euler XYZ; the generator reads
        # them back with rotation_delta.  If those two disagree, every compound whose
        # first part turns would be aimed wrong.
        rng = random.Random(4)
        generator = mt.MotionTemplateGenerator()
        for _ in range(200):
            quaternion = mt.quat_normalize((
                rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(-1, 1),
            ))
            euler = [math.degrees(value) for value in mt.quat_to_euler_xyz(quaternion)]
            back = generator.rotation_delta(euler)
            close(mt.quat_angle_between(quaternion, back), 0.0, tol=1e-4,
                  message="quat -> euler XYZ -> quat must be the same orientation")

    @suite.case("flattening chains translations in the anchor frame")
    def _():
        library = _library([_push_in(distance=3.0), _truck_right(distance=2.0)])
        recipe = mc.CompoundRecipe(parts=("push_in", "truck_right"))
        flat = mc.flatten_recipe(recipe, library)
        equal(flat.frame_min, 0)
        equal(flat.frame_max, 80)
        equal([key.frame for key in flat.keyframes], list(range(81)),
              "the flattened template carries one key per frame")
        by_frame = {key.frame: key for key in flat.keyframes}
        for expected, frame in (((0.0, 0.0, 0.0), 0),
                                ((0.0, 0.0, -3.0), 40),
                                ((0.0, 0.0, -3.0), 41),
                                ((2.0, 0.0, -3.0), 80)):
            got = by_frame[frame].location
            ok(max(abs(a - b) for a, b in zip(got, expected)) < 1e-9,
               f"frame {frame}: expected {expected}, got {got}")
        # A translation-only compound must not turn the camera.
        for key in flat.keyframes:
            equal([round(value, 6) for value in key.rotation], [0.0, 0.0, 0.0])

    @suite.case("flattening carries the first part's rotation into the second")
    def _():
        library = _library([_pan_right(yaw=-30.0), _truck_right(distance=2.0)])
        recipe = mc.CompoundRecipe(parts=("pan_right", "truck_right"))
        flat = mc.flatten_recipe(recipe, library)
        by_frame = {key.frame: key for key in flat.keyframes}
        end = by_frame[80]
        # The truck offset must be rotated by the pan the camera ends part 1 with.
        pan = mt.quat_from_axis_angle("Y", -30.0)
        expected = mt.quat_rotate(pan, (2.0, 0.0, 0.0))
        ok(max(abs(a - b) for a, b in zip(end.location, expected)) < 1e-6,
           f"expected {expected}, got {end.location}")
        close(mt.quat_angle_between(
            mt.MotionTemplateGenerator().rotation_delta(end.rotation), pan), 0.0, tol=1e-6,
            message="the compound's final orientation is the accumulated rotation")

    @suite.case("a compound behaves like the parts played back to back")
    def _():
        library = _library([_pan_right(yaw=-25.0), _push_in(distance=4.0)])
        recipe = mc.CompoundRecipe(parts=("pan_right", "push_in"))
        flat = mc.flatten_recipe(recipe, library)
        generator = mt.MotionTemplateGenerator(frame_start=0)
        animation = generator.generate(flat, base_matrix=_base(), base_focal=35.0)
        equal(animation.frame_start, 0)
        equal(animation.frame_end, 80)
        equal(animation.frame_count, 81, "the compound is as long as a single template")

        # (a) the generated samples reproduce the flattened keys exactly, which is
        #     what the "one key per frame" design buys: a flattening change cannot be
        #     hidden by re-interpolation.
        by_frame = {key.frame: key for key in flat.keyframes}
        worst = 0.0
        for sample in animation.samples:
            key = by_frame[sample.frame]
            wanted = (key.location[0], key.location[1], key.location[2] + 10.0)
            worst = max(worst, max(abs(a - b) for a, b in zip(sample.position, wanted)))
        ok(worst < 1e-6, f"samples must reproduce the flattened keys (worst {worst:g})")

        # (b) the first part's own motion is preserved up to its window, angle included.
        start_aim = mt.quat_rotate(animation.samples[0].quaternion, (0.0, 0.0, -1.0))
        mid_aim = mt.quat_rotate(animation.samples[40].quaternion, (0.0, 0.0, -1.0))
        end_aim = mt.quat_rotate(animation.samples[-1].quaternion, (0.0, 0.0, -1.0))
        close(mt.quat_angle_between(animation.samples[0].quaternion,
                                    animation.samples[40].quaternion), 25.0, tol=0.05,
              message="half of the pan happens in the first window")
        close(mt.quat_angle_between(animation.samples[40].quaternion,
                                    animation.samples[-1].quaternion), 0.0, tol=0.05,
              message="a push does not turn the camera")
        ok(math.dist(mid_aim, end_aim) < 1e-6, "the pan is over before the push starts")
        ok(abs(start_aim[0] - mid_aim[0]) > 0.05 or abs(start_aim[1] - mid_aim[1]) > 0.05,
           "the aim must have swung during the first window")

    @suite.case("a compound spans the configured range, or the parts' own span")
    def _():
        library = _library([_push_in(frames=(0, 60)), _truck_right(frames=(0, 40))])
        equal(mc.compound_range(library, ("push_in", "truck_right")), (0, 60),
              "without a configured range the parts' span wins")
        equal(mc.compound_range(library, ("push_in", "truck_right"),
                                frame_start=0, frame_end=80), (0, 80))
        flat = mc.flatten_recipe(mc.CompoundRecipe(parts=("push_in", "truck_right")),
                                 library, frame_start=0, frame_end=80)
        equal((flat.frame_min, flat.frame_max), (0, 80))
        raises(ConfigError, lambda: mc.compound_range(
            library, ("push_in", "truck_right"), frame_start=80, frame_end=0))

    @suite.case("build_compound_templates flattens a whole recipe list")
    def _():
        library = _library([_push_in(), _truck_right(), _pan_right()])
        templates, warnings = mc.build_compound_templates(
            library, mode=mc.MODE_PARTIAL, types_per_sequence=2, sequence_count=4, seed=3
        )
        equal(len(templates), 4)
        equal(warnings, [])
        for template in templates:
            equal(len([name for name in template.parameters["compound"]["parts"]]), 2)
            equal((template.frame_min, template.frame_max), (0, 80))
            ok(template.name.startswith("compound_"), template.name)
            ok(" -> " in template.description, template.description)
        empty, _warnings = mc.build_compound_templates(
            library, mode=mc.MODE_PARTIAL, types_per_sequence=2, sequence_count=0
        )
        equal(empty, [], "a count of zero produces no compound")

    # -- configuration ---------------------------------------------------
    @suite.case("the composite section validates and round trips")
    def _():
        section = CompositeSection.from_dict({"enabled": True, "mode": "partial",
                                              "types_per_sequence": 3, "sequence_count": 7,
                                              "seed": 5, "output_mode": "only_compound"}, [])
        equal(section.enabled, True)
        equal(section.mode, "partial")
        equal(section.types_per_sequence, 3)
        equal(section.sequence_count, 7)
        equal(section.want_compound(), True)
        equal(section.want_base(), False, "only_compound must drop the base shots")
        equal(CompositeSection.from_dict({"enabled": True}, []).want_base(), True)
        equal(CompositeSection.from_dict({"enabled": True, "output_mode": "only_base"}, [])
              .want_compound(), False)
        equal(CompositeSection().want_compound(), False, "off by default")

        raises(ConfigError, lambda: CompositeSection.from_dict({"types_per_sequence": 1}, []))
        raises(ConfigError, lambda: CompositeSection.from_dict({"types_per_sequence": 11}, []))
        raises(ConfigError, lambda: CompositeSection.from_dict({"sequence_count": 0}, []))
        # An unknown mode falls back with a warning; validate() still refuses it.
        mode_warnings: "list[str]" = []
        fallback_mode = CompositeSection.from_dict({"mode": "sometimes"}, mode_warnings)
        equal(fallback_mode.mode, "full")
        ok(any("sometimes" in w for w in mode_warnings), mode_warnings)
        warnings: "list[str]" = []
        fallback = CompositeSection.from_dict({"output_mode": "nonsense"}, warnings)
        equal(fallback.output_mode, "with_base")
        ok(any("nonsense" in w for w in warnings), warnings)
        raises(ConfigError, lambda: CompositeSection(mode="nope").validate())

        config = BatchConfig.from_dict({"composite": {"enabled": True, "mode": "partial"}})
        equal(config.composite.enabled, True)
        equal(config.to_dict()["composite"]["mode"], "partial")
        again = BatchConfig.from_dict(config.to_dict())
        equal(again.composite.to_dict(), config.composite.to_dict())
        ok("composite" in str(config.to_dict()), config.to_dict().keys())

    @suite.case("the config description mentions the compound settings")
    def _():
        from blender_motion_pipeline.config.models import describe_config

        config = BatchConfig()
        ok("composite      : off" in describe_config(config), describe_config(config))
        config.composite.enabled = True
        config.composite.mode = "partial"
        config.composite.types_per_sequence = 4
        text = describe_config(config)
        ok("x=4" in text and "output=with_base" in text, text)

    return suite


if __name__ == "__main__":
    raise SystemExit(build_suite().run())
